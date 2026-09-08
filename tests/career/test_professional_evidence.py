import tempfile
import unittest
from pathlib import Path

from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory
from n0te.professional_evidence import (
    ProfessionalEvidenceIntegrityError,
    ProfessionalEvidenceService,
)


class ProfessionalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Working Artist")
        self.addCleanup(self.hq.close)
        self.service = ProfessionalEvidenceService(self.hq.store, self.hq.evidence)
        self.song = self.hq.store.create_song("Professional Work")
        self.version = self.hq.store.create_version(self.song.id, label="delivered mix")

    def test_verified_credit_is_source_bound_song_bound_and_role_reusable(self):
        record = self.service.record(
            roles=("producer", "mix engineer"),
            kind="credit",
            title="Producer and mix engineer credit",
            statement="Credited for production and mix delivery on the released work.",
            evidence_source_kind="PROVIDER_VERIFIED",
            evidence_source_ref="provider:credits:release-17",
            share_scope="PUBLIC",
            song_id=self.song.id,
            version_id=self.version.id,
            role_evidence_kind="CREDIT",
        )

        self.assertEqual(record.roles, ("MIX_ENGINEER", "PRODUCER"))
        self.assertTrue(record.verified)
        self.assertEqual(record.song_id, self.song.id)
        self.assertEqual(record.version_id, self.version.id)
        self.assertFalse(hasattr(record, "score"))
        portable = self.service.portable_for_role("producer", audience="PUBLIC")
        self.assertEqual([item.evidence_id for item in portable], [record.evidence_id])

        role_evidence = self.service.to_role_evidence(record.evidence_id)
        self.assertEqual(role_evidence.kind, "CREDIT")
        self.assertEqual(role_evidence.source_kind, "VERIFIED_EXTERNAL")
        self.assertTrue(role_evidence.verified)
        self.assertEqual(role_evidence.source_ref, "provider:credits:release-17")

    def test_artist_declared_credit_stays_declared_and_is_not_portable_by_default(self):
        record = self.service.record(
            roles=("SONGWRITER",),
            kind="CREDIT",
            title="Writer credit I entered",
            statement="I wrote the second verse.",
            evidence_source_kind="USER_DECLARED",
            evidence_source_ref="artist:manual-credit:1",
            share_scope="PUBLIC",
            role_evidence_kind="CREDIT",
        )

        self.assertFalse(record.verified)
        self.assertEqual(self.service.portable_for_role("SONGWRITER"), ())
        explicitly_unverified = self.service.portable_for_role(
            "SONGWRITER", verified_only=False
        )
        self.assertEqual(explicitly_unverified[0].evidence_id, record.evidence_id)
        mapped = self.service.to_role_evidence(record.evidence_id)
        self.assertEqual(mapped.source_kind, "ARTIST_DECLARED")
        self.assertFalse(mapped.verified)

    def test_client_material_testimonials_and_referrals_require_verified_permission(self):
        for kind in ("WORK_SAMPLE", "CASE_STUDY", "TESTIMONIAL", "REFERRAL"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValidationError, "permission evidence"):
                    self.service.record(
                        roles=("PRODUCER",),
                        kind=kind,
                        title=f"{kind} example",
                        statement="Client-facing evidence.",
                        evidence_source_kind="PROVIDER_VERIFIED",
                        evidence_source_ref=f"provider:{kind}:1",
                        share_scope="OPPORTUNITY",
                    )
                with self.assertRaisesRegex(
                    ValidationError, "observed or provider-verified permission"
                ):
                    self.service.record(
                        roles=("PRODUCER",),
                        kind=kind,
                        title=f"{kind} example",
                        statement="Client-facing evidence.",
                        evidence_source_kind="PROVIDER_VERIFIED",
                        evidence_source_ref=f"provider:{kind}:2",
                        share_scope="OPPORTUNITY",
                        permission_source_kind="USER_DECLARED",
                        permission_source_ref=f"artist:permission:{kind}",
                    )

        allowed = self.service.record(
            roles=("PRODUCER",),
            kind="TESTIMONIAL",
            title="Client testimonial",
            statement="The client approved this testimonial for portfolio use.",
            evidence_source_kind="OBSERVED",
            evidence_source_ref="message:testimonial:44",
            share_scope="PUBLIC",
            permission_source_kind="OBSERVED",
            permission_source_ref="message:consent:45",
        )
        self.assertTrue(allowed.permission_verified)
        self.assertEqual(
            self.service.portable_for_role("PRODUCER", audience="PUBLIC")[0].evidence_id,
            allowed.evidence_id,
        )

    def test_artist_cannot_publish_self_asserted_testimonial_or_referral(self):
        for kind in ("TESTIMONIAL", "REFERRAL"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValidationError, "artist-declared"):
                    self.service.record(
                        roles=("PRODUCER",),
                        kind=kind,
                        title=f"Claimed {kind.lower()}",
                        statement="Do not let artist-entered praise become verified proof.",
                        evidence_source_kind="USER_DECLARED",
                        evidence_source_ref=f"artist:{kind}:1",
                        share_scope="PUBLIC",
                        permission_source_kind="OBSERVED",
                        permission_source_ref=f"message:permission:{kind}:1",
                    )

        private_note = self.service.record(
            roles=("PRODUCER",),
            kind="TESTIMONIAL",
            title="Unverified testimonial note",
            statement="Artist-entered note retained privately until verified.",
            evidence_source_kind="USER_DECLARED",
            evidence_source_ref="artist:testimonial-note:2",
        )
        self.assertEqual(private_note.share_scope, "PRIVATE")
        self.assertEqual(self.service.portable_for_role("PRODUCER"), ())

    def test_confidential_material_never_becomes_portable(self):
        with self.assertRaisesRegex(ValidationError, "must remain PRIVATE"):
            self.service.record(
                roles=("MIX_ENGINEER",),
                kind="CREDIT",
                title="Confidential client work",
                statement="Private engagement.",
                evidence_source_kind="PROVIDER_VERIFIED",
                evidence_source_ref="contract:confidential:1",
                share_scope="PUBLIC",
                confidential=True,
            )

        private = self.service.record(
            roles=("MIX_ENGINEER",),
            kind="CREDIT",
            title="Confidential client work",
            statement="Private engagement.",
            evidence_source_kind="PROVIDER_VERIFIED",
            evidence_source_ref="contract:confidential:2",
            confidential=True,
        )
        self.assertEqual(private.share_scope, "PRIVATE")
        self.assertEqual(self.service.portable_for_role("MIX_ENGINEER"), ())

    def test_correction_supersedes_without_erasing_history(self):
        original = self.service.record(
            roles=("PRODUCER",),
            kind="CREDIT",
            title="Producer credit",
            statement="Artist-entered draft credit.",
            evidence_source_kind="USER_DECLARED",
            evidence_source_ref="artist:credit:draft",
            role_evidence_kind="CREDIT",
        )
        corrected = self.service.correct(
            original.evidence_id,
            reason="Distributor credit record arrived and corrected the wording.",
            revision_source_kind="PROVIDER_VERIFIED",
            revision_source_ref="provider:credit-correction:event-9",
            statement="Verified co-producer credit.",
            evidence_source_kind="PROVIDER_VERIFIED",
            evidence_source_ref="provider:credit:release-9",
            share_scope="PUBLIC",
        )

        self.assertEqual(corrected.evidence_id, original.evidence_id)
        self.assertNotEqual(corrected.revision_claim_id, original.revision_claim_id)
        self.assertTrue(corrected.verified)
        history = self.service.history(original.evidence_id)
        self.assertEqual([item.revision_kind for item in history], ["CREATE", "CORRECTION"])
        self.assertEqual(history[0].statement, "Artist-entered draft credit.")
        self.assertEqual(history[1].statement, "Verified co-producer credit.")
        self.assertEqual(
            history[1].revision_source_ref, "provider:credit-correction:event-9"
        )
        self.assertEqual(self.service.current(original.evidence_id), corrected)

    def test_dispute_blocks_reuse_but_preserves_underlying_source_then_correction_resolves(self):
        original = self.service.record(
            roles=("RECORDING_ENGINEER",),
            kind="CREDIT",
            title="Recording engineer credit",
            statement="Provider-listed recording engineer credit.",
            evidence_source_kind="PROVIDER_VERIFIED",
            evidence_source_ref="provider:credit:rec-1",
            share_scope="PUBLIC",
            role_evidence_kind="CREDIT",
        )
        disputed = self.service.dispute(
            original.evidence_id,
            reason="The provider assigned the wrong role.",
            revision_source_kind="USER_DECLARED",
            revision_source_ref="artist:dispute:rec-1",
        )
        self.assertEqual(disputed.state, "DISPUTED")
        self.assertEqual(disputed.evidence_source_ref, original.evidence_source_ref)
        self.assertEqual(disputed.revision_source_kind, "USER_DECLARED")
        self.assertEqual(self.service.portable_for_role("RECORDING_ENGINEER"), ())
        with self.assertRaisesRegex(ValidationError, "only active"):
            self.service.to_role_evidence(original.evidence_id)

        corrected = self.service.correct(
            original.evidence_id,
            reason="Provider issued the corrected mixing credit.",
            revision_source_kind="PROVIDER_VERIFIED",
            revision_source_ref="provider:correction:rec-2",
            roles=("MIX_ENGINEER",),
            title="Mix engineer credit",
            statement="Corrected provider-listed mix engineer credit.",
            evidence_source_kind="PROVIDER_VERIFIED",
            evidence_source_ref="provider:credit:mix-2",
        )
        self.assertEqual(corrected.state, "ACTIVE")
        self.assertEqual(corrected.roles, ("MIX_ENGINEER",))
        self.assertEqual(
            [item.revision_kind for item in self.service.history(original.evidence_id)],
            ["CREATE", "DISPUTE", "CORRECTION"],
        )
        self.assertEqual(
            self.service.portable_for_role("MIX_ENGINEER")[0].evidence_id,
            original.evidence_id,
        )

    def test_withdraw_and_restore_are_explicit_and_preserve_lineage(self):
        record = self.service.record(
            roles=("PRODUCER",),
            kind="RELIABILITY",
            title="Three on-time deliveries",
            statement="Observed delivery history across three completed jobs.",
            evidence_source_kind="OBSERVED",
            evidence_source_ref="activity:delivery-series:3",
            share_scope="OPPORTUNITY",
        )
        withdrawn = self.service.withdraw(
            record.evidence_id,
            reason="Do not use this history while the underlying jobs are under review.",
            revision_source_kind="USER_DECLARED",
            revision_source_ref="artist:withdraw:1",
        )
        self.assertEqual(withdrawn.state, "WITHDRAWN")
        self.assertEqual(self.service.portable_for_role("PRODUCER"), ())
        with self.assertRaisesRegex(ValidationError, "restored before correction"):
            self.service.correct(
                record.evidence_id,
                reason="Attempt to edit a withdrawn record.",
                revision_source_kind="USER_DECLARED",
                revision_source_ref="artist:edit:withdrawn",
                title="Edited while withdrawn",
            )

        restored = self.service.restore(
            record.evidence_id,
            reason="Review completed; the observed delivery record remains valid.",
            revision_source_kind="OBSERVED",
            revision_source_ref="review:delivery-series:3",
        )
        self.assertEqual(restored.state, "ACTIVE")
        self.assertEqual(
            [item.revision_kind for item in self.service.history(record.evidence_id)],
            ["CREATE", "WITHDRAWAL", "RESTORE"],
        )
        self.assertEqual(
            self.service.portable_for_role("PRODUCER")[0].evidence_id,
            record.evidence_id,
        )

    def test_opportunity_and_public_audiences_are_distinct(self):
        opportunity = self.service.record(
            roles=("PRODUCER",),
            kind="CREDIT",
            title="Opportunity-only credit",
            statement="Verified evidence approved for direct opportunities only.",
            evidence_source_kind="PROVIDER_VERIFIED",
            evidence_source_ref="provider:credit:opp",
            share_scope="OPPORTUNITY",
        )
        public = self.service.record(
            roles=("PRODUCER",),
            kind="CREDIT",
            title="Public credit",
            statement="Verified public credit.",
            evidence_source_kind="PROVIDER_VERIFIED",
            evidence_source_ref="provider:credit:public",
            share_scope="PUBLIC",
        )
        private = self.service.record(
            roles=("PRODUCER",),
            kind="CREDIT",
            title="Private credit",
            statement="Verified but not approved for reuse.",
            evidence_source_kind="PROVIDER_VERIFIED",
            evidence_source_ref="provider:credit:private",
        )

        self.assertEqual(
            {item.evidence_id for item in self.service.portable_for_role("PRODUCER")},
            {opportunity.evidence_id, public.evidence_id},
        )
        self.assertEqual(
            [item.evidence_id for item in self.service.portable_for_role("PRODUCER", audience="PUBLIC")],
            [public.evidence_id],
        )
        self.assertNotIn(private.evidence_id, {item.evidence_id for item in self.service.portable_for_role("PRODUCER")})

    def test_version_binding_cannot_cross_song(self):
        other_song = self.hq.store.create_song("Other Client Work")
        other_version = self.hq.store.create_version(other_song.id, label="other")
        with self.assertRaisesRegex(ValidationError, "different Song"):
            self.service.record(
                roles=("MIX_ENGINEER",),
                kind="CREDIT",
                title="Cross-song credit",
                statement="Must not bind a Version from another Song.",
                evidence_source_kind="OBSERVED",
                evidence_source_ref="activity:mix:wrong-binding",
                song_id=self.song.id,
                version_id=other_version.id,
            )
        valid = self.service.record(
            roles=("MIX_ENGINEER",),
            kind="CREDIT",
            title="Exact work binding",
            statement="Bound to the exact delivered Version.",
            evidence_source_kind="OBSERVED",
            evidence_source_ref="activity:mix:exact",
            song_id=self.song.id,
            version_id=self.version.id,
        )
        self.assertEqual(valid.version_id, self.version.id)

    def test_history_survives_relaunch_from_canonical_evidence_memory(self):
        profile_id = self.hq.store.profile_id
        record = self.service.record(
            roles=("PRODUCER",),
            kind="CREDIT",
            title="Persistent credit",
            statement="Source-bound credit survives process restart.",
            evidence_source_kind="PROVIDER_VERIFIED",
            evidence_source_ref="provider:persistent:1",
            share_scope="PUBLIC",
        )
        self.service.dispute(
            record.evidence_id,
            reason="Reviewing an attribution mismatch.",
            revision_source_kind="USER_DECLARED",
            revision_source_ref="artist:dispute:persistent",
        )
        self.hq.close()

        reopened = HeadquartersMemory.open(self.root, profile_id)
        self.addCleanup(reopened.close)
        service = ProfessionalEvidenceService(reopened.store, reopened.evidence)
        history = service.history(record.evidence_id)
        self.assertEqual([item.revision_kind for item in history], ["CREATE", "DISPUTE"])
        self.assertEqual(service.current(record.evidence_id).state, "DISPUTED")

    def test_malformed_or_conflicting_owned_evidence_fails_closed(self):
        malformed_id = "pe_" + "a" * 32
        self.hq.evidence.record_claim(
            scope_kind="PROFILE",
            scope_id=self.hq.store.profile_id,
            key=f"professional.evidence.{malformed_id}",
            value={"schema_version": 1},
            source_kind="USER_DECLARED",
            source_ref="artist:malformed:1",
        )
        with self.assertRaises(ProfessionalEvidenceIntegrityError):
            self.service.current(malformed_id)

        valid = self.service.record(
            roles=("PRODUCER",),
            kind="CREDIT",
            title="Conflict target",
            statement="The service expects one immutable revision chain.",
            evidence_source_kind="OBSERVED",
            evidence_source_ref="activity:conflict:1",
        )
        claim = self.hq.evidence.get_claim(valid.revision_claim_id)
        self.assertIsNotNone(claim)
        self.hq.evidence.record_claim(
            scope_kind="PROFILE",
            scope_id=self.hq.store.profile_id,
            key=claim.key,
            value=claim.value,
            source_kind="OBSERVED",
            source_ref="activity:conflict:parallel",
        )
        with self.assertRaises(ProfessionalEvidenceIntegrityError):
            self.service.current(valid.evidence_id)

    def test_source_and_permission_pairs_cannot_float_without_provenance(self):
        with self.assertRaisesRegex(ValidationError, "source_ref must not be empty"):
            self.service.record(
                roles=("PRODUCER",),
                kind="CREDIT",
                title="Floating credit",
                statement="No source means no professional proof.",
                evidence_source_kind="OBSERVED",
                evidence_source_ref=" ",
            )
        with self.assertRaisesRegex(ValidationError, "supplied together"):
            self.service.record(
                roles=("PRODUCER",),
                kind="WORK_SAMPLE",
                title="Half-bound consent",
                statement="Permission metadata cannot be split.",
                evidence_source_kind="OBSERVED",
                evidence_source_ref="activity:work:1",
                permission_source_kind="OBSERVED",
            )


if __name__ == "__main__":
    unittest.main()
