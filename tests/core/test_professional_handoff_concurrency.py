import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory
from n0te.professional_handoffs import HandoffSpec, ProfessionalHandoffService


class ProfessionalHandoffConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Handoff Race Artist")
        self.addCleanup(self.hq.close)
        self.song = self.hq.store.create_song("Handoff Race Song")
        self.v1 = self.hq.store.create_version(self.song.id, label="production lock")
        self.service = ProfessionalHandoffService(self.hq.store)

    @staticmethod
    def full_inputs(service, spec_id="H07"):
        spec = service.core_spec(spec_id)
        return {name: f"evidence:{spec_id}:{name}" for name in spec.required_inputs}

    def competing_writer(self):
        conn = sqlite3.connect(
            self.hq.store.database_path,
            timeout=0.0,
            isolation_level=None,
        )
        conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def move_current(conn, song_id, version_id):
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE songs SET current_version_id=? WHERE id=?",
                (version_id, song_id),
            )
            conn.commit()
            return "MOVED"
        except sqlite3.OperationalError as exc:
            conn.rollback()
            return str(exc).lower()

    def test_submit_holds_write_lock_across_pointer_snapshot(self):
        v2 = self.hq.store.create_version(
            self.song.id,
            label="competing production revision",
            parent_version_id=self.v1.id,
        )
        self.hq.store.set_current_version(self.song.id, self.v1.id)
        writer = self.competing_writer()
        attempts = []
        original = self.service._require_version_for_song

        def wrapped(version_id, song_id):
            attempts.append(self.move_current(writer, self.song.id, v2.id))
            return original(version_id, song_id)

        self.service._require_version_for_song = wrapped
        try:
            submitted = self.service.submit(
                spec="H07",
                song_id=self.song.id,
                upstream_version_id=self.v1.id,
                inputs=self.full_inputs(self.service),
            )
        finally:
            self.service._require_version_for_song = original

        self.assertEqual(len(attempts), 1)
        self.assertIn("locked", attempts[0])
        self.assertEqual(submitted.expected_current_version_id, self.v1.id)
        pending = self.service.verify_freshness(submitted.id)
        self.assertEqual(pending.status, "PENDING")
        self.assertFalse(pending.usable)

        self.assertEqual(self.move_current(writer, self.song.id, v2.id), "MOVED")
        stale = self.service.verify_freshness(submitted.id)
        self.assertEqual(stale.status, "STALE")
        self.assertFalse(stale.usable)

    def test_accept_holds_write_lock_across_freshness_check_and_receipt(self):
        v2 = self.hq.store.create_version(
            self.song.id,
            label="competing acceptance revision",
            parent_version_id=self.v1.id,
        )
        self.hq.store.set_current_version(self.song.id, self.v1.id)
        submitted = self.service.submit(
            spec="H07",
            song_id=self.song.id,
            upstream_version_id=self.v1.id,
            inputs=self.full_inputs(self.service),
        )
        writer = self.competing_writer()
        attempts = []
        original = self.service._freshness_reason

        def wrapped(handoff):
            attempts.append(self.move_current(writer, self.song.id, v2.id))
            return original(handoff)

        self.service._freshness_reason = wrapped
        try:
            accepted = self.service.accept(submitted.id)
        finally:
            self.service._freshness_reason = original

        self.assertEqual(len(attempts), 1)
        self.assertIn("locked", attempts[0])
        self.assertIsNotNone(accepted.acceptance_receipt)
        self.assertEqual(self.hq.store.get_song(self.song.id).current_version_id, self.v1.id)
        current = self.service.verify_freshness(accepted.id)
        self.assertEqual(current.status, "CURRENT")
        self.assertTrue(current.usable)

        self.assertEqual(self.move_current(writer, self.song.id, v2.id), "MOVED")
        stale = self.service.verify_freshness(accepted.id)
        self.assertEqual(stale.status, "STALE")
        self.assertFalse(stale.usable)

        self.hq.store.set_current_version(self.song.id, self.v1.id)
        still_stale = self.service.verify_freshness(accepted.id)
        self.assertEqual(still_stale.status, "STALE")
        self.assertFalse(still_stale.usable)

    def test_null_required_input_reference_cannot_become_evidence(self):
        inputs = self.full_inputs(self.service)
        inputs["credits"] = None
        with self.assertRaisesRegex(ValidationError, "must be text"):
            self.service.submit(
                spec="H07",
                song_id=self.song.id,
                upstream_version_id=self.v1.id,
                inputs=inputs,
            )

    def test_reserved_core_contract_id_cannot_be_overridden(self):
        canonical = self.service.core_spec("H07")
        forged = HandoffSpec(
            id="H07",
            from_role=canonical.from_role,
            to_role=canonical.to_role,
            trigger=canonical.trigger,
            required_inputs=("rough_mix",),
            required_outputs=canonical.required_outputs,
            approval_owner=canonical.approval_owner,
            rights_metadata=canonical.rights_metadata,
            return_owner=canonical.return_owner,
            version_policy=canonical.version_policy,
        )
        with self.assertRaisesRegex(ValidationError, "reserved core"):
            self.service.submit(
                spec=forged,
                song_id=self.song.id,
                upstream_version_id=self.v1.id,
                inputs={"rough_mix": "evidence:forged"},
            )


if __name__ == "__main__":
    unittest.main()
