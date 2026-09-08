import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory
from n0te.professional_handoffs import (
    ProfessionalHandoffIntegrityError,
    ProfessionalHandoffService,
    StaleProfessionalHandoffError,
)


class ProfessionalHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Handoff Artist")
        self.addCleanup(self.hq.close)
        self.song = self.hq.store.create_song("Handoff Song")
        self.v1 = self.hq.store.create_version(self.song.id, label="production lock")
        self.service = ProfessionalHandoffService(self.hq.store)

    @staticmethod
    def full_inputs(service, spec_id):
        spec = service.core_spec(spec_id)
        return {name: f"evidence:{spec_id}:{name}" for name in spec.required_inputs}

    def test_h07_complete_package_is_submitted_then_explicitly_accepted(self):
        inputs = self.full_inputs(self.service, "H07")
        handoff = self.service.submit(
            spec="H07",
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=inputs,
        )

        self.assertEqual(handoff.state, "SUBMITTED")
        self.assertEqual(handoff.spec.from_role, "Producer / Editor")
        self.assertEqual(handoff.spec.to_role, "Mix Engineer")
        self.assertEqual(handoff.spec.version_policy, "CURRENT")
        self.assertEqual(handoff.expected_current_version_id, self.v1.id)
        self.assertIsNone(handoff.expected_approved_version_id)
        self.assertEqual(handoff.missing_inputs, ())
        self.assertIsNone(handoff.acceptance_receipt)
        self.assertFalse(handoff.grants_execution_authority)
        self.assertFalse(hasattr(handoff, "usable"))

        accepted = self.service.accept(handoff.id)
        self.assertEqual(accepted.state, "ACCEPTED")
        self.assertTrue(accepted.accepted)
        self.assertFalse(hasattr(accepted, "usable"))
        self.assertIsNotNone(accepted.acceptance_receipt)
        self.assertEqual(len(accepted.acceptance_receipt), 64)
        freshness = self.service.verify_freshness(accepted.id)
        self.assertEqual(freshness.status, "CURRENT")
        self.assertTrue(freshness.usable)

        event_types = [event.event_type for event in self.hq.activity.for_song(self.song.id)]
        self.assertIn("PROFESSIONAL_HANDOFF_SUBMITTED", event_types)
        self.assertIn("PROFESSIONAL_HANDOFF_ACCEPTED", event_types)

    def test_missing_intake_is_returned_with_exact_missing_items_and_owner(self):
        spec = self.service.core_spec("H07")
        inputs = self.full_inputs(self.service, "H07")
        del inputs["credits"]
        del inputs["unresolved_issues"]

        returned = self.service.submit(
            spec=spec,
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=inputs,
        )

        self.assertEqual(returned.state, "RETURNED")
        self.assertEqual(returned.missing_inputs, ("credits", "unresolved_issues"))
        self.assertIn("Producer / Editor", returned.status_reason)
        self.assertIn("credits", returned.status_reason)
        self.assertIsNone(returned.acceptance_receipt)
        freshness = self.service.verify_freshness(returned.id)
        self.assertEqual(freshness.status, "RETURNED")
        self.assertFalse(freshness.usable)
        with self.assertRaisesRegex(ValidationError, "SUBMITTED"):
            self.service.accept(returned.id)

    def test_complete_intake_can_still_be_returned_for_semantic_problem(self):
        handoff = self.service.submit(
            spec="H07",
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=self.full_inputs(self.service, "H07"),
        )
        returned = self.service.return_submission(
            handoff.id,
            reason="The multitrack manifest and rough mix describe different edit passes.",
        )
        self.assertEqual(returned.state, "RETURNED")
        self.assertEqual(returned.missing_inputs, ())
        self.assertIn("different edit passes", returned.status_reason)
        self.assertIsNone(returned.acceptance_receipt)
        self.assertFalse(self.service.verify_freshness(returned.id).usable)

    def test_h08_requires_current_and_approved_to_be_the_same_exact_mix(self):
        with self.assertRaisesRegex(ValidationError, "current and approved Version"):
            self.service.submit(
                spec="H08",
                song_id=self.song.id,
                upstream_version_id=self.v1.id,
                inputs=self.full_inputs(self.service, "H08"),
            )

        self.hq.store.approve_version(self.song.id, self.v1.id)
        submitted = self.service.submit(
            spec="H08",
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=self.full_inputs(self.service, "H08"),
        )
        self.assertEqual(submitted.expected_current_version_id, self.v1.id)
        self.assertEqual(submitted.expected_approved_version_id, self.v1.id)
        accepted = self.service.accept(submitted.id)
        self.assertEqual(accepted.state, "ACCEPTED")
        self.assertTrue(self.service.verify_freshness(accepted.id).usable)

    def test_current_pointer_change_permanently_stales_accepted_handoff(self):
        handoff = self.service.submit(
            spec="H07",
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=self.full_inputs(self.service, "H07"),
        )
        accepted = self.service.accept(handoff.id)
        receipt = accepted.acceptance_receipt

        v2 = self.hq.store.create_version(
            self.song.id,
            label="post-handoff production revision",
            parent_version_id=self.v1.id,
        )
        stale = self.service.verify_freshness(accepted.id)
        self.assertEqual(stale.status, "STALE")
        self.assertEqual(stale.handoff.state, "STALE")
        self.assertIn("current Version changed", stale.reason)
        self.assertEqual(stale.handoff.acceptance_receipt, receipt)
        self.assertFalse(stale.usable)

        self.hq.store.set_current_version(self.song.id, self.v1.id)
        still_stale = self.service.verify_freshness(accepted.id)
        self.assertEqual(still_stale.status, "STALE")
        self.assertEqual(still_stale.handoff.state, "STALE")
        self.assertFalse(still_stale.usable)
        self.assertNotEqual(v2.id, self.v1.id)

    def test_upstream_change_before_accept_marks_package_stale_and_refuses_acceptance(self):
        submitted = self.service.submit(
            spec="H07",
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=self.full_inputs(self.service, "H07"),
        )
        self.hq.store.create_version(
            self.song.id,
            label="changed before mixer accepted",
            parent_version_id=self.v1.id,
        )

        with self.assertRaisesRegex(StaleProfessionalHandoffError, "current Version changed"):
            self.service.accept(submitted.id)
        durable = self.service.get(submitted.id)
        self.assertEqual(durable.state, "STALE")
        self.assertIsNone(durable.acceptance_receipt)
        self.assertFalse(self.service.verify_freshness(durable.id).usable)

    def test_approved_pointer_change_stales_h08_even_when_old_version_still_exists(self):
        self.hq.store.approve_version(self.song.id, self.v1.id)
        submitted = self.service.submit(
            spec="H08",
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=self.full_inputs(self.service, "H08"),
        )
        accepted = self.service.accept(submitted.id)

        v2 = self.hq.store.create_version(
            self.song.id,
            label="revised approved mix",
            parent_version_id=self.v1.id,
        )
        self.hq.store.approve_version(self.song.id, v2.id)
        stale = self.service.verify_freshness(accepted.id)
        self.assertEqual(stale.status, "STALE")
        self.assertFalse(stale.usable)
        self.assertTrue(
            "current Version changed" in stale.reason
            or "approved Version changed" in stale.reason
        )

    def test_resubmit_creates_new_lineage_after_stale_without_rewriting_old_receipt(self):
        first = self.service.accept(
            self.service.submit(
                spec="H07",
                song_id=self.song.id,
                upstream_version_id=self.v1.id,
                inputs=self.full_inputs(self.service, "H07"),
            ).id
        )
        first_receipt = first.acceptance_receipt
        v2 = self.hq.store.create_version(
            self.song.id,
            label="corrected production lock",
            parent_version_id=self.v1.id,
        )
        self.assertEqual(self.service.verify_freshness(first.id).status, "STALE")

        second = self.service.resubmit(
            first.id,
            upstream_version_id=v2.id,
            inputs=self.full_inputs(self.service, "H07"),
        )
        self.assertEqual(second.state, "SUBMITTED")
        self.assertEqual(second.supersedes_handoff_id, first.id)
        self.assertNotEqual(second.id, first.id)
        accepted_second = self.service.accept(second.id)
        self.assertEqual(accepted_second.state, "ACCEPTED")
        self.assertNotEqual(accepted_second.acceptance_receipt, first_receipt)
        self.assertEqual(self.service.get(first.id).acceptance_receipt, first_receipt)
        self.assertEqual(self.service.get(first.id).state, "STALE")
        self.assertTrue(self.service.verify_freshness(accepted_second.id).usable)

    def test_cross_song_version_binding_and_unknown_input_fail_closed(self):
        other_song = self.hq.store.create_song("Other Handoff Song")
        other_version = self.hq.store.create_version(other_song.id, label="other")
        self.hq.store.select_song(self.song.id)
        self.hq.store.set_current_version(self.song.id, self.v1.id)

        with self.assertRaisesRegex(ValidationError, "different Song"):
            self.service.submit(
                spec="H07",
                song_id=self.song.id,
                upstream_version_id=other_version.id,
                inputs=self.full_inputs(self.service, "H07"),
            )

        inputs = self.full_inputs(self.service, "H07")
        inputs["surprise_authority"] = "authority:nope"
        with self.assertRaisesRegex(ValidationError, "unexpected professional handoff input"):
            self.service.submit(
                spec="H07",
                song_id=self.song.id,
                upstream_version_id=self.v1.id,
                inputs=inputs,
            )

    def test_binding_columns_are_immutable_even_through_direct_sql(self):
        handoff = self.service.submit(
            spec="H07",
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=self.full_inputs(self.service, "H07"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "UPDATE professional_handoffs SET upstream_version_id=? WHERE id=?",
                (self.v1.id + "tamper", handoff.id),
            )
        self.hq.store._conn.rollback()
        self.assertEqual(self.service.get(handoff.id).upstream_version_id, self.v1.id)

    def test_history_survives_relaunch_with_same_package_and_receipt(self):
        profile_id = self.hq.store.profile_id
        accepted = self.service.accept(
            self.service.submit(
                spec="H07",
                song_id=self.song.id,
                upstream_version_id=self.v1.id,
                inputs=self.full_inputs(self.service, "H07"),
            ).id
        )
        fingerprint = accepted.package_fingerprint
        receipt = accepted.acceptance_receipt
        handoff_id = accepted.id
        song_id = self.song.id

        self.hq.close()
        reopened = HeadquartersMemory.open(self.root, profile_id)
        self.addCleanup(reopened.close)
        service = ProfessionalHandoffService(reopened.store)
        durable = service.get(handoff_id)

        self.assertEqual(durable.package_fingerprint, fingerprint)
        self.assertEqual(durable.acceptance_receipt, receipt)
        self.assertEqual(durable.state, "ACCEPTED")
        self.assertEqual(service.for_song(song_id)[0].id, handoff_id)
        freshness = service.verify_freshness(handoff_id)
        self.assertEqual(freshness.status, "CURRENT")
        self.assertTrue(freshness.usable)

    def test_malformed_owned_package_fails_closed(self):
        malformed_id = "ph_" + "a" * 32
        with self.hq.store._tx():
            self.hq.store._conn.execute(
                "INSERT INTO professional_handoffs("
                "id,artist_id,song_id,upstream_version_id,spec_id,spec_json,"
                "provided_inputs_json,missing_inputs_json,expected_current_version_id,"
                "expected_approved_version_id,package_fingerprint,state,status_reason,"
                "acceptance_receipt,supersedes_handoff_id"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    malformed_id,
                    self.hq.store.primary_artist_id,
                    self.song.id,
                    self.v1.id,
                    "H07",
                    "{}",
                    "{}",
                    "[]",
                    self.v1.id,
                    None,
                    "0" * 64,
                    "SUBMITTED",
                    None,
                    None,
                    None,
                ),
            )
        with self.assertRaises(ProfessionalHandoffIntegrityError):
            self.service.get(malformed_id)

    def test_submitted_package_does_not_mutate_versions_or_approval(self):
        before = self.hq.store.get_song(self.song.id)
        before_versions = tuple(self.hq.store.versions_for_song(self.song.id))
        handoff = self.service.submit(
            spec="H07",
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=self.full_inputs(self.service, "H07"),
        )
        after_submit = self.hq.store.get_song(self.song.id)
        self.service.accept(handoff.id)
        after_accept = self.hq.store.get_song(self.song.id)

        self.assertEqual(before.current_version_id, after_submit.current_version_id)
        self.assertEqual(before.approved_version_id, after_submit.approved_version_id)
        self.assertEqual(before.current_version_id, after_accept.current_version_id)
        self.assertEqual(before.approved_version_id, after_accept.approved_version_id)
        self.assertEqual(before_versions, tuple(self.hq.store.versions_for_song(self.song.id)))


if __name__ == "__main__":
    unittest.main()
