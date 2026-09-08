from __future__ import annotations

from datetime import datetime, timezone

EVIDENCE_DIMENSIONS = (
    "MAPPED","IMPLEMENTED","INTEGRATED","REACHABLE","VERIFIED",
    "RECOVERABLE","AUTHORITY_SAFE","CONSUMER_ACCEPTED","VALUE_EVIDENCED"
)
GENERIC_COMPLETION = {"DONE","PASS","COMPLETE","ACCEPTED","READY","SUPPORTED"}
DISCOVERY_DISPOSITIONS = {"EXECUTED","DURABLY_CAPTURED","DUPLICATE","BLOCKED","REJECTED","NON_ACTIONABLE"}
UNFINISHED_STATES = {"ACTIVE","WAITING","BLOCKED"}
CHECKPOINT_FIELDS = {
    "canonical_work_id","outcome","owner","state","progress_checkpoint",
    "state_basis_or_evidence","remaining_work","blocker_or_waiting_condition",
    "next_admissible_action","wake_condition","completion_condition"
}
OPTIONAL_CLASSES = {"LATER","BOUNDARY","PROVIDER_OPTIONAL","HOST_OPTIONAL"}
WORK_PACKAGE_STATES = {"ACTIVE","WAITING","BLOCKED","COMPLETE"}
PROGRAM_STATES = {"ACTIVE","WAITING","BLOCKED","COMPLETE"}


def validate_scope_projection(canonical_ids, repo_ids):
    canonical = tuple(dict.fromkeys(canonical_ids))
    projected = tuple(dict.fromkeys(repo_ids))
    if set(canonical) != set(projected):
        missing = sorted(set(canonical) - set(projected))
        extra = sorted(set(projected) - set(canonical))
        raise ValueError(f"canonical scope mismatch missing={missing} extra={extra}")
    return max(canonical, key=lambda x: int(x.rsplit("-", 1)[1])) if canonical else None


def claim_state(record, dimension):
    if dimension not in EVIDENCE_DIMENSIONS:
        raise ValueError(f"unknown evidence dimension: {dimension}")
    cell = record.get(dimension)
    if not isinstance(cell, dict):
        return "UNPROVEN"
    state = str(cell.get("state", "UNPROVEN"))
    if state in GENERIC_COMPLETION:
        return "UNPROVEN"
    return state if state in {"PROVEN","UNPROVEN"} else "UNPROVEN"


def validate_requirement_evidence(canonical_ids, evidence):
    reqs = evidence.get("requirements", {})
    for rid in canonical_ids:
        if rid not in reqs:
            raise ValueError(f"missing requirement evidence instance: {rid}")
        for dim in EVIDENCE_DIMENSIONS:
            if dim not in reqs[rid]:
                raise ValueError(f"missing evidence dimension {rid}:{dim}")
            claim_state(reqs[rid], dim)
    return True


def evidence_is_fresh(observed_at, ttl_seconds, now=None):
    if not observed_at or ttl_seconds is None:
        return False
    stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    now = now or datetime.now(timezone.utc)
    return 0 <= (now - stamp).total_seconds() <= int(ttl_seconds)


def obligation_is_required(obligation):
    cls = str(obligation.get("class", "REQUIRED"))
    if cls == "REQUIRED":
        return True
    if cls == "CONDITIONAL":
        return bool(obligation.get("applies"))
    if cls in OPTIONAL_CLASSES:
        return bool(obligation.get("advertised")) or bool(obligation.get("acceptance_profile_requires"))
    if cls == "ADVERTISED_CAPABILITY":
        return bool(obligation.get("advertised", True))
    if cls == "ACCEPTANCE_PROFILE":
        return bool(obligation.get("acceptance_profile_requires", True))
    raise ValueError(f"unknown obligation class: {cls}")


def derive_parent_state(children, required_dimension="CONSUMER_ACCEPTED"):
    required = [c for c in children if obligation_is_required(c)]
    if not required:
        return "UNPROVEN"
    return "PROVEN" if all(claim_state(c["evidence"], required_dimension) == "PROVEN" for c in required) else "UNPROVEN"


def scoped_acceptance(scoped_children, scope_id):
    selected = [c for c in scoped_children if c.get("scope_id") == scope_id]
    return derive_parent_state(selected, "CONSUMER_ACCEPTED")


def reopen_from_contradiction(record, dimension, observation):
    if dimension not in EVIDENCE_DIMENSIONS:
        raise ValueError(dimension)
    start = EVIDENCE_DIMENSIONS.index(dimension)
    for dim in EVIDENCE_DIMENSIONS[start:]:
        record.setdefault(dim, {})["state"] = "UNPROVEN"
    return {
        "kind": "BOUNDED_REPAIR_CANDIDATE",
        "trigger": "USER_OR_REAL_WORLD_CONTRADICTION",
        "affected_dimension": dimension,
        "observation": observation,
        "requires_global_reselection": True,
        "authority_granted": False,
    }


