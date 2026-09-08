import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import (
    CONTEXT_IMPORT_AUTHORITY,
    ContextIsolationService,
    HeadquartersMemory,
    LineageCorruptionError,
    ValidationError,
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Core01IContextIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_profile_gets_generic_product_context_and_only_own_private_context(self):
        a = HeadquartersMemory.create(self.root, "Artist A")
        profile_a = a.store.profile_id
        song_a = a.store.create_song("A Song")
        artist_claim_a = a.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=a.store.primary_artist_id,
            key="artist.identity.note",
            value="private A",
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
        )
        song_claim_a = a.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_a.id,
            key="song.intent",
            value="private A song",
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
        )
        product_fingerprint = a.context.product.fingerprint
        a_db = a.store.database_path
        a.close()
        a_hash_before = sha256_file(a_db)

        b = HeadquartersMemory.create(self.root, "Artist B")
        self.addCleanup(b.close)
        env_b = b.context.envelope()
        self.assertEqual(env_b.product.fingerprint, product_fingerprint)
        self.assertEqual(env_b.artist_name, "Artist B")
        self.assertNotEqual(env_b.profile_id, profile_a)
        self.assertNotEqual(env_b.artist_id, artist_claim_a.scope_id)
        self.assertNotIn(artist_claim_a.id, {c.id for c in env_b.artist_claims})
        self.assertNotIn(song_claim_a.id, {c.id for c in env_b.song_claims})
        self.assertEqual(sha256_file(a_db), a_hash_before)

        a = HeadquartersMemory.open(self.root, profile_a)
        self.addCleanup(a.close)
        env_a = a.context.envelope(song_id=song_a.id)
        self.assertEqual(env_a.product.fingerprint, product_fingerprint)
        self.assertIn(artist_claim_a.id, {c.id for c in env_a.artist_claims})
        self.assertIn(song_claim_a.id, {c.id for c in env_a.song_claims})

    def test_import_cannot_replace_product_artist_identity_or_evidence_authority(self):
        hq = HeadquartersMemory.create(self.root, "Artist B")
        self.addCleanup(hq.close)
        song = hq.store.create_song("B Song")
        product_before = hq.context.product
        artist_before = hq.store.artist()
        evidence_before = int(
            hq.store._conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_claims"
            ).fetchone()["n"]
        )
        imported = hq.context.import_context(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            source_kind="IMPORTED",
            source_ref="other-profile-export",
            payload={
                "artist_name": "Artist A",
                "product_context": {
                    "primary_object": "OTHER",
                    "daw_role": "PRODUCT_OWNER",
                },
                "authority": "MUTATE_EVERYTHING",
                "song_id": "song_from_someone_else",
            },
        )
        envelope = hq.context.envelope(song_id=song.id)
        self.assertEqual(imported.authority, CONTEXT_IMPORT_AUTHORITY)
        self.assertEqual(imported.authority, "EVIDENCE_ONLY")
        self.assertEqual(envelope.product, product_before)
        self.assertEqual(envelope.product.fingerprint, product_before.fingerprint)
        self.assertEqual(envelope.artist_id, artist_before.id)
        self.assertEqual(envelope.artist_name, artist_before.display_name)
        self.assertIn(imported, envelope.imports)
        self.assertEqual(
            int(
                hq.store._conn.execute(
                    "SELECT COUNT(*) AS n FROM evidence_claims"
                ).fetchone()["n"]
            ),
            evidence_before,
        )

    def test_song_import_is_scoped_and_cannot_cross_song_or_profile(self):
        a = HeadquartersMemory.create(self.root, "Artist A")
        song_a = a.store.create_song("A Song")
        b = HeadquartersMemory.create(self.root, "Artist B")
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b1 = b.store.create_song("B1")
        b2 = b.store.create_song("B2")

        imported = b.context.import_context(
            scope_kind="SONG",
            scope_id=b1.id,
            source_kind="SYNCED",
            source_ref="sync:1",
            payload={"note": "B1 only"},
        )
        self.assertIn(imported, b.context.envelope(song_id=b1.id).imports)
        self.assertNotIn(imported, b.context.envelope(song_id=b2.id).imports)
        with self.assertRaises(ValidationError):
            b.context.import_context(
                scope_kind="SONG",
                scope_id=song_a.id,
                source_kind="IMPORTED",
                source_ref="illegal-cross-profile",
                payload={"x": 1},
            )

    def test_imports_are_append_only_and_survive_restart(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        imported = hq.context.import_context(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            source_kind="IMPORTED",
            source_ref="notes.json",
            payload={"ideas": ["one", "two"]},
        )
        with self.assertRaises(sqlite3.IntegrityError):
            hq.store._conn.execute(
                "UPDATE context_imports SET source_ref='changed' WHERE id=?",
                (imported.id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            hq.store._conn.execute(
                "DELETE FROM context_imports WHERE id=?", (imported.id,)
            )
        hq.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        restored = hq.context.imports_for()
        self.assertEqual(restored, (imported,))

    def test_context_envelope_reads_are_pure(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key="artist.note",
            value="mine",
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
        )
        hq.context.import_context(
            scope_kind="SONG",
            scope_id=song.id,
            source_kind="IMPORTED",
            source_ref="ref",
            payload={"external": True},
        )
        conn = hq.store._conn
        tables = (
            "metadata",
            "songs",
            "versions",
            "assets",
            "evidence_claims",
            "evidence_supersessions",
            "activity_events",
            "provenance_records",
            "context_imports",
        )
        before = {
            table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in tables
        }
        changes = conn.total_changes
        first = hq.context.envelope(song_id=song.id)
        second = hq.context.envelope(song_id=song.id)
        after = {
            table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in tables
        }
        self.assertEqual(first, second)
        self.assertEqual(before, after)
        self.assertEqual(changes, conn.total_changes)

    def test_tampered_import_authority_fails_visibly(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        conn = hq.store._conn
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "INSERT INTO context_imports("
            "id,scope_kind,scope_id,source_kind,source_ref,payload_json,authority"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                "ctximp_" + "9" * 32,
                "ARTIST",
                hq.store.primary_artist_id,
                "IMPORTED",
                "tampered",
                "{}",
                "MUTATE",
            ),
        )
        conn.commit()
        conn.execute("PRAGMA ignore_check_constraints=OFF")
        with self.assertRaises(LineageCorruptionError):
            ContextIsolationService(hq.store, hq.evidence)

    def test_payload_must_be_json_and_import_source_is_bounded(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        with self.assertRaises(ValidationError):
            hq.context.import_context(
                scope_kind="ARTIST",
                scope_id=hq.store.primary_artist_id,
                source_kind="TRUST_ME",
                source_ref="x",
                payload={},
            )
        with self.assertRaises(ValidationError):
            hq.context.import_context(
                scope_kind="ARTIST",
                scope_id=hq.store.primary_artist_id,
                source_kind="IMPORTED",
                source_ref="x",
                payload={"not_json": {1, 2, 3}},
            )


if __name__ == "__main__":
    unittest.main()
