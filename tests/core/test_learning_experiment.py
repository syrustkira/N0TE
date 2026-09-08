from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory
from n0te.learning_experiment import (
    LearningExperimentService,
    StaleLearningExperimentError,
)


class LearningExperimentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def setup_work(self):
        hq = HeadquartersMemory.create(self.root / "data", "Learning Artist")
        song = hq.store.create_song("Learning Song")
        session = hq.sessions.start_session(
            song_id=song.id,
            objective="Try one deliberate mix change",
        )
        return hq, song, session

    def test_change_observation_decision_uses_canonical_learning_memory(self) -> None:
        hq, song, session = self.setup_work()
        try:
            service = LearningExperimentService(hq.learning)
            binding = service.start_binding()
            self.assertIsNotNone(binding)
            self.assertEqual(binding.song_id, song.id)
            self.assertEqual(binding.session_id, session.id)

            episode = service.start_episode(
                binding,
                domain="Mixing",
                subject="Vocal compression",
                change_description="Lengthened the compressor attack so the vocal transient could pass.",
            )
            self.assertEqual(episode.song_id, song.id)
            self.assertEqual(episode.session_id, session.id)
            self.assertEqual(episode.consequences, ())
            self.assertIsNone(episode.decision)

            observation = service.append_observation(
                episode.id,
                observation="The consonants felt clearer while the level stayed controlled.",
                confidence="MEDIUM",
                conditions="Same vocal take and matched monitor level",
                confounders="I also listened after a short ear break",
            )
            self.assertEqual(observation.source_kind, "USER_DECLARED")
            self.assertTrue(
                observation.source_ref.startswith("consumer-learning-observation:")
            )
            self.assertEqual(observation.confidence, 0.7)
            self.assertEqual(
                observation.conditions,
                ("Same vocal take and matched monitor level",),
            )
            self.assertEqual(
                observation.confounders,
                ("I also listened after a short ear break",),
            )

            decision_binding = service.decision_binding(episode.id)
            decision = service.decide(
                decision_binding,
                decision="KEEP",
                rationale="Keep the slower attack for this vocal and compare again later in the full mix.",
                confidence="MEDIUM",
            )
            self.assertEqual(decision.decision, "KEEP")
            self.assertEqual(decision.confidence, 0.7)

            stored = hq.learning.get_episode(episode.id)
            self.assertIsNotNone(stored)
            self.assertEqual(len(stored.consequences), 1)
            self.assertEqual(stored.decision, decision)

            kinds = [item.event_type for item in hq.activity.for_song(song.id)]
            self.assertIn("LEARNING_EPISODE_STARTED", kinds)
            self.assertIn("LEARNING_CONSEQUENCE_RECORDED", kinds)
            self.assertIn("LEARNING_DECISION_RECORDED", kinds)
        finally:
            hq.close()

    def test_start_binding_fails_closed_when_rendered_session_closes(self) -> None:
        hq, _, session = self.setup_work()
        try:
            service = LearningExperimentService(hq.learning)
            binding = service.start_binding()
            self.assertIsNotNone(binding)
            hq.sessions.close_session(
                session.id,
                debrief_summary="Stopped before running the experiment",
                next_action="Start a fresh work Session",
            )
            with self.assertRaisesRegex(
                StaleLearningExperimentError,
                "Session changed",
            ):
                service.start_episode(
                    binding,
                    domain="Arrangement",
                    subject="Chorus density",
                    change_description="Remove one supporting layer.",
                )
            self.assertEqual(hq.learning.episodes_for_song(binding.song_id), ())
        finally:
            hq.close()

    def test_decision_rejects_unseen_new_observation_atomically(self) -> None:
        hq, _, _ = self.setup_work()
        try:
            service = LearningExperimentService(hq.learning)
            start = service.start_binding()
            self.assertIsNotNone(start)
            episode = service.start_episode(
                start,
                domain="Arrangement",
                subject="Pre-chorus lift",
                change_description="Muted the bass for the final half-bar before the chorus.",
            )
            service.append_observation(
                episode.id,
                observation="The chorus entrance felt larger.",
                confidence="MEDIUM",
            )
            stale = service.decision_binding(episode.id)
            service.append_observation(
                episode.id,
                observation="The pre-chorus also felt slightly emptier than intended.",
                confidence="HIGH",
                confounders="The chorus synth was louder than the reference balance",
            )

            with self.assertRaisesRegex(
                StaleLearningExperimentError,
                "New Learning evidence",
            ):
                service.decide(
                    stale,
                    decision="KEEP",
                    rationale="The first observation looked positive.",
                    confidence="HIGH",
                )
            stored = hq.learning.get_episode(episode.id)
            self.assertIsNotNone(stored)
            self.assertEqual(len(stored.consequences), 2)
            self.assertIsNone(stored.decision)
        finally:
            hq.close()

    def test_terminal_decision_closes_episode_but_not_session(self) -> None:
        hq, song, session = self.setup_work()
        try:
            service = LearningExperimentService(hq.learning)
            start = service.start_binding()
            self.assertIsNotNone(start)
            episode = service.start_episode(
                start,
                domain="Sound design",
                subject="Pad width",
                change_description="Reduced the chorus width before the vocal entered.",
            )
            service.append_observation(
                episode.id,
                observation="The vocal center felt easier to locate.",
                confidence="LOW",
            )
            service.decide(
                service.decision_binding(episode.id),
                decision="REVISE",
                rationale="Try a smaller width reduction before keeping the move.",
                confidence="MEDIUM",
            )

            with self.assertRaisesRegex(
                StaleLearningExperimentError,
                "final decision",
            ):
                service.append_observation(
                    episode.id,
                    observation="Another observation after closure",
                    confidence="MEDIUM",
                )
            self.assertEqual(hq.sessions.get_session(session.id).state, "OPEN")
            self.assertEqual(hq.store.active_song().id, song.id)
        finally:
            hq.close()

    def test_decision_remains_possible_after_session_close_and_reopen(self) -> None:
        data_root = self.root / "data"
        hq = HeadquartersMemory.create(data_root, "Learning Artist")
        profile_id = hq.store.profile_id
        song = hq.store.create_song("Learning Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Try one move")
        service = LearningExperimentService(hq.learning)
        start = service.start_binding()
        self.assertIsNotNone(start)
        episode = service.start_episode(
            start,
            domain="Mixing",
            subject="Reverb pre-delay",
            change_description="Increased vocal reverb pre-delay.",
        )
        service.append_observation(
            episode.id,
            observation="The vocal felt more forward while the tail stayed audible.",
            confidence="MEDIUM",
        )
        hq.sessions.close_session(
            session.id,
            debrief_summary="Captured the result and stopped",
            next_action="Review the Learning observation",
        )
        hq.close()

        reopened = HeadquartersMemory.open(data_root, profile_id)
        try:
            service = LearningExperimentService(reopened.learning)
            self.assertIsNone(service.start_binding())
            stored = reopened.learning.get_episode(episode.id)
            self.assertIsNotNone(stored)
            self.assertIsNone(stored.decision)
            decision = service.decide(
                service.decision_binding(episode.id),
                decision="INCONCLUSIVE",
                rationale="I need another matched comparison before adopting the move.",
                confidence="LOW",
            )
            self.assertEqual(decision.decision, "INCONCLUSIVE")
            self.assertEqual(reopened.sessions.get_session(session.id).state, "CLOSED")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
