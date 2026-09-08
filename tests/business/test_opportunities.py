from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from n0te.capture_opportunities import CaptureOpportunityService
from n0te.lineage import LineageCorruptionError, NotFoundError, ValidationError
from n0te.memory import HeadquartersMemory
from n0te.opportunities import BusinessOpportunityService


def _service(hq: HeadquartersMemory) -> BusinessOpportunityService:
    return BusinessOpportunityService(hq.store, hq.evidence, hq.people)


def _claim(
    hq: HeadquartersMemory,
    *,
    key: str,
    value: object,
    scope_kind: str = "ARTIST",
    scope_id: str | None = None,
    source_kind: str = "USER_DECLARED",
    source_ref: str | None = None,
    supersedes: tuple[str, ...] = (),
):
    if scope_id is None:
        if scope_kind == "PROFILE":
            scope_id = hq.store.profile_id
        elif scope_kind == "ARTIST":
            scope_id = hq.store.primary_artist_id
        else:
            raise AssertionError("explicit scope_id required for Song/Version evidence")
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


def _table_names(hq: HeadquartersMemory) -> set[str]:
    return {
        str(row["name"])
        for row in hq.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def test_artist_opportunity_is_source_bound_unscored_and_non_authorizing(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Opportunity Artist")
    try:
        source = _claim(
            hq,
            key="business.source.showcase",
            value={"listing": "Artist showcase submissions open"},
            source_kind="OBSERVED",
            source_ref="https://example.invalid/showcase",
        )
        opportunity = _service(hq).create(
            kind="PERFORMANCE",
            summary="Local artist showcase submission",
            source_claim_id=source.id,
            deadline_on="2026-10-01",
        )

        assert opportunity.artist_id == hq.store.primary_artist_id
        assert opportunity.profile_id == hq.store.profile_id
        assert opportunity.song_id is None
        assert opportunity.person_id is None
        assert opportunity.source_claim_id == source.id
        assert opportunity.source_kind == "OBSERVED"
        assert opportunity.source_ref == "https://example.invalid/showcase"
        assert opportunity.source_current is True
        assert opportunity.attention_state == "AVAILABLE"
        assert opportunity.authority == "EVIDENCE_ONLY"
        assert opportunity.application_authority_granted is False
        assert opportunity.messaging_authority_granted is False
        assert opportunity.acceptance_authority_granted is False
        assert opportunity.contract_authority_granted is False
        assert opportunity.payment_authority_granted is False
        assert opportunity.purchase_authority_granted is False
        assert opportunity.scheduling_authority_granted is False
        assert opportunity.publication_authority_granted is False
        assert opportunity.provider_authority_granted is False
        assert opportunity.external_action_authority_granted is False
        assert opportunity.obligation_created is False
        assert opportunity.followup_created is False
        assert opportunity.fit_score is None
        assert opportunity.readiness_score is None
        assert opportunity.value_score is None
        assert opportunity.cost_score is None
        assert opportunity.priority_score is None
        assert opportunity.predicted_success is None
    finally:
        hq.close()


def test_song_person_and_deadline_are_context_only_and_create_no_work_items(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Context Artist")
    try:
        song = hq.store.create_song("Single")
        person = hq.people.create_person("A&R Contact")
        source = _claim(
            hq,
            key="business.source.pitch",
            value={"message": "Send this Song for consideration"},
            scope_kind="SONG",
            scope_id=song.id,
        )
        tables_before = _table_names(hq)
        followups_before = hq.people.followups(person_id=person.id)

        opportunity = _service(hq).create(
            kind="PITCH",
            summary="Submit Single for playlist consideration",
            source_claim_id=source.id,
            song_id=song.id,
            person_id=person.id,
            deadline_on="2026-09-30",
        )

        assert opportunity.song_id == song.id
        assert opportunity.person_id == person.id
        assert opportunity.deadline_on == "2026-09-30"
        assert opportunity.obligation_created is False
        assert opportunity.followup_created is False
        assert _service(hq).for_song(song.id) == (opportunity,)
        assert _service(hq).for_person(person.id) == (opportunity,)
        assert hq.people.followups(person_id=person.id) == followups_before
        assert _table_names(hq) == tables_before
    finally:
        hq.close()


def test_profile_and_artist_evidence_can_contextualize_song_opportunity(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Broad Context Artist")
    try:
        song = hq.store.create_song("Song")
        profile_source = _claim(
            hq,
            key="business.source.profile-program",
            value={"program": "all profile projects eligible"},
            scope_kind="PROFILE",
        )
        artist_source = _claim(
            hq,
            key="business.source.artist-program",
            value={"program": "artist invited"},
        )
        service = _service(hq)

        first = service.create(
            kind="GRANT",
            summary="Profile-level development grant",
            source_claim_id=profile_source.id,
            song_id=song.id,
        )
        second = service.create(
            kind="PARTNERSHIP",
            summary="Artist-level partner program",
            source_claim_id=artist_source.id,
            song_id=song.id,
        )

        assert first.source_scope_kind == "PROFILE"
        assert second.source_scope_kind == "ARTIST"
        assert {item.id for item in service.for_song(song.id)} == {first.id, second.id}
    finally:
        hq.close()


def test_version_evidence_requires_exact_same_song_context(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Version Artist")
    try:
        song_a = hq.store.create_song("Song A")
        song_b = hq.store.create_song("Song B")
        version_a = hq.store.create_version(song_a.id, label="A1")
        source = _claim(
            hq,
            key="business.source.version-pitch",
            value={"version": version_a.id},
            scope_kind="VERSION",
            scope_id=version_a.id,
        )
        service = _service(hq)

        opportunity = service.create(
            kind="SYNC",
            summary="Pitch exact Song A version context",
            source_claim_id=source.id,
            song_id=song_a.id,
        )
        assert opportunity.source_scope_kind == "VERSION"
        assert opportunity.source_scope_id == version_a.id

        with pytest.raises(ValidationError, match="scope does not match"):
            service.create(
                kind="SYNC",
                summary="Wrong Song",
                source_claim_id=source.id,
                song_id=song_b.id,
            )
        with pytest.raises(ValidationError, match="scope does not match"):
            service.create(
                kind="SYNC",
                summary="Missing Song binding",
                source_claim_id=source.id,
            )
    finally:
        hq.close()


def test_person_binding_requires_people_memory_on_same_store(tmp_path: Path) -> None:
    hq_a = HeadquartersMemory.create((tmp_path / "a").resolve(), "Artist A")
    hq_b = HeadquartersMemory.create((tmp_path / "b").resolve(), "Artist B")
    try:
        foreign_person = hq_b.people.create_person("Foreign Contact")
        source = _claim(
            hq_a,
            key="business.source.person",
            value={"contact": "someone"},
        )
        with pytest.raises(NotFoundError, match="person not found"):
            _service(hq_a).create(
                kind="COLLABORATION",
                summary="Foreign person cannot cross profiles",
                source_claim_id=source.id,
                person_id=foreign_person.id,
            )
        with pytest.raises(TypeError, match="PeopleMemory on the same LineageStore"):
            BusinessOpportunityService(hq_a.store, hq_a.evidence, hq_b.people)
    finally:
        hq_a.close()
        hq_b.close()


def test_observed_measured_provider_and_inferred_sources_require_provenance(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Provenance Artist")
    try:
        service = _service(hq)
        for source_kind in ("OBSERVED", "MEASURED", "PROVIDER_VERIFIED", "INFERRED"):
            source = _claim(
                hq,
                key=f"business.source.{source_kind.lower()}",
                value={"source_kind": source_kind},
                source_kind=source_kind,
            )
            with pytest.raises(ValidationError, match="requires source_ref provenance"):
                service.create(
                    kind="OTHER",
                    summary=f"Provenance check {source_kind}",
                    source_claim_id=source.id,
                )
    finally:
        hq.close()


def test_provider_verified_source_is_consumed_without_source_upgrading_or_authority(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Provider Artist")
    try:
        source = _claim(
            hq,
            key="business.source.provider-listing",
            value={"listing_id": "listing-42"},
            source_kind="PROVIDER_VERIFIED",
            source_ref="provider:listings:42",
        )
        opportunity = _service(hq).create(
            kind="COMMISSION",
            summary="Provider-listed commission brief",
            source_claim_id=source.id,
        )
        representation = hq.evidence.get_claim(opportunity.representation_claim_id)

        assert opportunity.source_kind == "PROVIDER_VERIFIED"
        assert representation is not None
        assert representation.source_kind == "INFERRED"
        assert representation.source_ref == source.id
        assert opportunity.provider_authority_granted is False
        assert opportunity.application_authority_granted is False
        assert opportunity.external_action_authority_granted is False
    finally:
        hq.close()


def test_source_must_be_current_at_creation_and_supersession_requires_revalidation(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Freshness Artist")
    try:
        source = _claim(
            hq,
            key="business.source.deadline",
            value={"deadline": "2026-10-01"},
            source_kind="OBSERVED",
            source_ref="email:thread:1",
        )
        service = _service(hq)
        opportunity = service.create(
            kind="PITCH",
            summary="Submit before listed deadline",
            source_claim_id=source.id,
            deadline_on="2026-10-01",
        )
        replacement = _claim(
            hq,
            key=source.key,
            value={"deadline": "2026-10-05"},
            source_kind="OBSERVED",
            source_ref="email:thread:2",
            supersedes=(source.id,),
        )

        refreshed = service.get(opportunity.id)
        assert replacement.id != source.id
        assert refreshed is not None
        assert refreshed.source_claim_id == source.id
        assert refreshed.source_current is False
        assert refreshed.attention_state == "NEEDS_REVALIDATION"

        with pytest.raises(ValidationError, match="currently active evidence"):
            service.create(
                kind="PITCH",
                summary="Cannot create from superseded evidence",
                source_claim_id=source.id,
            )
    finally:
        hq.close()


def test_semantic_duplicate_from_same_source_and_context_is_rejected(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Duplicate Artist")
    try:
        source = _claim(
            hq,
            key="business.source.duplicate",
            value={"listing": "same"},
        )
        service = _service(hq)
        service.create(
            kind="JOB",
            summary="Session musician call",
            source_claim_id=source.id,
            deadline_on="2026-09-20",
        )
        with pytest.raises(ValidationError, match="semantically duplicate"):
            service.create(
                kind="JOB",
                summary="Session   musician call",
                source_claim_id=source.id,
                deadline_on="2026-09-20",
            )
    finally:
        hq.close()


def test_creator_capture_opportunity_cannot_masquerade_as_business_source(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Boundary Artist")
    try:
        basis = _claim(
            hq,
            key="creator.source.session-moment",
            value={"moment": "interesting studio decision"},
        )
        capture = CaptureOpportunityService(hq.store, hq.evidence).create_opportunity(
            basis_claim_id=basis.id,
            kind="PROCESS",
            summary="Capture studio decision",
            reason="Useful process context",
            suggested_mediums=("NOTE",),
        )

        with pytest.raises(ValidationError, match="independent non-opportunity source evidence"):
            _service(hq).create(
                kind="PARTNERSHIP",
                summary="Capture representation is not a business listing",
                source_claim_id=capture.revision_claim_id,
            )
    finally:
        hq.close()


def test_business_opportunity_representation_cannot_recursively_become_source(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Recursive Artist")
    try:
        source = _claim(
            hq,
            key="business.source.original",
            value={"listing": "one"},
        )
        first = _service(hq).create(
            kind="OTHER",
            summary="Original opportunity",
            source_claim_id=source.id,
        )
        with pytest.raises(ValidationError, match="independent non-opportunity source evidence"):
            _service(hq).create(
                kind="OTHER",
                summary="Cannot derive from normalized Opportunity representation",
                source_claim_id=first.representation_claim_id,
            )
    finally:
        hq.close()


def test_invalid_deadline_and_semantic_inputs_fail_closed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Input Artist")
    try:
        source = _claim(
            hq,
            key="business.source.input",
            value={"ok": True},
        )
        service = _service(hq)
        with pytest.raises(ValidationError, match="ISO calendar date"):
            service.create(
                kind="GRANT",
                summary="Bad date",
                source_claim_id=source.id,
                deadline_on="10/07/2026",
            )
        with pytest.raises(ValidationError, match="kind must be text"):
            service.create(  # type: ignore[arg-type]
                kind=7,
                summary="Bad kind type",
                source_claim_id=source.id,
            )
        with pytest.raises(ValidationError, match="summary must be text"):
            service.create(  # type: ignore[arg-type]
                kind="OTHER",
                summary=7,
                source_claim_id=source.id,
            )
        with pytest.raises(ValidationError, match="unsupported business opportunity kind"):
            service.create(
                kind="VIRAL_SCORE",
                summary="Unknown semantic kind",
                source_claim_id=source.id,
            )
    finally:
        hq.close()


def test_restart_persists_through_evidence_without_second_opportunity_store(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Restart Artist")
    profile_id = hq.store.profile_id
    try:
        person = hq.people.create_person("Manager")
        song = hq.store.create_song("Song")
        source = _claim(
            hq,
            key="business.source.restart",
            value={"listing": "persistent"},
            scope_kind="SONG",
            scope_id=song.id,
        )
        tables_before = _table_names(hq)
        opportunity = _service(hq).create(
            kind="PARTNERSHIP",
            summary="Persistent partner opportunity",
            source_claim_id=source.id,
            song_id=song.id,
            person_id=person.id,
        )
        opportunity_id = opportunity.id
        assert _table_names(hq) == tables_before
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        recovered = _service(reopened).get(opportunity_id)
        assert recovered is not None
        assert recovered.id == opportunity_id
        assert recovered.source_current is True
        assert not any(
            name.startswith("business_opportun") or name == "opportunities"
            for name in _table_names(reopened)
        )
    finally:
        reopened.close()


def test_malformed_owned_namespace_fails_closed_on_service_open(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Corruption Artist")
    try:
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key="business.opportunity.corrupt",
            value={"wrong": "shape"},
            source_kind="INFERRED",
            source_ref="evidence:malformed-test",
            twin_domain="UNSPECIFIED",
        )
        with pytest.raises(LineageCorruptionError, match="payload shape"):
            _service(hq)
    finally:
        hq.close()


def test_canonical_evidence_immutability_blocks_opportunity_rewrite(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Immutable Artist")
    try:
        source = _claim(
            hq,
            key="business.source.immutable",
            value={"listing": "immutable"},
        )
        opportunity = _service(hq).create(
            kind="PRESS",
            summary="Interview invitation context",
            source_claim_id=source.id,
        )
        with pytest.raises(sqlite3.IntegrityError, match="evidence claims are immutable"):
            hq.store._conn.execute(
                "UPDATE evidence_claims SET value_json='{}' WHERE id=?",
                (opportunity.representation_claim_id,),
            )
    finally:
        hq.close()
