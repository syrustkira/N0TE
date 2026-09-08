from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from n0te.lineage import LineageCorruptionError, ValidationError
from n0te.memory import HeadquartersMemory
from n0te.obligations import ObligationMemory


def _service(hq: HeadquartersMemory) -> ObligationMemory:
    return ObligationMemory(hq.store, hq.people, hq.evidence)


def _claim(
    hq: HeadquartersMemory,
    *,
    key: str,
    value: object,
    song_id: str | None = None,
    source_kind: str = "USER_DECLARED",
    source_ref: str | None = None,
    supersedes: tuple[str, ...] = (),
):
    if song_id is None:
        scope_kind = "ARTIST"
        scope_id = hq.store.primary_artist_id
    else:
        scope_kind = "SONG"
        scope_id = song_id
    return hq.evidence.record_claim(
        scope_kind=scope_kind,
        scope_id=scope_id,
        key=key,
        value=value,
        source_kind=source_kind,
        source_ref=source_ref,
        twin_domain="UNSPECIFIED",
        supersedes=supersedes,
    )


def test_artist_obligation_is_source_bound_unscored_and_non_authorizing(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Obligation Artist")
    try:
        person = hq.people.create_person("Manager")
        source = _claim(
            hq,
            key="obligation.source.manager-deliverable",
            value={"summary": "Send updated EPK"},
        )
        obligation = _service(hq).create_obligation(
            person.id,
            kind="DELIVERABLE",
            responsibility="ARTIST_OWES",
            summary="Send updated EPK",
            source_claim_id=source.id,
            due_on="2026-09-10",
            consequence_note="Manager cannot pitch the release until the materials arrive.",
        )

        assert obligation.status == "OPEN"
        assert obligation.source_claim_id == source.id
        assert obligation.source_truth_class == "DECLARED"
        assert obligation.due_state(as_of="2026-09-09") == "UPCOMING"
        assert obligation.due_state(as_of="2026-09-10") == "DUE"
        assert obligation.due_state(as_of="2026-09-11") == "OVERDUE"
        assert obligation.attention_state(as_of="2026-09-11") == "OVERDUE"
        assert obligation.priority_score is None
        assert obligation.legal_entitlement_verified is False
        assert obligation.payment_authority_granted is False
        assert obligation.license_authority_granted is False
        assert obligation.messaging_authority_granted is False
        assert obligation.calendar_authority_granted is False
        assert obligation.external_action_authority_granted is False
    finally:
        hq.close()


def test_linked_followup_resolution_requires_reconciliation_not_auto_completion(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Follow-up Artist")
    try:
        song = hq.store.create_song("Single")
        person = hq.people.create_person("Mix Engineer")
        followup = hq.people.create_followup(
            person.id,
            "Return mix notes",
            responsibility="ARTIST_OWES",
            song_id=song.id,
            due_on="2026-09-08",
        )
        source = _claim(
            hq,
            key="obligation.source.mix-notes",
            value={"promise": "Return mix notes"},
            song_id=song.id,
        )
        service = _service(hq)
        obligation = service.create_obligation(
            person.id,
            kind="DELIVERABLE",
            responsibility="ARTIST_OWES",
            summary="Return mix notes",
            source_claim_id=source.id,
            song_id=song.id,
            followup_id=followup.id,
            due_on="2026-09-08",
        )

        hq.people.resolve_followup(followup.id, resolution_note="Notes sent in email")
        refreshed = service.get(obligation.id)

        assert refreshed.status == "OPEN"
        assert refreshed.followup_state == "RESOLVED"
        assert refreshed.attention_state(as_of="2026-09-08") == "NEEDS_RECONCILIATION"
        assert refreshed.external_action_authority_granted is False
    finally:
        hq.close()


def test_superseded_creation_evidence_preserves_history_but_requires_revalidation(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Revalidation Artist")
    try:
        person = hq.people.create_person("Label Contact")
        source = _claim(
            hq,
            key="obligation.source.label-date",
            value={"due": "2026-09-12"},
            source_kind="OBSERVED",
            source_ref="email:label-thread:17",
        )
        service = _service(hq)
        obligation = service.create_obligation(
            person.id,
            kind="DEADLINE",
            responsibility="ARTIST_OWES",
            summary="Deliver final metadata",
            source_claim_id=source.id,
            due_on="2026-09-12",
        )
        replacement = _claim(
            hq,
            key=source.key,
            value={"due": "2026-09-15"},
            source_kind="OBSERVED",
            source_ref="email:label-thread:18",
            supersedes=(source.id,),
        )

        refreshed = service.get(obligation.id)
        assert replacement.id != source.id
        assert refreshed.source_claim_id == source.id
        assert refreshed.source_current is False
        assert refreshed.status == "OPEN"
        assert refreshed.attention_state(as_of="2026-09-12") == "NEEDS_REVALIDATION"
    finally:
        hq.close()


def test_observed_measured_provider_and_inferred_evidence_require_provenance(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Provenance Artist")
    try:
        person = hq.people.create_person("Agent")
        service = _service(hq)
        for source_kind in ("OBSERVED", "MEASURED", "PROVIDER_VERIFIED", "INFERRED"):
            claim = _claim(
                hq,
                key=f"obligation.source.{source_kind.lower()}",
                value={"kind": source_kind},
                source_kind=source_kind,
            )
            with pytest.raises(ValidationError, match="requires source_ref provenance"):
                service.create_obligation(
                    person.id,
                    kind="OTHER",
                    responsibility="MUTUAL",
                    summary=f"Provenance check {source_kind}",
                    source_claim_id=claim.id,
                )
    finally:
        hq.close()


def test_cross_song_evidence_cannot_bind_obligation(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Cross Song Artist")
    try:
        song_a = hq.store.create_song("Song A")
        song_b = hq.store.create_song("Song B")
        person = hq.people.create_person("Publisher")
        source = _claim(
            hq,
            key="obligation.source.song-a",
            value={"song": "A"},
            song_id=song_a.id,
        )

        with pytest.raises(ValidationError, match="scope does not match"):
            _service(hq).create_obligation(
                person.id,
                kind="LICENSE",
                responsibility="WAITING_ON_OTHER",
                summary="Approve sample license",
                source_claim_id=source.id,
                song_id=song_b.id,
            )
    finally:
        hq.close()


def test_trigger_is_explicit_evidence_and_never_execution_authority(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Trigger Artist")
    try:
        song = hq.store.create_song("Trigger Song")
        person = hq.people.create_person("Distributor")
        source = _claim(
            hq,
            key="obligation.source.delivery",
            value={"trigger": "master approved"},
            song_id=song.id,
        )
        service = _service(hq)
        obligation = service.create_obligation(
            person.id,
            kind="DELIVERABLE",
            responsibility="ARTIST_OWES",
            summary="Upload distribution package",
            source_claim_id=source.id,
            song_id=song.id,
            trigger_ref="master-approved",
            due_on="2026-09-20",
        )
        assert obligation.trigger_state == "PENDING"
        assert obligation.due_state(as_of="2026-09-20") == "WAITING_FOR_TRIGGER"
        assert obligation.attention_state(as_of="2026-09-20") == "WAITING"

        trigger = _claim(
            hq,
            key="obligation.trigger.master-approved",
            value={"approved": True},
            song_id=song.id,
            source_kind="OBSERVED",
            source_ref="version-approval:receipt-7",
        )
        refreshed = service.record_trigger(
            obligation.id,
            evidence_claim_id=trigger.id,
            note="Master approval was observed.",
        )

        assert refreshed.trigger_state == "OBSERVED"
        assert refreshed.due_state(as_of="2026-09-20") == "DUE"
        assert refreshed.trigger_events[-1].evidence_claim_id == trigger.id
        assert refreshed.trigger_events[-1].action_authority_granted is False
        assert refreshed.external_action_authority_granted is False
    finally:
        hq.close()


def test_lifecycle_is_append_only_reopen_is_bounded_and_terminal_is_immutable(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Lifecycle Artist")
    try:
        person = hq.people.create_person("Client")
        source = _claim(
            hq,
            key="obligation.source.client",
            value={"deliverable": "stems"},
        )
        service = _service(hq)
        obligation = service.create_obligation(
            person.id,
            kind="DELIVERABLE",
            responsibility="ARTIST_OWES",
            summary="Deliver stems",
            source_claim_id=source.id,
        )
        blocked_evidence = _claim(
            hq,
            key="obligation.status.blocked",
            value={"reason": "client has not confirmed format"},
        )
        blocked = service.transition(
            obligation.id,
            status="BLOCKED",
            evidence_claim_id=blocked_evidence.id,
            note="Waiting for format confirmation.",
        )
        assert tuple(event.status for event in blocked.events) == ("OPEN", "BLOCKED")

        reopen_evidence = _claim(
            hq,
            key="obligation.status.reopen",
            value={"confirmed": "24-bit WAV"},
        )
        reopened = service.transition(
            obligation.id,
            status="OPEN",
            evidence_claim_id=reopen_evidence.id,
            note="Client confirmed the delivery format.",
        )
        assert tuple(event.status for event in reopened.events) == ("OPEN", "BLOCKED", "OPEN")

        satisfied_evidence = _claim(
            hq,
            key="obligation.status.satisfied",
            value={"receipt": "delivered"},
            source_kind="OBSERVED",
            source_ref="delivery:receipt:42",
        )
        satisfied = service.transition(
            obligation.id,
            status="SATISFIED",
            evidence_claim_id=satisfied_evidence.id,
            note="Client delivery receipt observed.",
        )
        assert satisfied.terminal is True
        assert satisfied.due_state(as_of="2026-09-30") == "CLOSED"
        assert satisfied.attention_state(as_of="2026-09-30") == "CLOSED"

        with pytest.raises(ValidationError, match="terminal obligation lifecycle is immutable"):
            service.transition(
                obligation.id,
                status="DISPUTED",
                evidence_claim_id=satisfied_evidence.id,
                note="Attempted late rewrite.",
            )
    finally:
        hq.close()


def test_provider_verified_evidence_is_consumed_without_granting_provider_authority(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Provider Artist")
    try:
        person = hq.people.create_person("Store")
        claim = _claim(
            hq,
            key="obligation.source.provider-payment",
            value={"invoice": "inv-4"},
            source_kind="PROVIDER_VERIFIED",
            source_ref="provider-receipt:inv-4",
        )
        obligation = _service(hq).create_obligation(
            person.id,
            kind="PAYMENT",
            responsibility="WAITING_ON_OTHER",
            summary="Await invoice payment",
            source_claim_id=claim.id,
        )

        assert obligation.source_truth_class == "PROVIDER_VERIFIED"
        assert obligation.payment_authority_granted is False
        assert obligation.external_action_authority_granted is False
    finally:
        hq.close()


def test_activity_chronology_records_create_status_and_trigger_events(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Chronology Artist")
    try:
        person = hq.people.create_person("Promoter")
        source = _claim(
            hq,
            key="obligation.source.show",
            value={"trigger": "venue confirms"},
        )
        service = _service(hq)
        obligation = service.create_obligation(
            person.id,
            kind="OTHER",
            responsibility="MUTUAL",
            summary="Confirm show logistics",
            source_claim_id=source.id,
            trigger_ref="venue-confirmed",
        )
        trigger = _claim(
            hq,
            key="obligation.trigger.venue",
            value={"confirmed": True},
            source_kind="OBSERVED",
            source_ref="email:venue:confirmation",
        )
        service.record_trigger(
            obligation.id,
            evidence_claim_id=trigger.id,
            note="Venue confirmation received.",
        )
        blocked = _claim(
            hq,
            key="obligation.status.blocked-show",
            value={"reason": "stage plot missing"},
        )
        service.transition(
            obligation.id,
            status="BLOCKED",
            evidence_claim_id=blocked.id,
            note="Stage plot is still missing.",
        )

        event_types = tuple(event.event_type for event in hq.activity.for_profile())
        assert "OBLIGATION_CREATED" in event_types
        assert "OBLIGATION_STATUS_OPEN" in event_types
        assert "OBLIGATION_TRIGGER_OBSERVED" in event_types
        assert "OBLIGATION_STATUS_BLOCKED" in event_types
    finally:
        hq.close()


def test_obligation_history_survives_headquarters_restart(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Restart Artist")
    profile_id = hq.store.profile_id
    try:
        song = hq.store.create_song("Restart Song")
        person = hq.people.create_person("Collaborator")
        source = _claim(
            hq,
            key="obligation.source.restart",
            value={"summary": "send stems"},
            song_id=song.id,
        )
        obligation = _service(hq).create_obligation(
            person.id,
            kind="DELIVERABLE",
            responsibility="ARTIST_OWES",
            summary="Send stems",
            source_claim_id=source.id,
            song_id=song.id,
        )
        obligation_id = obligation.id
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        restored = _service(reopened).get(obligation_id)
        assert restored.id == obligation_id
        assert restored.song_id == song.id
        assert restored.person_id == person.id
        assert tuple(event.status for event in restored.events) == ("OPEN",)
    finally:
        reopened.close()


def test_direct_sql_rewrite_and_delete_of_obligation_history_fail_closed(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Immutable Artist")
    try:
        person = hq.people.create_person("Business Partner")
        source = _claim(
            hq,
            key="obligation.source.immutable",
            value={"summary": "split confirmation"},
        )
        obligation = _service(hq).create_obligation(
            person.id,
            kind="OTHER",
            responsibility="MUTUAL",
            summary="Confirm business terms",
            source_claim_id=source.id,
        )

        with pytest.raises(sqlite3.IntegrityError, match="identity and binding are immutable"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "UPDATE business_obligations SET summary='rewritten' WHERE id=?",
                    (obligation.id,),
                )
        with pytest.raises(sqlite3.IntegrityError, match="history is immutable"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "DELETE FROM business_obligations WHERE id=?",
                    (obligation.id,),
                )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "UPDATE business_obligation_events SET status='CANCELED' "
                    "WHERE obligation_id=?",
                    (obligation.id,),
                )
    finally:
        hq.close()


def test_missing_integrity_hook_fails_closed_on_relaunch(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Integrity Artist")
    profile_id = hq.store.profile_id
    try:
        _service(hq)
        with hq.store._tx():
            hq.store._conn.execute("DROP TRIGGER obligation_delete_immutable")
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        with pytest.raises(LineageCorruptionError, match="integrity hooks are incomplete"):
            _service(reopened)
    finally:
        reopened.close()
