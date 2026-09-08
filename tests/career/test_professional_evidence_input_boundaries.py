import tempfile
import unittest
from pathlib import Path

from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory
from n0te.professional_evidence import (
    ProfessionalEvidenceIntegrityError,
    ProfessionalEvidenceService,
)


class ProfessionalEvidenceInputBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Boundary Artist")
        self.addCleanup(self.hq.close)
        self.service = ProfessionalEvidenceService(self.hq.store, self.hq.evidence)
        self.song = self.hq.store.create_song("Boundary Work")
        self.version = self.hq.store.create_version(self.song.id, label="proof")

    def _record(self, **overrides):
        values = {
            "roles": ("PRODUCER",),
            "kind": "CREDIT",
            "title": "Verified producer credit",
            "statement": "Observed professional credit.",
            "evidence_source_kind": "OBSERVED",
            "evidence_source_ref": "activity:credit:boundary",
            "share_scope": "OPPORTUNITY",
            "song_id": self.song.id,
            "version_id": self.version.id,
            "role_evidence_kind": "CREDIT",
        }
        values.update(overrides)
        return self.service.record(**values)

    def test_roles_never_coerce_non_text_values(self):
        for value in (None, True, 7, object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaisesRegex(ValidationError, "role must be text"):
                    self._record(roles=(value,))

    def test_required_enum_fields_never_coerce_non_text_values(self):
        for field in ("kind", "evidence_source_kind", "share_scope"):
            for value in (None, True, 7):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValidationError, "must be text"):
                        self._record(**{field: value})

    def test_optional_role_evidence_kind_preserves_absence_but_rejects_wrong_types(self):
        without_mapping = self._record(role_evidence_kind=None)
        self.assertIsNone(without_mapping.role_evidence_kind)
        for value in (True, 7, object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaisesRegex(ValidationError, "must be text"):
                    self._record(role_evidence_kind=value)

    def test_work_ids_never_coerce_non_text_values(self):
        for field in ("song_id", "version_id"):
            for value in (True, 7, object()):
                with self.subTest(field=field, value=type(value).__name__):
                    with self.assertRaisesRegex(ValidationError, "must be text"):
                        self._record(**{field: value})

    def test_public_evidence_id_lookups_reject_non_text(self):
        for value in (None, True, 7, object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaisesRegex(ValidationError, "id must be text"):
                    self.service.current(value)

    def test_correction_replacements_do_not_gain_meaning_through_str(self):
        record = self._record()
        replacements = (
            ("kind", True),
            ("title", None),
            ("statement", 7),
            ("evidence_source_kind", True),
            ("evidence_source_ref", None),
            ("share_scope", 7),
            ("song_id", True),
            ("version_id", object()),
            ("role_evidence_kind", 7),
        )
        for field, value in replacements:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValidationError, "must be text"):
                    self.service.correct(
                        record.evidence_id,
                        reason="Boundary validation",
                        revision_source_kind="OBSERVED",
                        revision_source_ref="activity:correction:boundary",
                        **{field: value},
                    )
        self.assertEqual(
            [item.revision_kind for item in self.service.history(record.evidence_id)],
            ["CREATE"],
        )

    def test_revision_reason_and_revision_source_are_type_strict(self):
        record = self._record()
        with self.assertRaisesRegex(ValidationError, "revision reason must be text"):
            self.service.dispute(
                record.evidence_id,
                reason=True,
                revision_source_kind="USER_DECLARED",
                revision_source_ref="artist:dispute:boundary",
            )
        with self.assertRaisesRegex(ValidationError, "source must be text"):
            self.service.dispute(
                record.evidence_id,
                reason="Boundary dispute",
                revision_source_kind=True,
                revision_source_ref="artist:dispute:boundary",
            )
        self.assertEqual(self.service.current(record.evidence_id).state, "ACTIVE")

    def test_portable_filters_are_type_strict(self):
        self._record()
        with self.assertRaisesRegex(ValidationError, "role must be text"):
            self.service.portable_for_role(True)
        with self.assertRaisesRegex(ValidationError, "audience must be text"):
            self.service.portable_for_role("PRODUCER", audience=True)
        with self.assertRaisesRegex(ValidationError, "verified_only must be boolean"):
            self.service.portable_for_role("PRODUCER", verified_only=1)

    def test_non_text_owned_payload_fails_as_integrity_error(self):
        malformed_id = "pe_" + "b" * 32
        payload = {
            "schema_version": 1,
            "evidence_id": malformed_id,
            "roles": [True],
            "kind": "CREDIT",
            "title": "Malformed",
            "statement": "Owned data must fail closed.",
            "evidence_source_kind": "OBSERVED",
            "evidence_source_ref": "activity:malformed:boundary",
            "share_scope": "PRIVATE",
            "permission_source_kind": None,
            "permission_source_ref": None,
            "confidential": False,
            "state": "ACTIVE",
            "song_id": None,
            "version_id": None,
            "role_evidence_kind": None,
            "revision_kind": "CREATE",
            "revision_reason": None,
        }
        self.hq.evidence.record_claim(
            scope_kind="PROFILE",
            scope_id=self.hq.store.profile_id,
            key=f"professional.evidence.{malformed_id}",
            value=payload,
            source_kind="OBSERVED",
            source_ref="activity:malformed:boundary",
        )
        with self.assertRaises(ProfessionalEvidenceIntegrityError):
            self.service.current(malformed_id)


if __name__ == "__main__":
    unittest.main()
