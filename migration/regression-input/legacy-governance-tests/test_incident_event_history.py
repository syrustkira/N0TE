from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "n0te2_governance_incident_event_history",
    ROOT / "governance/check_governance.py",
)
gov = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gov)

AUTH_SPEC = importlib.util.spec_from_file_location(
    "n0te2_governance_incident_repair_completion",
    ROOT / "governance/check_incident_repair_authority.py",
)
authority = importlib.util.module_from_spec(AUTH_SPEC)
AUTH_SPEC.loader.exec_module(authority)

INCIDENT_206 = "INC-2026-09-05-STEWARD-INTEGRATION-206"


class IncidentEventHistoryTests(unittest.TestCase):
    def clone(self) -> Path:
        td = tempfile.TemporaryDirectory()
        dst = Path(td.name) / "repo"
        shutil.copytree(
            ROOT,
            dst,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        self.addCleanup(td.cleanup)
        return dst

    def test_incident_history_allows_later_event_with_same_incident_id(self) -> None:
        repo = self.clone()
        path = repo / "governance/incidents.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        source = next(row for row in rows if row.get("id") == INCIDENT_206)
        initial_count = sum(row.get("id") == source["id"] for row in rows)
        rows.append(
            {
                "id": source["id"],
                "recorded_at": "2026-09-05",
                "status": "RESOLVED_TEST_FIXTURE",
                "severity": "TEST_ONLY",
                "summary": "Append-only resolution event for duplicate-ID policy regression.",
            }
        )
        path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n")
        parsed = gov.check_jsonl_ids(repo, "governance/incidents.jsonl", allow_repeated_ids=True)
        self.assertEqual(
            sum(row.get("id") == source["id"] for row in parsed),
            initial_count + 1,
        )

    def test_bounded_repair_completion_can_leave_parent_incident_open(self) -> None:
        repo = self.clone()
        path = repo / "governance/incidents.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        source = next(row for row in rows if row.get("id") == INCIDENT_206)
        rows.append(
            {
                "id": INCIDENT_206,
                "recorded_at": "2026-09-05",
                "status": "OPEN_REPAIR_COMPLETED_TEST_FIXTURE",
                "severity": source["severity"],
                "summary": "One bounded child repair completed while the parent integration incident remains open.",
                "repair_completed_at": "2026-09-05T19:35:00-05:00",
                "repair_completion_condition": "The exact bounded governance repair completed and returned to terminal state without satisfying the parent incident closure contract.",
                "remaining_open_obligations": [
                    "Template Catalog repair remains",
                    "Capability Negotiation repair remains",
                    "Opportunity Matching repair remains",
                    "Creative Partner repair remains",
                ],
                "repair_contract": source["repair_contract"],
                "evidence": {"test_fixture": True},
                "repair_receipt_id": "N0TE2-INCIDENT-REPAIR-TEST-01",
                "closure_receipt_id": "N0TE2-INCIDENT-REPAIR-TEST-01-CLOSURE",
            }
        )
        path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n")

        authority.validate_repair_completion_events(
            repo,
            [INCIDENT_206],
            closed_repair_receipt_id="N0TE2-INCIDENT-REPAIR-TEST-01",
            closure_receipt_id="N0TE2-INCIDENT-REPAIR-TEST-01-CLOSURE",
        )

        current = authority.current_incident(
            authority.load_jsonl(path), INCIDENT_206
        )
        self.assertIsNotNone(current)
        self.assertTrue(str(current["status"]).startswith("OPEN"))
        self.assertEqual(
            authority.validate_incidents_open(repo, [INCIDENT_206])[0]["id"],
            INCIDENT_206,
            "parent incident must remain eligible to authorize the next bounded repair",
        )

    def test_open_repair_completion_requires_remaining_obligations(self) -> None:
        repo = self.clone()
        path = repo / "governance/incidents.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        source = next(row for row in rows if row.get("id") == INCIDENT_206)
        rows.append(
            {
                "id": INCIDENT_206,
                "recorded_at": "2026-09-05",
                "status": "OPEN_REPAIR_COMPLETED_TEST_FIXTURE",
                "severity": source["severity"],
                "summary": "Invalid fixture omits remaining parent obligations.",
                "repair_completed_at": "2026-09-05T19:35:00-05:00",
                "repair_completion_condition": "One bounded repair completed.",
                "repair_contract": source["repair_contract"],
                "evidence": {"test_fixture": True},
                "repair_receipt_id": "N0TE2-INCIDENT-REPAIR-TEST-01",
                "closure_receipt_id": "N0TE2-INCIDENT-REPAIR-TEST-01-CLOSURE",
            }
        )
        path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n")

        with self.assertRaises(authority.RepairAuthorityError) as cm:
            authority.validate_repair_completion_events(
                repo,
                [INCIDENT_206],
                closed_repair_receipt_id="N0TE2-INCIDENT-REPAIR-TEST-01",
                closure_receipt_id="N0TE2-INCIDENT-REPAIR-TEST-01-CLOSURE",
            )
        self.assertIn("remaining_open_obligations", str(cm.exception))

    def test_non_incident_registry_still_rejects_duplicate_stable_id(self) -> None:
        repo = self.clone()
        path = repo / "governance/decisions.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        rows.append(dict(rows[0]))
        path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n")
        with self.assertRaises(gov.GovernanceError) as cm:
            gov.check_jsonl_ids(repo, "governance/decisions.jsonl")
        self.assertIn("duplicate stable ids", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
