import tempfile
import unittest

from n0te.activity import ActivityLog
from n0te.creative_suggestions import CreativeSuggestionService
from n0te.evidence import EvidenceMemory
from n0te.lineage import LineageStore, ValidationError
from n0te.memory import HeadquartersMemory
from n0te.session import SessionMemory
from n0te.suggestion_deferral import (
    LATER_THIS_SONG,
    NEVER_SUGGEST_AGAIN,
    NEXT_SONG,
    SuggestionDeferralMemory,
)


class SuggestionDeferralHorizonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hq = HeadquartersMemory.create(self.tmp.name, "Horizon Artist")
        self.song = self.hq.store.create_song("Source Song")
        self.session = self.hq.sessions.start_session(
            song_id=self.song.id,
            objective="Keep the current Song focused",
        )
        self.suggestions = CreativeSuggestionService(self.hq.store, self.hq.sessions)

    def tearDown(self):
        self.hq.close()

    def _deferred_activity_count(self) -> int:
        return len(
            [
                event
                for event in self.hq.activity.for_profile()
                if event.event_type == "SUGGESTION_DEFERRED"
            ]
        )

    def test_next_song_horizon_expires_after_different_song_and_can_be_chosen_again(self):
        key = self.suggestions.suggest(distance="ADJACENT").semantic_key
        before_deferred = self._deferred_activity_count()

        first = self.hq.suggestion_deferrals.defer_until_next_song(key)
        self.assertEqual(first.scope, NEXT_SONG)
        self.assertEqual(first.song_id, self.song.id)
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(key))

        self.hq.sessions.close_session(
            self.session.id,
            debrief_summary="Keep this pattern parked until another Song",
            next_action="Continue this Song without that suggestion",
        )
        later = self.hq.sessions.start_session(
            song_id=self.song.id,
            objective="Return to the same Song in a new Session",
        )
        same_horizon = self.hq.suggestion_deferrals.defer_until_next_song(key)
        self.assertEqual(same_horizon, first)
        self.assertNotEqual(later.id, first.session_id)
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(key))

        other = self.hq.store.create_song("Next Song")
        self.hq.sessions.start_session(
            song_id=other.id,
            objective="Cross the next-Song horizon",
        )
        self.assertFalse(self.hq.suggestion_deferrals.is_deferred_now(key))

        self.hq.store.select_song(self.song.id)
        self.assertFalse(self.hq.suggestion_deferrals.is_deferred_now(key))
        second = self.hq.suggestion_deferrals.defer_until_next_song(key)
        self.assertNotEqual(second.id, first.id)
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(key))
        self.assertEqual(self._deferred_activity_count(), before_deferred + 2)

    def test_never_suggest_again_is_artist_wide_and_idempotent_across_songs(self):
        key = self.suggestions.suggest(distance="WILDCARD").semantic_key
        before_deferred = self._deferred_activity_count()

        first = self.hq.suggestion_deferrals.never_suggest_again(key)
        self.assertEqual(first.scope, NEVER_SUGGEST_AGAIN)
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(key))

        other = self.hq.store.create_song("Other Song")
        self.hq.sessions.start_session(
            song_id=other.id,
            objective="Prove Artist-wide suppression crosses Songs",
        )
        second = self.hq.suggestion_deferrals.never_suggest_again(key)
        self.assertEqual(second, first)
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(key))

        profile_id = self.hq.store.profile_id
        self.hq.close()
        self.hq = HeadquartersMemory.open(self.tmp.name, profile_id)
        self.assertTrue(self.hq.suggestion_deferrals.is_deferred_now(key))
        self.assertEqual(self._deferred_activity_count(), before_deferred + 1)

    def test_next_song_and_never_are_available_without_a_work_session(self):
        other_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(other_tmp.cleanup)
        hq = HeadquartersMemory.create(other_tmp.name, "No Session Artist")
        try:
            hq.store.create_song("No Session Song")
            next_record = hq.suggestion_deferrals.defer_until_next_song(
                "arrangement:contrast-window"
            )
            never_record = hq.suggestion_deferrals.never_suggest_again(
                "rhythm:single-groove-variable"
            )
            self.assertIsNone(next_record.session_id)
            self.assertIsNone(never_record.session_id)
            with self.assertRaisesRegex(ValidationError, "Start a work Session"):
                hq.suggestion_deferrals.defer_later_this_song(
                    "harmony:one-chord-pressure-test"
                )
        finally:
            hq.close()


