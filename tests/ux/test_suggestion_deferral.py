import tempfile
import unittest

from n0te.creative_suggestions import CreativeSuggestionService
from n0te.memory import HeadquartersMemory


class SuggestionDeferralTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hq = HeadquartersMemory.create(self.tmp.name, "Deferral Artist")
        self.song = self.hq.store.create_song("Deferred Song")
        self.session = self.hq.sessions.start_session(
            song_id=self.song.id,
            objective="Keep the current idea focused",
        )
        self.suggestions = CreativeSuggestionService(self.hq.store, self.hq.sessions)

    def tearDown(self):
        self.hq.close()

    def test_explicit_deferral_is_durable_for_exact_song_session(self):
        suggestion = self.suggestions.suggest(distance="ADJACENT")
        record = self.hq.suggestion_deferrals.defer_later_this_song(suggestion.semantic_key)

        self.assertEqual(record.song_id, self.song.id)
        self.assertEqual(record.session_id, self.session.id)
        self.assertEqual(record.semantic_key, suggestion.semantic_key)
        self.assertEqual(record.scope, "LATER_THIS_SONG")
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(suggestion.semantic_key))

        profile_id = self.hq.store.profile_id
        self.hq.close()
        self.hq = HeadquartersMemory.open(self.tmp.name, profile_id)
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(suggestion.semantic_key))
        self.assertEqual(len(self.hq.suggestion_deferrals.history()), 1)

    def test_distinct_later_session_makes_key_eligible_without_deleting_history(self):
        suggestion = self.suggestions.suggest(distance="FAMILIAR")
        self.hq.suggestion_deferrals.defer_later_this_song(suggestion.semantic_key)
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(suggestion.semantic_key))

        self.hq.sessions.close_session(
            self.session.id,
            debrief_summary="Paused this idea for now",
            next_action="Return with fresh ears",
        )
        later = self.hq.sessions.start_session(
            song_id=self.song.id,
            objective="Continue the Song in a later work Session",
        )

        self.assertNotEqual(later.id, self.session.id)
        self.assertFalse(self.hq.suggestion_deferrals.is_deferred_now(suggestion.semantic_key))
        history = self.hq.suggestion_deferrals.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].session_id, self.session.id)

    def test_deferral_is_song_scoped(self):
        suggestion = self.suggestions.suggest(distance="WILDCARD")
        self.hq.suggestion_deferrals.defer_later_this_song(suggestion.semantic_key)
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(suggestion.semantic_key))

        other = self.hq.store.create_song("Other Song")
        self.hq.store.select_song(other.id)
        self.hq.sessions.start_session(song_id=other.id, objective="Work on another Song")
        self.assertFalse(self.hq.suggestion_deferrals.is_deferred_now(suggestion.semantic_key))

    def test_duplicate_explicit_action_is_idempotent_and_activity_is_recorded_once(self):
        suggestion = self.suggestions.suggest(distance="ADJACENT")
        before = len(self.hq.activity.for_profile())
        first = self.hq.suggestion_deferrals.defer_later_this_song(suggestion.semantic_key)
        second = self.hq.suggestion_deferrals.defer_later_this_song(suggestion.semantic_key)
        self.assertEqual(first, second)
        after = self.hq.activity.for_profile()
        self.assertEqual(len(after), before + 1)
        self.assertEqual(after[-1].event_type, "SUGGESTION_DEFERRED")


if __name__ == "__main__":
    unittest.main()
