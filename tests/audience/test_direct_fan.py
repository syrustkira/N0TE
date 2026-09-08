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
from n0te.lineage import NotFoundError, ValidationError
from n0te.memory import HeadquartersMemory


def _service(hq: HeadquartersMemory) -> DirectFanService:
    return DirectFanService(hq.store, hq.people, hq.evidence)


def _journey(hq: HeadquartersMemory) -> FanJourneyService:
    return FanJourneyService(hq.store, hq.people, hq.evidence)


def test_stage_or_contact_point_alone_never_becomes_contact_permission(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Direct Fan Artist")
    try:
        person = hq.people.create_person("Listener One")
        song = hq.store.create_song("Release One")
        journey = _journey(hq)
        direct = _service(hq)

        journey.record_stage(
            person.id,
            "JOIN",
            source_kind="OBSERVED",
            source_ref="community:join:1",
        )
        contact_claim = direct.record_contact_point(
            person.id,
            "EMAIL",
            "listener@example.test",
            source_kind="USER_DECLARED",
        )
        contact = direct.current_contact_point(person.id, "EMAIL")
        assert contact is not None and contact.claim_id == contact_claim.id
        assert contact.identity_verified is False
        assert contact.consent_granted is False
        assert contact.contact_authority_granted is False

        with pytest.raises(ValidationError, match="requires explicit current OPTED_IN consent"):
            direct.record_contact_intent(
                person.id,
                song.id,
                "EMAIL",
                "RELEASE_NOTIFICATION",
            )
        assert direct.reviewable_intents() == ()
    finally:
        hq.close()


def test_exact_contact_and_opt_in_can_create_reviewable_but_non_authorizing_intent(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Intent Artist")
    try:
        person = hq.people.create_person("Listener Two")
        song = hq.store.create_song("Release Two")
        journey = _journey(hq)
        direct = _service(hq)
        contact_claim = direct.record_contact_point(
            person.id,
            "EMAIL",
            "listener2@example.test",
            source_kind="OBSERVED",
            source_ref="signup:address:2",
            observed_at="2026-09-04T10:00:00Z",
        )
        consent_claim = journey.record_consent(
            person.id,
            "EMAIL",
            "OPTED_IN",
            source_kind="OBSERVED",
            source_ref="signup:consent:2",
            observed_at="2026-09-04T10:00:00Z",
        )

        intent_claim = direct.record_contact_intent(
            person.id,
            song.id,
            "EMAIL",
            "RELEASE_NOTIFICATION",
            note="Tell this listener when the exact Song is released",
        )
        intent = direct.get_intent(intent_claim.id)
        assessment = direct.assess_intent(intent.claim_id)

        assert intent.contact_claim_id == contact_claim.id
        assert intent.consent_claim_id == consent_claim.id
        assert intent.song_id == song.id
        assert intent.purpose == "RELEASE_NOTIFICATION"
        assert intent.send_authority_granted is False
        assert intent.provider_authority_granted is False
        assert intent.publication_authority_granted is False
        assert intent.spend_authority_granted is False
        assert assessment.state == "REVIEWABLE"
        assert assessment.reviewable is True
        assert assessment.current_consent_status == "OPTED_IN"
        assert assessment.separate_authorization_required is True
        assert assessment.send_authority_granted is False
        assert assessment.provider_authority_granted is False
        assert assessment.delivery_verified is False
        assert assessment.pre_save_verified is False
        assert [item.intent.claim_id for item in direct.reviewable_intents()] == [intent.claim_id]
    finally:
        hq.close()


def test_opt_out_revokes_reviewability_and_later_reconsent_does_not_resurrect_old_intent(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Revocation Artist")
    try:
        person = hq.people.create_person("Listener Three")
        song = hq.store.create_song("Release Three")
        journey = _journey(hq)
        direct = _service(hq)
        direct.record_contact_point(
            person.id,
            "SMS",
            "+15555550123",
            source_kind="USER_DECLARED",
        )
        first_consent = journey.record_consent(
            person.id,
            "SMS",
            "OPTED_IN",
            source_kind="USER_DECLARED",
        )
        old_intent_claim = direct.record_contact_intent(
            person.id,
            song.id,
            "SMS",
            "RELEASE_NOTIFICATION",
        )
        assert direct.assess_intent(old_intent_claim.id).state == "REVIEWABLE"

        opt_out = journey.record_consent(
            person.id,
            "SMS",
            "OPTED_OUT",
            source_kind="USER_DECLARED",
        )
        revoked = direct.assess_intent(old_intent_claim.id)
        assert revoked.state == "CONSENT_REVOKED"
        assert revoked.current_consent_claim_id == opt_out.id
        assert revoked.reviewable is False
        assert direct.reviewable_intents(person.id) == ()

        second_consent = journey.record_consent(
            person.id,
            "SMS",
            "OPTED_IN",
            source_kind="USER_DECLARED",
        )
        assert second_consent.id not in {first_consent.id, opt_out.id}
        old_after_reconsent = direct.assess_intent(old_intent_claim.id)
        assert old_after_reconsent.state == "CONSENT_CHANGED"
        assert old_after_reconsent.reviewable is False

        replacement = direct.record_contact_intent(
            person.id,
            song.id,
            "SMS",
            "RELEASE_NOTIFICATION",
            note="New intent after explicit re-consent",
        )
        replacement_intent = direct.get_intent(replacement.id)
        assert replacement_intent.consent_claim_id == second_consent.id
        assert direct.assess_intent(replacement.id).state == "REVIEWABLE"
    finally:
        hq.close()


def test_contact_point_change_stales_old_intent_without_changing_consent(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Contact Change Artist")
    try:
        person = hq.people.create_person("Listener Four")
        song = hq.store.create_song("Release Four")
        journey = _journey(hq)
        direct = _service(hq)
        first_contact = direct.record_contact_point(
            person.id,
            "EMAIL",
            "old@example.test",
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
            "PRE_SAVE_INVITE",
        )
        assert direct.get_intent(intent_claim.id).contact_claim_id == first_contact.id

        new_contact = direct.record_contact_point(
            person.id,
            "EMAIL",
            "new@example.test",
            source_kind="USER_DECLARED",
        )
        assessment = direct.assess_intent(intent_claim.id)
        assert assessment.state == "CONTACT_CHANGED"
        assert assessment.current_contact_claim_id == new_contact.id
        assert assessment.pre_save_verified is False
        assert assessment.reviewable is False
    finally:
        hq.close()


def test_equivalent_intent_is_not_duplicated_on_same_contact_and_consent(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Dedupe Artist")
    try:
        person = hq.people.create_person("Listener Five")
        song = hq.store.create_song("Release Five")
        journey = _journey(hq)
        direct = _service(hq)
        direct.record_contact_point(
            person.id,
            "EMAIL",
            "dedupe@example.test",
            source_kind="USER_DECLARED",
        )
        journey.record_consent(
            person.id,
            "EMAIL",
            "OPTED_IN",
            source_kind="USER_DECLARED",
        )
        direct.record_contact_intent(
            person.id,
            song.id,
            "EMAIL",
            "RELEASE_NOTIFICATION",
        )
        with pytest.raises(ValidationError, match="equivalent Direct Fan contact intent already exists"):
            direct.record_contact_intent(
                person.id,
                song.id,
                "EMAIL",
                "RELEASE_NOTIFICATION",
            )
    finally:
        hq.close()


def test_contact_points_cannot_be_inferred_remembered_measured_or_self_provider_verified(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Contact Source Artist")
    try:
        person = hq.people.create_person("Listener Six")
        direct = _service(hq)
        for source in ("INFERRED", "REMEMBERED", "MEASURED"):
            with pytest.raises(ValidationError, match="may never be inferred"):
                direct.record_contact_point(
                    person.id,
                    "EMAIL",
                    "source@example.test",
                    source_kind=source,
                    source_ref="not-valid-contact-proof",
                )
        with pytest.raises(ValidationError, match="cannot self-issue PROVIDER_VERIFIED contact evidence"):
            direct.record_contact_point(
                person.id,
                "EMAIL",
                "provider@example.test",
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider:contact:6",
            )
    finally:
        hq.close()


def test_verifier_backed_provider_contact_can_be_consumed_but_never_becomes_identity_proof(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Provider Contact Artist")
    try:
        person = hq.people.create_person("Listener Seven")
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key=f"{DIRECT_FAN_EVIDENCE_PREFIX}.{person.id}.contact.provider_1",
            value={
                "schema_version": DIRECT_FAN_SCHEMA_VERSION,
                "kind": "CONTACT_POINT",
                "person_id": person.id,
                "channel": "EMAIL",
                "endpoint": "verified@example.test",
                "observed_at": "2026-09-04T11:00:00Z",
                "note": "provider-backed contact endpoint",
            },
            source_kind="PROVIDER_VERIFIED",
            source_ref="provider-receipt:contact-7",
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )
        contact = _service(hq).current_contact_point(person.id, "EMAIL")
        assert contact is not None
        assert contact.source_kind == "PROVIDER_VERIFIED"
        assert contact.identity_verified is False
        assert contact.consent_granted is False
        assert contact.contact_authority_granted is False
    finally:
        hq.close()


def test_unknown_person_song_and_invalid_channel_fail_closed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Scope Artist")
    try:
        direct = _service(hq)
        person = hq.people.create_person("Listener Eight")
        with pytest.raises(NotFoundError, match="person not found"):
            direct.record_contact_point(
                "person_missing",
                "EMAIL",
                "missing@example.test",
                source_kind="USER_DECLARED",
            )
        with pytest.raises(ValidationError, match="unsupported contact channel"):
            direct.record_contact_point(
                person.id,
                "FAX",
                "555-0100",
                source_kind="USER_DECLARED",
            )
        direct.record_contact_point(
            person.id,
            "EMAIL",
            "scope@example.test",
            source_kind="USER_DECLARED",
        )
        _journey(hq).record_consent(
            person.id,
            "EMAIL",
            "OPTED_IN",
            source_kind="USER_DECLARED",
        )
        with pytest.raises(NotFoundError, match="Song not found"):
            direct.record_contact_intent(
                person.id,
                "song_missing",
                "EMAIL",
                "RELEASE_NOTIFICATION",
            )
    finally:
        hq.close()


def test_owned_namespace_rejects_inferred_or_malformed_contact_evidence(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Poison Artist")
    try:
        person = hq.people.create_person("Listener Nine")
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key=f"{DIRECT_FAN_EVIDENCE_PREFIX}.{person.id}.contact.bad",
            value={
                "schema_version": DIRECT_FAN_SCHEMA_VERSION,
                "kind": "CONTACT_POINT",
                "person_id": person.id,
                "channel": "EMAIL",
                "endpoint": "guessed@example.test",
                "observed_at": None,
                "note": "model guessed this",
            },
            source_kind="INFERRED",
            source_ref="model:guess:9",
            confidence=0.95,
            twin_domain="UNSPECIFIED",
        )
        with pytest.raises(DirectFanError, match="may not be inferred"):
            _service(hq).current_contact_point(person.id, "EMAIL")
    finally:
        hq.close()


def test_direct_fan_contact_and_intent_survive_relaunch_without_new_store(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Durable Direct Fan Artist")
    try:
        person = hq.people.create_person("Listener Ten")
        song = hq.store.create_song("Durable Release")
        direct = _service(hq)
        journey = _journey(hq)
        contact = direct.record_contact_point(
            person.id,
            "EMAIL",
            "durable@example.test",
            source_kind="USER_DECLARED",
        )
        consent = journey.record_consent(
            person.id,
            "EMAIL",
            "OPTED_IN",
            source_kind="USER_DECLARED",
        )
        intent = direct.record_contact_intent(
            person.id,
            song.id,
            "EMAIL",
            "RELEASE_NOTIFICATION",
        )
        profile_id = hq.store.profile_id
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        direct = _service(reopened)
        current_contact = direct.current_contact_point(person.id, "EMAIL")
        restored_intent = direct.get_intent(intent.id)
        assessment = direct.assess_intent(intent.id)
        assert current_contact is not None and current_contact.claim_id == contact.id
        assert restored_intent.consent_claim_id == consent.id
        assert restored_intent.song_id == song.id
        assert assessment.state == "REVIEWABLE"
        assert assessment.send_authority_granted is False
    finally:
        reopened.close()
