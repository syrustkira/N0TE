from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOV = ROOT / "governance"
TESTS = ROOT / "tests" / "governance"
GOV.mkdir(parents=True, exist_ok=True)
TESTS.mkdir(parents=True, exist_ok=True)

DIMS = [
    "MAPPED",
    "IMPLEMENTED",
    "INTEGRATED",
    "REACHABLE",
    "VERIFIED",
    "RECOVERABLE",
    "AUTHORITY_SAFE",
    "CONSUMER_ACCEPTED",
    "VALUE_EVIDENCED",
]
RETAINED = [f"REQ-SCOPE-{i:03d}" for i in range(2, 173)]


def write_json(name: str, value: object) -> None:
    (GOV / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


write_json(
    "canonical_scope_manifest.json",
    {
        "schema_version": 1,
        "source_class": "externally_refreshed_canonical_semantic_snapshot",
        "semantic_owner": "N0TE_PRODUCT_DB/SCOPE_LEDGER",
        "semantic_owner_drive_id": "1adKMCG_FZ_lgEfEOlT5lwUh4oUTIgxCrBThHs1eGggs",
        "refreshed_at": "2026-09-08T16:30:00Z",
        "retained_requirement_ids": RETAINED,
        "latest_observed_requirement_id": RETAINED[-1],
        "known_current_additions": {
            "REQ-SCOPE-171": "sem-provider-agnostic-llm-reasoning-tool-use-context-capability-substrate",
            "REQ-SCOPE-172": "sem-whole-product-construction-program-progressive-proof-personal-production-acceptance",
        },
        "freshness_rule": "Normal work selection must compare repository projection to a freshly retrieved canonical manifest. Offline validation cannot certify Drive freshness.",
    },
)

write_json(
    "authority.json",
    {
        "schema_version": 1,
        "truth_classes": {
            "PRODUCT_SEMANTICS": "current canonical semantic owners in clean N0TE Drive environment",
            "IMPLEMENTATION": "syrustkira/N0TE current source",
            "CURRENT_EXECUTABLE_STATE": "governance/current_state.json and executable validators",
            "EVIDENCE_ACCEPTANCE": "governance/evidence_state.json plus referenced observed evidence",
            "HISTORY_MIGRATION": "migration/ only, explicitly non-commanding",
        },
        "legacy_governance_authority": "NONE",
        "bootstrap_migration_authority": {
            "state": "ACTIVE_UNTIL_VERIFIED_CUTOVER",
            "source": "explicit_user_authorized_clean_room_migration_2026_09_08",
            "may_be_vetoed_by_legacy_lifecycle": False,
            "ordinary_product_construction_may_use": False,
        },
    },
)

evidence = {"schema_version": 1, "dimensions": DIMS, "requirements": {}}
for rid in RETAINED:
    evidence["requirements"][rid] = {
        d: {
            "state": "PROVEN" if d == "MAPPED" else "UNPROVEN",
            "evidence_refs": ["N0TE_PRODUCT_DB/SCOPE_LEDGER"] if d == "MAPPED" else [],
        }
        for d in DIMS
    }
evidence["migration_rule"] = "Historical construction closure is not acceptance. Missing evidence remains UNPROVEN."
write_json("evidence_state.json", evidence)

write_json(
    "discovery_closure.json",
    {
        "schema_version": 1,
        "allowed_dispositions": [
            "EXECUTED",
            "DURABLY_CAPTURED",
            "DUPLICATE",
            "BLOCKED",
            "REJECTED",
            "NON_ACTIONABLE",
        ],
        "orphan_discovery_is_defect": True,
        "started_unfinished_work_requires_checkpoint": True,
    },
)

write_json(
    "current_state.json",
    {
        "schema_version": 1,
        "repository": "syrustkira/N0TE",
        "migration_state": "CANDIDATE_NOT_CUT_OVER",
        "unfinished_work": [
            {
                "canonical_work_id": "MIGRATION-CLEANROOM-2026-09-08",
                "outcome": "Verified clean N0TE cutover without stale command authority",
                "owner": "authorized clean-room migration",
                "state": "ACTIVE",
                "progress_checkpoint": "Implementation and product behavior tests migrated; clean governance candidate under verification",
                "state_basis_or_evidence": ["migration/provenance/source.json"],
                "remaining_work": [
                    "verify clean governance",
                    "construct and verify clean Drive authority environment",
                    "prove reconstruction and normal user journey",
                    "cut over only if all gates pass",
                ],
                "blocker_or_waiting_condition": None,
                "next_admissible_action": "verify clean governance candidate and clean Drive semantic parity",
                "wake_condition": "current invocation or next eligible clean invocation",
                "completion_condition": "all migration cutover gates independently pass and bootstrap authority expires",
            }
        ],
    },
)

write_json(
    "constitutional_migration.json",
    {
        "schema_version": 1,
        "ordinary_product_path_may_invoke": False,
        "required_sequence": [
            "EXPLICIT_AUTHORIZED_MIGRATION",
            "NAMED_MIGRATION_SCOPE",
            "CURRENT_SEMANTIC_OWNER_RECONCILIATION",
            "LEGACY_GOVERNANCE_AS_INPUT_ONLY",
            "EXTERNAL_MIGRATION_INVARIANTS",
            "CANDIDATE",
            "INDEPENDENT_REGRESSION_MIGRATION_PROOF",
            "STATE_MIGRATION",
            "EXACT_HEAD_REVIEW",
            "ACTIVATION",
            "MIGRATION_AUTHORITY_EXPIRATION",
            "DURABLE_MIGRATION_RECEIPT",
        ],
        "candidate_may_certify_itself": False,
        "permanent_bypass": False,
    },
)

(GOV / "model.py").write_text(
    '''from __future__ import annotations

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
''',
    encoding="utf-8",
)

(GOV / "check_governance.py").write_text(
    '''from __future__ import annotations
import argparse
import json
from pathlib import Path
from governance.model import validate_requirement_evidence, validate_scope_projection

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--canonical-manifest")
    args = ap.parse_args()
    repo_manifest = json.loads((ROOT / "governance/canonical_scope_manifest.json").read_text())
    evidence = json.loads((ROOT / "governance/evidence_state.json").read_text())
    repo_ids = repo_manifest["retained_requirement_ids"]
    if args.canonical_manifest:
        fresh = json.loads(Path(args.canonical_manifest).read_text())
        validate_scope_projection(fresh["retained_requirement_ids"], repo_ids)
    elif not args.offline:
        raise SystemExit("Fresh externally retrieved canonical manifest required for normal governance/work selection")
    validate_requirement_evidence(repo_ids, evidence)
    print("offline structural governance validation passed" if args.offline else "fresh canonical governance validation passed")


if __name__ == "__main__":
    main()
''',
    encoding="utf-8",
)

(TESTS / "test_clean_governance.py").write_text(
    '''import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from governance.model import (
    EVIDENCE_DIMENSIONS, claim_state, derive_parent_state, evidence_is_fresh,
    expire_bootstrap_authority, legacy_veto_blocks_authorized_migration,
    reopen_from_contradiction, scoped_acceptance, select_next_work,
    validate_constitutional_migration, validate_discovery_disposition,
    validate_requirement_evidence, validate_scope_projection,
)

ROOT = Path(__file__).resolve().parents[2]
def data(name): return json.loads((ROOT / "governance" / name).read_text())
def proven_record(dim="MAPPED"):
    return {d: {"state": "PROVEN" if d == dim else "UNPROVEN", "evidence_refs": []} for d in EVIDENCE_DIMENSIONS}


def test_semantic_projection_includes_current_drive_scope_171_and_172():
    m = data("canonical_scope_manifest.json")
    assert m["retained_requirement_ids"][-2:] == ["REQ-SCOPE-171", "REQ-SCOPE-172"]
    assert validate_scope_projection(m["retained_requirement_ids"], m["retained_requirement_ids"]) == "REQ-SCOPE-172"


def test_future_scope_mismatch_fails_visibly_without_hardcoded_max_logic():
    m = data("canonical_scope_manifest.json")
    future = m["retained_requirement_ids"] + ["REQ-SCOPE-173"]
    with pytest.raises(ValueError, match="REQ-SCOPE-173"):
        validate_scope_projection(future, m["retained_requirement_ids"])


def test_every_requirement_has_every_non_collapsing_evidence_dimension():
    m, e = data("canonical_scope_manifest.json"), data("evidence_state.json")
    assert validate_requirement_evidence(m["retained_requirement_ids"], e)
    for rid in m["retained_requirement_ids"]:
        assert set(e["requirements"][rid]) == set(EVIDENCE_DIMENSIONS)


def test_construction_or_generic_done_never_becomes_acceptance():
    r = proven_record()
    r["CONSUMER_ACCEPTED"] = {"state": "DONE", "evidence_refs": ["legacy construction graph"]}
    assert claim_state(r, "CONSUMER_ACCEPTED") == "UNPROVEN"


def test_missing_evidence_remains_unproven():
    assert claim_state({}, "VERIFIED") == "UNPROVEN"


def test_parent_rollup_requires_every_required_sibling():
    a, b = proven_record("CONSUMER_ACCEPTED"), proven_record()
    children = [
        {"class": "REQUIRED", "evidence": a},
        {"class": "REQUIRED", "evidence": b},
        {"class": "HOST_OPTIONAL", "evidence": b},
    ]
    assert derive_parent_state(children) == "UNPROVEN"
    b["CONSUMER_ACCEPTED"]["state"] = "PROVEN"
    assert derive_parent_state(children) == "PROVEN"


def test_scoped_acceptance_cannot_globalize_itself():
    good, bad = proven_record("CONSUMER_ACCEPTED"), proven_record()
    rows = [
        {"scope_id": "linux-ableton", "class": "REQUIRED", "evidence": good},
        {"scope_id": "windows-ableton", "class": "REQUIRED", "evidence": bad},
    ]
    assert scoped_acceptance(rows, "linux-ableton") == "PROVEN"
    assert derive_parent_state(rows) == "UNPROVEN"


def test_contradictory_observation_reopens_current_and_downstream_claims():
    r = {d: {"state": "PROVEN", "evidence_refs": ["old"]} for d in EVIDENCE_DIMENSIONS}
    candidate = reopen_from_contradiction(r, "REACHABLE", {"source": "user", "claim": "cannot reach Services"})
    assert r["IMPLEMENTED"]["state"] == "PROVEN"
    start = EVIDENCE_DIMENSIONS.index("REACHABLE")
    assert all(r[d]["state"] == "UNPROVEN" for d in EVIDENCE_DIMENSIONS[start:])
    assert candidate["kind"] == "BOUNDED_REPAIR_CANDIDATE"
    assert candidate["authority_granted"] is False


def test_stale_evidence_invalidates():
    now = datetime.now(timezone.utc)
    assert evidence_is_fresh((now - timedelta(seconds=5)).isoformat(), 60, now)
    assert not evidence_is_fresh((now - timedelta(hours=2)).isoformat(), 60, now)


def test_discovery_closure_has_only_explicit_dispositions():
    for value in data("discovery_closure.json")["allowed_dispositions"]:
        assert validate_discovery_disposition(value)
    with pytest.raises(ValueError):
        validate_discovery_disposition("MENTIONED_IN_CHAT")


def test_unfinished_work_resumes_before_adjacency():
    state = data("current_state.json")
    selected = select_next_work(state["unfinished_work"], adjacent={"canonical_work_id": "ADJACENT"})
    assert selected["canonical_work_id"] == "MIGRATION-CLEANROOM-2026-09-08"


def test_old_governance_cannot_veto_explicit_authorized_migration():
    assert legacy_veto_blocks_authorized_migration(data("authority.json"), {"blocks": True}) is False


def test_candidate_governance_cannot_certify_itself():
    with pytest.raises(ValueError, match="cannot certify itself"):
        validate_constitutional_migration({"candidate_owner": "candidate", "candidate_certifier": "candidate", "external_invariants": ["semantic parity"], "ordinary_product_path": False})
    assert validate_constitutional_migration({"candidate_owner": "candidate", "candidate_certifier": "independent-verifier", "external_invariants": ["semantic parity"], "ordinary_product_path": False})


def test_bootstrap_authority_expires_only_after_proven_cutover():
    a = copy.deepcopy(data("authority.json"))
    with pytest.raises(ValueError):
        expire_bootstrap_authority(a, False)
    assert expire_bootstrap_authority(a, True)["bootstrap_migration_authority"]["state"] == "EXPIRED"
''',
    encoding="utf-8",
)

(TESTS / "test_personal_production_continuity.py").write_text(
    '''from n0te import HeadquartersMemory, SongResumeService


def test_artist_song_intent_receipt_quit_relaunch_resume_loop(tmp_path):
    hq = HeadquartersMemory.create(tmp_path, "TellMeN0TE")
    profile = hq.store.profile_id
    song = hq.store.create_song("Clean Journey")
    asset = hq.store.attach_asset(song.id, name="idea.wav", sha256="a" * 64)
    v1 = hq.store.create_version(song.id, label="captured idea", asset_ids=[asset.id])
    hq.store.approve_version(song.id, v1.id)
    v2 = hq.store.create_version(song.id, label="revision", parent_version_id=v1.id, asset_ids=[asset.id])
    hq.evidence.record_claim(scope_kind="SONG", scope_id=song.id, key="current.intent", value="evaluate chorus revision", source_kind="USER_DECLARED")
    hq.evidence.record_claim(scope_kind="VERSION", scope_id=v2.id, key="next.action", value="audition chorus and keep or revise", source_kind="USER_DECLARED", source_ref="journey-checkpoint")
    hq.evidence.record_claim(scope_kind="VERSION", scope_id=v2.id, key="authority.decision", value="KEEP_PENDING_AUDITION", source_kind="USER_DECLARED")
    hq.store.select_song(song.id)
    hq.close()

    relaunched = HeadquartersMemory.open(tmp_path, profile)
    try:
        brief = SongResumeService(relaunched).brief()
        assert brief.artist_name == "TellMeN0TE"
        assert brief.song_title == "Clean Journey"
        assert brief.current_version.id == v2.id
        assert brief.approved_version.id == v1.id
        assert brief.next_action == "audition chorus and keep or revise"
        assert brief.next_action_evidence[0].source_ref == "journey-checkpoint"
    finally:
        relaunched.close()
''',
    encoding="utf-8",
)

(ROOT / ".github" / "workflows" / "governance.yml").write_text(
    '''name: N0TE governance and regression
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install --upgrade pip && python -m pip install -r requirements.txt && python -m pip install pytest
      - run: python -m compileall -q n0te governance
      - run: python governance/check_governance.py --offline
      - run: python -m pytest -q tests
      - name: Active contamination scan
        run: |
          set -euo pipefail
          if grep -RIn --exclude-dir="__pycache__" --exclude-dir="migration" --exclude="*.yml" --exclude="*.yaml" -E "N0TE2|n0te2" n0te governance tests README.md; then
            echo "Legacy repository identity found in active surfaces" >&2
            exit 1
          fi
''',
    encoding="utf-8",
)
