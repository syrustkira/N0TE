from __future__ import annotations

import sqlite3

import pytest

from n0te.activity import ActivityLog
from n0te.attention import FOCUS_MODES
from n0te.evidence import EvidenceMemory
from n0te.lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from n0te.memory import HeadquartersMemory


def test_focus_modes_are_exact_and_invalid_mode_never_persists(tmp_path) -> None:
    with HeadquartersMemory.create(tmp_path.resolve(), "Mode Artist") as headquarters:
        assert FOCUS_MODES == {"MAKE", "FINISH", "MANAGE", "RELEASE", "PERFORM"}
        for mode in sorted(FOCUS_MODES):
            current = headquarters.attention.start_focus(mode)
            assert current.mode == mode
            assert current.state == "ACTIVE"
        before = headquarters.attention.history()
        with pytest.raises(ValidationError):
            headquarters.attention.start_focus("admin")
        assert headquarters.attention.history() == before


def test_focus_switch_is_transactional_idempotent_and_preserves_history(tmp_path) -> None:
    with HeadquartersMemory.create(tmp_path.resolve(), "Switch Artist") as headquarters:
        song = headquarters.store.create_song("Bound Song")
        first = headquarters.attention.start_focus("MAKE", song_id=song.id)
        repeated = headquarters.attention.start_focus("make", song_id=song.id)
        assert repeated == first
        assert len(headquarters.attention.history()) == 1

        second = headquarters.attention.start_focus("FINISH", song_id=song.id)
        history = headquarters.attention.history()
        assert len(history) == 2
        assert history[0].id == first.id
        assert history[0].state == "ENDED"
        assert history[0].end_reason == "SWITCHED"
        assert history[1] == second
        assert second.state == "ACTIVE"
        assert second.end_reason is None
        assert headquarters.attention.active_focus() == second


def test_end_focus_is_explicit_idempotent_and_history_survives_reopen(tmp_path) -> None:
    root = tmp_path.resolve()
    headquarters = HeadquartersMemory.create(root, "Persistent Artist")
    profile_id = headquarters.store.profile_id
    song = headquarters.store.create_song("Persistent Song")
    focus = headquarters.attention.start_focus("MAKE", song_id=song.id)
    ended = headquarters.attention.end_focus()
    assert ended is not None
    assert ended.id == focus.id
    assert ended.state == "ENDED"
    assert ended.end_reason == "ENDED"
    assert headquarters.attention.active_focus() is None
    assert headquarters.attention.end_focus() is None
    headquarters.close()

    with HeadquartersMemory.open(root, profile_id) as reopened:
        assert reopened.attention.active_focus() is None
        history = reopened.attention.history()
        assert len(history) == 1
        assert history[0].id == focus.id
        assert history[0].state == "ENDED"
        replacement = reopened.attention.start_focus("MANAGE")
        assert replacement.mode == "MANAGE"
        assert replacement.song_id is None


def test_existing_cleanroom_profile_adds_attention_schema_without_identity_change(tmp_path) -> None:
    root = tmp_path.resolve()
    store = LineageStore.create(root, "Existing Artist")
    EvidenceMemory(store)
    ActivityLog(store)
    profile_id = store.profile_id
    artist_id = store.primary_artist_id
    song = store.create_song("Existing Song")
    store.close()

    with HeadquartersMemory.open(root, profile_id) as reopened:
        assert reopened.store.primary_artist_id == artist_id
        assert reopened.store.active_song() is not None
        assert reopened.store.active_song().id == song.id
        assert reopened.attention.active_focus() is None
        focus = reopened.attention.start_focus("MAKE", song_id=song.id)
        assert focus.song_id == song.id


