import tempfile
import unittest
from pathlib import Path

from n0te import HeadquartersMemory
from n0te.activity_timeline import SongActivityTimeline


class SongActivityTimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_activity_is_artist_readable_newest_first_and_resolves_labels(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Glass House")
        asset = hq.store.attach_asset(song.id, name="mix 01.wav", sha256="a" * 64)
        version = hq.store.create_version(song.id, label="First mix", asset_ids=[asset.id])
        hq.store.approve_version(song.id, version.id)

        items = SongActivityTimeline(hq.store, hq.activity).for_song(song.id)
        summaries = [item.summary for item in items]
        self.assertEqual(summaries[0], "Version approved")
        self.assertIn("Version preserved", summaries)
        self.assertIn("Song material added", summaries)
        approved = next(item for item in items if item.summary == "Version approved")
        material = next(item for item in items if item.summary == "Song material added")
        self.assertEqual(approved.detail, "Version 1: First mix")
        self.assertEqual(material.detail, "mix 01.wav")
        self.assertEqual([item.sequence for item in items], sorted((item.sequence for item in items), reverse=True))

    def test_unknown_future_event_degrades_without_leaking_internal_fields(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        secret_object = "internal_object_should_not_render"
        secret_payload = "internal_payload_should_not_render"
        with hq.store._tx():
            hq.store._conn.execute(
                "INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json) "
                "VALUES('act_future','FUTURE_INTERNAL_EVENT',?,?,NULL,'FUTURE',?,?)",
                (hq.store.primary_artist_id, song.id, secret_object, '{\"secret\":\"' + secret_payload + '\"}'),
            )
        item = SongActivityTimeline(hq.store, hq.activity).for_song(song.id)[0]
        self.assertEqual(item.summary, "Activity recorded")
        self.assertIsNone(item.detail)
        rendered = f"{item.summary} {item.detail or ''}"
        self.assertNotIn("FUTURE_INTERNAL_EVENT", rendered)
        self.assertNotIn(secret_object, rendered)
        self.assertNotIn(secret_payload, rendered)

    def test_projection_is_read_only_and_restart_stable(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        profile = hq.store.profile_id
        song = hq.store.create_song("Song")
        asset = hq.store.attach_asset(song.id, name="take.wav", sha256="b" * 64)
        version = hq.store.create_version(song.id, label="Take", asset_ids=[asset.id])
        before_song = hq.store.get_song(song.id)
        before_events = hq.activity.for_song(song.id)
        first = SongActivityTimeline(hq.store, hq.activity).for_song(song.id)
        second = SongActivityTimeline(hq.store, hq.activity).for_song(song.id)
        self.assertEqual(first, second)
        self.assertEqual(hq.store.get_song(song.id), before_song)
        self.assertEqual(hq.activity.for_song(song.id), before_events)
        hq.close()

        hq = HeadquartersMemory.open(self.root, profile)
        self.addCleanup(hq.close)
        reopened = SongActivityTimeline(hq.store, hq.activity).for_song(song.id)
        self.assertEqual(reopened, first)
        self.assertEqual(hq.store.get_version(version.id).label, "Take")

    def test_song_histories_remain_isolated(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        a = hq.store.create_song("A")
        hq.store.attach_asset(a.id, name="a.wav", sha256="c" * 64)
        b = hq.store.create_song("B")
        hq.store.attach_asset(b.id, name="b.wav", sha256="d" * 64)
        timeline = SongActivityTimeline(hq.store, hq.activity)
        a_text = " ".join((item.detail or "") for item in timeline.for_song(a.id))
        b_text = " ".join((item.detail or "") for item in timeline.for_song(b.id))
        self.assertIn("a.wav", a_text)
        self.assertNotIn("b.wav", a_text)
        self.assertIn("b.wav", b_text)
        self.assertNotIn("a.wav", b_text)

    def test_projection_contains_no_timestamp_field_or_claim(self):
        hq = HeadquartersMemory.create(self.root, "Artist")
        self.addCleanup(hq.close)
        song = hq.store.create_song("Song")
        item = SongActivityTimeline(hq.store, hq.activity).for_song(song.id)[0]
        self.assertFalse(hasattr(item, "timestamp"))
        self.assertFalse(hasattr(item, "created_at"))
        self.assertNotIn("ago", item.summary.lower())
