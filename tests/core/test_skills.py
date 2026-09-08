import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory, NotFoundError, ValidationError


class Core02BReviewableSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_scratch_does_not_auto_create_skill_state(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(
            song_id=song.id,
            objective="Practice gain staging while making the verse",
        )
        hq.sessions.append_scratch(
            session.id,
            kind="OBSERVATION",
            body="I think I understand headroom better now",
        )
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="learning.note",
            value="gain staging felt clearer",
            source_kind="USER_DECLARED",
        )
        self.assertEqual(hq.skills.state("skill:gain-staging").level, "UNKNOWN")
        self.assertEqual(hq.skills.history("skill:gain-staging"), ())

    def test_introduced_is_explicit_and_reviewable(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        assessment = hq.skills.record_assessment(
            skill_id="skill:gain-staging",
            level="INTRODUCED",
            source_kind="N0TE_ASSESSED",
            source_ref="lesson:gain-staging:intro",
            confidence=0.8,
            assistance_level=1.0,
        )
        state = hq.skills.state("skill:gain-staging")
        self.assertEqual(state.level, "INTRODUCED")
        self.assertEqual(state.latest_assessment, assessment)
        self.assertEqual(
            hq.skills.history("skill:gain-staging"),
            (assessment,),
        )

    def test_nonartist_real_work_level_requires_closed_session(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Practice")
        with self.assertRaises(ValidationError):
            hq.skills.record_assessment(
                skill_id="skill:gain-staging",
                level="PRACTICED",
                source_kind="OBSERVED",
                source_ref="assessment:open-session",
                session_id=session.id,
                assistance_level=0.5,
            )

    def test_closed_session_can_ground_practiced_and_applied_evidence(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        version = hq.store.create_version(song.id, label="v1")
        session = hq.sessions.start_session(
            song_id=song.id,
            version_id=version.id,
            objective="Apply gain staging to the verse",
        )
        item = hq.sessions.append_scratch(
            session.id,
            kind="DECISION",
            body="Leave more headroom before the mix bus",
        )
        claim = hq.sessions.promote_item(
            item.id,
            scope_kind="VERSION",
            key="gain.headroom.decision",
            twin_domain="TECHNICAL",
        )
        hq.sessions.close_session(
            session.id,
            debrief_summary="Applied the headroom decision across the verse",
            next_action="Repeat it without prompts on the next pass",
        )
        practiced = hq.skills.record_assessment(
            skill_id="skill:gain-staging",
            level="PRACTICED",
            source_kind="OBSERVED",
            source_ref="assessment:practice-1",
            confidence=0.75,
            assistance_level=0.5,
            session_id=session.id,
            evidence_claim_ids=(claim.id,),
        )
        applied = hq.skills.record_assessment(
            skill_id="skill:gain-staging",
            level="APPLIED",
            source_kind="N0TE_ASSESSED",
            source_ref="assessment:application-1",
            confidence=0.8,
            assistance_level=0.2,
            session_id=session.id,
            evidence_claim_ids=(claim.id,),
        )
        self.assertEqual(practiced.song_id, song.id)
        self.assertEqual(applied.evidence_claim_ids, (claim.id,))
        self.assertEqual(hq.skills.state("skill:gain-staging").level, "APPLIED")

    def test_cross_song_evidence_cannot_ground_session_assessment(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song_a = hq.store.create_song("A")
        session = hq.sessions.start_session(song_id=song_a.id, objective="Practice A")
        hq.sessions.close_session(
            session.id,
            debrief_summary="A done",
            next_action="Try again",
        )
        song_b = hq.store.create_song("B")
        claim_b = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_b.id,
            key="skill.evidence",
            value="belongs to B",
            source_kind="OBSERVED",
        )
        with self.assertRaises(ValidationError):
            hq.skills.record_assessment(
                skill_id="skill:gain-staging",
                level="APPLIED",
                source_kind="OBSERVED",
                source_ref="assessment:wrong-song",
                assistance_level=0.2,
                session_id=session.id,
                evidence_claim_ids=(claim_b.id,),
            )

    def test_independent_requires_explicit_zero_assistance(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Independent pass")
        hq.sessions.close_session(
            session.id,
            debrief_summary="Completed the pass",
            next_action="Verify on another Song",
        )
        with self.assertRaises(ValidationError):
            hq.skills.record_assessment(
                skill_id="skill:gain-staging",
                level="INDEPENDENT",
                source_kind="OBSERVED",
                source_ref="assessment:still-assisted",
                assistance_level=0.1,
                session_id=session.id,
            )
        independent = hq.skills.record_assessment(
            skill_id="skill:gain-staging",
            level="INDEPENDENT",
            source_kind="OBSERVED",
            source_ref="assessment:zero-assistance",
            assistance_level=0.0,
            session_id=session.id,
        )
        self.assertEqual(independent.level, "INDEPENDENT")
        self.assertEqual(hq.skills.state("skill:gain-staging").level, "INDEPENDENT")

    def test_artist_correction_can_regress_or_reset_without_deleting_history(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Independent pass")
        hq.sessions.close_session(
            session.id,
            debrief_summary="Completed",
            next_action="Review skill model",
        )
        hq.skills.record_assessment(
            skill_id="skill:gain-staging",
            level="INDEPENDENT",
            source_kind="OBSERVED",
            source_ref="assessment:observed-independent",
            assistance_level=0.0,
            session_id=session.id,
        )
        hq.skills.correct_skill(
            skill_id="skill:gain-staging",
            level="PRACTICED",
            source_ref="artist:correction:1",
            reason="I still need help deciding targets",
            assistance_level=0.5,
        )
        hq.skills.correct_skill(
            skill_id="skill:gain-staging",
            level="UNKNOWN",
            source_ref="artist:correction:2",
            reason="That assessment was tracking the wrong skill",
            assistance_level=1.0,
        )
        history = hq.skills.history("skill:gain-staging")
        self.assertEqual(
            [item.level for item in history],
            ["INDEPENDENT", "PRACTICED", "UNKNOWN"],
        )
        self.assertEqual(hq.skills.state("skill:gain-staging").level, "UNKNOWN")

    def test_foreign_profile_session_is_rejected(self):
        a = HeadquartersMemory.create(self.root, "Artist A")
        b = HeadquartersMemory.create(self.root, "Artist B")
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        song_b = b.store.create_song("B")
        session_b = b.sessions.start_session(song_id=song_b.id, objective="B work")
        b.sessions.close_session(
            session_b.id,
            debrief_summary="B complete",
            next_action="Continue B",
        )
        with self.assertRaises(NotFoundError):
            a.skills.record_assessment(
                skill_id="skill:gain-staging",
                level="PRACTICED",
                source_kind="OBSERVED",
                source_ref="assessment:foreign-session",
                assistance_level=0.5,
                session_id=session_b.id,
            )

    def test_restart_preserves_order_and_current_state(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        hq.skills.record_assessment(
            skill_id="skill:inversions",
            level="INTRODUCED",
            source_kind="ARTIST_DECLARED",
            source_ref="artist:learned-basics",
            assistance_level=1.0,
        )
        hq.skills.correct_skill(
            skill_id="skill:inversions",
            level="PRACTICED",
            source_ref="artist:practice-update",
            reason="I can use first inversions with some prompting",
            assistance_level=0.4,
        )
        hq.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        self.assertEqual(hq.skills.state("skill:inversions").level, "PRACTICED")
        self.assertEqual(
            [item.level for item in hq.skills.history("skill:inversions")],
            ["INTRODUCED", "PRACTICED"],
        )

    def test_skill_history_is_immutable_and_reads_are_pure(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        assessment = hq.skills.record_assessment(
            skill_id="skill:inversions",
            level="INTRODUCED",
            source_kind="ARTIST_DECLARED",
            source_ref="artist:intro",
            assistance_level=1.0,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            hq.store._conn.execute(
                "DELETE FROM skill_assessments WHERE id=?",
                (assessment.id,),
            )
        before = hq.store._conn.total_changes
        state = hq.skills.state("skill:inversions")
        history = hq.skills.history("skill:inversions")
        self.assertEqual(state.latest_assessment, history[-1])
        self.assertEqual(hq.store._conn.total_changes, before)

    def test_skill_assessment_journals_activity_without_mutating_taste_evidence(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Practice")
        hq.sessions.close_session(
            session.id,
            debrief_summary="Practice complete",
            next_action="Repeat",
        )
        evidence_before = int(
            hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0]
        )
        assessment = hq.skills.record_assessment(
            skill_id="skill:gain-staging",
            level="PRACTICED",
            source_kind="OBSERVED",
            source_ref="assessment:activity",
            assistance_level=0.5,
            session_id=session.id,
        )
        self.assertEqual(
            int(hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0]),
            evidence_before,
        )
        events = [
            event
            for event in hq.activity.for_song(song.id)
            if event.event_type == "SKILL_ASSESSED"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].object_id, assessment.id)


if __name__ == "__main__":
    unittest.main()
