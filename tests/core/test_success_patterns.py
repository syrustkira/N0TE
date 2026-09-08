from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from n0te import HeadquartersMemory
from n0te.success_patterns import SongSuccessPatterns


def add_episode(
    hq: HeadquartersMemory,
    song_id: str,
    *,
    subject: str,
    change: str,
    decision: str | None,
    observation: str = "Observed result",
    source_kind: str = "USER_DECLARED",
    confidence: float = 0.8,
    conditions: tuple[str, ...] = (),
    confounders: tuple[str, ...] = (),
):
    session = hq.sessions.start_session(song_id=song_id, objective=f"Test {subject}")
    episode = hq.learning.create_episode(
        session_id=session.id,
        domain="Mixing",
        subject_ref=subject,
        change_description=change,
    )
    if observation:
        hq.learning.append_consequence(
            episode.id,
            observation=observation,
            source_kind=source_kind,
            source_ref=f"test:{subject}:{session.id}",
            confidence=confidence,
            conditions=conditions,
            confounders=confounders,
        )
    if decision is not None:
        hq.learning.decide(
            episode.id,
            decision=decision,
            rationale=f"Decision for {subject}",
            confidence=confidence,
        )
    hq.sessions.close_session(
        session.id,
        debrief_summary=f"Completed bounded {subject} experiment",
        next_action="Review the represented Learning result",
    )
    return episode


class SongSuccessPatternsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_projection_preserves_all_humility_states_and_strips_internal_identity(self) -> None:
        hq = HeadquartersMemory.create(self.root / "data", "Pattern Artist")
        try:
            song = hq.store.create_song("Pattern Song")
            pending = add_episode(
                hq,
                song.id,
                subject="Pending",
                change="Try pending move",
                decision=None,
                observation="Early observation",
            )
            single = add_episode(
                hq,
                song.id,
                subject="Single",
                change="Try single move",
                decision="KEEP",
                observation="Single retained result",
                source_kind="MEASURED",
                conditions=("matched level",),
                confounders=("different listening position",),
            )
            add_episode(hq, song.id, subject="Success", change="Try repeated move", decision="KEEP")
            add_episode(hq, song.id, subject="Success", change="Try repeated move", decision="KEEP")
            add_episode(hq, song.id, subject="Mixed", change="Try mixed move", decision="KEEP")
            add_episode(hq, song.id, subject="Mixed", change="Try mixed move", decision="REVERT")
            add_episode(hq, song.id, subject="No keep", change="Try weak move", decision="REVISE")
            add_episode(hq, song.id, subject="Unclear", change="Try unclear move", decision="INCONCLUSIVE")

            views = SongSuccessPatterns(hq.store, hq.success).for_song(song.id)
            by_subject = {item.subject: item for item in views}
            self.assertEqual(by_subject["Pending"].humility_state, "NO_COMPLETED_EVIDENCE")
            self.assertEqual(by_subject["Single"].humility_state, "SINGLE_OBSERVATION")
            self.assertEqual(by_subject["Success"].humility_state, "SUCCESS_ONLY")
            self.assertEqual(by_subject["Mixed"].humility_state, "MIXED")
            self.assertEqual(by_subject["No keep"].humility_state, "NO_KEEP_EVIDENCE")
            self.assertEqual(by_subject["Unclear"].humility_state, "INCONCLUSIVE_ONLY")

            single_view = by_subject["Single"]
            self.assertEqual(single_view.causal_status, "ASSOCIATION_ONLY")
            self.assertEqual(single_view.completed_count, 1)
            self.assertEqual(single_view.pending_count, 0)
            self.assertEqual(single_view.keep_count, 1)
            self.assertEqual(single_view.observations[0].source_labels, ("measured",))
            self.assertEqual(single_view.observations[0].confidence_mean, 0.8)
            self.assertEqual(single_view.conditions[0].term, "matched level")
            self.assertEqual(
                single_view.alternative_explanations[0].term,
                "different listening position",
            )
            self.assertIn("not proof", single_view.warning.lower())

            serialized = repr(views)
            self.assertNotIn(pending.id, serialized)
            self.assertNotIn(single.id, serialized)
            self.assertNotIn("success_", serialized)
            self.assertNotIn("test:Single:", serialized)
        finally:
            hq.close()

    def test_projection_is_song_scoped_and_pure_read(self) -> None:
        hq = HeadquartersMemory.create(self.root / "data", "Pattern Artist")
        try:
            song_a = hq.store.create_song("A")
            song_b = hq.store.create_song("B")
            add_episode(hq, song_a.id, subject="A-only", change="Move A", decision="KEEP")
            add_episode(hq, song_b.id, subject="B-only", change="Move B", decision="REVERT")

            before_a = hq.learning.episodes_for_song(song_a.id)
            before_b = hq.learning.episodes_for_song(song_b.id)
            a_views = SongSuccessPatterns(hq.store, hq.success).for_song(song_a.id)
            b_views = SongSuccessPatterns(hq.store, hq.success).for_song(song_b.id)
            self.assertEqual([item.subject for item in a_views], ["A-only"])
            self.assertEqual([item.subject for item in b_views], ["B-only"])
            self.assertEqual(hq.learning.episodes_for_song(song_a.id), before_a)
            self.assertEqual(hq.learning.episodes_for_song(song_b.id), before_b)
        finally:
            hq.close()

    def test_projection_fails_closed_if_causal_or_source_semantics_drift(self) -> None:
        hq = HeadquartersMemory.create(self.root / "data", "Pattern Artist")
        try:
            song = hq.store.create_song("Drift Song")
            add_episode(
                hq,
                song.id,
                subject="Drift",
                change="Try drift move",
                decision="KEEP",
                source_kind="MEASURED",
            )
            raw = hq.success.patterns_for_song(song.id)[0]

            with self.assertRaisesRegex(RuntimeError, "causal semantics changed"):
                SongSuccessPatterns._view(replace(raw, causal_status="CAUSAL"))

            changed_consequence = replace(
                raw.consequences[0],
                source_kinds=("FUTURE_SOURCE",),
            )
            with self.assertRaisesRegex(RuntimeError, "source semantics changed"):
                SongSuccessPatterns._view(
                    replace(raw, consequences=(changed_consequence,))
                )
        finally:
            hq.close()


if __name__ == "__main__":
    unittest.main()
