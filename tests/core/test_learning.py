import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import (
    HeadquartersMemory,
    LineageCorruptionError,
    NotFoundError,
    ValidationError,
)


class Core02CChangeConsequenceDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _hq_with_session(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        song = hq.store.create_song("Song")
        version = hq.store.create_version(song.id, label="v1")
        session = hq.sessions.start_session(
            song_id=song.id,
            version_id=version.id,
            objective="Try one bounded production change",
        )
        return hq, song, version, session

    def test_episode_records_change_without_auto_doctrine_or_skill(self):
        hq, song, _, session = self._hq_with_session()
        self.addCleanup(hq.close)
        evidence_before = hq.store._conn.execute(
            "SELECT COUNT(*) FROM evidence_claims"
        ).fetchone()[0]
        episode = hq.learning.create_episode(
            session_id=session.id,
            domain="MIX",
            subject_ref="chorus.vocal.compression",
            change_description="Lower ratio from 6:1 to 3:1",
        )
        hq.learning.append_consequence(
            episode.id,
            observation="The vocal transient feels more alive",
            source_kind="OBSERVED",
            source_ref="artist:listen:1",
            confidence=0.7,
            conditions=("same monitor level", "same chorus section"),
            confounders=("fresh ears",),
        )
        self.assertEqual(
            hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0],
            evidence_before,
        )
        self.assertEqual(hq.skills.state("skill:compression").level, "UNKNOWN")
        self.assertFalse(
            hq.store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name IN ('success_rules','friction_rules','causal_rules')"
            ).fetchone()
        )
        self.assertEqual(hq.learning.episodes_for_song(song.id)[0].id, episode.id)

    def test_consequence_preserves_conditions_confounders_and_confidence(self):
        hq, _, _, session = self._hq_with_session()
        self.addCleanup(hq.close)
        episode = hq.learning.create_episode(
            session_id=session.id,
            domain="ARRANGEMENT",
            subject_ref="chorus.density",
            change_description="Remove the second guitar layer",
        )
        consequence = hq.learning.append_consequence(
            episode.id,
            observation="The lead vocal is easier to follow",
            source_kind="USER_DECLARED",
            source_ref="artist:comparison:1",
            confidence=0.6,
            conditions=("level matched", "same chorus"),
            confounders=("different listening order", "fresh ears"),
        )
        self.assertEqual(consequence.conditions, ("level matched", "same chorus"))
        self.assertEqual(
            consequence.confounders,
            ("different listening order", "fresh ears"),
        )
        self.assertEqual(consequence.confidence, 0.6)

    def test_decision_requires_observation_and_is_final(self):
        hq, _, _, session = self._hq_with_session()
        self.addCleanup(hq.close)
        episode = hq.learning.create_episode(
            session_id=session.id,
            domain="WRITING",
            subject_ref="chorus.harmony",
            change_description="Try a lower harmony on the final line",
        )
        with self.assertRaises(ValidationError):
            hq.learning.decide(
                episode.id,
                decision="KEEP",
                rationale="No observation exists yet",
            )
        hq.learning.append_consequence(
            episode.id,
            observation="The line feels less crowded",
            source_kind="OBSERVED",
            source_ref="artist:listen:2",
            confidence=0.65,
        )
        decision = hq.learning.decide(
            episode.id,
            decision="INCONCLUSIVE",
            rationale="Promising, but one listen is not enough to generalize",
            confidence=0.55,
        )
        self.assertEqual(decision.decision, "INCONCLUSIVE")
        with self.assertRaises(ValidationError):
            hq.learning.decide(
                episode.id,
                decision="KEEP",
                rationale="Try to overwrite the decision",
            )
        with self.assertRaises(ValidationError):
            hq.learning.append_consequence(
                episode.id,
                observation="Late observation",
                source_kind="OBSERVED",
                source_ref="artist:late",
            )

    def test_corrupt_cross_song_session_binding_fails_on_reopen(self):
        hq, _, _, session = self._hq_with_session()
        profile = hq.store.profile_id
        other = hq.store.create_song("Other")
        bad_id = "learn_" + "b" * 32
        with hq.store._tx():
            hq.store._conn.execute(
                "INSERT INTO learning_episodes("
                "id,artist_id,song_id,version_id,session_id,domain,subject_ref,"
                "change_description) VALUES(?,?,?,?,?,?,?,?)",
                (
                    bad_id,
                    hq.store.primary_artist_id,
                    other.id,
                    None,
                    session.id,
                    "MIX",
                    "bad",
                    "Cross a Session into another Song",
                ),
            )
        hq.close()
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, profile)

    def test_foreign_profile_episode_id_is_not_visible(self):
        hq, _, _, session = self._hq_with_session()
        self.addCleanup(hq.close)
        episode = hq.learning.create_episode(
            session_id=session.id,
            domain="MIX",
            subject_ref="bass",
            change_description="Change bass saturation",
        )
        with tempfile.TemporaryDirectory() as td2:
            with HeadquartersMemory.create(Path(td2), "Other Artist") as other:
                with self.assertRaises(NotFoundError):
                    other.learning.append_consequence(
                        episode.id,
                        observation="Should never resolve across profiles",
                        source_kind="OBSERVED",
                        source_ref="foreign",
                    )

    def test_history_rows_are_immutable(self):
        hq, _, _, session = self._hq_with_session()
        self.addCleanup(hq.close)
        episode = hq.learning.create_episode(
            session_id=session.id,
            domain="MIX",
            subject_ref="snare",
            change_description="Shorten the room tail",
        )
        consequence = hq.learning.append_consequence(
            episode.id,
            observation="Backbeat feels more forward",
            source_kind="OBSERVED",
            source_ref="listen:immutability",
        )
        decision = hq.learning.decide(
            episode.id,
            decision="KEEP",
            rationale="Keep for this version and re-evaluate later",
        )
        for sql, params in (
            ("UPDATE learning_episodes SET change_description='rewrite' WHERE id=?", (episode.id,)),
            ("DELETE FROM learning_consequences WHERE id=?", (consequence.id,)),
            ("UPDATE learning_decisions SET decision='REVERT' WHERE id=?", (decision.id,)),
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                hq.store._conn.execute(sql, params)
            hq.store._conn.rollback()

    def test_restart_preserves_episode_activity_and_pure_reads(self):
        hq, song, _, session = self._hq_with_session()
        profile = hq.store.profile_id
        episode = hq.learning.create_episode(
            session_id=session.id,
            domain="PRODUCTION",
            subject_ref="chorus.energy",
            change_description="Mute the extra kick layer in the first half",
        )
        hq.learning.append_consequence(
            episode.id,
            observation="The second half feels larger by contrast",
            source_kind="USER_DECLARED",
            source_ref="artist:contrast",
            confidence=0.75,
            conditions=("same arrangement render",),
            confounders=("novelty",),
        )
        hq.learning.decide(
            episode.id,
            decision="KEEP",
            rationale="Keep in this version; the contrast supports the intended arc",
            confidence=0.7,
        )
        hq.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        restored = hq.learning.get_episode(episode.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.decision.decision, "KEEP")
        self.assertEqual(restored.consequences[0].confounders, ("novelty",))
        event_types = [event.event_type for event in hq.activity.for_song(song.id)]
        for required in (
            "LEARNING_EPISODE_STARTED",
            "LEARNING_CONSEQUENCE_RECORDED",
            "LEARNING_DECISION_RECORDED",
        ):
            self.assertIn(required, event_types)

        before = hq.store._conn.total_changes
        self.assertEqual(hq.learning.get_episode(episode.id), restored)
        self.assertEqual(hq.learning.episodes_for_song(song.id), (restored,))
        self.assertEqual(hq.store._conn.total_changes, before)

    def test_invalid_source_and_confidence_are_rejected(self):
        hq, _, _, session = self._hq_with_session()
        self.addCleanup(hq.close)
        episode = hq.learning.create_episode(
            session_id=session.id,
            domain="MIX",
            subject_ref="vocal",
            change_description="Try a shorter plate",
        )
        with self.assertRaises(ValidationError):
            hq.learning.append_consequence(
                episode.id,
                observation="Something changed",
                source_kind="MAGIC",
                source_ref="bad",
            )
        with self.assertRaises(ValidationError):
            hq.learning.append_consequence(
                episode.id,
                observation="Something changed",
                source_kind="OBSERVED",
                source_ref="bad-confidence",
                confidence=1.1,
            )


if __name__ == "__main__":
    unittest.main()
