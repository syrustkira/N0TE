import json
import tempfile
import unittest
from pathlib import Path

from n0te.memory import HeadquartersMemory
from n0te.lineage import ValidationError


class SongRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _populated(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        song = hq.store.create_song("Retention Song")
        version = hq.store.create_version(song.id, label="v1")

        first = hq.sessions.start_session(
            song_id=song.id,
            version_id=version.id,
            objective="Make the chorus lift without crowding the vocal",
        )
        note = hq.sessions.append_scratch(
            first.id,
            kind="OBSERVATION",
            body="Keep the vocal melody unchanged while testing chorus density",
        )
        promoted = hq.sessions.promote_item(
            note.id,
            scope_kind="SONG",
            key="chorus.constraint",
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
            confidence=0.9,
        )
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="chorus.constraint",
            value="Keep the vocal melody unchanged; density may move around it",
            source_kind="USER_DECLARED",
            source_ref="artist:retention:update",
            confidence=1.0,
            twin_domain="CREATIVE",
            supersedes=(promoted.id,),
        )
        episode = hq.learning.create_episode(
            session_id=first.id,
            domain="ARRANGEMENT",
            subject_ref="chorus.density",
            change_description="Remove one supporting layer from the first half",
        )
        hq.learning.append_consequence(
            episode.id,
            observation="The second half feels larger by contrast",
            source_kind="USER_DECLARED",
            source_ref="artist:listen:retention:1",
            confidence=0.75,
            conditions=("same chorus", "same playback level"),
            confounders=("novelty",),
        )
        hq.learning.decide(
            episode.id,
            decision="KEEP",
            rationale="The contrast supports the intended lift in this version",
            confidence=0.7,
        )
        hq.friction.record(
            episode_id=episode.id,
            friction_key="too-many-layers",
            description="I kept adding layers instead of deciding which role was missing",
            source_kind="USER_DECLARED",
            source_ref="artist:friction:retention:1",
            confidence=0.8,
            prevention_hint="Name the missing role before adding another layer",
        )
        hq.skills.record_assessment(
            skill_id="skill:arrangement-density",
            level="INTRODUCED",
            source_kind="ARTIST_DECLARED",
            source_ref="artist:skill:retention",
            confidence=0.8,
            assistance_level=0.6,
            session_id=first.id,
        )
        hq.sessions.close_session(
            first.id,
            debrief_summary="Less density created more contrast without changing the topline",
            next_action="Test whether the pre-chorus needs the same density cleanup",
        )

        second = hq.sessions.start_session(
            song_id=song.id,
            version_id=version.id,
            objective="Check the pre-chorus transition",
        )
        episode2 = hq.learning.create_episode(
            session_id=second.id,
            domain="ARRANGEMENT",
            subject_ref="prechorus.density",
            change_description="Mute one redundant support layer",
        )
        hq.friction.record(
            episode_id=episode2.id,
            friction_key="too-many-layers",
            description="I again reached for another layer before defining the missing role",
            source_kind="USER_DECLARED",
            source_ref="artist:friction:retention:2",
            confidence=0.85,
            prevention_hint="Name the missing role before adding another layer",
        )
        hq.sessions.close_session(
            second.id,
            debrief_summary="The blocker repeated even though the section changed",
            next_action="Start the next arrangement pass by naming each layer role",
        )
        hq.context.import_context(
            scope_kind="SONG",
            scope_id=song.id,
            source_kind="IMPORTED",
            source_ref="reference:brief",
            payload={"reference_note": "Protect vocal clarity while increasing impact"},
        )
        return hq, song

    def test_retention_composes_existing_memories_without_parallel_persistence(self):
        hq, song = self._populated()
        self.addCleanup(hq.close)
        before = hq.store._conn.total_changes
        brief = hq.retention.brief_for_song(song.id)
        after = hq.store._conn.total_changes

        self.assertEqual(after, before)
        self.assertEqual(brief.song_title, "Retention Song")
        self.assertEqual(
            brief.next_action,
            "Start the next arrangement pass by naming each layer role",
        )
        self.assertEqual(len(brief.sessions), 2)
        self.assertEqual(len(brief.learning), 2)
        self.assertEqual(brief.learning[0].decision, "KEEP")
        self.assertEqual(brief.success_patterns[0].causal_status, "ASSOCIATION_ONLY")
        self.assertEqual(
            [item.value for item in brief.durable_facts if item.key == "chorus.constraint"],
            ["Keep the vocal melody unchanged; density may move around it"],
        )
        recurring = [item for item in brief.friction if item.key == "too-many-layers"]
        self.assertEqual(len(recurring), 2)
        self.assertTrue(all(item.recurring_session_count == 2 for item in recurring))
        self.assertEqual(brief.skills[0].skill_id, "skill:arrangement-density")
        self.assertEqual(brief.skills[0].level, "INTRODUCED")
        self.assertEqual(len(brief.imported_context), 1)
        self.assertGreater(len(brief.activity), 0)

    def test_context_packet_is_json_safe_selective_and_hides_internal_ids(self):
        hq, song = self._populated()
        self.addCleanup(hq.close)
        before = hq.store._conn.total_changes
        packet = hq.retention.context_packet_for_song(
            song.id,
            sections=("SESSIONS", "LEARNING", "FRICTION"),
        )
        self.assertEqual(hq.store._conn.total_changes, before)
        encoded = json.dumps(packet, sort_keys=True)
        self.assertIn("sessions", packet)
        self.assertIn("learning", packet)
        self.assertIn("friction", packet)
        self.assertNotIn("durable_facts", packet)
        self.assertNotIn("skills", packet)
        for prefix in ("sess_", "learn_", "claim_", "fric_", "lobs_", "ldec_"):
            self.assertNotIn(prefix, encoded)
        self.assertFalse(packet["retention_policy"]["automatic_promotion"])
        self.assertEqual(packet["retention_policy"]["authority"], "read-only; grants no action authority")

    def test_unknown_section_fails_closed(self):
        hq, song = self._populated()
        self.addCleanup(hq.close)
        with self.assertRaises(ValidationError):
            hq.retention.context_packet_for_song(song.id, sections=("MAGIC",))

    def test_restart_preserves_the_same_retained_thread(self):
        hq, song = self._populated()
        profile_id = hq.store.profile_id
        packet_before = hq.retention.context_packet_for_song(song.id)
        hq.close()

        reopened = HeadquartersMemory.open(self.root, profile_id)
        self.addCleanup(reopened.close)
        packet_after = reopened.retention.context_packet_for_song(song.id)
        self.assertEqual(packet_after, packet_before)
        self.assertEqual(
            reopened.retention.brief_for_song(song.id).next_action,
            "Start the next arrangement pass by naming each layer role",
        )


if __name__ == "__main__":
    unittest.main()
