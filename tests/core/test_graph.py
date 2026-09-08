import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory, LineageCorruptionError, NotFoundError


class Core01GSongKnowledgeMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.song_a = self.hq.store.create_song("A")
        self.asset_a = self.hq.store.attach_asset(
            self.song_a.id, name="a.wav", sha256="a" * 64, source_uri="file:///a.wav"
        )
        self.v1 = self.hq.store.create_version(
            self.song_a.id, label="v1", asset_ids=[self.asset_a.id]
        )
        self.hq.store.approve_version(self.song_a.id, self.v1.id)
        self.v2 = self.hq.store.create_version(
            self.song_a.id,
            label="v2",
            parent_version_id=self.v1.id,
            asset_ids=[self.asset_a.id],
        )
        self.old = self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=self.song_a.id,
            key="energy",
            value="low",
            source_kind="OBSERVED",
        )
        self.other = self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=self.song_a.id,
            key="energy",
            value="high",
            source_kind="INFERRED",
            confidence=0.5,
        )
        self.resolved = self.hq.evidence.reconcile_for_song(
            song_id=self.song_a.id,
            key="energy",
            value="high",
            source_kind="USER_DECLARED",
        )
        self.external_prov = self.hq.provenance.record(
            output_kind="ASSET",
            output_id=self.asset_a.id,
            input_kind="EXTERNAL",
            input_ref="file:///imports/source.wav",
            operation="IMPORTED",
            evidence_source_kind="OBSERVED",
        )
        self.version_prov = self.hq.provenance.record(
            output_kind="VERSION",
            output_id=self.v2.id,
            input_kind="VERSION",
            input_ref=self.v1.id,
            operation="TRANSFORMED",
            evidence_source_kind="USER_DECLARED",
        )

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_current_approved_parent_and_asset_edges_are_exact(self):
        graph = self.hq.knowledge.for_song(self.song_a.id)
        self.assertEqual(
            [edge.target_id for edge in graph.edges_of_kind("CURRENT_VERSION")],
            [self.v2.id],
        )
        self.assertEqual(
            [edge.target_id for edge in graph.edges_of_kind("APPROVED_VERSION")],
            [self.v1.id],
        )
        self.assertIn(
            ("VERSION_PARENT", self.v2.id, self.v1.id),
            {(edge.kind, edge.source_id, edge.target_id) for edge in graph.edges},
        )
        self.assertIn(
            ("VERSION_USES_ASSET", self.v2.id, self.asset_a.id),
            {(edge.kind, edge.source_id, edge.target_id) for edge in graph.edges},
        )

    def test_superseded_evidence_remains_history_with_explicit_edges(self):
        graph = self.hq.knowledge.for_song(self.song_a.id)
        for claim in (self.old, self.other, self.resolved):
            self.assertIsNotNone(graph.node("EVIDENCE_CLAIM", claim.id))
        self.assertTrue(graph.node("EVIDENCE_CLAIM", self.old.id).data["superseded"])
        self.assertTrue(graph.node("EVIDENCE_CLAIM", self.other.id).data["superseded"])
        self.assertFalse(graph.node("EVIDENCE_CLAIM", self.resolved.id).data["superseded"])
        self.assertEqual(
            {
                (edge.source_id, edge.target_id)
                for edge in graph.edges_of_kind("EVIDENCE_SUPERSEDES")
                if edge.source_id == self.resolved.id
            },
            {(self.resolved.id, self.old.id), (self.resolved.id, self.other.id)},
        )

    def test_external_provenance_is_explicit_external_ref_not_canonical_object(self):
        graph = self.hq.knowledge.for_song(self.song_a.id)
        external_nodes = [node for node in graph.nodes if node.kind == "EXTERNAL_REF"]
        self.assertEqual(len(external_nodes), 1)
        self.assertEqual(external_nodes[0].data["ref"], "file:///imports/source.wav")
        edge = next(
            edge
            for edge in graph.edges_of_kind("DERIVED_FROM")
            if edge.source_id == self.external_prov.id
        )
        self.assertEqual(edge.target_kind, "EXTERNAL_REF")
        self.assertEqual(edge.target_id, external_nodes[0].id)
        self.assertIsNone(graph.node("ASSET", external_nodes[0].id))
        self.assertIsNone(graph.node("VERSION", external_nodes[0].id))

    def test_canonical_provenance_links_to_same_song_input(self):
        graph = self.hq.knowledge.for_song(self.song_a.id)
        self.assertIn(
            ("PROVENANCE_DESCRIBES", self.version_prov.id, self.v2.id),
            {(edge.kind, edge.source_id, edge.target_id) for edge in graph.edges},
        )
        self.assertIn(
            ("DERIVED_FROM", self.version_prov.id, self.v1.id),
            {(edge.kind, edge.source_id, edge.target_id) for edge in graph.edges},
        )

    def test_other_song_objects_and_history_are_absent(self):
        song_b = self.hq.store.create_song("B")
        asset_b = self.hq.store.attach_asset(song_b.id, name="b.wav", sha256="b" * 64)
        vb = self.hq.store.create_version(song_b.id, label="b1", asset_ids=[asset_b.id])
        claim_b = self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_b.id,
            key="only.b",
            value=True,
            source_kind="USER_DECLARED",
        )
        prov_b = self.hq.provenance.record(
            output_kind="VERSION",
            output_id=vb.id,
            input_kind="EXTERNAL",
            input_ref="file:///b.wav",
            operation="IMPORTED",
            evidence_source_kind="OBSERVED",
        )
        graph = self.hq.knowledge.for_song(self.song_a.id)
        forbidden = {song_b.id, asset_b.id, vb.id, claim_b.id, prov_b.id}
        self.assertFalse(any(node.id in forbidden for node in graph.nodes))
        self.assertFalse(
            any(edge.source_id in forbidden or edge.target_id in forbidden for edge in graph.edges)
        )

    def test_repeated_restart_reads_are_deterministic_and_write_zero_rows(self):
        profile = self.hq.store.profile_id
        expected = self.hq.knowledge.for_song(self.song_a.id)
        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, profile)
        conn = self.hq.store._conn
        tables = (
            "metadata",
            "songs",
            "versions",
            "assets",
            "version_assets",
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
        first = self.hq.knowledge.for_song(self.song_a.id)
        second = self.hq.knowledge.for_song(self.song_a.id)
        after = {
            table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in tables
        }
        self.assertEqual(expected, first)
        self.assertEqual(first, second)
        self.assertEqual(before, after)
        self.assertEqual(changes, conn.total_changes)
        identities = [(node.kind, node.id) for node in first.nodes]
        self.assertEqual(len(identities), len(set(identities)))
        edge_ids = [edge.identity for edge in first.edges]
        self.assertEqual(len(edge_ids), len(set(edge_ids)))

    def test_missing_song_is_a_bounded_not_found(self):
        with self.assertRaises(NotFoundError):
            self.hq.knowledge.for_song("song_" + "9" * 32)

    def test_invalid_activity_target_fails_visibly_instead_of_being_omitted(self):
        self.hq.store._conn.execute(
            """INSERT INTO activity_events(
                id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
            ) VALUES(?,?,?,?,NULL,'VERSION',?,'{}')""",
            (
                "act_" + "9" * 32,
                "TAMPERED",
                self.hq.store.primary_artist_id,
                self.song_a.id,
                "ver_" + "9" * 32,
            ),
        )
        self.hq.store._conn.commit()
        with self.assertRaises(LineageCorruptionError):
            self.hq.knowledge.for_song(self.song_a.id)


if __name__ == "__main__":
    unittest.main()
