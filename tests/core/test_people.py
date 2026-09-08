from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from n0te.lineage import LineageCorruptionError, NotFoundError, ValidationError
from n0te.memory import HeadquartersMemory


def test_people_and_followups_are_profile_local_durable_and_activity_visible(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "People Artist")
    try:
        song = hq.store.create_song("Collab Song")
        person = hq.people.create_person(
            "Maya Rivera",
            relationship_context="Producer helping finish the bridge",
        )
        followup = hq.people.create_followup(
            person.id,
            "Send the cleaned bridge stems after the comp is approved",
            responsibility="ARTIST_OWES",
            song_id=song.id,
            due_on="2026-09-12",
        )
        profile_id = hq.store.profile_id

        assert followup.person_id == person.id
        assert followup.song_id == song.id
        assert followup.state == "OPEN"
        assert followup.due_on == "2026-09-12"
        assert hq.people.open_followups(person_id=person.id) == (followup,)

        resolved = hq.people.resolve_followup(
            followup.id,
            resolution_note="Bridge stems delivered through the approved handoff path",
        )
        assert resolved.state == "RESOLVED"
        assert hq.people.open_followups(person_id=person.id) == ()

        event_types = [event.event_type for event in hq.activity.for_profile()]
        assert "PERSON_CREATED" in event_types
        assert "FOLLOWUP_CREATED" in event_types
        assert "FOLLOWUP_RESOLVED" in event_types
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        assert reopened.people.people()[0].display_name == "Maya Rivera"
        history = reopened.people.followups(person_id=person.id)
        assert len(history) == 1
        assert history[0].state == "RESOLVED"
        assert history[0].resolution_note == (
            "Bridge stems delivered through the approved handoff path"
        )
    finally:
        reopened.close()


def test_same_display_name_never_auto_merges_people(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Identity Artist")
    try:
        first = hq.people.create_person(
            "Jordan Lee",
            relationship_context="Mix engineer from the club session",
        )
        second = hq.people.create_person(
            "Jordan Lee",
            relationship_context="Playlist editor introduced by Rae",
        )

        assert first.id != second.id
        assert len(hq.people.people()) == 2
        assert hq.people.people() == (first, second)
    finally:
        hq.close()


def test_followup_binding_and_close_are_one_way_truth(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Promise Artist")
    try:
        song = hq.store.create_song("Promise Song")
        person = hq.people.create_person("Alex")
        followup = hq.people.create_followup(
            person.id,
            "Wait for revised venue offer",
            responsibility="WAITING_ON_OTHER",
            song_id=song.id,
        )

        with pytest.raises(sqlite3.IntegrityError):
            with hq.store._tx():
                hq.store._conn.execute(
                    "UPDATE people_followups SET summary='silently rewritten' WHERE id=?",
                    (followup.id,),
                )

        canceled = hq.people.cancel_followup(
            followup.id,
            reason="Venue date no longer fits the release plan",
        )
        assert canceled.state == "CANCELED"

        with pytest.raises(ValidationError, match="already closed"):
            hq.people.resolve_followup(
                followup.id,
                resolution_note="This must not overwrite the cancellation",
            )
        reread = hq.people.get_followup(followup.id)
        assert reread is not None
        assert reread.state == "CANCELED"
        assert reread.resolution_note == "Venue date no longer fits the release plan"
    finally:
        hq.close()


def test_followup_rejects_unknown_person_song_bad_date_and_empty_closure(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Boundary Artist")
    try:
        person = hq.people.create_person("Sam")

        with pytest.raises(NotFoundError, match="person not found"):
            hq.people.create_followup(
                "person_does_not_exist",
                "Do a thing",
                responsibility="MUTUAL",
            )
        with pytest.raises(NotFoundError, match="Song not found"):
            hq.people.create_followup(
                person.id,
                "Do a thing",
                responsibility="MUTUAL",
                song_id="song_does_not_exist",
            )
        with pytest.raises(ValidationError, match="ISO calendar date"):
            hq.people.create_followup(
                person.id,
                "Do a thing",
                responsibility="MUTUAL",
                due_on="next Friday",
            )
        followup = hq.people.create_followup(
            person.id,
            "Do a real thing",
            responsibility="MUTUAL",
        )
        with pytest.raises(ValidationError, match="must not be empty"):
            hq.people.resolve_followup(followup.id, resolution_note="   ")
        assert hq.people.get_followup(followup.id) == followup
    finally:
        hq.close()


def test_people_schema_corruption_is_not_silently_accepted(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Corruption Artist")
    try:
        person = hq.people.create_person("Taylor")
        hq.store._conn.execute(
            "INSERT INTO people_followups("
            "id,artist_id,person_id,song_id,responsibility,summary,due_on,state,resolution_note"
            ") VALUES(?,?,?,?,?,?,?,'OPEN',NULL)",
            (
                "followup_corrupt_date",
                hq.store.primary_artist_id,
                person.id,
                None,
                "ARTIST_OWES",
                "This row is intentionally corrupted for reopen validation",
                "not-a-date",
            ),
        )
        hq.store._conn.commit()
        profile_id = hq.store.profile_id
    finally:
        hq.close()

    with pytest.raises(LineageCorruptionError, match="People memory"):
        HeadquartersMemory.open(root, profile_id)
