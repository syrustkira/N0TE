import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from n0te import HeadquartersMemory, ValidationError


class Core02ASongSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scratch_survives_restart_without_becoming_evidence(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        song = hq.store.create_song("Song")
        version = hq.store.create_version(song.id, label="v1")
        session = hq.sessions.start_session(
            song_id=song.id,
            version_id=version.id,
            objective="Find a chorus shape without polishing the mix",
        )
        rejected = hq.sessions.append_scratch(
            session.id, kind="REJECTED_IDEA", body="Double the chorus guitars"
        )
        unresolved = hq.sessions.append_scratch(
            session.id, kind="UNRESOLVED", body="Maybe the vocal needs more space"
        )
        closed = hq.sessions.close_session(
            session.id,
            debrief_summary="The smaller chorus arrangement feels more honest",
            next_action="Try a lower harmony before changing the mix",
        )
        self.assertEqual(closed.state, "CLOSED")
        self.assertEqual(
            hq.evidence.resolve_for_song(song_id=song.id, key="chorus.guitars").status,
            "UNKNOWN",
        )
        self.assertEqual(
            hq.evidence.resolve_for_song(song_id=song.id, key="vocal.space").status,
            "UNKNOWN",
        )
        hq.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        latest = hq.sessions.latest_for_song(song.id)
        self.assertEqual(latest.id, session.id)
        self.assertEqual(latest.state, "CLOSED")
        self.assertEqual(latest.next_action, "Try a lower harmony before changing the mix")
        self.assertEqual(
            [item.id for item in hq.sessions.items_for_session(session.id)],
            [rejected.id, unresolved.id],
        )
        self.assertEqual(
            hq.evidence.resolve_for_song(song_id=song.id, key="chorus.guitars").status,
            "UNKNOWN",
        )

    def test_explicit_promotion_is_idempotent_and_only_then_resolves(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Choose the chorus feel")
        item = hq.sessions.append_scratch(
            session.id, kind="DECISION", body="Keep the chorus small and tense"
        )
        self.assertEqual(
            hq.evidence.resolve_for_song(song_id=song.id, key="chorus.intent").status,
            "UNKNOWN",
        )

        first = hq.sessions.promote_item(
            item.id,
            scope_kind="SONG",
            key="chorus.intent",
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
        )
        second = hq.sessions.promote_item(
            item.id,
            scope_kind="SONG",
            key="chorus.intent",
            source_kind="USER_DECLARED",
            twin_domain="CREATIVE",
        )
        self.assertEqual(first.id, second.id)
        self.assertTrue(first.source_ref.startswith("session-promotion:"))
        resolved = hq.evidence.resolve_for_song(song_id=song.id, key="chorus.intent")
        self.assertEqual(resolved.status, "RESOLVED")
        self.assertEqual(resolved.value, "Keep the chorus small and tense")
        self.assertEqual(resolved.claim_ids, (first.id,))
        promoted_events = [
            event
            for event in hq.activity.for_song(song.id)
            if event.event_type == "SESSION_ITEM_PROMOTED"
        ]
        self.assertEqual(len(promoted_events), 1)
        self.assertEqual(promoted_events[0].object_id, first.id)

    def test_failed_claim_write_leaves_retryable_request_not_durable_doctrine(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Explore")
        item = hq.sessions.append_scratch(
            session.id, kind="OBSERVATION", body="The verse breathes at lower density"
        )

        original = hq.evidence.record_claim
        with mock.patch.object(
            hq.evidence,
            "record_claim",
            side_effect=ValidationError("simulated evidence failure"),
        ):
            with self.assertRaises(ValidationError):
                hq.sessions.promote_item(
                    item.id,
                    scope_kind="SONG",
                    key="verse.density",
                    twin_domain="CREATIVE",
                )

        self.assertIsNone(hq.sessions.promotion_for_item(item.id))
        self.assertEqual(
            hq.evidence.resolve_for_song(song_id=song.id, key="verse.density").status,
            "UNKNOWN",
        )
        request_count = hq.store._conn.execute(
            "SELECT COUNT(*) FROM session_promotion_requests WHERE item_id=?", (item.id,)
        ).fetchone()[0]
        self.assertEqual(request_count, 1)

        with mock.patch.object(hq.evidence, "record_claim", wraps=original):
            claim = hq.sessions.promote_item(
                item.id,
                scope_kind="SONG",
                key="verse.density",
                twin_domain="CREATIVE",
            )
        self.assertEqual(hq.sessions.promotion_for_item(item.id).claim_id, claim.id)
        self.assertEqual(
            hq.evidence.resolve_for_song(song_id=song.id, key="verse.density").value,
            "The verse breathes at lower density",
        )

    def test_open_session_is_unique_per_song_but_other_song_can_work(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        a = hq.store.create_song("A")
        b = hq.store.create_song("B")
        first = hq.sessions.start_session(song_id=a.id, objective="Work A")
        with self.assertRaises(ValidationError):
            hq.sessions.start_session(song_id=a.id, objective="Duplicate A")
        other = hq.sessions.start_session(song_id=b.id, objective="Work B")
        self.assertNotEqual(first.id, other.id)

    def test_session_version_binding_cannot_cross_songs(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        a = hq.store.create_song("A")
        version_a = hq.store.create_version(a.id, label="A1")
        b = hq.store.create_song("B")
        with self.assertRaises(ValidationError):
            hq.sessions.start_session(
                song_id=b.id,
                version_id=version_a.id,
                objective="Cross Song",
            )

    def test_closed_session_rejects_more_scratch_and_second_close(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Finish a pass")
        hq.sessions.close_session(
            session.id,
            debrief_summary="Pass complete",
            next_action="Listen tomorrow",
        )
        with self.assertRaises(ValidationError):
            hq.sessions.append_scratch(session.id, kind="MARK", body="late thought")
        with self.assertRaises(ValidationError):
            hq.sessions.close_session(
                session.id,
                debrief_summary="rewrite history",
                next_action="overwrite",
            )

    def test_session_reads_do_not_mutate_canonical_state(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Read purity")
        item = hq.sessions.append_scratch(session.id, kind="MARK", body="interesting bar 17")
        hq.sessions.close_session(
            session.id,
            debrief_summary="Marked the useful moment",
            next_action="Revisit bar 17",
        )
        before = hq.store._conn.total_changes
        self.assertEqual(hq.sessions.get_session(session.id).id, session.id)
        self.assertEqual(hq.sessions.latest_for_song(song.id).id, session.id)
        self.assertEqual(hq.sessions.items_for_session(session.id)[0].id, item.id)
        self.assertIsNone(hq.sessions.promotion_for_item(item.id))
        self.assertEqual(hq.store._conn.total_changes, before)

    def test_session_history_row_cannot_be_deleted(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="History must survive")
        with self.assertRaises(sqlite3.IntegrityError):
            hq.store._conn.execute("DELETE FROM sessions WHERE id=?", (session.id,))
        self.assertEqual(hq.sessions.get_session(session.id).id, session.id)


if __name__ == "__main__":
    unittest.main()
