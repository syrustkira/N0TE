from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from n0te.credits import CREDIT_TRUTH_TYPE, CreditsMemory
from n0te.lineage import LineageCorruptionError, ValidationError
from n0te.memory import HeadquartersMemory


def test_song_credits_split_confirmations_and_history_survive_relaunch(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Credits Artist")
    try:
        song = hq.store.create_song("Shared Song")
        writer = hq.people.create_person("Maya Rivera", relationship_context="Co-writer")
        producer = hq.people.create_person("Alex Chen", relationship_context="Producer")
        credits = CreditsMemory(hq.store, hq.people)

        writer_credit = credits.record_credit(song.id, writer.id, "Songwriter")
        producer_credit = credits.record_credit(
            song.id,
            producer.id,
            "Producer",
            role_context="Production credit only; ownership is a separate question",
        )
        assert writer_credit.truth_type == CREDIT_TRUTH_TYPE
        assert producer_credit.truth_type == CREDIT_TRUTH_TYPE

        sheet = credits.create_split_draft(song.id)
        allocations = credits.set_draft_allocations(
            sheet.id,
            {writer.id: 6000, producer.id: 4000},
        )
        assert sum(item.basis_points for item in allocations) == 10000
        submitted = credits.submit_split(sheet.id)
        assert submitted.state == "OPEN_CONFIRMATION"
        assert credits.confirmation_state(sheet.id, writer.id) == "PENDING"
        assert credits.confirmation_state(sheet.id, producer.id) == "PENDING"

        credits.record_confirmation(
            sheet.id,
            writer.id,
            status="RECORDED_CONFIRMED",
            note="Artist records that Maya confirmed the 60% composition share by email",
        )
        credits.record_confirmation(
            sheet.id,
            producer.id,
            status="RECORDED_DISPUTED",
            note="Artist records that Alex questioned the 40% composition share",
        )
        assert credits.all_recorded_confirmed(sheet.id) is False
        credits.record_confirmation(
            sheet.id,
            producer.id,
            status="RECORDED_CONFIRMED",
            note="Artist records a later message accepting the revised understanding",
        )
        assert credits.all_recorded_confirmed(sheet.id) is True
        assert [
            item.status for item in credits.confirmation_history(sheet.id, person_id=producer.id)
        ] == ["RECORDED_DISPUTED", "RECORDED_CONFIRMED"]

        profile_id = hq.store.profile_id
        event_types = [event.event_type for event in hq.activity.for_profile()]
        assert "SONG_CREDIT_RECORDED" in event_types
        assert "COMPOSITION_SPLIT_DRAFT_CREATED" in event_types
        assert "COMPOSITION_SPLIT_SUBMITTED" in event_types
        assert "COMPOSITION_SPLIT_CONFIRMATION_RECORDED" in event_types
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        credits = CreditsMemory(reopened.store, reopened.people)
        roster = credits.credits_for_song(song.id)
        assert [(item.person_id, item.role) for item in roster] == [
            (writer.id, "Songwriter"),
            (producer.id, "Producer"),
        ]
        active = credits.active_split_for_song(song.id)
        assert active is not None
        assert active.id == sheet.id
        assert active.state == "OPEN_CONFIRMATION"
        assert credits.all_recorded_confirmed(sheet.id) is True
        history = credits.confirmation_history(sheet.id)
        assert len(history) == 3
        assert all(item.truth_type == "USER_DECLARED" for item in history)
    finally:
        reopened.close()


def test_split_submission_requires_exact_100_percent_then_freezes_allocations(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Split Artist")
    try:
        song = hq.store.create_song("Math Song")
        first = hq.people.create_person("First Writer")
        second = hq.people.create_person("Second Writer")
        credits = CreditsMemory(hq.store, hq.people)
        sheet = credits.create_split_draft(song.id)

        with pytest.raises(ValidationError, match="exceeds 100 percent"):
            credits.set_draft_allocations(sheet.id, {first.id: 7000, second.id: 4000})
        assert credits.split_allocations(sheet.id) == ()

        credits.set_draft_allocations(sheet.id, {first.id: 5000, second.id: 4500})
        with pytest.raises(ValidationError, match="exactly 100.00 percent"):
            credits.submit_split(sheet.id)
        assert credits.get_split_sheet(sheet.id).state == "DRAFT"  # type: ignore[union-attr]

        credits.set_draft_allocations(sheet.id, {first.id: 5500, second.id: 4500})
        credits.submit_split(sheet.id)

        with pytest.raises(ValidationError, match="allocations are immutable"):
            credits.set_draft_allocations(sheet.id, {first.id: 5000, second.id: 5000})
        with pytest.raises(sqlite3.IntegrityError):
            with hq.store._tx():
                allocation = credits.split_allocations(sheet.id)[0]
                hq.store._conn.execute(
                    "UPDATE composition_split_allocations SET basis_points=5000 WHERE id=?",
                    (allocation.id,),
                )
        assert sum(item.basis_points for item in credits.split_allocations(sheet.id)) == 10000
    finally:
        hq.close()


def test_void_preserves_history_and_allows_an_explicit_new_proposal(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Revision Artist")
    try:
        song = hq.store.create_song("Revision Song")
        person = hq.people.create_person("Writer")
        credits = CreditsMemory(hq.store, hq.people)
        first = credits.create_split_draft(song.id)
        credits.set_draft_allocations(first.id, {person.id: 10000})
        credits.submit_split(first.id)
        credits.record_confirmation(
            first.id,
            person.id,
            status="RECORDED_DISPUTED",
            note="Artist records that the writer disputed this proposal",
        )

        voided = credits.void_split(
            first.id,
            reason="Replace the disputed proposal rather than rewriting submitted history",
        )
        assert voided.state == "VOIDED"
        assert credits.confirmation_history(first.id)[0].status == "RECORDED_DISPUTED"
        second = credits.create_split_draft(song.id)
        assert second.id != first.id
        assert [item.state for item in credits.split_history(song.id)] == ["VOIDED", "DRAFT"]
    finally:
        hq.close()


def test_confirmation_is_only_artist_recorded_evidence_and_only_for_participants(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Confirmation Artist")
    try:
        song = hq.store.create_song("Consent Song")
        participant = hq.people.create_person("Participant")
        bystander = hq.people.create_person("Bystander")
        credits = CreditsMemory(hq.store, hq.people)
        sheet = credits.create_split_draft(song.id)
        credits.set_draft_allocations(sheet.id, {participant.id: 10000})

        with pytest.raises(ValidationError, match="after submission"):
            credits.record_confirmation(
                sheet.id,
                participant.id,
                status="RECORDED_CONFIRMED",
                note="Too early",
            )
        credits.submit_split(sheet.id)
        with pytest.raises(ValidationError, match="only split participants"):
            credits.record_confirmation(
                sheet.id,
                bystander.id,
                status="RECORDED_CONFIRMED",
                note="This person is not part of the proposal",
            )

        recorded = credits.record_confirmation(
            sheet.id,
            participant.id,
            status="RECORDED_CONFIRMED",
            note="Artist records seeing a confirmation message",
        )
        assert recorded.truth_type == "USER_DECLARED"
        with pytest.raises(ValidationError, match="already the latest"):
            credits.record_confirmation(
                sheet.id,
                participant.id,
                status="RECORDED_CONFIRMED",
                note="Do not manufacture duplicate certainty",
            )
        disputed = credits.record_confirmation(
            sheet.id,
            participant.id,
            status="RECORDED_DISPUTED",
            note="Artist records a later dispute; history must not be overwritten",
        )
        assert disputed.sequence > recorded.sequence
        assert credits.all_recorded_confirmed(sheet.id) is False
    finally:
        hq.close()


def test_credit_roles_are_case_insensitive_duplicates_but_distinct_roles_survive(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Credit Artist")
    try:
        song = hq.store.create_song("Roster Song")
        person = hq.people.create_person("Multi-role Person")
        credits = CreditsMemory(hq.store, hq.people)
        credits.record_credit(song.id, person.id, "Producer")
        with pytest.raises(ValidationError, match="cannot record Song credit"):
            credits.record_credit(song.id, person.id, "producer")
        credits.record_credit(song.id, person.id, "Mixer")
        assert [item.role for item in credits.credits_for_song(song.id)] == [
            "Producer",
            "Mixer",
        ]
    finally:
        hq.close()


def test_credits_schema_integrity_loss_fails_closed_on_next_use(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Corruption Artist")
    try:
        song = hq.store.create_song("Corruption Song")
        person = hq.people.create_person("Writer")
        credits = CreditsMemory(hq.store, hq.people)
        credits.record_credit(song.id, person.id, "Writer")
        profile_id = hq.store.profile_id
        hq.store._conn.execute("DROP TRIGGER credits_credit_immutable")
        hq.store._conn.commit()
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        with pytest.raises(LineageCorruptionError, match="Credits integrity hooks"):
            CreditsMemory(reopened.store, reopened.people)
    finally:
        reopened.close()
