from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory
from n0te.friction import FrictionObservation
from n0te.friction_journey import (
    FrictionJourneyError,
    SongFrictionJourney,
    StaleFrictionJourneyError,
)


def create_episode(hq: HeadquartersMemory, song_id: str, objective: str):
    session = hq.sessions.start_session(song_id=song_id, objective=objective)
    episode = hq.learning.create_episode(
        session_id=session.id,
        domain="PROCESS",
        subject_ref="creative.flow",
        change_description="Observe the work honestly",
    )
    hq.sessions.close_session(
        session.id,
        debrief_summary="Captured the work state",
        next_action="Continue the Song",
    )
    return session, episode


class SongFrictionJourneyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_consumer_capture_is_user_declared_server_owned_and_song_bound(self) -> None:
        hq = HeadquartersMemory.create(self.root, "Artist")
        try:
            song = hq.store.create_song("Song")
            session, episode = create_episode(hq, song.id, "Track verse")
            service = SongFrictionJourney(hq.friction)
            binding = service.capture_binding(episode.id)
            observation = service.record(
                binding,
                friction_key="context-switching",
                description="Notifications broke focus",
                confidence="MEDIUM",
                prevention_hint="Silence notifications before tracking",
            )
            self.assertEqual(observation.song_id, song.id)
            self.assertEqual(observation.session_id, session.id)
            self.assertEqual(observation.episode_id, episode.id)
            self.assertEqual(observation.source_kind, "USER_DECLARED")
            self.assertTrue(observation.source_ref.startswith("consumer-friction:"))
            self.assertEqual(observation.confidence, 0.7)
            self.assertEqual(hq.friction.recurring_patterns(song_id=song.id), ())
            events = [event.event_type for event in hq.activity.for_song(song.id)]
            self.assertEqual(events.count("FRICTION_OBSERVED"), 1)
        finally:
            hq.close()

    def test_duplicate_key_in_same_episode_is_rejected_without_false_recurrence(self) -> None:
        hq = HeadquartersMemory.create(self.root, "Artist")
        try:
            song = hq.store.create_song("Song")
            _, episode = create_episode(hq, song.id, "Track verse")
            service = SongFrictionJourney(hq.friction)
            binding = service.capture_binding(episode.id)
            service.record(
                binding,
                friction_key="context-switching",
                description="Notifications broke focus",
                confidence="MEDIUM",
            )
            with self.assertRaises(FrictionJourneyError) as cm:
                service.record(
                    binding,
                    friction_key="context-switching",
                    description="Same blocker must not count twice",
                    confidence="HIGH",
                )
            self.assertIn("already recorded", str(cm.exception))
            self.assertEqual(len(hq.friction.observations(song_id=song.id)), 1)
            self.assertEqual(service.recurring_for_active_song(), ())
        finally:
            hq.close()

    def test_recurrence_requires_same_explicit_key_across_distinct_sessions(self) -> None:
        hq = HeadquartersMemory.create(self.root, "Artist")
        try:
            song = hq.store.create_song("Song")
            _, first = create_episode(hq, song.id, "Track verse")
            _, second = create_episode(hq, song.id, "Track chorus")
            service = SongFrictionJourney(hq.friction)
            service.record(
                service.capture_binding(first.id),
                friction_key="plugin-browsing",
                description="Browsing broke the verse flow",
                confidence="HIGH",
                prevention_hint="Choose three tools before the Session",
            )
            self.assertEqual(service.recurring_for_active_song(), ())
            service.record(
                service.capture_binding(second.id),
                friction_key="plugin-browsing",
                description="Browsing broke the chorus flow",
                confidence="MEDIUM",
                prevention_hint="Choose three tools before the Session",
            )
            patterns = service.recurring_for_active_song()
            self.assertEqual(len(patterns), 1)
            self.assertEqual(patterns[0].key, "plugin-browsing")
            self.assertEqual(patterns[0].session_count, 2)
            self.assertEqual(patterns[0].occurrence_count, 2)
            self.assertEqual(
                patterns[0].prevention_hints,
                ("Choose three tools before the Session",),
            )
        finally:
            hq.close()

    def test_two_learning_episodes_in_one_session_do_not_manufacture_recurrence(self) -> None:
        hq = HeadquartersMemory.create(self.root, "Artist")
        try:
            song = hq.store.create_song("Song")
            session = hq.sessions.start_session(song_id=song.id, objective="One long pass")
            episodes = [
                hq.learning.create_episode(
                    session_id=session.id,
                    domain="PROCESS",
                    subject_ref=f"pass.{index}",
                    change_description="Observe this pass",
                )
                for index in (1, 2)
            ]
            service = SongFrictionJourney(hq.friction)
            for episode in episodes:
                service.record(
                    service.capture_binding(episode.id),
                    friction_key="plugin-browsing",
                    description="Browsing interrupted work",
                    confidence="MEDIUM",
                )
            self.assertEqual(service.recurring_for_active_song(), ())
        finally:
            hq.close()

    def test_stale_active_song_fails_closed_without_write(self) -> None:
        hq = HeadquartersMemory.create(self.root, "Artist")
        try:
            first_song = hq.store.create_song("First")
            _, episode = create_episode(hq, first_song.id, "First work")
            service = SongFrictionJourney(hq.friction)
            binding = service.capture_binding(episode.id)
            second_song = hq.store.create_song("Second")
            hq.store.select_song(second_song.id)
            before = hq.store._conn.execute(
                "SELECT COUNT(*) FROM friction_observations"
            ).fetchone()[0]
            with self.assertRaises(StaleFrictionJourneyError):
                service.record(
                    binding,
                    friction_key="context-switching",
                    description="Must not land on stale authority",
                    confidence="MEDIUM",
                )
            after = hq.store._conn.execute(
                "SELECT COUNT(*) FROM friction_observations"
            ).fetchone()[0]
            self.assertEqual(after, before)
        finally:
            hq.close()

    def test_projection_fails_closed_on_unknown_source_semantics(self) -> None:
        observation = FrictionObservation(
            sequence=1,
            id="fric_internal",
            episode_id="learn_internal",
            friction_key="unknown",
            description="Unknown source semantics",
            source_kind="FUTURE_SOURCE",
            source_ref="future:1",
            confidence=0.5,
            prevention_hint=None,
            song_id="song_internal",
            session_id="sess_internal",
        )
        with self.assertRaises(FrictionJourneyError):
            SongFrictionJourney._observation_view(observation)


if __name__ == "__main__":
    unittest.main()
