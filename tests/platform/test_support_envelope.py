import unittest

from n0te.support import (
    SupportEnvelope,
    SupportEnvelopeError,
    SupportEvidence,
    SupportTarget,
    default_architecture_targets,
    default_support_envelope,
)


class Platform00CSupportEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.targets = default_architecture_targets()
        self.core = [target for target in self.targets if target.policy_tier == "CORE"]
        self.extended = [
            target for target in self.targets if target.policy_tier == "EXTENDED"
        ]

    def test_default_policy_has_six_core_and_four_extended_architecture_targets(self):
        self.assertEqual(len(self.core), 6)
        self.assertEqual(len(self.extended), 4)
        self.assertEqual(
            {(target.os_family, target.architecture) for target in self.core},
            {
                ("MACOS", "ARM64"),
                ("MACOS", "X86_64"),
                ("WINDOWS", "X86_64"),
                ("WINDOWS", "ARM64"),
                ("LINUX", "X86_64"),
                ("LINUX", "ARM64"),
            },
        )

    def test_every_core_target_is_initially_an_unverified_customer_mode_blocker(self):
        envelope = default_support_envelope()
        blockers = envelope.customer_mode_blockers()
        self.assertEqual(len(blockers), 6)
        self.assertTrue(all(blocker.state == "UNVERIFIED" for blocker in blockers))

    def test_acceptance_removes_only_the_exact_core_blocker(self):
        target = self.core[0]
        envelope = default_support_envelope(
            [SupportEvidence(target.fingerprint, "ACCEPTED", "ci:accept")]
        )
        self.assertEqual(len(envelope.customer_mode_blockers()), 5)
        self.assertEqual(envelope.status(target).state, "ACCEPTED")

    def test_legacy_acceptance_requires_and_preserves_upstream_limitation(self):
        target = self.core[0]
        with self.assertRaises(SupportEnvelopeError):
            SupportEvidence(target.fingerprint, "LEGACY_ACCEPTED", "ci:legacy")
        envelope = default_support_envelope(
            [
                SupportEvidence(
                    target.fingerprint,
                    "LEGACY_ACCEPTED",
                    "ci:legacy",
                    upstream_limitation="Vendor OS servicing ended",
                )
            ]
        )
        status = envelope.status(target)
        self.assertEqual(status.state, "LEGACY_ACCEPTED")
        self.assertEqual(status.upstream_limitation, "Vendor OS servicing ended")
        self.assertNotIn(
            target.fingerprint,
            {blocker.target_fingerprint for blocker in envelope.customer_mode_blockers()},
        )

    def test_known_break_requires_a_named_reason_and_blocks_core(self):
        target = self.core[0]
        with self.assertRaises(SupportEnvelopeError):
            SupportEvidence(target.fingerprint, "KNOWN_BREAK", "probe:fail")
        envelope = default_support_envelope(
            [
                SupportEvidence(
                    target.fingerprint,
                    "KNOWN_BREAK",
                    "probe:fail",
                    known_break_reason="Required ABI missing",
                )
            ]
        )
        blocker = next(
            blocker
            for blocker in envelope.customer_mode_blockers()
            if blocker.target_fingerprint == target.fingerprint
        )
        self.assertIn("Required ABI missing", blocker.reason)

    def test_extended_failure_never_blocks_a_core_target(self):
        target = self.extended[0]
        envelope = default_support_envelope(
            [
                SupportEvidence(
                    target.fingerprint,
                    "KNOWN_BREAK",
                    "probe:extended",
                    known_break_reason="Legacy compiler unavailable",
                )
            ]
        )
        self.assertEqual(len(envelope.customer_mode_blockers()), 6)
        self.assertEqual(len(envelope.extended_findings()), 4)

    def test_accepting_every_extended_target_cannot_substitute_for_core(self):
        evidence = [
            SupportEvidence(target.fingerprint, "ACCEPTED", f"ci:extended:{index}")
            for index, target in enumerate(self.extended)
        ]
        envelope = default_support_envelope(evidence)
        self.assertEqual(len(envelope.customer_mode_blockers()), 6)

    def test_evidence_for_target_outside_envelope_is_rejected(self):
        scoped = SupportTarget.from_runtime_labels(
            os_name="Linux",
            machine="amd64",
            scope_tags=("distro:ubuntu",),
        )
        with self.assertRaises(SupportEnvelopeError):
            default_support_envelope(
                [SupportEvidence(scoped.fingerprint, "ACCEPTED", "ci:other")]
            )

    def test_duplicate_or_conflicting_evidence_for_one_target_is_rejected(self):
        target = self.core[0]
        with self.assertRaises(SupportEnvelopeError):
            default_support_envelope(
                [
                    SupportEvidence(target.fingerprint, "ACCEPTED", "ci:a"),
                    SupportEvidence(
                        target.fingerprint,
                        "KNOWN_BREAK",
                        "ci:b",
                        known_break_reason="break",
                    ),
                ]
            )

    def test_canonical_core_target_cannot_be_downgraded_to_extended(self):
        with self.assertRaises(SupportEnvelopeError):
            SupportTarget("MACOS", "ARM64", "EXTENDED")

    def test_runtime_aliases_converge_to_same_target_identity(self):
        first = SupportTarget.from_runtime_labels(os_name="Darwin", machine="aarch64")
        second = SupportTarget.from_runtime_labels(os_name="macOS", machine="arm64")
        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_scope_tags_create_exact_subtargets(self):
        ubuntu = SupportTarget.from_runtime_labels(
            os_name="Linux", machine="amd64", scope_tags=("distro:ubuntu",)
        )
        debian = SupportTarget.from_runtime_labels(
            os_name="Linux", machine="amd64", scope_tags=("distro:debian",)
        )
        self.assertNotEqual(ubuntu.fingerprint, debian.fingerprint)

    def test_scope_tag_order_and_duplicates_do_not_change_identity(self):
        first = SupportTarget.from_runtime_labels(
            os_name="Linux",
            machine="amd64",
            scope_tags=("package:appimage", "distro:ubuntu"),
        )
        second = SupportTarget.from_runtime_labels(
            os_name="Linux",
            machine="amd64",
            scope_tags=("distro:ubuntu", "package:appimage", "package:appimage"),
        )
        self.assertEqual(first, second)

    def test_version_policy_is_exact_identity_data_but_is_not_interpreted(self):
        evidence_driven = SupportTarget.from_runtime_labels(
            os_name="Windows",
            machine="amd64",
            os_version_policy="evidence-driven",
        )
        marketing_floor = SupportTarget.from_runtime_labels(
            os_name="Windows",
            machine="amd64",
            os_version_policy="marketing-name-floor",
        )
        self.assertNotEqual(evidence_driven.fingerprint, marketing_floor.fingerprint)

    def test_model_exposes_no_generic_supported_boolean_shortcut(self):
        target = self.core[0]
        status = default_support_envelope().status(target)
        self.assertFalse(hasattr(status, "supported"))
        self.assertFalse(hasattr(target, "supported"))

    def test_target_and_status_order_is_deterministic(self):
        reversed_envelope = SupportEnvelope(reversed(self.targets))
        normal_envelope = SupportEnvelope(self.targets)
        self.assertEqual(reversed_envelope.targets, normal_envelope.targets)
        self.assertEqual(reversed_envelope.statuses(), normal_envelope.statuses())

    def test_acceptance_evidence_requires_a_nonempty_reference(self):
        target = self.core[0]
        with self.assertRaises(SupportEnvelopeError):
            SupportEvidence(target.fingerprint, "ACCEPTED", " ")

    def test_experimental_core_target_stays_blocking(self):
        target = self.core[0]
        envelope = default_support_envelope(
            [SupportEvidence(target.fingerprint, "EXPERIMENTAL", "probe:exp")]
        )
        blocker = next(
            blocker
            for blocker in envelope.customer_mode_blockers()
            if blocker.target_fingerprint == target.fingerprint
        )
        self.assertEqual(blocker.state, "EXPERIMENTAL")

    def test_status_rejects_structurally_valid_but_foreign_target(self):
        envelope = default_support_envelope()
        scoped = SupportTarget.from_runtime_labels(
            os_name="Linux",
            machine="amd64",
            scope_tags=("distro:ubuntu",),
        )
        with self.assertRaises(SupportEnvelopeError):
            envelope.status(scoped)

    def test_target_id_is_deterministic(self):
        target = self.core[0]
        same = SupportTarget(target.os_family, target.architecture, target.policy_tier)
        self.assertEqual(target.target_id, same.target_id)


if __name__ == "__main__":
    unittest.main()