def test_focus_cannot_bind_song_from_another_artist_profile(tmp_path) -> None:
    root = tmp_path.resolve()
    first = HeadquartersMemory.create(root, "Artist One")
    foreign_song = first.store.create_song("One Song")
    first.close()

    second = HeadquartersMemory.create(root, "Artist Two")
    try:
        with pytest.raises(NotFoundError):
            second.attention.start_focus("MAKE", song_id=foreign_song.id)
        assert second.attention.active_focus() is None
        assert second.attention.history() == ()
    finally:
        second.close()


def test_focus_activity_is_append_only_and_unrelated_creative_state_is_unchanged(tmp_path) -> None:
    with HeadquartersMemory.create(tmp_path.resolve(), "Receipt Artist") as headquarters:
        song = headquarters.store.create_song("Receipt Song")
        before_song = headquarters.store.get_song(song.id)
        before_evidence = headquarters.store._conn.execute(
            "SELECT COUNT(*) AS n FROM evidence_claims"
        ).fetchone()["n"]
        checkpoint = headquarters.activity.checkpoint()

        first = headquarters.attention.start_focus("MAKE", song_id=song.id)
        headquarters.attention.start_focus("FINISH", song_id=song.id)
        headquarters.attention.end_focus()

        after_song = headquarters.store.get_song(song.id)
        after_evidence = headquarters.store._conn.execute(
            "SELECT COUNT(*) AS n FROM evidence_claims"
        ).fetchone()["n"]
        assert after_song == before_song
        assert after_evidence == before_evidence

        events = headquarters.activity.for_profile(after_sequence=checkpoint)
        focus_events = [event for event in events if event.object_type == "FOCUS_SESSION"]
        assert [event.event_type for event in focus_events] == [
            "FOCUS_SESSION_STARTED",
            "FOCUS_SESSION_ENDED",
            "FOCUS_SESSION_STARTED",
            "FOCUS_SESSION_ENDED",
        ]
        assert focus_events[0].object_id == first.id
        assert focus_events[0].payload == {"mode": "MAKE"}
        assert focus_events[1].payload == {"mode": "MAKE", "reason": "SWITCHED"}
        assert focus_events[2].payload == {"mode": "FINISH"}
        assert focus_events[3].payload == {"mode": "FINISH", "reason": "ENDED"}


def test_focus_history_rejects_update_delete_and_second_active_row(tmp_path) -> None:
    with HeadquartersMemory.create(tmp_path.resolve(), "Tamper Artist") as headquarters:
        active = headquarters.attention.start_focus("MAKE")
        conn = headquarters.store._conn
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE attention_focus_sessions SET mode='FINISH' WHERE id=?",
                (active.id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "DELETE FROM attention_focus_sessions WHERE id=?",
                (active.id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "INSERT INTO attention_focus_sessions("
                "id,artist_id,song_id,mode,state,end_reason) "
                "VALUES('focus_tamper',?,NULL,'MANAGE','ACTIVE',NULL)",
                (headquarters.store.primary_artist_id,),
            )
        conn.rollback()
        assert headquarters.attention.active_focus() == active


def test_missing_attention_integrity_hook_fails_reopen_closed(tmp_path) -> None:
    root = tmp_path.resolve()
    headquarters = HeadquartersMemory.create(root, "Corrupt Artist")
    profile_id = headquarters.store.profile_id
    headquarters.store._conn.execute("DROP TRIGGER attention_focus_delete_immutable")
    headquarters.store._conn.commit()
    headquarters.close()

    with pytest.raises(LineageCorruptionError):
        HeadquartersMemory.open(root, profile_id)


def test_missing_attention_uniqueness_index_fails_reopen_closed(tmp_path) -> None:
    root = tmp_path.resolve()
    headquarters = HeadquartersMemory.create(root, "Index Artist")
    profile_id = headquarters.store.profile_id
    headquarters.store._conn.execute("DROP INDEX attention_one_active_focus_per_artist")
    headquarters.store._conn.commit()
    headquarters.close()

    with pytest.raises(LineageCorruptionError):
        HeadquartersMemory.open(root, profile_id)