def validate_discovery_disposition(value):
    if value not in DISCOVERY_DISPOSITIONS:
        raise ValueError(f"orphan discovery disposition: {value}")
    return True


def validate_checkpoint(item):
    missing = CHECKPOINT_FIELDS - set(item)
    if missing:
        raise ValueError(f"unfinished work checkpoint missing {sorted(missing)}")
    if item["state"] not in UNFINISHED_STATES:
        raise ValueError("not unfinished work")
    return True


def select_next_work(items, adjacent=None):
    for state in ("ACTIVE","WAITING","BLOCKED"):
        for item in items:
            if item.get("state") == state:
                validate_checkpoint(item)
                return item
    return adjacent


def _as_set(value, field):
    if value is None:
        return set()
    if not isinstance(value, (list, tuple, set)):
        raise ValueError(f"{field} must be a collection")
    normalized = {str(item).strip() for item in value if str(item).strip()}
    if len(normalized) != len(value):
        raise ValueError(f"{field} contains empty or duplicate values")
    return normalized


def validate_construction_program(program):
    if not isinstance(program, dict):
        raise ValueError("construction program must be an object")
    required = {
        "program_id","state","authority_source","allowed_paths",
        "allowed_requirement_ids","allowed_authority_classes","work_packages"
    }
    missing = required - set(program)
    if missing:
        raise ValueError(f"construction program missing {sorted(missing)}")
    program_id = str(program["program_id"]).strip()
    if not program_id:
        raise ValueError("construction program id required")
    if program["state"] not in PROGRAM_STATES:
        raise ValueError("invalid construction program state")
    if not str(program["authority_source"]).strip():
        raise ValueError("construction program authority source required")
    allowed_paths = _as_set(program["allowed_paths"], "allowed_paths")
    allowed_requirements = _as_set(program["allowed_requirement_ids"], "allowed_requirement_ids")
    allowed_authority = _as_set(program["allowed_authority_classes"], "allowed_authority_classes")
    packages = program["work_packages"]
    if not isinstance(packages, list):
        raise ValueError("work_packages must be a list")
    seen = set()
    for package in packages:
        validate_work_package(
            package,
            program_id=program_id,
            allowed_paths=allowed_paths,
            allowed_requirement_ids=allowed_requirements,
            allowed_authority_classes=allowed_authority,
        )
        work_id = package["work_id"]
        if work_id in seen:
            raise ValueError(f"duplicate work package id: {work_id}")
        seen.add(work_id)
    for package in packages:
        unknown_dependencies = _as_set(package.get("dependencies", []), "dependencies") - seen
        if unknown_dependencies:
            raise ValueError(
                f"unknown work package dependencies for {package['work_id']}: {sorted(unknown_dependencies)}"
            )
        if package["work_id"] in _as_set(package.get("dependencies", []), "dependencies"):
            raise ValueError(f"work package cannot depend on itself: {package['work_id']}")
    return True


def validate_work_package(
    package,
    *,
    program_id,
    allowed_paths,
    allowed_requirement_ids,
    allowed_authority_classes,
):
    required = {
        "work_id","parent_program_id","state","paths","requirement_ids",
        "authority_class","dependencies","evidence"
    }
    missing = required - set(package)
    if missing:
        raise ValueError(f"work package missing {sorted(missing)}")
    work_id = str(package["work_id"]).strip()
    if not work_id:
        raise ValueError("work package id required")
    if package["parent_program_id"] != program_id:
        raise ValueError(f"work package parent mismatch: {work_id}")
    if package["state"] not in WORK_PACKAGE_STATES:
        raise ValueError(f"invalid work package state: {work_id}")
    paths = _as_set(package["paths"], "paths")
    requirements = _as_set(package["requirement_ids"], "requirement_ids")
    if not paths:
        raise ValueError(f"work package must declare bounded paths: {work_id}")
    if not requirements:
        raise ValueError(f"work package must declare bounded requirements: {work_id}")
    if not paths <= set(allowed_paths):
        raise ValueError(f"work package path authority leak: {work_id}")
    if not requirements <= set(allowed_requirement_ids):
        raise ValueError(f"work package requirement authority leak: {work_id}")
    if package["authority_class"] not in set(allowed_authority_classes):
        raise ValueError(f"work package authority class leak: {work_id}")
    if not isinstance(package["evidence"], dict):
        raise ValueError(f"work package evidence must be an object: {work_id}")
    for dim in EVIDENCE_DIMENSIONS:
        claim_state(package["evidence"], dim)
    _as_set(package.get("dependencies", []), "dependencies")
    return True


