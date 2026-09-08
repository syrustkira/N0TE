from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from n0te.fan_journey import (
    FAN_EVIDENCE_PREFIX,
    FAN_JOURNEY_SCHEMA_VERSION,
    FanJourneyError,
    FanJourneyService,
    StaleFanJourneyError,
)
from n0te.lineage import NotFoundError, ValidationError
from n0te.memory import HeadquartersMemory


def _service(hq: HeadquartersMemory) -> FanJourneyService:
    return FanJourneyService(hq.store, hq.people, hq.evidence)


def test_unknown_fan_journey_is_non_authorizing_and_unscored(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Audience Artist")
    try:
        person = hq.people.create_person("Listener A")
        snapshot = _service(hq).snapshot(person.id)

        assert snapshot.status == "UNKNOWN"
        assert snapshot.observed_stages == ()
        assert snapshot.declared_stages == ()
        assert snapshot.inferred_stages == ()
        assert snapshot.furthest_observed_stage is None
        assert snapshot.consent_status("EMAIL") == "UNKNOWN"
        assert snapshot.relationship_score is None
        assert snapshot.action_authority_granted is False
        assert snapshot.contact_authority_granted is False
        assert snapshot.marketing_permission_granted is False
        assert snapshot.causal_claim_supported is False
        assert snapshot.linear_funnel_assumed is False
    finally:
        hq.close()


def test_observed_support_does_not_manufacture_prior_funnel_stages(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Support Artist")
    try:
        person = hq.people.create_person("Supporter")
        service = _service(hq)
        service.record_stage(
            person.id,
            "SUPPORT",
            source_kind="OBSERVED",
            source_ref="receipt:merch-order-123",
            observed_at="2026-09-04T12:00:00Z",
        )

        snapshot = service.snapshot(person.id)
        assert snapshot.status == "OBSERVED"
        assert snapshot.observed_stages == ("SUPPORT",)
        assert snapshot.furthest_observed_stage == "SUPPORT"
        assert "DISCOVER" not in snapshot.observed_stages
        assert "LISTEN" not in snapshot.observed_stages
        assert "FOLLOW" not in snapshot.observed_stages
        assert snapshot.signals[0].causal_claim_supported is False
    finally:
        hq.close()


def test_declared_inferred_and_observed_relationship_evidence_remain_distinct(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Evidence Artist")
    try:
        person = hq.people.create_person("Listener B")
        service = _service(hq)
        service.record_stage(person.id, "FOLLOW", source_kind="USER_DECLARED")
        service.record_stage(
            person.id,
            "ENGAGE",
            source_kind="INFERRED",
            source_ref="analysis:comment-pattern-v1",
            confidence=0.55,
        )
        service.record_stage(
            person.id,
            "RETURN",
            source_kind="MEASURED",
            source_ref="provider-export:repeat-listen-row-7",
            confidence=0.9,
        )

        snapshot = service.snapshot(person.id)
        assert snapshot.status == "MIXED"
        assert snapshot.observed_stages == ("RETURN",)
        assert snapshot.declared_stages == ("FOLLOW",)
        assert snapshot.inferred_stages == ("ENGAGE",)
        assert snapshot.furthest_observed_stage == "RETURN"
    finally:
        hq.close()


def test_relationship_stage_never_implies_channel_consent(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Consent Artist")
    try:
        person = hq.people.create_person("Listener C")
        service = _service(hq)
        service.record_stage(
            person.id,
            "FOLLOW",
            source_kind="OBSERVED",
            source_ref="social:event:follow-1",
        )
        snapshot = service.snapshot(person.id)

        assert snapshot.consent_status("EMAIL") == "UNKNOWN"
        assert snapshot.consent_status("DM") == "UNKNOWN"
        assert snapshot.contact_authority_granted is False
        assert all(signal.contact_authority_granted is False for signal in snapshot.signals)
    finally:
        hq.close()


def test_explicit_consent_history_is_channel_specific_and_still_non_authorizing(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Consent History Artist")
    try:
        person = hq.people.create_person("Listener D")
        service = _service(hq)
        first = service.record_consent(
            person.id,
            "EMAIL",
            "OPTED_OUT",
            source_kind="OBSERVED",
            source_ref="signup:preference-event-1",
            observed_at="2026-09-01T10:00:00Z",
        )
        second = service.record_consent(
            person.id,
            "EMAIL",
            "OPTED_IN",
            source_kind="OBSERVED",
            source_ref="signup:preference-event-2",
            observed_at="2026-09-04T10:00:00Z",
        )

        snapshot = service.snapshot(person.id)
        assert [item.claim_id for item in snapshot.consent_history] == [first.id, second.id]
        assert snapshot.consent_status("EMAIL") == "OPTED_IN"
        assert snapshot.consent_status("SMS") == "UNKNOWN"
        assert snapshot.consent_evidence("EMAIL") is not None
        assert snapshot.consent_evidence("EMAIL").external_action_authority_granted is False
        assert snapshot.action_authority_granted is False
        assert snapshot.contact_authority_granted is False
    finally:
        hq.close()


def test_consent_cannot_be_inferred_remembered_measured_or_self_provider_verified(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Consent Boundary Artist")
    try:
        person = hq.people.create_person("Listener E")
        service = _service(hq)
        for source in ("INFERRED", "REMEMBERED", "MEASURED"):
            with pytest.raises(ValidationError, match="may never be inferred"):
                service.record_consent(
                    person.id,
                    "EMAIL",
                    "OPTED_IN",
                    source_kind=source,
                    source_ref="not-valid-consent-provenance",
                )
        with pytest.raises(ValidationError, match="cannot self-issue PROVIDER_VERIFIED consent"):
            service.record_consent(
                person.id,
                "EMAIL",
                "OPTED_IN",
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider:consent-1",
            )
        with pytest.raises(ValidationError, match="cannot self-issue PROVIDER_VERIFIED evidence"):
            service.record_stage(
                person.id,
                "FOLLOW",
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider:follow-1",
            )
    finally:
        hq.close()


def test_verifier_backed_canonical_provider_evidence_can_be_consumed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Provider Audience Artist")
    try:
        person = hq.people.create_person("Listener F")
        prefix = f"{FAN_EVIDENCE_PREFIX}.{person.id}"
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key=f"{prefix}.signal.provider_stage_1",
            value={
                "schema_version": FAN_JOURNEY_SCHEMA_VERSION,
                "kind": "STAGE",
                "person_id": person.id,
                "stage": "JOIN",
                "observed_at": "2026-09-04T11:00:00Z",
                "song_id": None,
                "note": "mailing-list membership provider event",
            },
            source_kind="PROVIDER_VERIFIED",
            source_ref="provider-receipt:join-44",
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key=f"{prefix}.consent.provider_consent_1",
            value={
                "schema_version": FAN_JOURNEY_SCHEMA_VERSION,
                "kind": "CONSENT",
                "person_id": person.id,
                "channel": "EMAIL",
                "status": "OPTED_IN",
                "observed_at": "2026-09-04T11:00:00Z",
                "note": "provider-backed consent event",
            },
            source_kind="PROVIDER_VERIFIED",
            source_ref="provider-receipt:consent-44",
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )

        snapshot = _service(hq).snapshot(person.id)
        assert snapshot.observed_stages == ("JOIN",)
        assert snapshot.signals[0].source_kind == "PROVIDER_VERIFIED"
        assert snapshot.consent_status("EMAIL") == "OPTED_IN"
        assert snapshot.consent_evidence("EMAIL").source_kind == "PROVIDER_VERIFIED"
        assert snapshot.contact_authority_granted is False
    finally:
        hq.close()


def test_song_attribution_is_exact_and_unknown_song_fails_closed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Song Audience Artist")
    try:
        person = hq.people.create_person("Listener G")
        song = hq.store.create_song("Signal Song")
        service = _service(hq)
        service.record_stage(
            person.id,
            "LISTEN",
            source_kind="OBSERVED",
            source_ref="play:event:1",
            song_id=song.id,
        )
        assert service.snapshot(person.id).signals[0].song_id == song.id

        with pytest.raises(NotFoundError, match="Song not found"):
            service.record_stage(
                person.id,
                "RETURN",
                source_kind="OBSERVED",
                source_ref="play:event:2",
                song_id="song_does_not_exist",
            )
    finally:
        hq.close()


def test_snapshot_stales_on_new_evidence_and_rejects_forged_payload(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Fresh Audience Artist")
    try:
        person = hq.people.create_person("Listener H")
        service = _service(hq)
        service.record_stage(person.id, "DISCOVER", source_kind="USER_DECLARED")
        first = service.snapshot(person.id)
        assert service.is_current(first) is True

        forged = replace(first, person_display_name="Forged Name")
        assert service.is_current(forged) is False

        service.record_stage(
            person.id,
            "LISTEN",
            source_kind="OBSERVED",
            source_ref="play:event:3",
        )
        assert service.is_current(first) is False
        with pytest.raises(StaleFanJourneyError, match="evidence changed"):
            service.assert_current(first)
    finally:
        hq.close()


def test_fan_journey_evidence_survives_relaunch_without_new_store(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Durable Audience Artist")
    try:
        person = hq.people.create_person("Listener I")
        service = _service(hq)
        service.record_stage(
            person.id,
            "ADVOCATE",
            source_kind="OBSERVED",
            source_ref="referral:event:1",
        )
        service.record_consent(
            person.id,
            "DM",
            "OPTED_OUT",
            source_kind="USER_DECLARED",
        )
        expected = service.snapshot(person.id)
        profile_id = hq.store.profile_id
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        actual = _service(reopened).snapshot(person.id)
        assert actual.fingerprint == expected.fingerprint
        assert actual.observed_stages == ("ADVOCATE",)
        assert actual.consent_status("DM") == "OPTED_OUT"
    finally:
        reopened.close()


def test_malformed_or_inferred_consent_in_owned_namespace_fails_closed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Adversarial Audience Artist")
    try:
        person = hq.people.create_person("Listener J")
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key=f"{FAN_EVIDENCE_PREFIX}.{person.id}.consent.bad_inference",
            value={
                "schema_version": FAN_JOURNEY_SCHEMA_VERSION,
                "kind": "CONSENT",
                "person_id": person.id,
                "channel": "EMAIL",
                "status": "OPTED_IN",
                "observed_at": None,
                "note": "model guessed this",
            },
            source_kind="INFERRED",
            source_ref="model:guess-1",
            confidence=0.99,
            twin_domain="UNSPECIFIED",
        )
        with pytest.raises(FanJourneyError, match="may not be inferred"):
            _service(hq).snapshot(person.id)
    finally:
        hq.close()
