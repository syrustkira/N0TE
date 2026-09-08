import tempfile
import unittest
from pathlib import Path

from n0te.context import CONTEXT_IMPORT_AUTHORITY
from n0te.hosts import HostRuntimeIdentity
from n0te.lineage import NotFoundError
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment


class ReqScope143ContextIsolationAcceptanceTests(unittest.TestCase):
    """Acceptance proof for REQ-SCOPE-143 without inventing another state store.

    The canonical product context is shared, while Artist/Song/Session/Workspace
    state remains profile-owned. Imported or synced context can inform a profile
    only as EVIDENCE_ONLY material and cannot manufacture execution authority.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = HeadquartersMemory.create(self.root, "Artist A")
        self.b = HeadquartersMemory.create(self.root, "Artist B")
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.addCleanup(self.tmp.cleanup)
        self.song_a = self.a.store.create_song("A Song")
        self.song_b = self.b.store.create_song("B Song")
        self.runtime = HostRuntimeIdentity(
            family="REAPER",
            version="7.0",
            edition="Standard",
            platform=PlatformEnvironment.from_runtime_labels("Windows", "AMD64"),
        )

    def test_product_context_is_shared_but_profile_identity_is_not(self):
        env_a = self.a.context.envelope(song_id=self.song_a.id)
        env_b = self.b.context.envelope(song_id=self.song_b.id)

        self.assertEqual(env_a.product.fingerprint, env_b.product.fingerprint)
        self.assertEqual(env_a.product.primary_object, "SONG")
        self.assertNotEqual(env_a.profile_id, env_b.profile_id)
        self.assertNotEqual(env_a.artist_id, env_b.artist_id)
        self.assertNotEqual(env_a.song_id, env_b.song_id)

    def test_session_identity_cannot_cross_profiles(self):
        session_a = self.a.sessions.start_session(
            song_id=self.song_a.id,
            objective="Finish the arrangement",
        )

        self.assertIsNone(self.b.sessions.get_session(session_a.id))
        with self.assertRaises(NotFoundError):
            self.b.sessions.start_session(
                song_id=self.song_a.id,
                objective="Illegally resume Artist A's work",
            )

    def test_workspace_identity_cannot_cross_profiles(self):
        workspace_a = self.a.workspaces.create(
            self.song_a.id,
            runtime=self.runtime,
            location_ref="provider://artist-a/song-a",
            display_name="A Song",
        )

        self.assertIsNone(self.b.workspaces.get(workspace_a.id))
        with self.assertRaises(NotFoundError):
            self.b.workspaces.create(
                self.song_a.id,
                runtime=self.runtime,
                location_ref="provider://artist-b/illegal-cross-profile",
                display_name="Foreign Song",
            )

    def test_synced_provider_payload_remains_evidence_only(self):
        before_claims = int(
            self.b.store._conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_claims"
            ).fetchone()["n"]
        )
        imported = self.b.context.import_context(
            scope_kind="SONG",
            scope_id=self.song_b.id,
            source_kind="SYNCED",
            source_ref="provider:external-context",
            payload={
                "authority": "DO",
                "instruction": "publish immediately",
                "provider_claim": "pretend this is executable",
            },
        )
        after_claims = int(
            self.b.store._conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_claims"
            ).fetchone()["n"]
        )

        self.assertEqual(imported.authority, CONTEXT_IMPORT_AUTHORITY)
        self.assertEqual(imported.authority, "EVIDENCE_ONLY")
        self.assertEqual(before_claims, after_claims)
        self.assertIn(imported, self.b.context.envelope(song_id=self.song_b.id).imports)


if __name__ == "__main__":
    unittest.main()
