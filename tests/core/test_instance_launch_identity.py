import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from n0te.instance import (
    InstanceLeaseManager,
    InstanceLeaseOwnershipError,
    ProcessIdentity,
)
from n0te.platforms import PlatformEnvironment


class AlwaysAliveProbe:
    def status(self, process):
        return "ALIVE"


class ExactLaunchLeaseRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.profile = "profile-exact-launch"
        self.platform = PlatformEnvironment.from_runtime_labels("Windows", "AMD64")
        self.manager = InstanceLeaseManager(self.root)
        self.probe = AlwaysAliveProbe()

    def tearDown(self):
        self.tmp.cleanup()

    def identity(self, launch_marker):
        return ProcessIdentity.from_start_token(
            self.platform,
            pid=4242,
            start_token="reused-workflow-start-token",
            launch_marker=launch_marker,
        )

    def test_live_pid_with_reused_workflow_fingerprint_is_stale(self):
        old = self.identity("launch-old")
        worker = self.identity("launch-new")
        self.assertEqual(old.workflow_fingerprint, worker.workflow_fingerprint)
        self.assertNotEqual(old.launch_fingerprint, worker.launch_fingerprint)

        first = self.manager.acquire(self.profile, old, self.probe)
        self.assertEqual(first.status, "ACQUIRED")
        self.assertIsNotNone(first.lease)

        replacement = self.manager.acquire(self.profile, worker, self.probe)
        self.assertEqual(replacement.status, "REPLACED_STALE")
        self.assertEqual(replacement.previous_lease, first.lease)
        current = self.manager.inspect(self.profile)
        self.assertIsNotNone(current)
        self.assertEqual(current.process, worker)
        self.assertTrue(current.process.same_launch(worker))

    def test_default_launch_marker_is_process_scoped_not_reused_workflow_token(self):
        default_identity = ProcessIdentity.from_start_token(
            self.platform,
            pid=4242,
            start_token="reused-workflow-start-token",
        )
        legacy_collapsed_identity = self.identity("reused-workflow-start-token")
        repeated_default = ProcessIdentity.from_start_token(
            self.platform,
            pid=4242,
            start_token="reused-workflow-start-token",
        )

        self.assertEqual(
            default_identity.workflow_fingerprint,
            legacy_collapsed_identity.workflow_fingerprint,
        )
        self.assertNotEqual(
            default_identity.launch_fingerprint,
            legacy_collapsed_identity.launch_fingerprint,
        )
        self.assertTrue(default_identity.same_launch(repeated_default))

    def test_stale_old_launch_cannot_release_newer_lease(self):
        old = self.identity("launch-old")
        worker = self.identity("launch-new")
        first = self.manager.acquire(self.profile, old, self.probe)
        replacement = self.manager.acquire(self.profile, worker, self.probe)
        self.assertIsNotNone(first.lease)
        self.assertIsNotNone(replacement.lease)

        with self.assertRaises(InstanceLeaseOwnershipError):
            self.manager.release(
                self.profile,
                process=old,
                lease_nonce=first.lease.lease_nonce,
            )

        current = self.manager.inspect(self.profile)
        self.assertIsNotNone(current)
        self.assertEqual(current.process, worker)
        self.assertEqual(current.lease_nonce, replacement.lease.lease_nonce)

    def test_same_exact_launch_reacquires_idempotently(self):
        worker = self.identity("launch-one")
        first = self.manager.acquire(self.profile, worker, self.probe)
        second = self.manager.acquire(self.profile, worker, self.probe)
        self.assertEqual(first.status, "ACQUIRED")
        self.assertEqual(second.status, "ALREADY_OWNED")
        self.assertEqual(second.lease, first.lease)

    def test_retry_exhaustion_returns_uncertain_instead_of_raising(self):
        worker = self.identity("launch-one")
        with mock.patch.object(self.manager, "_write_exclusive", return_value=False) as write:
            result = self.manager.acquire(self.profile, worker, self.probe)
        self.assertEqual(result.status, "UNCERTAIN")
        self.assertIsNone(result.lease)
        self.assertEqual(write.call_count, self.manager.MAX_ATTEMPTS)

    def test_schema_v1_process_data_remains_readable(self):
        worker = self.identity("reused-workflow-start-token")
        acquired = self.manager.acquire(self.profile, worker, self.probe)
        self.assertIsNotNone(acquired.lease)
        lease_path = self.manager._lease_path(self.profile)
        data = acquired.lease.to_data()
        data["schema_version"] = 1
        process = dict(data["process"])
        process.pop("launch_marker_fingerprint")
        process.pop("launch_fingerprint")
        data["process"] = process
        lease_path.write_text(json.dumps(data), encoding="utf-8")
        migrated = self.manager.inspect(self.profile)
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.process.workflow_fingerprint, worker.workflow_fingerprint)
        self.assertTrue(migrated.process.same_launch(worker))

    def test_reader_retries_bounded_transient_empty_publication(self):
        worker = self.identity("launch-one")
        acquired = self.manager.acquire(self.profile, worker, self.probe)
        self.assertIsNotNone(acquired.lease)
        lease_path = self.manager._lease_path(self.profile)
        published = lease_path.read_bytes()

        with mock.patch.object(Path, "read_bytes", side_effect=[b"", published]) as read:
            recovered = self.manager._read_json(lease_path)

        self.assertEqual(recovered, acquired.lease.to_data())
        self.assertEqual(read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
