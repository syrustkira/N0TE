import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory, LineageCorruptionError, ValidationError


class Core01EProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_explain_version_survives_restart_with_exact_derivation_references(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        song = hq.store.create_song("Song")
        asset = hq.store.attach_asset(song.id, name="take.wav", sha256="a" * 64)
        asset_prov = hq.provenance.record(
            output_kind="ASSET",
            output_id=asset.id,
            input_kind="EXTERNAL",
            input_ref="file:///recordings/take.wav",
            operation="IMPORTED",
            evidence_source_kind="OBSERVED",
            evidence_ref="import:1",
        )
        v1 = hq.store.create_version(song.id, label="v1", asset_ids=[asset.id])
        hq.provenance.record(
            output_kind="VERSION",
            output_id=v1.id,
            input_kind="ASSET",
            input_ref=asset.id,
            operation="ASSEMBLED",
            evidence_source_kind="USER_DECLARED",
        )
        v2 = hq.store.create_version(song.id, label="v2", parent_version_id=v1.id, asset_ids=[asset.id])
        transform = hq.provenance.record(
            output_kind="VERSION",
            output_id=v2.id,
            input_kind="VERSION",
            input_ref=v1.id,
            operation="TRANSFORMED",
            tool_ref="tool:owned-compressor",
            provider_ref="provider:local",
            model_ref="model:mix-assistant-v1",
            recipe_ref="recipe:chorus-lift-2",
            rights_ref="rights:unchanged-source",
            consent_ref="consent:artist-session",
            cost_ref="cost:evidence-42",
            evidence_source_kind="USER_DECLARED",
            evidence_ref="session:42",
        )
        hq.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        explanation = hq.provenance.explain_version(v2.id)
        self.assertEqual(explanation.version_id, v2.id)
        self.assertEqual(explanation.parent_version_id, v1.id)
        self.assertEqual([asset.id for asset in explanation.attached_assets], [asset.id])
        self.assertEqual(explanation.attached_assets[0].records[0].id, asset_prov.id)
        self.assertEqual([record.id for record in explanation.derivations], [transform.id])
        restored = explanation.derivations[0]
        self.assertEqual(restored.input_kind, "VERSION")
        self.assertEqual(restored.input_ref, v1.id)
        self.assertEqual(restored.tool_ref, "tool:owned-compressor")
        self.assertEqual(restored.provider_ref, "provider:local")
        self.assertEqual(restored.model_ref, "model:mix-assistant-v1")
        self.assertEqual(restored.recipe_ref, "recipe:chorus-lift-2")
        self.assertEqual(restored.rights_ref, "rights:unchanged-source")
        self.assertEqual(restored.consent_ref, "consent:artist-session")
        self.assertEqual(restored.cost_ref, "cost:evidence-42")
        self.assertEqual(restored.evidence_source_kind, "USER_DECLARED")
        self.assertEqual(restored.evidence_ref, "session:42")

    def test_cross_song_canonical_derivation_is_rejected(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song_a = hq.store.create_song("A")
        song_b = hq.store.create_song("B")
        v_a = hq.store.create_version(song_a.id, label="A1")
        v_b = hq.store.create_version(song_b.id, label="B1")
        with self.assertRaises(ValidationError):
            hq.provenance.record(
                output_kind="VERSION",
                output_id=v_b.id,
                input_kind="VERSION",
                input_ref=v_a.id,
                operation="TRANSFORMED",
                evidence_source_kind="OBSERVED",
            )

    def test_external_input_is_explicit_and_missing_metadata_stays_absent(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        asset = hq.store.attach_asset(song.id, name="reference.wav", sha256="b" * 64)
        record = hq.provenance.record(
            output_kind="ASSET",
            output_id=asset.id,
            input_kind="EXTERNAL",
            input_ref="provider-object:abc",
            operation="IMPORTED",
            evidence_source_kind="PROVIDER_VERIFIED",
            provider_ref="provider:example",
            evidence_ref="provider-receipt:abc",
        )
        self.assertEqual(record.input_kind, "EXTERNAL")
        self.assertEqual(record.input_ref, "provider-object:abc")
        self.assertIsNone(record.tool_ref)
        self.assertIsNone(record.model_ref)
        self.assertIsNone(record.recipe_ref)
        self.assertIsNone(record.rights_ref)
        self.assertIsNone(record.consent_ref)
        self.assertIsNone(record.cost_ref)

    def test_record_is_immutable_and_appends_activity_in_same_successful_write(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        v1 = hq.store.create_version(song.id, label="v1")
        before = hq.activity.checkpoint()
        record = hq.provenance.record(
            output_kind="VERSION",
            output_id=v1.id,
            input_kind="EXTERNAL",
            input_ref="session:raw-idea",
            operation="CREATED",
            evidence_source_kind="USER_DECLARED",
        )
        events = hq.activity.for_song(song.id, after_sequence=before)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "PROVENANCE_RECORDED")
        self.assertEqual(events[0].object_id, v1.id)
        self.assertEqual(events[0].payload["provenance_id"], record.id)
        with self.assertRaises(sqlite3.DatabaseError):
            hq.store._conn.execute(
                "UPDATE provenance_records SET operation='MUTATED' WHERE id=?", (record.id,)
            )
        with self.assertRaises(sqlite3.DatabaseError):
            hq.store._conn.execute("DELETE FROM provenance_records WHERE id=?", (record.id,))

    def test_explain_version_is_read_only(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        asset = hq.store.attach_asset(song.id, name="a.wav", sha256="c" * 64)
        v1 = hq.store.create_version(song.id, label="v1", asset_ids=[asset.id])
        hq.provenance.record(
            output_kind="VERSION",
            output_id=v1.id,
            input_kind="ASSET",
            input_ref=asset.id,
            operation="ASSEMBLED",
            evidence_source_kind="OBSERVED",
        )
        conn = hq.store._conn
        tables = ("metadata", "songs", "versions", "assets", "provenance_records", "activity_events")
        before = {t: int(conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]) for t in tables}
        hq.provenance.explain_version(v1.id)
        after = {t: int(conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]) for t in tables}
        self.assertEqual(before, after)

    def test_tampered_output_reference_fails_visibly_on_reopen(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        song = hq.store.create_song("Song")
        hq.store._conn.execute(
            """INSERT INTO provenance_records(
                id,song_id,output_kind,output_id,input_kind,input_ref,operation,
                evidence_source_kind
            ) VALUES('prov_bad',?,'VERSION','ver_missing','EXTERNAL','x','BROKEN','REMEMBERED')""",
            (song.id,),
        )
        hq.store._conn.commit()
        hq.close()
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, profile)


if __name__ == "__main__":
    unittest.main()
