from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory, ValidationError
from n0te.skill_model import (
    SkillModelBinding,
    SkillModelService,
    StaleSkillModelError,
)


class SongSkillModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(self.hq.close)
        self.service = SkillModelService(self.hq.skills)

    def tearDown(self):
        self.tmp.cleanup()

    def test_artist_declaration_is_append_only_visible_and_activity_receipted(self):
        before = self.hq.activity.checkpoint()
        assessment = self.service.declare(
            skill_id="Compression",
            level="PRACTICED",
            assistance="SOME",
        )
        self.assertEqual(assessment.source_kind, "ARTIST_DECLARED")
        self.assertEqual(assessment.assistance_level, 0.5)
        views = self.service.views()
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].skill_id, "Compression")
        self.assertEqual(views[0].level, "PRACTICED")
        self.assertEqual(views[0].source_label, "You told N0TE")
        self.assertEqual(views[0].assistance_label, "Some assistance")
        events = self.hq.activity.for_profile(after_sequence=before)
        self.assertEqual([event.event_type for event in events], ["SKILL_ASSESSED"])
        self.assertIsNone(events[0].song_id)

    def test_correction_appends_history_and_can_restore_unknown(self):
        first = self.service.declare(
            skill_id="EQ",
            level="APPLIED",
            assistance="SOME",
        )
        binding = self.service.binding_for("EQ")
        corrected = self.service.correct(
            binding,
            level="UNKNOWN",
            assistance="HIGH",
            reason="I overstated this and want N0TE to treat it as unknown.",
        )
        history = self.hq.skills.history("EQ")
        self.assertEqual([item.id for item in history], [first.id, corrected.id])
        self.assertEqual([item.level for item in history], ["APPLIED", "UNKNOWN"])
        self.assertEqual(corrected.source_kind, "ARTIST_CORRECTION")
        self.assertIn("overstated", corrected.note or "")
        view = self.service.views()[0]
        self.assertEqual(view.level, "UNKNOWN")
        self.assertEqual(view.source_label, "You corrected N0TE")
        self.assertIn("overstated", view.correction_note or "")

    def test_stale_binding_cannot_overwrite_newer_assessment(self):
        self.service.declare(skill_id="Arrangement", level="INTRODUCED", assistance="HIGH")
        stale = self.service.binding_for("Arrangement")
        self.service.correct(
            stale,
            level="PRACTICED",
            assistance="SOME",
            reason="I have practiced this in real projects.",
        )
        with self.assertRaises(StaleSkillModelError):
            self.service.correct(
                stale,
                level="APPLIED",
                assistance="NONE",
                reason="Old page should not win.",
            )
        self.assertEqual(self.hq.skills.state("Arrangement").level, "PRACTICED")
        self.assertEqual(len(self.hq.skills.history("Arrangement")), 2)

    def test_duplicate_declaration_fails_closed(self):
        self.service.declare(skill_id="Melody", level="INTRODUCED", assistance="HIGH")
        with self.assertRaises(StaleSkillModelError):
            self.service.declare(skill_id="Melody", level="APPLIED", assistance="NONE")
        self.assertEqual(len(self.hq.skills.history("Melody")), 1)

    def test_independent_requires_zero_assistance_and_unknown_is_correction_only(self):
        with self.assertRaises(ValidationError):
            self.service.declare(skill_id="Mixing", level="INDEPENDENT", assistance="SOME")
        with self.assertRaises(ValidationError):
            self.service.declare(skill_id="Mixing", level="UNKNOWN", assistance="HIGH")
        independent = self.service.declare(
            skill_id="Mixing",
            level="INDEPENDENT",
            assistance="NONE",
        )
        self.assertEqual(independent.assistance_level, 0.0)

    def test_real_n0te_assessment_truth_is_preserved_in_view(self):
        song = self.hq.store.create_song("Song")
        session = self.hq.sessions.start_session(song_id=song.id, objective="Practice gain staging")
        self.hq.sessions.close_session(
            session.id,
            debrief_summary="Completed gain staging pass",
            next_action="Repeat without prompts",
        )
        evidence = self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="skill.gain_staging",
            value="completed",
            source_kind="OBSERVED",
        )
        self.hq.skills.record_assessment(
            skill_id="Gain staging",
            level="PRACTICED",
            source_kind="N0TE_ASSESSED",
            source_ref="test:closed-session",
            confidence=0.8,
            assistance_level=0.5,
            session_id=session.id,
            evidence_claim_ids=(evidence.id,),
        )
        view = self.service.views()[0]
        self.assertEqual(view.source_label, "N0TE assessment")
        self.assertEqual(view.evidence_count, 1)
        self.assertEqual(view.assistance_label, "Some assistance")

    def test_restart_preserves_current_state_and_history(self):
        profile_id = self.hq.store.profile_id
        self.service.declare(skill_id="Bass writing", level="PRACTICED", assistance="SOME")
        binding = self.service.binding_for("Bass writing")
        self.service.correct(
            binding,
            level="APPLIED",
            assistance="NONE",
            reason="I can apply it deliberately in a finished arrangement.",
        )
        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, profile_id)
        self.addCleanup(self.hq.close)
        self.service = SkillModelService(self.hq.skills)
        self.assertEqual(self.service.views()[0].level, "APPLIED")
        self.assertEqual(len(self.hq.skills.history("Bass writing")), 2)
