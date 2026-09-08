import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory, LineageCorruptionError


class Core01CActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_history_survives_restart_and_keeps_current_distinct_from_approved(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        song = hq.store.create_song("Song")
        asset = hq.store.attach_asset(song.id, name="source.wav", sha256="a" * 64)
        v1 = hq.store.create_version(song.id, label="v1", asset_ids=[asset.id])
        hq.store.approve_version(song.id, v1.id)
        checkpoint = hq.activity.checkpoint()
        v2 = hq.store.create_version(song.id, label="v2", parent_version_id=v1.id, asset_ids=[asset.id])
        hq.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        events = hq.activity.for_song(song.id, after_sequence=checkpoint)
        self.assertEqual([e.event_type for e in events], ["VERSION_CREATED", "CURRENT_VERSION_CHANGED"])
        restored = hq.store.get_song(song.id)
        self.assertEqual(restored.current_version_id, v2.id)
        self.assertEqual(restored.approved_version_id, v1.id)

    def test_song_history_does_not_leak(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        a = hq.store.create_song("A")
        b = hq.store.create_song("B")
        asset_a = hq.store.attach_asset(a.id, name="a.wav", sha256="b" * 64)
        asset_b = hq.store.attach_asset(b.id, name="b.wav", sha256="c" * 64)
        a_events = hq.activity.for_song(a.id)
        b_events = hq.activity.for_song(b.id)
        self.assertIn(asset_a.id, {e.object_id for e in a_events})
        self.assertNotIn(asset_b.id, {e.object_id for e in a_events})
        self.assertTrue(all(e.song_id == a.id for e in a_events))
        self.assertTrue(all(e.song_id == b.id for e in b_events))

    def test_evidence_conflict_reconciliation_keeps_chronological_links(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        first = hq.evidence.record_claim(
            scope_kind="SONG", scope_id=song.id, key="chorus.energy", value="needs lift", source_kind="USER_DECLARED"
        )
        second = hq.evidence.record_claim(
            scope_kind="SONG", scope_id=song.id, key="chorus.energy", value="already right", source_kind="INFERRED"
        )
        self.assertEqual(hq.evidence.resolve_for_song(song_id=song.id, key="chorus.energy").status, "CONFLICT")
        reconciled = hq.evidence.reconcile_for_song(
            song_id=song.id, key="chorus.energy", value="needs lift", source_kind="USER_DECLARED"
        )
        events = hq.activity.for_song(song.id)
        claims = [e for e in events if e.event_type == "EVIDENCE_CLAIM_RECORDED"]
        links = [e for e in events if e.event_type == "EVIDENCE_SUPERSESSION_LINKED"]
        self.assertEqual([e.object_id for e in claims[-3:]], [first.id, second.id, reconciled.id])
        self.assertEqual([e.object_id for e in links[-2:]], [reconciled.id, reconciled.id])
        self.assertIsNotNone(hq.evidence.get_claim(first.id))
        self.assertIsNotNone(hq.evidence.get_claim(second.id))

    def test_activity_is_append_only_and_not_fixed_size_truncated(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        for i in range(150):
            hq.evidence.record_claim(
                scope_kind="SONG", scope_id=song.id, key=f"note.{i}", value=i, source_kind="OBSERVED"
            )
        events = hq.activity.for_song(song.id)
        self.assertGreaterEqual(len(events), 151)
        target = events[0]
        with self.assertRaises(sqlite3.DatabaseError):
            hq.store._conn.execute("UPDATE activity_events SET event_type='X' WHERE id=?", (target.id,))
        with self.assertRaises(sqlite3.DatabaseError):
            hq.store._conn.execute("DELETE FROM activity_events WHERE id=?", (target.id,))

    def test_existing_core01ab_state_enables_activity_without_identity_loss(self):
        from n0te import EvidenceMemory, LineageStore

        store = LineageStore.create(self.root, "Artist")
        profile = store.profile_id
        artist = store.primary_artist_id
        song = store.create_song("Old Song")
        v1 = store.create_version(song.id, label="v1")
        memory = EvidenceMemory(store)
        old_claim = memory.record_claim(
            scope_kind="SONG", scope_id=song.id, key="old.fact", value="preserved", source_kind="REMEMBERED"
        )
        store.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        self.assertEqual(hq.store.primary_artist_id, artist)
        self.assertEqual(hq.store.active_song().id, song.id)
        self.assertEqual(hq.store.get_version(v1.id).id, v1.id)
        self.assertEqual(hq.evidence.get_claim(old_claim.id).value, "preserved")
        self.assertEqual([e.event_type for e in hq.activity.for_profile()], ["ACTIVITY_TRACKING_ENABLED"])
        checkpoint = hq.activity.checkpoint()
        hq.store.create_version(song.id, label="v2", parent_version_id=v1.id)
        self.assertTrue(hq.activity.for_song(song.id, after_sequence=checkpoint))

    def test_corrupt_activity_reference_fails_visibly(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        artist = hq.store.primary_artist_id
        db = hq.store.database_path
        hq.close()
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TRIGGER activity_events_immutable_update")
        conn.execute(
            "INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json) "
            "VALUES('act_bad','BROKEN',?,'song_missing',NULL,'SONG','song_missing','{}')",
            (artist,),
        )
        conn.commit(); conn.close()
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, profile)


if __name__ == "__main__":
    unittest.main()
