import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory


class Core02ESuccessMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.song = self.hq.store.create_song("Song A")
        self.version = self.hq.store.create_version(self.song.id, label="v1")

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _episode(
        self,
        *,
        song=None,
        version=None,
        decision="KEEP",
        domain="MIX",
        subject_ref="vocal",
        change_description="Cut 300 Hz",
        observations=(("The mix feels clearer", "OBSERVED", "listen:1", 0.8),),
        conditions=("same monitor level",),
        confounders=(),
        decision_confidence=0.75,
    ):
        song = self.song if song is None else song
        version = self.version if version is None else version
        session = self.hq.sessions.start_session(
            song_id=song.id,
            version_id=version.id,
            objective="Evaluate one bounded change",
        )
        episode = self.hq.learning.create_episode(
            session_id=session.id,
            domain=domain,
            subject_ref=subject_ref,
            change_description=change_description,
        )
        for observation, source_kind, source_ref, confidence in observations:
            self.hq.learning.append_consequence(
                episode.id,
                observation=observation,
                source_kind=source_kind,
                source_ref=source_ref,
                confidence=confidence,
                conditions=conditions,
                confounders=confounders,
            )
        if decision is not None:
            self.hq.learning.decide(
                episode.id,
                decision=decision,
                rationale=f"Decision for {episode.id}",
                confidence=decision_confidence,
            )
        self.hq.sessions.close_session(
            session.id,
            debrief_summary="Completed one bounded Learning experiment",
            next_action="Review the represented result before another Session",
        )
        return episode

    def test_single_keep_is_thin_association_not_causal_rule(self):
        episode = self._episode(decision="KEEP")
        (pattern,) = self.hq.success.patterns_for_song(self.song.id)
        self.assertEqual(pattern.humility_state, "SINGLE_OBSERVATION")
        self.assertEqual(pattern.causal_status, "ASSOCIATION_ONLY")
        self.assertEqual(pattern.supporting_episode_ids, (episode.id,))
        self.assertEqual(pattern.sample_size, 1)
        self.assertIn("not proof of causation", pattern.warning)

    def test_repeated_keep_only_is_success_only_with_survivorship_warning(self):
        first = self._episode(decision="KEEP")
        second = self._episode(
            decision="KEEP",
            observations=(("The mix feels clearer", "OBSERVED", "listen:2", 0.6),),
        )
        (pattern,) = self.hq.success.patterns_for_song(self.song.id)
        self.assertEqual(pattern.humility_state, "SUCCESS_ONLY")
        self.assertEqual(pattern.supporting_episode_ids, (first.id, second.id))
        self.assertFalse(pattern.has_counterexamples)
        self.assertIn("Absence of counterexamples", pattern.warning)
        self.assertEqual(pattern.consequence_confidence.minimum, 0.6)
        self.assertEqual(pattern.consequence_confidence.maximum, 0.8)
        self.assertAlmostEqual(pattern.consequence_confidence.mean, 0.7)

    def test_keep_with_revert_and_revise_is_mixed(self):
        keep = self._episode(decision="KEEP")
        revert = self._episode(
            decision="REVERT",
            observations=(("The vocal became too thin", "OBSERVED", "listen:2", 0.9),),
        )
        revise = self._episode(
            decision="REVISE",
            observations=(("The idea helped but needed a smaller cut", "USER_DECLARED", "artist:3", 0.7),),
        )
        (pattern,) = self.hq.success.patterns_for_song(self.song.id)
        self.assertEqual(pattern.humility_state, "MIXED")
        self.assertEqual(pattern.supporting_episode_ids, (keep.id,))
        self.assertEqual(pattern.counterexample_episode_ids, (revert.id, revise.id))
        self.assertEqual((pattern.keep_count, pattern.revert_count, pattern.revise_count), (1, 1, 1))
        self.assertTrue(pattern.has_counterexamples)

    def test_no_keep_and_inconclusive_only_remain_explicit(self):
        self._episode(decision="REVERT")
        self._episode(decision="REVISE")
        (counter,) = self.hq.success.patterns_for_song(self.song.id)
        self.assertEqual(counter.humility_state, "NO_KEEP_EVIDENCE")

        other = self.hq.store.create_song("Song B")
        other_version = self.hq.store.create_version(other.id, label="v1")
        self._episode(song=other, version=other_version, decision="INCONCLUSIVE")
        self._episode(song=other, version=other_version, decision="INCONCLUSIVE")
        (inconclusive,) = self.hq.success.patterns_for_song(other.id)
        self.assertEqual(inconclusive.humility_state, "INCONCLUSIVE_ONLY")

    def test_pending_episode_is_visible_but_not_sample_size(self):
        pending = self._episode(decision=None)
        (pattern,) = self.hq.success.patterns_for_song(self.song.id)
        self.assertEqual(pattern.humility_state, "NO_COMPLETED_EVIDENCE")
        self.assertEqual(pattern.sample_size, 0)
        self.assertEqual(pattern.pending_episode_ids, (pending.id,))
        self.assertEqual(pattern.completed_episode_ids, ())

    def test_conditions_confounders_count_distinct_episodes_and_keep_source_refs(self):
        episode = self._episode(
            decision="KEEP",
            observations=(
                ("The mix feels clearer", "OBSERVED", "listen:a", 0.8),
                ("Masking decreased", "MEASURED", "analysis:b", 0.7),
            ),
            conditions=("same monitor level", "same chorus"),
            confounders=("fresh ears",),
        )
        (pattern,) = self.hq.success.patterns_for_song(self.song.id)
        conditions = {item.term: item for item in pattern.conditions}
        self.assertEqual(conditions["same monitor level"].count, 1)
        self.assertEqual(conditions["same monitor level"].episode_ids, (episode.id,))
        alternatives = {item.term: item for item in pattern.alternative_explanations}
        self.assertEqual(alternatives["fresh ears"].count, 1)
        refs = {ref for summary in pattern.consequences for ref in summary.source_refs}
        self.assertEqual(refs, {"listen:a", "analysis:b"})

    def test_exact_grouping_prevents_unrelated_subject_or_change_merge(self):
        self._episode(decision="KEEP", subject_ref="vocal", change_description="Cut 300 Hz")
        self._episode(decision="KEEP", subject_ref="bass", change_description="Cut 300 Hz")
        self._episode(decision="KEEP", subject_ref="vocal", change_description="Boost 5 kHz")
        patterns = self.hq.success.patterns_for_song(self.song.id)
        self.assertEqual(len(patterns), 3)
        self.assertEqual(len({pattern.id for pattern in patterns}), 3)

    def test_song_scope_isolated_artist_scope_merges_exact_pattern_with_song_provenance(self):
        first = self._episode(decision="KEEP")
        other = self.hq.store.create_song("Song B")
        other_version = self.hq.store.create_version(other.id, label="v1")
        second = self._episode(song=other, version=other_version, decision="REVERT")

        (song_pattern,) = self.hq.success.patterns_for_song(self.song.id)
        self.assertEqual(song_pattern.song_ids, (self.song.id,))
        self.assertEqual(song_pattern.completed_episode_ids, (first.id,))

        (artist_pattern,) = self.hq.success.patterns_for_artist()
        self.assertEqual(artist_pattern.song_ids, tuple(sorted((self.song.id, other.id))))
        self.assertEqual(artist_pattern.humility_state, "MIXED")
        self.assertEqual(set(artist_pattern.completed_episode_ids), {first.id, second.id})

    def test_reads_are_write_free_and_restart_is_identical_without_success_tables(self):
        self._episode(decision="KEEP", confounders=("novelty",))
        self._episode(
            decision="KEEP",
            observations=(("The mix feels clearer", "OBSERVED", "listen:2", 0.65),),
        )
        profile = self.hq.store.profile_id

        before = self.hq.store._conn.total_changes
        song_before = self.hq.success.patterns_for_song(self.song.id)
        artist_before = self.hq.success.patterns_for_artist()
        self.assertEqual(self.hq.store._conn.total_changes, before)
        self.assertEqual(
            self.hq.store._conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'success%'"
            ).fetchone()[0],
            0,
        )

        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, profile)
        self.assertEqual(self.hq.success.patterns_for_song(self.song.id), song_before)
        self.assertEqual(self.hq.success.patterns_for_artist(), artist_before)


if __name__ == "__main__":
    unittest.main()
