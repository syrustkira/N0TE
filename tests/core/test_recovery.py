import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from n0te import (
    HeadquartersMemory,
    LineageCorruptionError,
    RecoveryError,
    RecoveryManager,
    SnapshotHashMismatchError,
    SnapshotValidationError,
)


class Core01FRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_restore_recovers_snapshot_and_preserves_corrupt_live_database(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        artist = hq.store.primary_artist_id
        song = hq.store.create_song("Song")
        v1 = hq.store.create_version(song.id, label="v1")
        hq.store.approve_version(song.id, v1.id)
        snapshot = hq.recovery.create_snapshot()

        v2 = hq.store.create_version(song.id, label="v2", parent_version_id=v1.id)
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="post.snapshot",
            value="must disappear on restore",
            source_kind="OBSERVED",
        )
        self.assertEqual(hq.store.get_song(song.id).current_version_id, v2.id)
        live_path = hq.store.database_path
        hq.close()

        live_path.write_bytes(b"deliberately corrupt canonical database")
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, profile)

        inspected = RecoveryManager.inspect_snapshot(self.root, profile)
        self.assertEqual(inspected.sha256, snapshot.sha256)
        self.assertEqual(inspected.profile_id, profile)
        with self.assertRaises(SnapshotHashMismatchError):
            RecoveryManager.restore_snapshot(
                self.root, profile, expected_sha256="0" * 64
            )
        self.assertEqual(live_path.read_bytes(), b"deliberately corrupt canonical database")

        restored = RecoveryManager.restore_snapshot(
            self.root, profile, expected_sha256=snapshot.sha256
        )
        self.assertEqual(restored.installed_sha256, snapshot.sha256)
        self.assertIsNotNone(restored.preserved_database)
        self.assertTrue(restored.preserved_database.is_file())
        self.assertEqual(
            restored.preserved_database.read_bytes(),
            b"deliberately corrupt canonical database",
        )

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        self.assertEqual(hq.store.primary_artist_id, artist)
        recovered_song = hq.store.get_song(song.id)
        self.assertEqual(recovered_song.current_version_id, v1.id)
        self.assertEqual(recovered_song.approved_version_id, v1.id)
        self.assertIsNone(hq.store.get_version(v2.id))
        self.assertEqual(
            hq.evidence.resolve_for_song(song_id=song.id, key="post.snapshot").status,
            "UNKNOWN",
        )

    def test_valid_snapshot_does_not_silently_replace_corrupt_live_database_on_open(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        hq.store.create_song("Song")
        snapshot = hq.recovery.create_snapshot()
        live = hq.store.database_path
        hq.close()
        live.write_bytes(b"corrupt")

        self.assertEqual(RecoveryManager.inspect_snapshot(self.root, profile).sha256, snapshot.sha256)
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, profile)
        self.assertEqual(live.read_bytes(), b"corrupt")

    def test_snapshot_copied_to_another_profile_is_rejected(self):
        a = HeadquartersMemory.create(self.root, "Artist A")
        profile_a = a.store.profile_id
        a.store.create_song("A")
        snap_a = a.recovery.create_snapshot()
        a.close()

        b = HeadquartersMemory.create(self.root, "Artist B")
        profile_b = b.store.profile_id
        b.store.create_song("B")
        b.recovery.create_snapshot()
        b.close()

        target = RecoveryManager.snapshot_path(self.root, profile_b)
        shutil.copyfile(snap_a.path, target)
        with self.assertRaises(SnapshotValidationError):
            RecoveryManager.inspect_snapshot(self.root, profile_b)
        self.assertEqual(RecoveryManager.inspect_snapshot(self.root, profile_a).profile_id, profile_a)

    def test_failed_snapshot_replacement_preserves_previous_valid_snapshot(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        first = hq.recovery.create_snapshot()
        hq.store.create_version(song.id, label="later")

        with mock.patch("n0te.recovery.os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(RecoveryError):
                hq.recovery.create_snapshot()
        after = RecoveryManager.inspect_snapshot(self.root, hq.store.profile_id)
        self.assertEqual(after.sha256, first.sha256)

    def test_unreadable_snapshot_fails_as_snapshot_validation_not_internal_error(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        hq.store.create_song("Song")
        info = hq.recovery.create_snapshot()
        hq.close()
        info.path.write_bytes(b"not sqlite")
        with self.assertRaises(SnapshotValidationError):
            RecoveryManager.inspect_snapshot(self.root, profile)


if __name__ == "__main__":
    unittest.main()
