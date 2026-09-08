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
