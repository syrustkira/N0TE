import tempfile
import unittest
from pathlib import Path

from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory
from n0te.songwriting import (
    SONGWRITING_ASPECTS,
    SONGWRITING_SOURCE_KIND,
    SongwritingCaseHistoryIntegrityError,
    SongwritingCaseHistoryService,
)


class SongwritingCaseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Writing Artist")
        self.addCleanup(self.hq.close)
        self.song = self.hq.store.create_song("Writing Song")
        self.version = self.hq.store.create_version(self.song.id, label="writing pass")
        self.session = self.hq.sessions.start_session(
            song_id=self.song.id,
            version_id=self.version.id,
            objective="Find the chorus lyric and vocal shape",
        )
        self.service = SongwritingCaseHistoryService(self.hq.store, self.hq.sessions)

    def test_capture_reuses_session_memory_without_creating_parallel_doctrine(self):
        before_claims = self.hq.store._conn.execute(
            "SELECT COUNT(*) FROM evidence_claims"
        ).fetchone()[0]
        before_versions = self.hq.store._conn.execute(
            "SELECT COUNT(*) FROM versions WHERE song_id=?", (self.song.id,)
        ).fetchone()[0]

        entry = self.service.capture(
            song_id=self.song.id,
            session_id=self.session.id,
            aspect="lyrics",
            section=" Chorus 1 ",
            kind="mark",
            text="Keep the first line conversational; do not resolve the thought yet.",
        )

        self.assertEqual(entry.song_id, self.song.id)
        self.assertEqual(entry.session_id, self.session.id)
        self.assertEqual(entry.version_id, self.version.id)
        self.assertEqual(entry.aspect, "LYRICS")
        self.assertEqual(entry.section, "Chorus 1")
        self.assertEqual(entry.kind, "MARK")
        self.assertEqual(entry.source_kind, SONGWRITING_SOURCE_KIND)
        self.assertFalse(entry.promoted)
        self.assertFalse(entry.provider_used)
        self.assertFalse(entry.host_mutated)
        self.assertFalse(entry.action_authority_granted)
        self.assertEqual(
            self.hq.store._conn.execute(
                "SELECT COUNT(*) FROM evidence_claims"
            ).fetchone()[0],
            before_claims,
        )
        self.assertEqual(
            self.hq.store._conn.execute(
                "SELECT COUNT(*) FROM versions WHERE song_id=?", (self.song.id,)
            ).fetchone()[0],
            before_versions,
        )

    def test_all_retained_writing_and_vocal_case_aspects_are_representable(self):
        for index, aspect in enumerate(SONGWRITING_ASPECTS):
            entry = self.service.capture(
                song_id=self.song.id,
                session_id=self.session.id,
                aspect=aspect,
                section=f"Moment {index}",
                kind="OBSERVATION",
                text=f"Artist-entered case note for {aspect}",
            )
            self.assertEqual(entry.aspect, aspect)
        self.assertEqual(
            [entry.aspect for entry in self.service.entries_for_session(self.session.id)],
            list(SONGWRITING_ASPECTS),
        )

    def test_unrelated_session_scratch_is_not_reclassified_as_songwriting(self):
        foreign = self.hq.sessions.append_scratch(
            self.session.id,
            kind="MARK",
            body="Remember to export a rough mix for the collaborator",
        )
        writing = self.service.capture(
            song_id=self.song.id,
            session_id=self.session.id,
            aspect="TOPLINE",
            kind="UNRESOLVED",
            text="Does the last note of the phrase want to fall instead of rise?",
        )

        entries = self.service.entries_for_session(self.session.id)
        self.assertEqual([entry.item_id for entry in entries], [writing.item_id])
        self.assertNotEqual(entries[0].item_id, foreign.id)

    def test_case_history_survives_relaunch_and_spans_sessions_without_promotion(self):
        profile_id = self.hq.store.profile_id
        first = self.service.capture(
            song_id=self.song.id,
            session_id=self.session.id,
            aspect="PHRASING",
            section="Verse 1",
            kind="REJECTED_IDEA",
            text="Do not rush the pickup into line three.",
        )
        self.hq.sessions.close_session(
            self.session.id,
            debrief_summary="The verse reads better with more breath before line three.",
            next_action="Try a lower chorus entry tomorrow.",
        )
        second_session = self.hq.sessions.start_session(
            song_id=self.song.id,
            objective="Try a lower chorus entry",
        )
        second = self.service.capture(
            song_id=self.song.id,
            session_id=second_session.id,
            aspect="PERFORMANCE",
            section="Chorus",
            kind="OBSERVATION",
            text="The lower entry feels calmer when I sing it.",
        )

        self.hq.close()
        reopened = HeadquartersMemory.open(self.root, profile_id)
        self.addCleanup(reopened.close)
        service = SongwritingCaseHistoryService(reopened.store, reopened.sessions)
        history = service.entries_for_song(self.song.id)

        self.assertEqual([entry.item_id for entry in history], [first.item_id, second.item_id])
        self.assertEqual(history[0].session_state, "CLOSED")
        self.assertEqual(history[1].session_state, "OPEN")
        self.assertTrue(all(not entry.promoted for entry in history))
        self.assertEqual(
            reopened.store._conn.execute(
                "SELECT COUNT(*) FROM evidence_claims"
            ).fetchone()[0],
            0,
        )

    def test_only_explicit_decision_promotion_becomes_creative_evidence(self):
        idea = self.service.capture(
            song_id=self.song.id,
            session_id=self.session.id,
            aspect="HARMONIES",
            section="Final chorus",
            kind="MARK",
            text="Maybe add a third above the last word.",
        )
        with self.assertRaisesRegex(ValidationError, "only an explicit songwriting DECISION"):
            self.service.promote_decision(idea.item_id)

        decision = self.service.capture(
            song_id=self.song.id,
            session_id=self.session.id,
            aspect="HARMONIES",
            section="Final chorus",
            kind="DECISION",
            text="Keep the third-above harmony only on the final word.",
        )
        key = self.service.semantic_key(decision)
        self.assertEqual(
            self.hq.evidence.resolve_for_song(song_id=self.song.id, key=key).status,
            "UNKNOWN",
        )

        promoted = self.service.promote_decision(decision.item_id, scope_kind="SONG")
        self.assertEqual(promoted.semantic_key, "songwriting.harmonies.final_chorus")
        self.assertEqual(promoted.claim.source_kind, "USER_DECLARED")
        self.assertEqual(promoted.claim.twin_domain, "CREATIVE")
        self.assertEqual(promoted.claim.scope_kind, "SONG")
        self.assertEqual(promoted.claim.scope_id, self.song.id)
        self.assertTrue(promoted.entry.promoted)
        resolved = self.hq.evidence.resolve_for_song(
            song_id=self.song.id, key=promoted.semantic_key
        )
        self.assertEqual(resolved.status, "RESOLVED")
        self.assertIn(
            "Keep the third-above harmony only on the final word.",
            resolved.value,
        )

    def test_version_promotion_stays_bound_to_session_version(self):
        decision = self.service.capture(
            song_id=self.song.id,
            session_id=self.session.id,
            aspect="TAKE_COMP",
            section="Verse 2",
            kind="DECISION",
            text="Use take three for the first half of verse two.",
        )
        promoted = self.service.promote_decision(decision.item_id, scope_kind="VERSION")
        self.assertEqual(promoted.claim.scope_kind, "VERSION")
        self.assertEqual(promoted.claim.scope_id, self.version.id)

    def test_capture_rejects_cross_song_or_closed_session_binding(self):
        other = self.hq.store.create_song("Other Song")
        with self.assertRaisesRegex(ValidationError, "different Song"):
            self.service.capture(
                song_id=other.id,
                session_id=self.session.id,
                aspect="LYRICS",
                text="This must not cross Songs.",
            )

        self.hq.sessions.close_session(
            self.session.id,
            debrief_summary="Writing pass closed.",
            next_action="Review tomorrow.",
        )
        with self.assertRaisesRegex(ValidationError, "only to an open Session"):
            self.service.capture(
                song_id=self.song.id,
                session_id=self.session.id,
                aspect="LYRICS",
                text="Late mutation must fail.",
            )

    def test_namespaced_malformed_case_history_fails_closed(self):
        with self.hq.store._tx():
            self.hq.store._conn.execute(
                "INSERT INTO session_items(id,session_id,kind,body) VALUES(?,?,?,?)",
                (
                    "sitem_malformed_songwrite",
                    self.session.id,
                    "MARK",
                    "A line\n\n[N0TE-SONGWRITE/1] not-json",
                ),
            )
        with self.assertRaises(SongwritingCaseHistoryIntegrityError):
            self.service.entries_for_session(self.session.id)

    def test_validation_bounds_section_text_and_semantic_key(self):
        with self.assertRaises(ValidationError):
            self.service.capture(
                song_id=self.song.id,
                session_id=self.session.id,
                aspect="VOICE_CLONING",
                text="Voice cloning is intentionally not this feature.",
            )
        with self.assertRaisesRegex(ValidationError, "section label is too long"):
            self.service.capture(
                song_id=self.song.id,
                session_id=self.session.id,
                aspect="LYRICS",
                section="x" * 161,
                text="Bound the section label.",
            )
        with self.assertRaisesRegex(ValidationError, "case-history limit"):
            self.service.capture(
                song_id=self.song.id,
                session_id=self.session.id,
                aspect="LYRICS",
                text="x" * 12001,
            )


if __name__ == "__main__":
    unittest.main()
