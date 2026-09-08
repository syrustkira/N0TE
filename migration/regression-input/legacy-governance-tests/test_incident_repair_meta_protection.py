from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "n0te2_incident_repair_meta_protection",
    ROOT / "governance/check_incident_repair_authority.py",
)
authority = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(authority)


def test_lifecycle_enforcement_surfaces_require_meta_governance_reopen() -> None:
    expected = {
        "governance/build_handoff.py",
        "governance/check_context_lifecycle.py",
        "governance/check_governance.py",
        "governance/check_incident_repair_authority.py",
        "governance/check_steward_integration.py",
        "governance/smoke/consumer_smoke.py",
        "governance/supervision.py",
    }
    assert expected.issubset(authority.PROTECTED_CANDIDATE_GOVERNANCE)
