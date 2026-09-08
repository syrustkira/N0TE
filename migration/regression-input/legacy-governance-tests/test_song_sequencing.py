import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class SongConstructionSequencingTests(unittest.TestCase):
    def setUp(self):
        graph = json.loads((ROOT / "governance" / "completion_graph.json").read_text())
        self.nodes = {row["id"]: row for row in graph["nodes"]}

    def test_song_01_waits_for_product_foundations_not_cross_cutting_ux_convergence(self):
        self.assertEqual(
            set(self.nodes["SONG-01"]["depends_on"]),
            {"CORE-01", "CORE-02", "APP-01"},
        )
        self.assertNotIn("UX-01", self.nodes["SONG-01"]["depends_on"])

    def test_final_convergence_still_requires_both_song_and_ux(self):
        convergence = set(self.nodes["CONV-01"]["depends_on"])
        self.assertIn("SONG-01", convergence)
        self.assertIn("UX-01", convergence)

    def test_ux_scope_is_not_removed_by_sequencing_repair(self):
        requirements = self.nodes["UX-01"]["requirements"]
        for marker in ("006", "016-017", "096-097", "100", "102-103", "124", "136", "142-143"):
            self.assertIn(marker, requirements)


if __name__ == "__main__":
    unittest.main()