class SuggestionDeferralMigrationTests(unittest.TestCase):
    def test_v1_later_this_song_rows_migrate_without_duplicate_activity(self):
        with tempfile.TemporaryDirectory() as temp:
            store = LineageStore.create(temp, "Migration Artist")
            try:
                evidence = EvidenceMemory(store)
                activity = ActivityLog(store)
                sessions = SessionMemory(store, evidence)
                song = store.create_song("Migration Song")
                session = sessions.start_session(
                    song_id=song.id,
                    objective="Preserve the existing Not Now decision",
                )
                deferral_id = "defer_" + "a" * 32
                semantic_key = "melody:motif-variation"
                v1_triggers = (
                    """CREATE TRIGGER suggestion_deferral_binding_valid
                    BEFORE INSERT ON suggestion_deferrals
                    WHEN NOT EXISTS (
                        SELECT 1 FROM songs s
                        JOIN sessions x ON x.song_id=s.id
                        WHERE s.id=NEW.song_id
                          AND s.artist_id=NEW.artist_id
                          AND x.id=NEW.session_id
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'Suggestion deferral binding is invalid');
                    END""",
                    """CREATE TRIGGER suggestion_deferral_immutable
                    BEFORE UPDATE ON suggestion_deferrals
                    BEGIN
                        SELECT RAISE(ABORT, 'Suggestion deferral history is immutable');
                    END""",
                    """CREATE TRIGGER suggestion_deferral_delete_immutable
                    BEFORE DELETE ON suggestion_deferrals
                    BEGIN
                        SELECT RAISE(ABORT, 'Suggestion deferral history is immutable');
                    END""",
                    """CREATE TRIGGER suggestion_deferral_activity
                    AFTER INSERT ON suggestion_deferrals
                    BEGIN
                        INSERT INTO activity_events(
                            id,event_type,artist_id,song_id,version_id,
                            object_type,object_id,payload_json
                        ) VALUES(
                            'act_'||lower(hex(randomblob(16))),
                            'SUGGESTION_DEFERRED',NEW.artist_id,NEW.song_id,NULL,
                            'SUGGESTION_DEFERRAL',NEW.id,
                            '{\"scope\":\"LATER_THIS_SONG\"}'
                        );
                    END""",
                )
                with store._tx():
                    store._conn.execute(
                        """CREATE TABLE suggestion_deferrals (
                            seq INTEGER PRIMARY KEY AUTOINCREMENT,
                            id TEXT NOT NULL UNIQUE,
                            artist_id TEXT NOT NULL REFERENCES artists(id),
                            song_id TEXT NOT NULL REFERENCES songs(id),
                            session_id TEXT NOT NULL REFERENCES sessions(id),
                            semantic_key TEXT NOT NULL,
                            scope TEXT NOT NULL CHECK(scope='LATER_THIS_SONG'),
                            UNIQUE(artist_id,song_id,session_id,semantic_key,scope)
                        )"""
                    )
                    for statement in v1_triggers:
                        store._conn.execute(statement)
                    store._conn.execute(
                        "INSERT INTO metadata(key,value) VALUES(?,?)",
                        ("suggestion_deferral_schema_version", "1"),
                    )
                    store._conn.execute(
                        "INSERT INTO suggestion_deferrals("
                        "id,artist_id,song_id,session_id,semantic_key,scope) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            deferral_id,
                            store.primary_artist_id,
                            song.id,
                            session.id,
                            semantic_key,
                            LATER_THIS_SONG,
                        ),
                    )

                before = [
                    event
                    for event in activity.for_profile()
                    if event.event_type == "SUGGESTION_DEFERRED"
                ]
                self.assertEqual(len(before), 1)

                migrated = SuggestionDeferralMemory(store, sessions)
                self.assertTrue(migrated.is_deferred_now(semantic_key))
                self.assertEqual(migrated.history()[0].id, deferral_id)
                version = store._conn.execute(
                    "SELECT value FROM metadata "
                    "WHERE key='suggestion_deferral_schema_version'"
                ).fetchone()
                self.assertEqual(str(version["value"]), "2")
                after = [
                    event
                    for event in activity.for_profile()
                    if event.event_type == "SUGGESTION_DEFERRED"
                ]
                self.assertEqual(len(after), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
