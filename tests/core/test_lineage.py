import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import LineageCorruptionError, LineageStore, NotFoundError, ValidationError


class Core01ALineageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_version_approve_current_and_restart_preserve_identity(self):
        store = LineageStore.create(self.root, "Artist One")
        profile_id = store.profile_id
        artist_id = store.primary_artist_id

        song = store.create_song("First Song")
        source = store.attach_asset(
            song.id,
            name="demo.wav",
            sha256="a" * 64,
            source_uri="file:///music/demo.wav",
        )
        v1 = store.create_version(
            song.id, label="v1", asset_ids=[source.id], make_current=True
        )
        store.approve_version(song.id, v1.id)
        v2 = store.create_version(
            song.id,
            label="v2",
            parent_version_id=v1.id,
            asset_ids=[source.id],
            make_current=True,
        )

        before = store.get_song(song.id)
        self.assertEqual(before.current_version_id, v2.id)
        self.assertEqual(before.approved_version_id, v1.id)
        self.assertEqual(store.active_song().id, song.id)
        store.close()

        reopened = LineageStore.open(self.root, profile_id)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.primary_artist_id, artist_id)
        self.assertEqual(reopened.artist().id, artist_id)
        self.assertEqual(reopened.active_song().id, song.id)
        after = reopened.get_song(song.id)
        self.assertEqual(after.id, song.id)
        self.assertEqual(after.current_version_id, v2.id)
        self.assertEqual(after.approved_version_id, v1.id)
        self.assertEqual(reopened.get_version(v2.id).parent_version_id, v1.id)
        self.assertEqual(reopened.version_asset_ids(v1.id), (source.id,))
        self.assertEqual(reopened.version_asset_ids(v2.id), (source.id,))

    def test_current_never_aliases_approved_without_explicit_approval(self):
        store = LineageStore.create(self.root, "Artist")
        self.addCleanup(store.close)
        song = store.create_song("Song")
        v1 = store.create_version(song.id, label="v1")
        store.approve_version(song.id, v1.id)
        v2 = store.create_version(song.id, label="v2")
        state = store.get_song(song.id)
        self.assertEqual(state.current_version_id, v2.id)
        self.assertEqual(state.approved_version_id, v1.id)

    def test_profile_isolation_blocks_cross_profile_read_and_link(self):
        a = LineageStore.create(self.root, "Artist A")
        b = LineageStore.create(self.root, "Artist B")
        self.addCleanup(a.close)
        self.addCleanup(b.close)

        song_a = a.create_song("A Song")
        asset_a = a.attach_asset(song_a.id, name="a.wav", sha256="b" * 64)
        song_b = b.create_song("B Song")

        self.assertIsNone(b.get_song(song_a.id))
        self.assertIsNone(b.get_asset(asset_a.id))
        with self.assertRaises(NotFoundError):
            b.attach_asset(song_a.id, name="cross.wav", sha256="c" * 64)
        with self.assertRaises(NotFoundError):
            b.create_version(song_b.id, label="bad", asset_ids=[asset_a.id])

    def test_cross_song_parent_and_asset_are_rejected(self):
        store = LineageStore.create(self.root, "Artist")
        self.addCleanup(store.close)
        song_a = store.create_song("A")
        asset_a = store.attach_asset(song_a.id, name="a.wav", sha256="d" * 64)
        v_a = store.create_version(song_a.id, label="A1", asset_ids=[asset_a.id])

        song_b = store.create_song("B")
        with self.assertRaises(ValidationError):
            store.create_version(song_b.id, label="bad-parent", parent_version_id=v_a.id)
        with self.assertRaises(ValidationError):
            store.create_version(song_b.id, label="bad-asset", asset_ids=[asset_a.id])

    def test_corrupt_existing_state_fails_visibly_instead_of_resetting(self):
        profile_id = "prf_" + ("1" * 32)
        profile_dir = self.root / "profiles" / profile_id
        profile_dir.mkdir(parents=True)
        db_path = profile_dir / LineageStore.DB_NAME
        db_path.write_bytes(b"not a sqlite database")

        with self.assertRaises(LineageCorruptionError):
            LineageStore.open(self.root, profile_id)

        self.assertEqual(db_path.read_bytes(), b"not a sqlite database")

    def test_copied_database_under_another_profile_id_is_rejected(self):
        original = LineageStore.create(self.root, "Artist")
        source = original.database_path
        original_profile = original.profile_id
        original.close()

        copied_profile = "prf_" + ("2" * 32)
        copied_dir = self.root / "profiles" / copied_profile
        copied_dir.mkdir(parents=True)
        shutil.copy2(source, copied_dir / LineageStore.DB_NAME)

        with self.assertRaises(LineageCorruptionError):
            LineageStore.open(self.root, copied_profile)

        reopened = LineageStore.open(self.root, original_profile)
        reopened.close()

    def test_version_and_asset_identity_rows_are_immutable(self):
        store = LineageStore.create(self.root, "Artist")
        self.addCleanup(store.close)
        song = store.create_song("Song")
        asset = store.attach_asset(song.id, name="source.wav", sha256="e" * 64)
        version = store.create_version(song.id, label="v1", asset_ids=[asset.id])

        with self.assertRaises(sqlite3.IntegrityError):
            store._conn.execute(
                "UPDATE versions SET label = 'mutated' WHERE id = ?", (version.id,)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            store._conn.execute(
                "UPDATE assets SET name = 'mutated.wav' WHERE id = ?", (asset.id,)
            )


if __name__ == "__main__":
    unittest.main()
