import json
from pathlib import Path

from governance.model import validate_constitutional_migration

ROOT = Path(__file__).resolve().parents[2]


def test_fast_pipeline_candidate_is_constitutionally_bounded():
    candidate = json.loads(
        (
            ROOT
            / "migration/candidates/constitutional-fast-pipeline-2026-09-08.json"
        ).read_text()
    )
    assert validate_constitutional_migration(candidate)
    assert candidate["state"] == "CANDIDATE"
    assert candidate["ordinary_product_path"] is False
    assert candidate["permanent_bypass"] is False
    assert candidate["semantic_owner_reconciliation"]["product_semantics_changed"] is False
    assert candidate["semantic_owner_reconciliation"]["evidence_meanings_changed"] is False
    assert candidate["candidate_owner"] != candidate["candidate_certifier"]

    invariants = set(candidate["external_invariants"])
    assert any("exact proposed head" in item for item in invariants)
    assert any("resulting-main" in item for item in invariants)
    assert any("CONSUMER_ACCEPTED" in item for item in invariants)
