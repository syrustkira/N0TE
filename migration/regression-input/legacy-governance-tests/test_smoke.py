import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "governance/smoke/consumer_smoke.py"


class SmokeTests(unittest.TestCase):
    def test_current_consumer_smoke_passes(self):
        cp = subprocess.run(
            ["python", str(SCRIPT)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("N0TE CONSUMER SMOKE: GREEN", cp.stdout)

    def test_product_code_is_rejected_when_active_stage_lacks_product_authority(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            shutil.copytree(ROOT, repo)
            state_path = repo / "governance/current_state.json"
            state = json.loads(state_path.read_text())
            state.update(
                {
                    "lifecycle_state": "ACTIVE",
                    "active_node": "UX-01",
                    "active_increment": "UX-01-NO-AUTH-TEST",
                    "terminal_reason": None,
                    "wake_condition": None,
                    "product_code_authorized": False,
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
                "incident_repair_ids",
                "repair_target_merge_sha",
                "closed_repair_receipt_id",
            ):
                receipt.pop(key, None)
            receipt.update(
                {
                    "status": "ACTIVE",
                    "receipt_id": "N0TE2-UX-01-NO-AUTH-TEST",
                    "node_id": "UX-01",
                    "increment_id": "UX-01-NO-AUTH-TEST",
                    "product_code_allowed": False,
                    "legacy_admission_allowed": False,
                }
            )
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

            cp = subprocess.run(
                ["python", str(repo / "governance/smoke/consumer_smoke.py")],
                cwd=repo,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("active construction lacks product-code authority", cp.stderr)


if __name__ == "__main__":
    unittest.main()
