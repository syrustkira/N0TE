import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory, NotFoundError, ValidationError


class Core02DRepeatedFrictionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.song = self.hq.store.create_song("Song")
        self.version = self.hq.store.create_version(self.song.id, label="v1")

    def tearDown(self):
        self.hq.close()
        self.tmp.cleanup()

    def _episode(self, objective="Work"):
        session = self.hq.sessions.start_session(
            song_id=self.song.id, version_id=self.version.id, objective=objective
        )
        episode = self.hq.learning.create_episode(
            session_id=session.id,
            domain="PROCESS",
            subject_ref="session.flow",
            change_description="Observe the workflow without inferring a pattern",
        )
        self.hq.sessions.close_session(
            session.id,
            debrief_summary="Recorded the represented workflow incident",
            next_action="Continue with the Song",
        )
        return session, episode

    def test_one_incident_is_not_a_pattern(self):
        _, episode = self._episode()
        self.hq.friction.record(
            episode_id=episode.id,
            friction_key="context-switching",
            description="Notifications interrupted tracking",
            source_kind="USER_DECLARED",
            source_ref="artist:session-1",
            confidence=0.9,
            prevention_hint="Silence notifications before tracking",
        )
        self.assertEqual(self.hq.friction.recurring_patterns(), ())

    def test_same_explicit_key_across_distinct_sessions_becomes_pattern(self):
        first_session, first_episode = self._episode("Track verse")
        second_session, second_episode = self._episode("Track chorus")
        first = self.hq.friction.record(
            episode_id=first_episode.id,
            friction_key="context-switching",
            description="Notifications interrupted the verse take",
            source_kind="USER_DECLARED",
            source_ref="artist:first",
            confidence=0.9,
            prevention_hint="Silence notifications before tracking",
        )
        second = self.hq.friction.record(
            episode_id=second_episode.id,
            friction_key="context-switching",
            description="Message checking broke chorus focus",
            source_kind="OBSERVED",
            source_ref="artist:second",
            confidence=0.8,
            prevention_hint="Use a dedicated tracking focus mode",
        )
        patterns = self.hq.friction.recurring_patterns()
        self.assertEqual(len(patterns), 1)
        pattern = patterns[0]
        self.assertEqual(pattern.friction_key, "context-switching")
        self.assertEqual(pattern.occurrences, (first, second))
        self.assertEqual(pattern.session_count, 2)
        self.assertEqual(
            pattern.session_ids, (first_session.id, second_session.id)
        )
        self.assertEqual(
            pattern.prevention_hints,
            (
                "Silence notifications before tracking",
                "Use a dedicated tracking focus mode",
            ),
        )

    def test_two_episodes_in_same_session_do_not_satisfy_recurrence(self):
        session = self.hq.sessions.start_session(
            song_id=self.song.id, version_id=self.version.id, objective="One long session"
        )
        episodes = [
            self.hq.learning.create_episode(
                session_id=session.id,
                domain="PROCESS",
                subject_ref=f"pass.{index}",
                change_description="Observe another moment in the same Session",
            )
            for index in (1, 2)
        ]
        for index, episode in enumerate(episodes, start=1):
            self.hq.friction.record(
                episode_id=episode.id,
                friction_key="plugin-browsing",
                description=f"Browsing stalled pass {index}",
                source_kind="OBSERVED",
                source_ref=f"observe:{index}",
            )
        self.assertEqual(self.hq.friction.recurring_patterns(), ())

    def test_duplicate_key_within_episode_is_rejected(self):
        _, episode = self._episode()
        self.hq.friction.record(
            episode_id=episode.id,
            friction_key="plugin-browsing",
            description="Browsing stalled work",
            source_kind="OBSERVED",
            source_ref="observe:1",
        )
        with self.assertRaises(ValidationError):
            self.hq.friction.record(
                episode_id=episode.id,
                friction_key="plugin-browsing",
                description="Same incident cannot be counted twice",
                source_kind="OBSERVED",
                source_ref="observe:2",
            )

    def test_different_keys_remain_separate(self):
        _, first = self._episode("First")
        _, second = self._episode("Second")
        self.hq.friction.record(
            episode_id=first.id,
            friction_key="context-switching",
            description="Interrupted",
            source_kind="OBSERVED",
            source_ref="one",
        )
        self.hq.friction.record(
            episode_id=second.id,
            friction_key="plugin-browsing",
            description="Browsed too long",
            source_kind="OBSERVED",
            source_ref="two",
        )
        self.assertEqual(self.hq.friction.recurring_patterns(), ())

    def test_prevention_hints_are_preserved_not_invented(self):
        _, first = self._episode("First")
        _, second = self._episode("Second")
        for episode, ref, hint in (
            (first, "one", "Print a three-option shortlist before the Session"),
            (second, "two", None),
        ):
            self.hq.friction.record(
                episode_id=episode.id,
                friction_key="plugin-browsing",
                description="Plugin browsing interrupted creative flow",
                source_kind="USER_DECLARED",
                source_ref=ref,
                prevention_hint=hint,
            )
        pattern = self.hq.friction.recurring_patterns()[0]
        self.assertEqual(
            pattern.prevention_hints,
            ("Print a three-option shortlist before the Session",),
        )

    def test_foreign_episode_is_not_visible(self):
        _, episode = self._episode()
        with tempfile.TemporaryDirectory() as td2:
            other = HeadquartersMemory.create(Path(td2), "Other")
            try:
                with self.assertRaises(NotFoundError):
                    other.friction.record(
                        episode_id=episode.id,
                        friction_key="foreign",
                        description="Must not cross profiles",
                        source_kind="OBSERVED",
                        source_ref="foreign",
                    )
            finally:
                other.close()

    def test_history_is_immutable_and_inputs_are_validated(self):
        _, episode = self._episode()
        observation = self.hq.friction.record(
            episode_id=episode.id,
            friction_key="context-switching",
            description="Interrupted",
            source_kind="OBSERVED",
            source_ref="observe",
            confidence=0.7,
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.hq.store._conn.execute(
                "DELETE FROM friction_observations WHERE id=?", (observation.id,)
            )
        self.hq.store._conn.rollback()
        with self.assertRaises(ValidationError):
            self.hq.friction.record(
                episode_id=episode.id,
                friction_key="bad-source",
                description="Bad",
                source_kind="MAGIC",
                source_ref="bad",
            )
        with self.assertRaises(ValidationError):
            self.hq.friction.recurring_patterns(min_sessions=1)

    def test_restart_activity_and_read_purity(self):
        first_session, first = self._episode("First")
        second_session, second = self._episode("Second")
        for episode, ref in ((first, "one"), (second, "two")):
            self.hq.friction.record(
                episode_id=episode.id,
                friction_key="context-switching",
                description="Explicitly represented focus break",
                source_kind="USER_DECLARED",
                source_ref=ref,
                confidence=0.8,
                prevention_hint="Use focus mode",
            )
        profile = self.hq.store.profile_id
        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, profile)
        pattern = self.hq.friction.recurring_patterns()[0]
        self.assertEqual(pattern.session_count, 2)
        self.assertEqual(pattern.session_ids, (first_session.id, second_session.id))
        self.assertEqual(pattern.prevention_hints, ("Use focus mode",))
        events = [event.event_type for event in self.hq.activity.for_song(self.song.id)]
        self.assertEqual(events.count("FRICTION_OBSERVED"), 2)
        before = self.hq.store._conn.total_changes
        self.assertEqual(self.hq.friction.recurring_patterns(), (pattern,))
        self.assertEqual(self.hq.store._conn.total_changes, before)

    def test_friction_does_not_create_skill_evidence_or_rule_tables(self):
        _, first = self._episode("First")
        _, second = self._episode("Second")
        evidence_before = self.hq.store._conn.execute(
            "SELECT COUNT(*) FROM evidence_claims"
        ).fetchone()[0]
        skill_before = self.hq.store._conn.execute(
            "SELECT COUNT(*) FROM skill_assessments"
        ).fetchone()[0]
        for episode, ref in ((first, "one"), (second, "two")):
            self.hq.friction.record(
                episode_id=episode.id,
                friction_key="context-switching",
                description="Explicit blocker",
                source_kind="OBSERVED",
                source_ref=ref,
            )
        self.assertEqual(len(self.hq.friction.recurring_patterns()), 1)
        self.assertEqual(
            self.hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0],
            evidence_before,
        )
        self.assertEqual(
            self.hq.store._conn.execute("SELECT COUNT(*) FROM skill_assessments").fetchone()[0],
            skill_before,
        )
        self.assertEqual(
            self.hq.store._conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('causal_rules','success_rules','friction_rules')"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
