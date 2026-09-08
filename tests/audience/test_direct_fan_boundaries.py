from __future__ import annotations

from pathlib import Path

import pytest

from n0te.direct_fan import (
    DIRECT_FAN_EVIDENCE_PREFIX,
    DIRECT_FAN_SCHEMA_VERSION,
    DirectFanError,
    DirectFanService,
)
from n0te.fan_journey import FanJourneyService
from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory


def _direct(hq: HeadquartersMemory) -> DirectFanService:
    return DirectFanService(hq.store, hq.people, hq.evidence)


def _journey(hq: HeadquartersMemory) -> FanJourneyService:
    return FanJourneyService(hq.store, hq.people, hq.evidence)


def test_missing_contact_endpoint_fails_closed_in_owned_namespace(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Missing Endpoint Artist")
    try:
        person = hq.people.create_person("Listener Boundary A")
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key=f"{DIRECT_FAN_EVIDENCE_PREFIX}.{person.id}.contact.bad_missing",
            value={
                "schema_version": DIRECT_FAN_SCHEMA_VERSION,
                "kind": "CONTACT_POINT",
                "person_id": person.id,
                "channel": "EMAIL",
                "endpoint": None,
                "observed_at": None,
                "note": None,
            },
            source_kind="USER_DECLARED",
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )
        with pytest.raises(ValidationError, match="endpoint must not be empty"):
            _direct(hq).current_contact_point(person.id, "EMAIL")
    finally:
        hq.close()


def test_contact_payload_under_intent_key_fails_closed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Key Binding Artist")
    try:
        person = hq.people.create_person("Listener Boundary B")
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key=f"{DIRECT_FAN_EVIDENCE_PREFIX}.{person.id}.intent.wrong_family",
            value={
                "schema_version": DIRECT_FAN_SCHEMA_VERSION,
                "kind": "CONTACT_POINT",
                "person_id": person.id,
                "channel": "EMAIL",
                "endpoint": "binding@example.test",
                "observed_at": None,
                "note": None,
            },
            source_kind="USER_DECLARED",
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )
        with pytest.raises(DirectFanError, match="contact key binding is malformed"):
            _direct(hq).contact_history(person.id)
    finally:
        hq.close()


def test_intent_payload_under_contact_key_fails_closed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Intent Key Artist")
    try:
        person = hq.people.create_person("Listener Boundary C")
        song = hq.store.create_song("Boundary Release")
        direct = _direct(hq)
        journey = _journey(hq)
        contact = direct.record_contact_point(
            person.id,
            "EMAIL",
            "intent@example.test",
            source_kind="USER_DECLARED",
        )
        consent = journey.record_consent(
            person.id,
            "EMAIL",
            "OPTED_IN",
            source_kind="USER_DECLARED",
        )
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key=f"{DIRECT_FAN_EVIDENCE_PREFIX}.{person.id}.contact.wrong_family",
            value={
                "schema_version": DIRECT_FAN_SCHEMA_VERSION,
                "kind": "CONTACT_INTENT",
                "person_id": person.id,
                "song_id": song.id,
                "channel": "EMAIL",
                "purpose": "RELEASE_NOTIFICATION",
                "contact_claim_id": contact.id,
                "consent_claim_id": consent.id,
                "note": None,
            },
            source_kind="USER_DECLARED",
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )
        with pytest.raises(DirectFanError, match="intent key binding is malformed"):
            direct.intents_for_person(person.id)
    finally:
        hq.close()


def test_reviewable_state_still_has_no_marketing_scheduling_or_send_authority(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Authority Boundary Artist")
    try:
        person = hq.people.create_person("Listener Boundary D")
        song = hq.store.create_song("Authority Release")
        direct = _direct(hq)
        journey = _journey(hq)
        direct.record_contact_point(
            person.id,
            "EMAIL",
            "authority@example.test",
            source_kind="USER_DECLARED",
        )
        journey.record_consent(
            person.id,
            "EMAIL",
            "OPTED_IN",
            source_kind="USER_DECLARED",
        )
        intent_claim = direct.record_contact_intent(
            person.id,
            song.id,
            "EMAIL",
            "RELEASE_NOTIFICATION",
        )
        contact = direct.current_contact_point(person.id, "EMAIL")
        intent = direct.get_intent(intent_claim.id)
        assessment = direct.assess_intent(intent_claim.id)
        assert contact is not None and contact.marketing_permission_granted is False
        assert intent.scheduling_authority_granted is False
        assert intent.send_authority_granted is False
        assert assessment.state == "REVIEWABLE"
        assert assessment.scheduling_authority_granted is False
        assert assessment.send_authority_granted is False
        assert assessment.provider_authority_granted is False
    finally:
        hq.close()
