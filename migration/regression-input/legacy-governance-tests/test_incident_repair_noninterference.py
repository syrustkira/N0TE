from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "n0te2_incident_repair_noninterference",
    ROOT / "governance/check_incident_repair_authority.py",
)
authority = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(authority)


def test_normal_active_product_receipt_is_not_retyped_as_incident_repair() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        shutil.copytree(
            ROOT,
            repo,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )

        state_path = repo / "governance/current_state.json"
        state = json.loads(state_path.read_text())
        state.update(
            {
                "lifecycle_state": "ACTIVE",
                "active_node": "UX-01",
                "active_increment": "UX-01-NORMAL-PRODUCT-TEST",
                "terminal_reason": None,
                "product_code_authorized": True,
                "legacy_admission_authorized": False,
            }
        )
        state_path.write_text(json.dumps(state, indent=2) + "\n")

        receipt_path = repo / "governance/active_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        for key in (
            "repair_kind",
            "repair_target_kind",
            "repair_issue",
            "repair_target_merge_sha",
            "incident_repair_ids",
            "closed_incident_repair_ids",
            "closed_repair_receipt_id",
        ):
            receipt.pop(key, None)
        receipt.update(
            {
                "status": "ACTIVE",
                "receipt_id": "N0TE2-UX-01-NORMAL-PRODUCT-TEST",
                "node_id": "UX-01",
                "increment_id": "UX-01-NORMAL-PRODUCT-TEST",
                "baseline_sha": "a" * 40,
                "product_code_allowed": True,
                "legacy_admission_allowed": False,
            }
        )
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

        with mock.patch.dict(
            os.environ,
            {
                "N0TE2_BASE_SHA": "a" * 40,
                "N0TE2_EVENT_MODE": "",
                "N0TE2_META_GOVERNANCE_REOPEN": "",
            },
            clear=False,
        ):
            authority.run(repo)
