import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import (
    EvidenceMemory,
    HeadquartersMemory,
    LineageCorruptionError,
    LineageStore,
    TwinEvidenceService,
    ValidationError,
)


V1_EVIDENCE_SCHEMA = """
CREATE TABLE evidence_claims (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('PROFILE','ARTIST','SONG','VERSION')),
    scope_id TEXT NOT NULL,
    key TEXT NOT NULL CHECK(length(trim(key)) > 0),
    value_json TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('USER_DECLARED','OBSERVED','MEASURED','PROVIDER_VERIFIED','REMEMBERED','INFERRED')),
    source_ref TEXT NULL,
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0)
);
CREATE TABLE evidence_supersessions (
    new_claim_id TEXT NOT NULL REFERENCES evidence_claims(id),
    old_claim_id TEXT NOT NULL REFERENCES evidence_claims(id),
    PRIMARY KEY(new_claim_id, old_claim_id),
    CHECK(new_claim_id <> old_claim_id)
);
CREATE INDEX evidence_claim_lookup
ON evidence_claims(scope_kind, scope_id, key, seq);
CREATE INDEX evidence_superseded_lookup
ON evidence_supersessions(old_claim_id);
CREATE TRIGGER evidence_claims_immutable_update
BEFORE UPDATE ON evidence_claims BEGIN
    SELECT RAISE(ABORT, 'evidence claims are immutable');
END;
CREATE TRIGGER evidence_claims_immutable_delete
BEFORE DELETE ON evidence_claims BEGIN
    SELECT RAISE(ABORT, 'evidence claims are immutable');
END;
CREATE TRIGGER evidence_supersessions_immutable_update
BEFORE UPDATE ON evidence_supersessions BEGIN
    SELECT RAISE(ABORT, 'evidence supersession is immutable');
END;
CREATE TRIGGER evidence_supersessions_immutable_delete
BEFORE DELETE ON evidence_supersessions BEGIN
    SELECT RAISE(ABORT, 'evidence supersession is immutable');
END;
CREATE TRIGGER evidence_supersession_same_target
BEFORE INSERT ON evidence_supersessions
WHEN NOT EXISTS (
    SELECT 1
    FROM evidence_claims newer
    JOIN evidence_claims older ON older.id = NEW.old_claim_id
    WHERE newer.id = NEW.new_claim_id
      AND newer.scope_kind = older.scope_kind
      AND newer.scope_id = older.scope_id
      AND newer.key = older.key
      AND newer.seq > older.seq
)
BEGIN
    SELECT RAISE(ABORT, 'supersession must target an older claim with identical scope and key');
END;
INSERT INTO metadata(key, value) VALUES('evidence_schema_version', '1');
"""


