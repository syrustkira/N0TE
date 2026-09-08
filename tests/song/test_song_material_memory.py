import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from n0te import HeadquartersMemory, NotFoundError
from n0te.material import MAX_MATERIAL_BYTES, SongMaterialError


class SongMaterialMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.profile_id = self.hq.store.profile_id
        self.song = self.hq.store.create_song("Material Song")

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    def ingest(self, payload=b"demo-audio-bytes", filename="demo.wav"):
        return self.hq.materials.ingest_stream(
            self.song.id,
            filename=filename,
            stream=io.BytesIO(payload),
            declared_size=len(payload),
        )

    def test_ingest_preserves_bytes_and_creates_current_asset_version_atomically(self):
        checkpoint = self.hq.activity.checkpoint()
        result = self.ingest()
        digest = hashlib.sha256(b"demo-audio-bytes").hexdigest()
        self.assertEqual(result.material.sha256, digest)
        self.assertEqual(result.material.path.read_bytes(), b"demo-audio-bytes")
        self.assertEqual(result.asset.name, "demo.wav")
        self.assertEqual(result.asset.sha256, digest)
        self.assertEqual(result.asset.source_uri, result.material.source_uri)
        song = self.hq.store.get_song(self.song.id)
        self.assertEqual(song.current_version_id, result.version.id)
        self.assertIsNone(song.approved_version_id)
        self.assertIsNone(result.version.parent_version_id)
        self.assertEqual(
            self.hq.store.version_asset_ids(result.version.id),
            (result.asset.id,),
        )
        events = [
            event.event_type
            for event in self.hq.activity.for_song(
                self.song.id, after_sequence=checkpoint
            )
        ]
        self.assertEqual(
            events,
            ["ASSET_ATTACHED", "VERSION_CREATED", "CURRENT_VERSION_CHANGED"],
        )

    def test_new_import_parents_current_version_without_changing_approved(self):
        first = self.ingest(b"first", "first.wav")
        self.hq.store.approve_version(self.song.id, first.version.id)
        second = self.ingest(b"second", "second.wav")
        state = self.hq.store.get_song(self.song.id)
        self.assertEqual(second.version.parent_version_id, first.version.id)
        self.assertEqual(state.current_version_id, second.version.id)
        self.assertEqual(state.approved_version_id, first.version.id)

    def test_identical_bytes_reuse_blob_but_keep_explicit_asset_and_version_lineage(self):
        first = self.ingest(b"same", "mix-a.wav")
        second = self.ingest(b"same", "mix-b.wav")
        self.assertEqual(first.material.path, second.material.path)
        self.assertNotEqual(first.asset.id, second.asset.id)
        self.assertNotEqual(first.version.id, second.version.id)
        self.assertEqual(second.version.parent_version_id, first.version.id)
        blobs = list(self.hq.materials.blobs_dir.rglob("*.blob"))
        self.assertEqual(blobs, [first.material.path])

    def test_browser_style_path_is_only_a_safe_display_basename(self):
        result = self.ingest(b"payload", r"C:\\fakepath\\folder\\demo mix.wav")
        self.assertEqual(result.asset.name, "demo mix.wav")
        self.assertNotIn("fakepath", str(result.material.path))
        self.assertNotIn("demo mix.wav", str(result.material.path))

    def test_declared_size_mismatch_and_oversize_fail_without_lineage(self):
        with self.assertRaises(SongMaterialError):
            self.hq.materials.ingest_stream(
                self.song.id,
                filename="bad.wav",
                stream=io.BytesIO(b"abc"),
                declared_size=4,
            )
        with self.assertRaises(SongMaterialError):
            self.hq.materials.ingest_stream(
                self.song.id,
                filename="too-big.wav",
                stream=io.BytesIO(b"x"),
                declared_size=MAX_MATERIAL_BYTES + 1,
            )
        state = self.hq.store.get_song(self.song.id)
        self.assertIsNone(state.current_version_id)
        self.assertEqual(
            int(self.hq.store._conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]),
            0,
        )
        self.assertEqual(
            int(self.hq.store._conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"]),
            0,
        )
        if self.hq.materials.staging_dir.exists():
            self.assertEqual(list(self.hq.materials.staging_dir.iterdir()), [])
        if self.hq.materials.blobs_dir.exists():
            self.assertEqual(list(self.hq.materials.blobs_dir.rglob("*.blob")), [])

    def test_lineage_failure_can_leave_only_safe_unreferenced_blob(self):
        payload = b"durable-before-lineage"
        digest = hashlib.sha256(payload).hexdigest()
        with patch.object(
            self.hq.materials,
            "_commit_asset_version",
            side_effect=RuntimeError("simulated lineage failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.ingest(payload, "failure.wav")
        self.assertEqual(
            int(self.hq.store._conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]),
            0,
        )
        self.assertEqual(
            int(self.hq.store._conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"]),
            0,
        )
        blob = self.hq.materials._blob_path(digest)
        self.assertEqual(blob.read_bytes(), payload)

    def test_sql_failure_after_asset_insert_rolls_back_asset_and_version_together(self):
        payload = b"transaction-boundary"
        digest = hashlib.sha256(payload).hexdigest()
        self.hq.store._conn.execute(
            """CREATE TRIGGER test_reject_material_version
            BEFORE INSERT ON versions
            BEGIN SELECT RAISE(ABORT, 'simulated version failure'); END"""
        )
        self.hq.store._conn.commit()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.ingest(payload, "transaction.wav")
        finally:
            self.hq.store._conn.execute("DROP TRIGGER test_reject_material_version")
            self.hq.store._conn.commit()
        self.assertEqual(
            int(self.hq.store._conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]),
            0,
        )
        self.assertEqual(
            int(self.hq.store._conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"]),
            0,
        )
        self.assertIsNone(self.hq.store.get_song(self.song.id).current_version_id)
        self.assertEqual(self.hq.materials._blob_path(digest).read_bytes(), payload)

    def test_tampered_blob_is_reported_and_never_silently_replaced(self):
        first = self.ingest(b"original", "mix.wav")
        first.material.path.write_bytes(b"tampered")
        view = self.hq.materials.view_asset(first.asset)
        self.assertEqual(view.status, "INTEGRITY_ERROR")
        with self.assertRaises(SongMaterialError):
            self.ingest(b"original", "retry.wav")
        self.assertEqual(first.material.path.read_bytes(), b"tampered")
        self.assertEqual(
            int(self.hq.store._conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]),
            1,
        )
        self.assertEqual(
            int(self.hq.store._conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"]),
            1,
        )

    def test_profile_isolation_rejects_foreign_asset_even_under_shared_root(self):
        other = HeadquartersMemory.create(self.root, "Other Artist")
        self.addCleanup(other.close)
        other_song = other.store.create_song("Other Song")
        foreign = other.materials.ingest_stream(
            other_song.id,
            filename="foreign.wav",
            stream=io.BytesIO(b"foreign"),
            declared_size=7,
        )
        with self.assertRaises(NotFoundError):
            self.hq.materials.resolve_asset(foreign.asset)

    def test_reopen_reverifies_the_same_managed_material(self):
        imported = self.ingest(b"restart-me", "restart.wav")
        version_id = imported.version.id
        asset_id = imported.asset.id
        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, self.profile_id)
        song = self.hq.store.get_song(self.song.id)
        self.assertEqual(song.current_version_id, version_id)
        asset = self.hq.store.get_asset(asset_id)
        material = self.hq.materials.resolve_asset(asset)
        self.assertEqual(material.path.read_bytes(), b"restart-me")
        views = self.hq.materials.version_materials(version_id)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].status, "VERIFIED_MANAGED")


if __name__ == "__main__":
    unittest.main()
