import json
import tempfile
import unittest
from pathlib import Path

from n0te.context_lifecycle import ContextBudget
from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory


class ContextLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _memory(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        song = hq.store.create_song("Context Song")
        version = hq.store.create_version(song.id, label="v1")
        for index in range(3):
            session = hq.sessions.start_session(
                song_id=song.id,
                version_id=version.id,
                objective=f"Objective {index + 1}",
            )
            hq.sessions.close_session(
                session.id,
                debrief_summary=f"Debrief {index + 1}",
                next_action=f"Next {index + 1}",
            )
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="creative.constraint",
            value="Protect the topline",
            source_kind="USER_DECLARED",
            source_ref="artist:context-test",
            confidence=1.0,
            twin_domain="CREATIVE",
        )
        return hq, song

    def test_projection_is_bounded_regenerable_and_read_only(self):
        hq, song = self._memory()
        self.addCleanup(hq.close)
        before = hq.store._conn.total_changes
        projection = hq.context_projection.projection_for_song(
            song.id,
            purpose="Resume the current arrangement thread",
            sections=("DURABLE_FACTS", "SESSIONS"),
            budget=ContextBudget(max_items_per_section=2),
        )
        self.assertEqual(hq.store._conn.total_changes, before)
        self.assertEqual(projection["schema"], "n0te.context-projection.v1")
        self.assertEqual(projection["authority_ceiling"], "READ_ONLY_CONTEXT")
        self.assertFalse(projection["mutation_policy"]["grants_action_authority"])
        self.assertFalse(projection["mutation_policy"]["automatic_durable_promotion"])
        self.assertEqual(projection["lossiness"], "BOUNDED_PROJECTION")
        self.assertEqual(projection["budget"]["truncated_sections"]["SESSIONS"], 1)
        self.assertFalse(projection["budget"]["canonical_history_deleted"])
        self.assertEqual(
            [row["sequence"] for row in projection["context"]["sessions"]],
            [2, 3],
        )
        self.assertEqual(len(projection["source_digest"]), 64)
        json.dumps(projection, sort_keys=True, allow_nan=False)

        canonical = hq.retention.context_packet_for_song(song.id, sections=("SESSIONS",))
        self.assertEqual(len(canonical["sessions"]), 3)

    def test_narrow_projection_preserves_durable_contradiction_block(self):
        hq, song = self._memory()
        self.addCleanup(hq.close)
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="creative.constraint",
            value="Rewrite the topline",
            source_kind="USER_DECLARED",
            source_ref="artist:context-conflict",
            confidence=1.0,
            twin_domain="CREATIVE",
        )
        before = hq.store._conn.total_changes
        projection = hq.context_projection.projection_for_song(
            song.id,
            purpose="Resume only session history",
            sections=("SESSIONS",),
        )
        self.assertEqual(hq.store._conn.total_changes, before)
        self.assertNotIn("durable_facts", projection["context"])
        self.assertTrue(projection["contradictions"])
        self.assertEqual(
            projection["contradictions"][0]["kind"],
            "DURABLE_FACT_CONFLICT",
        )
        self.assertTrue(
            projection["mutation_policy"]["critical_contradiction_blocks_autonomous_mutation"]
        )

    def test_same_sources_produce_same_digest_across_restart(self):
        hq, song = self._memory()
        profile_id = hq.store.profile_id
        first = hq.context_projection.projection_for_song(
            song.id,
            purpose="Resume",
            sections=("SESSIONS", "DURABLE_FACTS"),
        )
        hq.close()
        reopened = HeadquartersMemory.open(self.root, profile_id)
        self.addCleanup(reopened.close)
        second = reopened.context_projection.projection_for_song(
            song.id,
            purpose="Resume",
            sections=("SESSIONS", "DURABLE_FACTS"),
        )
        self.assertEqual(first["source_digest"], second["source_digest"])
        self.assertEqual(first["context"], second["context"])

    def test_projection_requires_a_purpose_and_known_sections(self):
        hq, song = self._memory()
        self.addCleanup(hq.close)
        with self.assertRaises(ValidationError):
            hq.context_projection.projection_for_song(song.id, purpose="")
        with self.assertRaises(ValidationError):
            hq.context_projection.projection_for_song(
                song.id,
                purpose="Resume",
                sections=("EVERYTHING_FOREVER",),
            )

    def test_budget_cannot_be_unbounded_or_zero(self):
        with self.assertRaises(ValidationError):
            ContextBudget(max_items_per_section=0)


if __name__ == "__main__":
    unittest.main()
