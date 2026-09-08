import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory, NotFoundError, SongResumeService


class Core01DSongResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def counts(hq):
        conn = hq.store._conn
        tables = [
            "metadata",
            "songs",
            "versions",
            "assets",
            "evidence_claims",
            "evidence_supersessions",
            "activity_events",
        ]
        return {
            table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in tables
        }

    def test_resume_after_restart_is_truthful_scoped_and_read_only(self):
        hq = HeadquartersMemory.create(self.root, "Artist One")
        profile = hq.store.profile_id
        song_a = hq.store.create_song("Song A")
        song_b = hq.store.create_song("Song B")
        asset = hq.store.attach_asset(song_a.id, name="demo.wav", sha256="a" * 64)
        v1 = hq.store.create_version(song_a.id, label="v1", asset_ids=[asset.id])
        hq.store.approve_version(song_a.id, v1.id)
        v2 = hq.store.create_version(song_a.id, label="v2", parent_version_id=v1.id, asset_ids=[asset.id])

        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key="next.action",
            value="review arrangement",
            source_kind="REMEMBERED",
        )
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_a.id,
            key="next.action",
            value="record bridge",
            source_kind="USER_DECLARED",
        )
        next_claim = hq.evidence.record_claim(
            scope_kind="VERSION",
            scope_id=v2.id,
            key="next.action",
            value="tighten chorus",
            source_kind="USER_DECLARED",
            source_ref="session-end",
        )
        first = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_a.id,
            key="chorus.energy",
            value="needs lift",
            source_kind="USER_DECLARED",
        )
        second = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_a.id,
            key="chorus.energy",
            value="already right",
            source_kind="INFERRED",
            source_ref="analysis:1",
            confidence=0.55,
        )
        hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_b.id,
            key="chorus.energy",
            value="other song only",
            source_kind="USER_DECLARED",
        )
        hq.store.select_song(song_a.id)
        hq.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        before = self.counts(hq)
        brief = SongResumeService(hq).brief(recent_limit=50)
        after = self.counts(hq)

        self.assertEqual(before, after)
        self.assertEqual(brief.artist_name, "Artist One")
        self.assertEqual(brief.song_id, song_a.id)
        self.assertEqual(brief.song_title, "Song A")
        self.assertTrue(brief.is_active_song)
        self.assertEqual(brief.current_version.id, v2.id)
        self.assertEqual(brief.approved_version.id, v1.id)
        self.assertNotEqual(brief.current_version.id, brief.approved_version.id)
        self.assertEqual(brief.next_action_status, "RESOLVED")
        self.assertEqual(brief.next_action, "tighten chorus")
        self.assertEqual(brief.next_action_evidence[0].claim_id, next_claim.id)
        self.assertEqual(brief.next_action_evidence[0].source_ref, "session-end")
        conflict = next(c for c in brief.unresolved_conflicts if c.key == "chorus.energy")
        self.assertEqual(conflict.scope_kind, "SONG")
        self.assertEqual({e.claim_id for e in conflict.evidence}, {first.id, second.id})
        self.assertEqual({e.source_kind for e in conflict.evidence}, {"USER_DECLARED", "INFERRED"})
        self.assertTrue(all(change.sequence > 0 for change in brief.recent_changes))
        self.assertEqual(
            [change.sequence for change in brief.recent_changes],
            sorted(change.sequence for change in brief.recent_changes),
        )
        self.assertNotIn(song_b.id, {change.object_id for change in brief.recent_changes})

    def test_next_action_is_unknown_when_not_explicitly_represented(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        hq.store.create_version(song.id, label="v1")
        brief = SongResumeService(hq).brief(song.id)
        self.assertEqual(brief.next_action_status, "UNKNOWN")
        self.assertIsNone(brief.next_action)
        self.assertEqual(brief.next_action_evidence, ())

    def test_conflicting_next_action_is_not_silently_selected(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        hq.evidence.record_claim(
            scope_kind="SONG", scope_id=song.id, key="next.action", value="mix", source_kind="USER_DECLARED"
        )
        hq.evidence.record_claim(
            scope_kind="SONG", scope_id=song.id, key="next.action", value="rewrite", source_kind="INFERRED"
        )
        brief = SongResumeService(hq).brief(song.id)
        self.assertEqual(brief.next_action_status, "CONFLICT")
        self.assertIsNone(brief.next_action)
        self.assertIn("next.action", {conflict.key for conflict in brief.unresolved_conflicts})

    def test_recent_limit_returns_latest_changes_in_chronological_order(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        for i in range(5):
            hq.evidence.record_claim(
                scope_kind="SONG",
                scope_id=song.id,
                key=f"note.{i}",
                value=i,
                source_kind="OBSERVED",
            )
        all_changes = SongResumeService(hq).brief(song.id, recent_limit=100).recent_changes
        limited = SongResumeService(hq).brief(song.id, recent_limit=3).recent_changes
        self.assertEqual(limited, all_changes[-3:])
        self.assertEqual([c.sequence for c in limited], sorted(c.sequence for c in limited))

    def test_missing_song_fails_visibly(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        with self.assertRaises(NotFoundError):
            SongResumeService(hq).brief("song_" + "9" * 32)


if __name__ == "__main__":
    unittest.main()
