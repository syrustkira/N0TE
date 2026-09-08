from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory
from n0te.interaction_depth import (
    INTERACTION_DEPTH_MODES,
    InteractionDepthError,
    InteractionDepthService,
    StaleInteractionDepthError,
)
from n0te.learning import ConsequenceObservation, LearningEpisode


def seed_episode(hq: HeadquartersMemory):
    song = hq.store.create_song("Interaction Song")
    session = hq.sessions.start_session(song_id=song.id, objective="Improve the chorus")
    episode = hq.learning.create_episode(
        session_id=session.id,
        domain="ARRANGEMENT",
        subject_ref="chorus impact",
        change_description="Mute the pre-chorus kick for one bar before the chorus",
    )
    return song, session, episode


class InteractionDepthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Interaction Artist")
        self.song, self.session, self.episode = seed_episode(self.hq)
        self.service = InteractionDepthService(self.hq.learning)

    def tearDown(self) -> None:
        self.hq.close()
        self.tmp.cleanup()

    def test_all_five_modes_are_distinct_and_never_grant_authority(self) -> None:
        binding = self.service.binding_for(self.episode.id)
        plans = [self.service.plan(binding, mode) for mode in INTERACTION_DEPTH_MODES]
        self.assertEqual({plan.mode for plan in plans}, set(INTERACTION_DEPTH_MODES))
        self.assertEqual(len({plan.n0te_role for plan in plans}), 5)
        self.assertEqual(len({plan.artist_role for plan in plans}), 5)
        self.assertEqual(len({plan.next_step for plan in plans}), 5)
        self.assertEqual(
            len(
                {
                    tuple((step.actor, step.instruction) for step in plan.steps)
                    for plan in plans
                }
            ),
            5,
        )
        self.assertTrue(all(len(plan.steps) >= 4 for plan in plans))
        self.assertTrue(all(plan.action_authority_granted is False for plan in plans))
        self.assertTrue(all(plan.mutation_permitted_by_mode is False for plan in plans))
        self.assertEqual(
            [plan.mode for plan in plans if plan.execution_requested],
            ["DO_IT"],
        )

    def test_do_it_requests_leadership_without_claiming_execution(self) -> None:
        plan = self.service.plan(self.service.binding_for(self.episode.id), "DO IT")
        self.assertEqual(plan.mode, "DO_IT")
        self.assertTrue(plan.execution_requested)
        self.assertFalse(plan.action_authority_granted)
        self.assertFalse(plan.mutation_permitted_by_mode)
        self.assertEqual(
            [step.actor for step in plan.steps],
            ["N0TE", "N0TE", "AUTHORITY", "YOU"],
        )
        self.assertIn("verified executor", plan.steps[1].instruction)
        self.assertIn("separate exact authority", plan.steps[2].instruction)
        self.assertIn("Do not silently broaden", plan.steps[0].instruction)

    def test_show_me_is_a_concrete_read_only_walkthrough(self) -> None:
        plan = self.service.plan(self.service.binding_for(self.episode.id), "SHOW ME")
        self.assertFalse(plan.execution_requested)
        self.assertIn("read-only walkthrough", plan.n0te_role)
        self.assertIn("BEFORE", plan.steps[1].instruction)
        self.assertIn("AFTER", plan.steps[1].instruction)
        self.assertIn("does not claim the project was modified", plan.steps[1].instruction)

    def test_explain_why_preserves_observation_conditions_confounders_and_causal_humility(self) -> None:
        self.hq.learning.append_consequence(
            self.episode.id,
            observation="The chorus entrance felt larger",
            source_kind="USER_DECLARED",
            source_ref="test:explain-observation",
            confidence=0.7,
            conditions=("Same monitoring level",),
            confounders=("Arrangement contrast may also have changed",),
        )
        plan = self.service.plan(
            self.service.binding_for(self.episode.id),
            "EXPLAIN WHY",
        )
        self.assertIn("question, not established causation", plan.steps[0].instruction)
        self.assertIn("changing fewer variables", plan.steps[1].instruction)
        summary = " ".join(plan.evidence_summary)
        self.assertIn("The chorus entrance felt larger", summary)
        self.assertIn("artist-reported", summary)
        self.assertIn("Conditions: Same monitoring level", summary)
        self.assertIn(
            "Possible confounders: Arrangement contrast may also have changed",
            summary,
        )

    def test_interaction_evidence_preserves_earlier_contradictory_observations(self) -> None:
        observations = (
            (
                "The first listen felt bigger",
                ("Reference level matched",),
                ("Fresh ears may have biased the judgment",),
            ),
            ("The second listen felt unchanged", (), ()),
            ("The third listen felt punchier", (), ()),
            ("The fourth listen felt brighter", (), ()),
        )
        for index, (observation, conditions, confounders) in enumerate(observations, start=1):
            self.hq.learning.append_consequence(
                self.episode.id,
                observation=observation,
                source_kind="USER_DECLARED",
                source_ref=f"test:history-{index}",
                confidence=0.6,
                conditions=conditions,
                confounders=confounders,
            )
        plan = self.service.plan(
            self.service.binding_for(self.episode.id),
            "EXPLAIN WHY",
        )
        summary = " ".join(plan.evidence_summary)
        self.assertIn("The first listen felt bigger", summary)
        self.assertIn("Conditions: Reference level matched", summary)
        self.assertIn("Possible confounders: Fresh ears may have biased the judgment", summary)
        self.assertIn("The second listen felt unchanged", summary)
        self.assertIn("The fourth listen felt brighter", summary)
        self.assertNotIn("earlier consequence observations are also recorded", summary)

    def test_unknown_learning_evidence_source_stops_guidance_safely(self) -> None:
        observation = ConsequenceObservation(
            sequence=1,
            id="lobs_future",
            episode_id="learn_future",
            observation="A future evidence class exists",
            source_kind="FUTURE_SOURCE",
            source_ref="future:1",
            confidence=0.5,
            conditions=(),
            confounders=(),
        )
        episode = LearningEpisode(
            sequence=1,
            id="learn_future",
            artist_id="artist_future",
            song_id="song_future",
            version_id=None,
            session_id="session_future",
            domain="ARRANGEMENT",
            subject_ref="future subject",
            change_description="future change",
            consequences=(observation,),
            decision=None,
        )
        with self.assertRaises(InteractionDepthError):
            InteractionDepthService._evidence_summary(episode)

    def test_let_me_try_keeps_n0te_out_of_mutation(self) -> None:
        plan = self.service.plan(self.service.binding_for(self.episode.id), "LET_ME_TRY")
        self.assertEqual(plan.mode, "LET_ME_TRY")
        self.assertFalse(plan.execution_requested)
        self.assertIn("stand back", plan.n0te_role.lower())
        self.assertIn("yourself", plan.artist_role)
        self.assertEqual(
            [step.actor for step in plan.steps],
            ["YOU", "YOU", "N0TE", "YOU"],
        )
        self.assertIn("Do not mutate", plan.steps[2].instruction)

    def test_planning_is_pure_and_does_not_create_learning_or_skill_evidence(self) -> None:
        before_changes = self.hq.store._conn.total_changes
        before_episode = self.hq.learning.get_episode(self.episode.id)
        before_skill_count = self.hq.store._conn.execute(
            "SELECT COUNT(*) FROM skill_assessments"
        ).fetchone()[0]
        binding = self.service.binding_for(self.episode.id)
        for mode in INTERACTION_DEPTH_MODES:
            self.service.plan(binding, mode)
        self.assertEqual(self.hq.store._conn.total_changes, before_changes)
        self.assertEqual(self.hq.learning.get_episode(self.episode.id), before_episode)
        after_skill_count = self.hq.store._conn.execute(
            "SELECT COUNT(*) FROM skill_assessments"
        ).fetchone()[0]
        self.assertEqual(after_skill_count, before_skill_count)

    def test_binding_fails_closed_when_learning_evidence_changes(self) -> None:
        binding = self.service.binding_for(self.episode.id)
        self.hq.learning.append_consequence(
            self.episode.id,
            observation="The chorus felt larger",
            source_kind="USER_DECLARED",
            source_ref="test:observation",
            confidence=0.7,
        )
        with self.assertRaises(StaleInteractionDepthError):
            self.service.plan(binding, "WITH_ME")

    def test_binding_fails_closed_when_newer_open_learning_job_becomes_current(self) -> None:
        binding = self.service.binding_for(self.episode.id)
        newer = self.hq.learning.create_episode(
            session_id=self.session.id,
            domain="ARRANGEMENT",
            subject_ref="chorus transition",
            change_description="Shorten the fill before the chorus",
        )
        current = self.service.current_binding()
        self.assertIsNotNone(current)
        self.assertEqual(current.episode_id, newer.id)
        with self.assertRaises(StaleInteractionDepthError):
            self.service.plan(binding, "SHOW_ME")
        with self.assertRaises(StaleInteractionDepthError):
            self.service.binding_for(self.episode.id)

    def test_binding_fails_closed_when_active_song_changes(self) -> None:
        binding = self.service.binding_for(self.episode.id)
        other = self.hq.store.create_song("Other Song")
        self.hq.store.select_song(other.id)
        with self.assertRaises(StaleInteractionDepthError):
            self.service.plan(binding, "EXPLAIN_WHY")

    def test_current_binding_uses_latest_open_learning_job_only(self) -> None:
        current = self.service.current_binding()
        self.assertIsNotNone(current)
        self.assertEqual(current.episode_id, self.episode.id)
        self.hq.learning.append_consequence(
            self.episode.id,
            observation="The result still needs another pass",
            source_kind="USER_DECLARED",
            source_ref="test:close-observation",
            confidence=0.7,
        )
        self.hq.learning.decide(
            self.episode.id,
            decision="INCONCLUSIVE",
            rationale="Need more evidence",
            confidence=0.5,
        )
        self.assertIsNone(self.service.current_binding())


if __name__ == "__main__":
    unittest.main()