class Core01HTwinEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_v1_claims_migrate_to_unspecified_without_semantic_loss(self):
        store = LineageStore.create(self.root, "Artist")
        song = store.create_song("Song")
        conn = store._conn
        conn.executescript(V1_EVIDENCE_SCHEMA)
        conn.execute(
            "INSERT INTO evidence_claims("
            "id,scope_kind,scope_id,key,value_json,source_kind,source_ref,confidence"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                "claim_" + "1" * 32,
                "SONG",
                song.id,
                "creative.note",
                '"keep the rough edge"',
                "USER_DECLARED",
                "legacy:note",
                0.83,
            ),
        )
        conn.execute(
            "INSERT INTO evidence_claims("
            "id,scope_kind,scope_id,key,value_json,source_kind,source_ref,confidence"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                "claim_" + "2" * 32,
                "SONG",
                song.id,
                "creative.note",
                '"keep it human"',
                "USER_DECLARED",
                "legacy:decision",
                1.0,
            ),
        )
        conn.execute(
            "INSERT INTO evidence_supersessions(new_claim_id,old_claim_id) VALUES(?,?)",
            ("claim_" + "2" * 32, "claim_" + "1" * 32),
        )
        conn.commit()

        memory = EvidenceMemory(store)
        self.assertEqual(
            conn.execute(
                "SELECT value FROM metadata WHERE key='evidence_schema_version'"
            ).fetchone()[0],
            "2",
        )
        first = memory.get_claim("claim_" + "1" * 32)
        second = memory.get_claim("claim_" + "2" * 32)
        self.assertEqual(first.twin_domain, "UNSPECIFIED")
        self.assertEqual(second.twin_domain, "UNSPECIFIED")
        self.assertEqual(first.value, "keep the rough edge")
        self.assertEqual(first.source_ref, "legacy:note")
        self.assertAlmostEqual(first.confidence, 0.83)
        self.assertEqual(
            memory.active_claims("SONG", song.id, "creative.note"),
            (second,),
        )
        store.close()

    def test_source_kind_and_twin_domain_are_orthogonal(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        declared_technical = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="routing.expected",
            value="stereo bus",
            source_kind="USER_DECLARED",
            twin_domain="TECHNICAL",
        )
        measured_creative = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="texture.intent",
            value="abrasive",
            source_kind="MEASURED",
            twin_domain="CREATIVE",
        )
        self.assertEqual(declared_technical.twin_domain, "TECHNICAL")
        self.assertEqual(declared_technical.source_kind, "USER_DECLARED")
        self.assertEqual(measured_creative.twin_domain, "CREATIVE")
        self.assertEqual(measured_creative.source_kind, "MEASURED")

    def test_version_technical_state_can_conflict_with_song_creative_intent_without_winner(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        version = hq.store.create_version(song.id, label="v1")
        technical = hq.evidence.record_claim(
            scope_kind="VERSION",
            scope_id=version.id,
            key="timing.feel",
            value="off-grid",
            source_kind="MEASURED",
            source_ref="analysis:timing",
            twin_domain="TECHNICAL",
        )
        creative = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="timing.feel",
            value="intentional push",
            source_kind="USER_DECLARED",
            source_ref="artist:intent",
            twin_domain="CREATIVE",
        )
        view = hq.twins.for_song(song_id=song.id, version_id=version.id)
        self.assertIn(technical, view.technical_claims)
        self.assertIn(creative, view.creative_claims)
        self.assertEqual(len(view.conflicts), 1)
        self.assertEqual(view.conflicts[0].key, "timing.feel")
        self.assertEqual(
            set(view.conflicts[0].claim_ids), {technical.id, creative.id}
        )
        self.assertIsNone(
            getattr(view.conflicts[0], "winner", None),
            "Twin conflict must not silently choose a winner",
        )

    def test_equal_cross_lens_values_do_not_manufacture_conflict(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="tempo.target",
            value=120,
            source_kind="MEASURED",
            twin_domain="TECHNICAL",
        )
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="tempo.target",
            value=120,
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
        )
        view = hq.twins.for_song(song_id=song.id)
        self.assertFalse(any(item.key == "tempo.target" for item in view.conflicts))

    def test_song_twin_evidence_never_leaks_to_another_song(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song_a = hq.store.create_song("A")
        song_b = hq.store.create_song("B")
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_a.id,
            key="chorus.intent",
            value="small and tense",
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
        )
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_a.id,
            key="chorus.level",
            value=-13.2,
            source_kind="MEASURED",
            twin_domain="TECHNICAL",
        )
        view_b = hq.twins.for_song(song_id=song_b.id)
        keys = {claim.key for claim in view_b.technical_claims + view_b.creative_claims}
        self.assertNotIn("chorus.intent", keys)
        self.assertNotIn("chorus.level", keys)

    def test_more_specific_scope_is_selected_independently_per_twin(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        version = hq.store.create_version(song.id, label="v1")
        song_technical = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="energy",
            value="moderate",
            source_kind="OBSERVED",
            twin_domain="TECHNICAL",
        )
        version_technical = hq.evidence.record_claim(
            scope_kind="VERSION",
            scope_id=version.id,
            key="energy",
            value="low",
            source_kind="MEASURED",
            twin_domain="TECHNICAL",
        )
        song_creative = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="energy",
            value="restrained",
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
        )
        view = hq.twins.for_song(song_id=song.id, version_id=version.id)
        energy_technical = [c for c in view.technical_claims if c.key == "energy"]
        energy_creative = [c for c in view.creative_claims if c.key == "energy"]
        self.assertEqual(energy_technical, [version_technical])
        self.assertNotIn(song_technical, energy_technical)
        self.assertEqual(energy_creative, [song_creative])

    def test_restart_preserves_domain_and_headquarters_graph_exposes_it(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        song = hq.store.create_song("Song")
        claim = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="mix.width",
            value="wide",
            source_kind="MEASURED",
            twin_domain="TECHNICAL",
        )
        hq.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        restored = hq.evidence.get_claim(claim.id)
        self.assertEqual(restored.twin_domain, "TECHNICAL")
        graph_claim = hq.knowledge.for_song(song.id).node("EVIDENCE_CLAIM", claim.id)
        self.assertIsNotNone(graph_claim)
        self.assertEqual(graph_claim.data["twin_domain"], "TECHNICAL")

    def test_twin_reads_are_pure_after_schema_initialization(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        version = hq.store.create_version(song.id, label="v1")
        hq.evidence.record_claim(
            scope_kind="VERSION",
            scope_id=version.id,
            key="level",
            value=-12.0,
            source_kind="MEASURED",
            twin_domain="TECHNICAL",
        )
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="level",
            value="quiet on purpose",
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
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
        )
        before = {
            table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in tables
        }
        changes = conn.total_changes
        first = hq.twins.for_song(song_id=song.id, version_id=version.id)
        second = hq.twins.for_song(song_id=song.id, version_id=version.id)
        after = {
            table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in tables
        }
        self.assertEqual(first, second)
        self.assertEqual(before, after)
        self.assertEqual(changes, conn.total_changes)

    def test_invalid_domain_is_rejected_and_tampered_domain_fails_validation(self):
        store = LineageStore.create(self.root, "Artist")
        song = store.create_song("Song")
        memory = EvidenceMemory(store)
        with self.assertRaises(ValidationError):
            memory.record_claim(
                scope_kind="SONG",
                scope_id=song.id,
                key="bad",
                value=True,
                source_kind="OBSERVED",
                twin_domain="MIXED",
            )

        conn = store._conn
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "INSERT INTO evidence_claims("
            "id,scope_kind,scope_id,key,value_json,source_kind,source_ref,confidence,twin_domain"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "claim_" + "9" * 32,
                "SONG",
                song.id,
                "tampered",
                "true",
                "OBSERVED",
                None,
                1.0,
                "MIXED",
            ),
        )
        conn.commit()
        conn.execute("PRAGMA ignore_check_constraints=OFF")
        with self.assertRaises(LineageCorruptionError):
            EvidenceMemory(store)
        store.close()


if __name__ == "__main__":
    unittest.main()
