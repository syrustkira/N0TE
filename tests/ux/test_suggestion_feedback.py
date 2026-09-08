import sqlite3
import tempfile
import unittest
from dataclasses import replace

from n0te.creative_suggestions import CreativeSuggestionService
from n0te.lineage import LineageCorruptionError, ValidationError
from n0te.memory import HeadquartersMemory
from n0te.suggestion_feedback import SuggestionFeedbackEvent


class SuggestionFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hq = HeadquartersMemory.create(self.tmp.name, "Feedback Artist")
        self.song = self.hq.store.create_song("Context Signal")
        self.session = self.hq.sessions.start_session(
            song_id=self.song.id,
            objective="Explore one bounded idea without turning it into doctrine",
        )
        self.suggestions = CreativeSuggestionService(
            self.hq.store, self.hq.sessions
        )

    def tearDown(self):
        if self.hq is not None:
            self.hq.close()

    def test_more_then_less_is_append_only_context_not_preference(self):
        suggestion = self.suggestions.suggest(
            distance="ADJACENT", locked_dimensions=("HARMONY",)
        )
        before_learning = self.hq.store._conn.execute(
            "SELECT COUNT(*) AS n FROM learning_episodes"
        ).fetchone()["n"]

        more = self.hq.suggestion_feedback.record(suggestion, direction="MORE")
        less = self.hq.suggestion_feedback.record(suggestion, direction="LESS")

        self.assertEqual([more.direction, less.direction], ["MORE", "LESS"])
        self.assertLess(more.sequence, less.sequence)
        self.assertEqual(
            [
                event.direction
                for event in self.hq.suggestion_feedback.for_semantic_key(
                    suggestion.semantic_key
                )
            ],
            ["MORE", "LESS"],
        )
        for event in (more, less):
            self.assertEqual(event.song_id, self.song.id)
            self.assertEqual(event.session_id, self.session.id)
            self.assertFalse(event.preference_promoted)
            self.assertFalse(event.learning_promoted)
            self.assertFalse(event.automatic_weighting_applied)
            self.assertFalse(event.song_mutation_authorized)
            self.assertFalse(event.external_action_authorized)

        after_learning = self.hq.store._conn.execute(
            "SELECT COUNT(*) AS n FROM learning_episodes"
        ).fetchone()["n"]
        self.assertEqual(before_learning, after_learning)

        repeated = self.suggestions.suggest(
            distance="ADJACENT", locked_dimensions=("HARMONY",)
        )
        self.assertEqual(repeated, suggestion)

    def test_feedback_requires_exact_current_song_and_session(self):
        suggestion = self.suggestions.suggest(distance="FAMILIAR")
        self.hq.sessions.close_session(
            self.session.id,
            debrief_summary="Move to a fresh context",
            next_action="Start another Session",
        )
        self.hq.sessions.start_session(
            song_id=self.song.id,
            objective="New context",
        )
        with self.assertRaises(ValidationError):
            self.hq.suggestion_feedback.record(suggestion, direction="MORE")

        fresh = self.suggestions.suggest(distance="FAMILIAR")
        other = self.hq.store.create_song("Other Song")
        self.hq.store.select_song(other.id)
        self.hq.sessions.start_session(song_id=other.id, objective="Other work")
        with self.assertRaises(ValidationError):
            self.hq.suggestion_feedback.record(fresh, direction="LESS")

    def test_feedback_rejects_fabricated_or_foreign_suggestion_witnesses(self):
        suggestion = self.suggestions.suggest(distance="ADJACENT")
        forged = (
            replace(suggestion, semantic_key="arrangement:fabricated"),
            replace(suggestion, title=suggestion.title + " forged"),
            replace(suggestion, personalized=True),
            replace(suggestion, provider_used=True),
            replace(suggestion, action_authority_granted=True),
        )
        for witness in forged:
            with self.subTest(witness=witness):
                with self.assertRaises(ValidationError):
                    self.hq.suggestion_feedback.record(witness, direction="MORE")

        other_tmp = tempfile.TemporaryDirectory()
        try:
            other_hq = HeadquartersMemory.create(other_tmp.name, "Foreign Artist")
            try:
                other_song = other_hq.store.create_song("Foreign Song")
                other_hq.sessions.start_session(
                    song_id=other_song.id,
                    objective="Foreign context",
                )
                foreign = CreativeSuggestionService(
                    other_hq.store, other_hq.sessions
                ).suggest(distance="ADJACENT")
            finally:
                other_hq.close()
        finally:
            other_tmp.cleanup()

        with self.assertRaises(ValidationError):
            self.hq.suggestion_feedback.record(foreign, direction="LESS")
        self.assertEqual(self.hq.suggestion_feedback.history(), ())

    def test_feedback_survives_relaunch_with_activity_history(self):
        suggestion = self.suggestions.suggest(distance="WILDCARD")
        before = len(self.hq.activity.for_profile())
        event = self.hq.suggestion_feedback.record(suggestion, direction="MORE")
        after = self.hq.activity.for_profile()
        self.assertEqual(len(after), before + 1)
        self.assertEqual(after[-1].event_type, "SUGGESTION_FEEDBACK_RECORDED")
        self.assertEqual(after[-1].object_id, event.id)

        profile_id = self.hq.store.profile_id
        self.hq.close()
        self.hq = HeadquartersMemory.open(self.tmp.name, profile_id)
        history = self.hq.suggestion_feedback.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0], event)

    def test_storage_boundary_rejects_rewrite_delete_and_cross_song_binding(self):
        suggestion = self.suggestions.suggest(distance="ADJACENT")
        event = self.hq.suggestion_feedback.record(suggestion, direction="MORE")
        conn = self.hq.store._conn

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE suggestion_feedback SET direction='LESS' WHERE id=?",
                (event.id,),
            )
        conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM suggestion_feedback WHERE id=?", (event.id,))
        conn.rollback()

        other = self.hq.store.create_song("Cross Bound Song")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO suggestion_feedback("
                "id,artist_id,song_id,session_id,semantic_key,direction,distance,dimension"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    "sfeedback_direct_invalid",
                    self.hq.store.primary_artist_id,
                    other.id,
                    self.session.id,
                    suggestion.semantic_key,
                    "MORE",
                    suggestion.distance,
                    suggestion.dimension,
                ),
            )
        conn.rollback()

    def test_malformed_inputs_and_authority_forgery_fail_closed(self):
        suggestion = self.suggestions.suggest(distance="ADJACENT")
        for invalid in ("LOVE", "", None, True):
            with self.subTest(direction=invalid):
                with self.assertRaises(ValidationError):
                    self.hq.suggestion_feedback.record(
                        suggestion, direction=invalid
                    )

        with self.assertRaises(TypeError):
            SuggestionFeedbackEvent(
                sequence=1,
                id="forged",
                artist_id=self.hq.store.primary_artist_id,
                song_id=self.song.id,
                session_id=self.session.id,
                semantic_key=suggestion.semantic_key,
                direction="MORE",
                distance=suggestion.distance,
                dimension=suggestion.dimension,
                preference_promoted=True,
            )

    def test_missing_integrity_hook_fails_on_relaunch(self):
        profile_id = self.hq.store.profile_id
        self.hq.store._conn.execute(
            "DROP TRIGGER suggestion_feedback_immutable"
        )
        self.hq.store._conn.commit()
        self.hq.close()
        self.hq = None
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.tmp.name, profile_id)


if __name__ == "__main__":
    unittest.main()
