from __future__ import annotations

import tempfile
import unittest

from n0te.credits import CreditsMemory
from n0te.lineage import LineageCorruptionError, ValidationError
from n0te.memory import HeadquartersMemory
from n0te.rights_evidence_chain import (
    RIGHTS_EVIDENCE_SCHEMA_VERSION,
    RightsEvidenceChainService,
    RightsEvidenceItem,
)


class RightsEvidenceChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hq = HeadquartersMemory.create(self.tmp.name, "Rights Evidence Artist")
        self.song = self.hq.store.create_song("Evidence Song")
        self.person = self.hq.people.create_person("Casey Writer")
        self.credits = CreditsMemory(self.hq.store, self.hq.people)
        self.credit = self.credits.record_credit(
            self.song.id,
            self.person.id,
            "Songwriter",
        )
        self.service = RightsEvidenceChainService(
            self.hq.store,
            self.credits,
            self.hq.evidence,
        )

    def tearDown(self) -> None:
        self.hq.close()

    def test_credit_starts_as_user_declaration_only(self) -> None:
        snapshot = self.service.snapshot("CREDIT", self.credit.id)
        self.assertEqual(snapshot.highest_contiguous_supported_stage, "USER_DECLARATION")
        self.assertEqual(snapshot.stage("USER_DECLARATION").status, "SUPPORTED")
        self.assertEqual(snapshot.stage("COMMUNICATION_CONFIRMATION").status, "UNKNOWN")
        self.assertEqual(snapshot.stage("SIGNED_DOCUMENT").status, "UNKNOWN")
        self.assertEqual(snapshot.stage("PROVIDER_RECEIPT").status, "UNKNOWN")
        self.assertFalse(snapshot.legal_conclusion)
        self.assertFalse(snapshot.ownership_verified)
        self.assertFalse(snapshot.registration_verified)
        self.assertFalse(snapshot.royalty_entitlement_verified)
        self.assertFalse(snapshot.payment_verified)
        self.assertFalse(snapshot.action_authority_granted)

    def test_artist_recorded_split_confirmation_is_not_communication_evidence(self) -> None:
        sheet = self.credits.create_split_draft(self.song.id)
        self.credits.set_draft_allocations(sheet.id, {self.person.id: 10000})
        self.credits.submit_split(sheet.id)
        self.credits.record_confirmation(
            sheet.id,
            self.person.id,
            status="RECORDED_CONFIRMED",
            note="Artist says Casey confirmed in a chat",
        )

        snapshot = self.service.snapshot("COMPOSITION_SPLIT", sheet.id)
        self.assertEqual(snapshot.stage("USER_DECLARATION").status, "SUPPORTED")
        self.assertEqual(snapshot.stage("COMMUNICATION_CONFIRMATION").status, "UNKNOWN")
        self.assertEqual(snapshot.highest_contiguous_supported_stage, "USER_DECLARATION")

    def test_manual_reference_is_visible_but_cannot_impersonate_observation(self) -> None:
        claim = self.service.record_user_declared_reference(
            "CREDIT",
            self.credit.id,
            stage="COMMUNICATION_CONFIRMATION",
            assertion="SUPPORTS",
            source_ref="email-thread:artist-entered",
            note="Artist says Casey acknowledged the credit",
        )
        self.assertEqual(claim.source_kind, "USER_DECLARED")

        snapshot = self.service.snapshot("CREDIT", self.credit.id)
        communication = snapshot.stage("COMMUNICATION_CONFIRMATION")
        self.assertEqual(communication.status, "UNVERIFIED")
        self.assertEqual(len(communication.items), 1)
        self.assertEqual(communication.items[0].source_kind, "USER_DECLARED")
        self.assertEqual(
            snapshot.highest_contiguous_supported_stage,
            "USER_DECLARATION",
        )

        self.service.record_observed(
            "CREDIT",
            self.credit.id,
            stage="COMMUNICATION_CONFIRMATION",
            assertion="SUPPORTS",
            source_ref="observer:email-thread:artist-entered",
            note="Trusted observer inspected the referenced communication",
        )
        snapshot = self.service.snapshot("CREDIT", self.credit.id)
        self.assertEqual(snapshot.stage("COMMUNICATION_CONFIRMATION").status, "SUPPORTED")
        self.assertEqual(
            snapshot.highest_contiguous_supported_stage,
            "COMMUNICATION_CONFIRMATION",
        )

    def test_manual_conflict_stays_visible_even_with_observed_support(self) -> None:
        self.service.record_user_declared_reference(
            "CREDIT",
            self.credit.id,
            stage="COMMUNICATION_CONFIRMATION",
            assertion="CONTRADICTS",
            source_ref="artist-note:dispute",
        )
        self.service.record_observed(
            "CREDIT",
            self.credit.id,
            stage="COMMUNICATION_CONFIRMATION",
            assertion="SUPPORTS",
            source_ref="observer:email-thread:support",
        )
        snapshot = self.service.snapshot("CREDIT", self.credit.id)
        stage = snapshot.stage("COMMUNICATION_CONFIRMATION")
        self.assertEqual(stage.status, "CONFLICT")
        self.assertEqual(
            {item.source_kind for item in stage.items},
            {"USER_DECLARED", "OBSERVED"},
        )
        self.assertEqual(snapshot.highest_contiguous_supported_stage, "USER_DECLARATION")

    def test_observed_communication_and_signed_document_advance_only_their_stages(self) -> None:
        self.service.record_observed(
            "CREDIT",
            self.credit.id,
            stage="COMMUNICATION_CONFIRMATION",
            assertion="SUPPORTS",
            source_ref="email-thread:casey-2026-09-04",
            note="Casey acknowledged the songwriting credit",
        )
        self.service.record_observed(
            "CREDIT",
            self.credit.id,
            stage="SIGNED_DOCUMENT",
            assertion="SUPPORTS",
            source_ref="sha256:0123456789abcdef",
            note="Reference to a signed document held outside N0TE",
        )
        snapshot = self.service.snapshot("CREDIT", self.credit.id)
        self.assertEqual(snapshot.stage("COMMUNICATION_CONFIRMATION").status, "SUPPORTED")
        self.assertEqual(snapshot.stage("SIGNED_DOCUMENT").status, "SUPPORTED")
        self.assertEqual(snapshot.stage("PROVIDER_RECEIPT").status, "UNKNOWN")
        self.assertEqual(snapshot.highest_contiguous_supported_stage, "SIGNED_DOCUMENT")
        self.assertFalse(snapshot.ownership_verified)
        self.assertFalse(snapshot.legal_conclusion)

    def test_contradictory_evidence_remains_visible_and_breaks_contiguous_chain(self) -> None:
        for assertion, source in (
            ("SUPPORTS", "email-thread:support"),
            ("CONTRADICTS", "email-thread:dispute"),
        ):
            self.service.record_observed(
                "CREDIT",
                self.credit.id,
                stage="COMMUNICATION_CONFIRMATION",
                assertion=assertion,
                source_ref=source,
            )
        snapshot = self.service.snapshot("CREDIT", self.credit.id)
        stage = snapshot.stage("COMMUNICATION_CONFIRMATION")
        self.assertEqual(stage.status, "CONFLICT")
        self.assertEqual({item.assertion for item in stage.items}, {"SUPPORTS", "CONTRADICTS"})
        self.assertEqual(snapshot.highest_contiguous_supported_stage, "USER_DECLARATION")

    def test_provider_receipt_cannot_be_self_issued_but_verified_evidence_is_readable(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.record_observed(
                "CREDIT",
                self.credit.id,
                stage="PROVIDER_RECEIPT",
                assertion="SUPPORTS",
                source_ref="provider:fake-observed",
            )
        with self.assertRaises(ValidationError):
            self.service.record_user_declared_reference(
                "CREDIT",
                self.credit.id,
                stage="PROVIDER_RECEIPT",
                assertion="SUPPORTS",
                source_ref="provider:fake-manual",
            )

        key = self.service.evidence_key("CREDIT", self.credit.id, "PROVIDER_RECEIPT")
        self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=self.song.id,
            key=key,
            value={
                "schema_version": RIGHTS_EVIDENCE_SCHEMA_VERSION,
                "target_kind": "CREDIT",
                "target_id": self.credit.id,
                "stage": "PROVIDER_RECEIPT",
                "assertion": "SUPPORTS",
                "note": "Provider acknowledgment imported by a trusted verifier",
            },
            source_kind="PROVIDER_VERIFIED",
            source_ref="provider-receipt:abc123",
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )
        snapshot = self.service.snapshot("CREDIT", self.credit.id)
        receipt = snapshot.stage("PROVIDER_RECEIPT")
        self.assertEqual(receipt.status, "SUPPORTED")
        self.assertTrue(receipt.items[0].provider_verified)
        self.assertEqual(snapshot.highest_contiguous_supported_stage, "USER_DECLARATION")
        self.assertFalse(snapshot.legal_conclusion)

    def test_reserved_rights_claim_with_wrong_truth_shape_fails_closed(self) -> None:
        key = self.service.evidence_key("CREDIT", self.credit.id, "SIGNED_DOCUMENT")
        self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=self.song.id,
            key=key,
            value={
                "schema_version": RIGHTS_EVIDENCE_SCHEMA_VERSION,
                "target_kind": "COMPOSITION_SPLIT",
                "target_id": self.credit.id,
                "stage": "SIGNED_DOCUMENT",
                "assertion": "SUPPORTS",
                "note": None,
            },
            source_kind="OBSERVED",
            source_ref="document:bad-binding",
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )
        with self.assertRaises(LineageCorruptionError):
            self.service.snapshot("CREDIT", self.credit.id)

    def test_cross_song_target_and_malformed_inputs_fail_closed(self) -> None:
        other = self.hq.store.create_song("Other Song")
        with self.assertRaises(ValidationError):
            self.service.snapshot(
                "CREDIT",
                self.credit.id,
                expected_song_id=other.id,
            )
        for value in (None, True, 7):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.service.record_observed(
                        "CREDIT",
                        self.credit.id,
                        stage="COMMUNICATION_CONFIRMATION",
                        assertion="SUPPORTS",
                        source_ref=value,  # type: ignore[arg-type]
                    )

    def test_authority_fields_are_not_constructor_forgeable(self) -> None:
        with self.assertRaises(TypeError):
            RightsEvidenceItem(
                claim_id=None,
                sequence=1,
                stage="USER_DECLARATION",
                assertion="SUPPORTS",
                source_kind="USER_DECLARED",
                source_ref=None,
                note=None,
                ownership_verified=True,  # type: ignore[call-arg]
            )


if __name__ == "__main__":
    unittest.main()