def _incident_hits_package(incident, package):
    if str(incident.get("state", "OPEN")).upper() not in {"OPEN","ACTIVE","BLOCKING"}:
        return False
    if incident.get("global") is True:
        return True
    blast = incident.get("blast_radius", {}) or {}
    if not isinstance(blast, dict):
        raise ValueError("incident blast_radius must be an object")
    work_ids = _as_set(blast.get("work_ids", []), "incident work_ids")
    paths = _as_set(blast.get("paths", []), "incident paths")
    requirements = _as_set(blast.get("requirement_ids", []), "incident requirement_ids")
    return (
        package["work_id"] in work_ids
        or bool(paths & _as_set(package["paths"], "paths"))
        or bool(requirements & _as_set(package["requirement_ids"], "requirement_ids"))
    )


def evaluate_construction_program(program, incidents=()):
    validate_construction_program(program)
    packages = {p["work_id"]: p for p in program["work_packages"]}
    active = [p for p in packages.values() if p["state"] == "ACTIVE"]
    blockers = {p["work_id"]: [] for p in active}

    for package in active:
        for dep_id in package.get("dependencies", []):
            if packages[dep_id]["state"] != "COMPLETE":
                blockers[package["work_id"]].append(f"dependency:{dep_id}")
        for incident in incidents:
            if _incident_hits_package(incident, package):
                blockers[package["work_id"]].append(
                    f"incident:{incident.get('incident_id', 'unknown')}"
                )

    for index, left in enumerate(active):
        left_paths = _as_set(left["paths"], "paths")
        for right in active[index + 1:]:
            if left_paths & _as_set(right["paths"], "paths"):
                blockers[left["work_id"]].append(f"path-conflict:{right['work_id']}")
                blockers[right["work_id"]].append(f"path-conflict:{left['work_id']}")

    eligible = [work_id for work_id, reasons in blockers.items() if not reasons]
    package_states = {
        work_id: {
            "implementation": claim_state(package["evidence"], "IMPLEMENTED"),
            "acceptance": claim_state(package["evidence"], "CONSUMER_ACCEPTED"),
        }
        for work_id, package in packages.items()
    }
    return {
        "program_id": program["program_id"],
        "eligible_work_package_ids": eligible,
        "blocked_work_packages": {k: v for k, v in blockers.items() if v},
        "package_states": package_states,
    }


def dogfood_tuple_is_eligible(evidence):
    required = (
        "IMPLEMENTED","INTEGRATED","REACHABLE","VERIFIED",
        "RECOVERABLE","AUTHORITY_SAFE"
    )
    return all(claim_state(evidence, dim) == "PROVEN" for dim in required)


def personal_production_accepted(
    *,
    daw_path_evidence,
    os_family_evidence,
    reliability_evidence,
    required_daw_paths,
    required_os_families,
):
    required_daws = set(required_daw_paths)
    required_os = set(required_os_families)
    if not required_daws or not required_os:
        return False
    if not required_daws <= set(daw_path_evidence):
        return False
    if not required_os <= set(os_family_evidence):
        return False
    if not reliability_evidence:
        return False
    required_records = [daw_path_evidence[name] for name in required_daws]
    required_records += [os_family_evidence[name] for name in required_os]
    required_records += list(reliability_evidence)
    return all(
        claim_state(record, "CONSUMER_ACCEPTED") == "PROVEN"
        for record in required_records
    )


def legacy_veto_blocks_authorized_migration(authority, legacy_state):
    bootstrap = authority.get("bootstrap_migration_authority", {})
    if bootstrap.get("state") == "ACTIVE_UNTIL_VERIFIED_CUTOVER" and bootstrap.get("may_be_vetoed_by_legacy_lifecycle") is False:
        return False
    return bool(legacy_state.get("blocks"))


def validate_constitutional_migration(candidate):
    if candidate.get("candidate_certifier") == candidate.get("candidate_owner"):
        raise ValueError("candidate governance cannot certify itself")
    if candidate.get("ordinary_product_path"):
        raise ValueError("ordinary product construction cannot invoke constitutional migration")
    if not candidate.get("external_invariants"):
        raise ValueError("external migration invariants required")
    return True


def expire_bootstrap_authority(authority, cutover_passed):
    if not cutover_passed:
        raise ValueError("bootstrap authority cannot expire before cutover proof")
    authority["bootstrap_migration_authority"]["state"] = "EXPIRED"
    return authority
