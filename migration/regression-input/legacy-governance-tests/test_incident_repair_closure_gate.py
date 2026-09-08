from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "n0te2_steward_integration_closure_gate",
    ROOT / "governance" / "check_steward_integration.py",
)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


INCIDENT_ID = "INC-CLOSURE-TEST-001"
INCIDENT = {
    "id": INCIDENT_ID,
    "repair_contract": {
        "future_receipt_field": "incident_repair_ids",
        "unrelated_merges_blocked": True,
    },
}


def _write_receipt(tmp_path: Path, receipt: dict) -> None:
    governance = tmp_path / "governance"
    governance.mkdir(parents=True, exist_ok=True)
    (governance / "active_receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n",
        encoding="utf-8",
    )


def test_active_incident_repair_remains_explicit_authority(tmp_path: Path) -> None:
    _write_receipt(
        tmp_path,
        {
            "status": "ACTIVE",
            "repair_kind": "INCIDENT_REPAIR",
            "incident_repair_ids": [INCIDENT_ID],
        },
    )
    assert gate._explicit_incident_repair(tmp_path, INCIDENT) is True


def test_terminal_incident_repair_closure_is_explicit_authority(tmp_path: Path) -> None:
    _write_receipt(
        tmp_path,
        {
            "status": "INACTIVE",
            "repair_kind": "INCIDENT_REPAIR_CLOSURE",
            "closed_incident_repair_ids": [INCIDENT_ID],
            "incident_repair_ids": [],
        },
    )
    assert gate._explicit_incident_repair(tmp_path, INCIDENT) is True


def test_closure_cannot_authorize_unrelated_incident(tmp_path: Path) -> None:
    _write_receipt(
        tmp_path,
        {
            "status": "INACTIVE",
            "repair_kind": "INCIDENT_REPAIR_CLOSURE",
            "closed_incident_repair_ids": ["INC-OTHER"],
            "incident_repair_ids": [],
        },
    )
    assert gate._explicit_incident_repair(tmp_path, INCIDENT) is False


def test_inert_closed_ids_without_closure_kind_are_not_authority(tmp_path: Path) -> None:
    _write_receipt(
        tmp_path,
        {
            "status": "INACTIVE",
            "repair_kind": "OTHER",
            "closed_incident_repair_ids": [INCIDENT_ID],
            "incident_repair_ids": [],
        },
    )
    assert gate._explicit_incident_repair(tmp_path, INCIDENT) is False
