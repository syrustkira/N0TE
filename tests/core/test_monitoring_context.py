import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from n0te import HeadquartersMemory
from n0te.monitoring_context import (
    DEFAULT_MONITORING_KEYS,
    MONITORING_AUTHORITY,
    MonitoringContextError,
    MonitoringContextService,
    StaleMonitoringContextError,
)


class MonitoringContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.service = MonitoringContextService(self.hq.store, self.hq.evidence)
        self.profile_id = self.hq.store.profile_id
        self.song = self.hq.store.create_song("Listening Song")
        self.version = self.hq.store.create_version(self.song.id, label="v1")

    def tearDown(self) -> None:
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    def snapshot(self, keys=DEFAULT_MONITORING_KEYS):
        return self.service.snapshot(
            song_id=self.song.id,
            version_id=self.version.id,
            keys=keys,
        )

    def test_unknown_context_stays_unknown_and_never_becomes_universal_truth(self):
        context = self.snapshot()

        self.assertEqual(context.status, "UNKNOWN")
        self.assertEqual(context.authority, MONITORING_AUTHORITY)
        self.assertFalse(context.action_authority_granted)
        self.assertFalse(context.universal_truth)
        self.assertEqual(context.song_id, self.song.id)
        self.assertEqual(context.version_id, self.version.id)
        self.assertEqual(context.keys, DEFAULT_MONITORING_KEYS)
        self.assertTrue(all(fact.status == "UNKNOWN" for fact in context.facts))
        self.assertTrue(all(not fact.claim_ids for fact in context.facts))

    def test_scope_specificity_and_source_kind_are_preserved(self):
        declared = self.service.record_fact(
            scope_kind="PROFILE",
            scope_id=self.profile_id,
            key="monitoring.output_path",
            value={"kind": "HEADPHONES", "label": "daily cans"},
            source_kind="USER_DECLARED",
        )
        observed = self.service.record_fact(
            scope_kind="VERSION",
            scope_id=self.version.id,
            key="monitoring.output_path",
            value={"kind": "SPEAKERS", "label": "desk pair"},
            source_kind="OBSERVED",
            source_ref="os-audio-route:session-17",
        )

        context = self.snapshot(keys=("monitoring.output_path",))
        fact = context.facts[0]

        self.assertEqual(context.status, "RESOLVED")
        self.assertEqual(fact.status, "RESOLVED")
        self.assertEqual(fact.scope_kind, "VERSION")
        self.assertEqual(fact.scope_id, self.version.id)
        self.assertEqual(fact.value["kind"], "SPEAKERS")
        self.assertEqual(fact.claim_ids, (observed.id,))
        self.assertEqual(fact.source_kinds, ("OBSERVED",))
        self.assertNotIn(declared.id, fact.claim_ids)
        self.assertEqual(observed.twin_domain, "TECHNICAL")

    def test_conflicting_same_scope_monitoring_evidence_remains_conflict(self):
        first = self.service.record_fact(
            scope_kind="VERSION",
            scope_id=self.version.id,
            key="monitoring.calibration",
            value={"status": "CALIBRATED"},
            source_kind="MEASURED",
            source_ref="calibration-report:a",
        )
        second = self.service.record_fact(
            scope_kind="VERSION",
            scope_id=self.version.id,
            key="monitoring.calibration",
            value={"status": "UNCERTAIN"},
            source_kind="USER_DECLARED",
        )

        context = self.snapshot(keys=("monitoring.calibration",))
        fact = context.facts[0]

        self.assertEqual(context.status, "CONFLICT")
        self.assertEqual(fact.status, "CONFLICT")
        self.assertIsNone(fact.value)
        self.assertEqual(set(fact.claim_ids), {first.id, second.id})
        self.assertEqual(set(fact.source_kinds), {"MEASURED", "USER_DECLARED"})

    def test_partial_context_is_distinct_from_unknown_and_conflict(self):
        self.service.record_fact(
            scope_kind="SONG",
            scope_id=self.song.id,
            key="monitoring.listening_environment",
            value={"room": "small bedroom", "treatment": "UNKNOWN"},
            source_kind="USER_DECLARED",
        )

        context = self.snapshot(
            keys=(
                "monitoring.listening_environment",
                "monitoring.reference_level",
            )
        )

        self.assertEqual(context.status, "PARTIAL")
        statuses = {fact.key: fact.status for fact in context.facts}
        self.assertEqual(statuses["monitoring.listening_environment"], "RESOLVED")
        self.assertEqual(statuses["monitoring.reference_level"], "UNKNOWN")

    def test_observed_and_measured_claims_need_source_reference(self):
        for source_kind in ("OBSERVED", "MEASURED"):
            with self.subTest(source_kind=source_kind):
                with self.assertRaises(MonitoringContextError):
                    self.service.record_fact(
                        scope_kind="VERSION",
                        scope_id=self.version.id,
                        key="monitoring.reference_level",
                        value={"level": 74},
                        source_kind=source_kind,
                    )

        declared = self.service.record_fact(
            scope_kind="VERSION",
            scope_id=self.version.id,
            key="monitoring.reference_level",
            value={"level": "quiet"},
            source_kind="USER_DECLARED",
        )
        self.assertIsNone(declared.source_ref)

    def test_service_cannot_self_issue_provider_verified_but_consumes_canonical_claim(self):
        with self.assertRaises(MonitoringContextError):
            self.service.record_fact(
                scope_kind="VERSION",
                scope_id=self.version.id,
                key="monitoring.output_path",
                value={"kind": "SPEAKERS"},
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider-receipt:claimed-only",
            )

        verified = self.hq.evidence.record_claim(
            scope_kind="VERSION",
            scope_id=self.version.id,
            key="monitoring.output_path",
            value={"kind": "SPEAKERS", "route": "Interface Out 1-2"},
            source_kind="PROVIDER_VERIFIED",
            source_ref="provider-verifier-receipt:route-17",
            twin_domain="TECHNICAL",
        )
        context = self.snapshot(keys=("monitoring.output_path",))

        self.assertEqual(context.status, "RESOLVED")
        self.assertEqual(context.facts[0].claim_ids, (verified.id,))
        self.assertEqual(context.facts[0].source_kinds, ("PROVIDER_VERIFIED",))

    def test_snapshot_and_judgment_binding_become_stale_after_applicable_change(self):
        old = self.service.record_fact(
            scope_kind="VERSION",
            scope_id=self.version.id,
            key="monitoring.output_path",
            value={"kind": "HEADPHONES"},
            source_kind="USER_DECLARED",
        )
        context = self.snapshot(keys=("monitoring.output_path",))
        binding = self.service.bind_judgment(
            judgment_ref="engineering-judgment:low-end:1",
            context=context,
        )

        self.assertTrue(self.service.is_current(context))
        self.assertTrue(self.service.binding_is_current(binding))
        self.assertEqual(binding.monitoring_keys, ("monitoring.output_path",))
        self.assertFalse(binding.action_authority_granted)
        self.assertFalse(binding.universal_truth)

        self.service.record_fact(
            scope_kind="VERSION",
            scope_id=self.version.id,
            key="monitoring.output_path",
            value={"kind": "SPEAKERS"},
            source_kind="OBSERVED",
            source_ref="os-audio-route:session-18",
            supersedes=(old.id,),
        )

        self.assertFalse(self.service.is_current(context))
        self.assertFalse(self.service.binding_is_current(binding))
        with self.assertRaises(StaleMonitoringContextError):
            self.service.assert_current(context)

    def test_altered_context_payload_cannot_reuse_a_valid_fingerprint(self):
        self.service.record_fact(
            scope_kind="VERSION",
            scope_id=self.version.id,
            key="monitoring.output_path",
            value={"kind": "HEADPHONES"},
            source_kind="USER_DECLARED",
        )
        context = self.snapshot(keys=("monitoring.output_path",))
        altered_fact = replace(
            context.facts[0],
            value={"kind": "SPEAKERS", "forged": True},
        )
        altered = replace(context, facts=(altered_fact,))

        self.assertFalse(self.service.is_current(altered))
        with self.assertRaises(StaleMonitoringContextError):
            self.service.bind_judgment(
                judgment_ref="engineering-judgment:forged:1",
                context=altered,
            )

    def test_cross_song_version_binding_is_rejected(self):
        other_song = self.hq.store.create_song("Other Song")
        other_version = self.hq.store.create_version(other_song.id, label="v1")

        with self.assertRaises(MonitoringContextError):
            self.service.snapshot(
                song_id=self.song.id,
                version_id=other_version.id,
            )

    def test_context_from_another_profile_is_not_current(self):
        context = self.snapshot(keys=("monitoring.output_path",))
        other_root = self.root / "other"
        other_root.mkdir()
        other = HeadquartersMemory.create(other_root, "Other Artist")
        self.addCleanup(other.close)
        other_song = other.store.create_song("Other")
        other.store.create_version(other_song.id, label="v1")
        other_service = MonitoringContextService(other.store, other.evidence)

        self.assertFalse(other_service.is_current(context))

    def test_fingerprint_and_exact_context_survive_restart(self):
        claim = self.service.record_fact(
            scope_kind="ARTIST",
            scope_id=self.hq.store.primary_artist_id,
            key="monitoring.translation_check",
            value={"targets": ["phone", "car"], "result": "NOT_RUN"},
            source_kind="USER_DECLARED",
        )
        context_before = self.snapshot(keys=("monitoring.translation_check",))
        self.hq.close()

        self.hq = HeadquartersMemory.open(self.root, self.profile_id)
        self.service = MonitoringContextService(self.hq.store, self.hq.evidence)
        context_after = self.service.snapshot(
            song_id=self.song.id,
            version_id=self.version.id,
            keys=("monitoring.translation_check",),
        )

        self.assertEqual(context_after.fingerprint, context_before.fingerprint)
        self.assertEqual(context_after.context_id, context_before.context_id)
        self.assertEqual(context_after.facts[0].claim_ids, (claim.id,))
        self.assertTrue(self.service.is_current(context_before))

    def test_custom_monitoring_keys_remain_exact_in_judgment_binding(self):
        self.service.record_fact(
            scope_kind="VERSION",
            scope_id=self.version.id,
            key="monitoring.custom_crossfeed",
            value={"enabled": False},
            source_kind="OBSERVED",
            source_ref="device-state:crossfeed",
        )
        context = self.snapshot(keys=("monitoring.custom_crossfeed",))
        binding = self.service.bind_judgment(
            judgment_ref="engineering-judgment:image:1",
            context=context,
        )

        self.assertEqual(binding.monitoring_keys, ("monitoring.custom_crossfeed",))
        self.assertTrue(self.service.binding_is_current(binding))

    def test_non_monitoring_namespace_is_rejected(self):
        with self.assertRaises(MonitoringContextError):
            self.service.record_fact(
                scope_kind="VERSION",
                scope_id=self.version.id,
                key="song.intent",
                value="do not hijack another evidence namespace",
                source_kind="USER_DECLARED",
            )
        with self.assertRaises(MonitoringContextError):
            self.snapshot(keys=("engineering.low_end",))


if __name__ == "__main__":
    unittest.main()
