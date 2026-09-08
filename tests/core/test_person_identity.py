from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from n0te.lineage import LineageCorruptionError, NotFoundError, ValidationError
from n0te.memory import HeadquartersMemory
from n0te.person_identity import (
    ExternalIdentity,
    IdentityResolution,
    PersonIdentityMemory,
)


def _identity_memory(hq: HeadquartersMemory) -> PersonIdentityMemory:
    return PersonIdentityMemory(hq.store, hq.people)


def test_same_name_people_and_external_labels_never_auto_merge(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Anti Flattening Artist")
    try:
        first = hq.people.create_person(
            "Jordan Lee",
            relationship_context="Mix engineer",
        )
        second = hq.people.create_person(
            "Jordan Lee",
            relationship_context="Playlist editor",
        )
        identities = _identity_memory(hq)
        gmail = identities.record_external_identity(
            realm="gmail",
            namespace="gmail.address",
            subject="jordan@example.com",
            display_label="Jordan Lee",
            source_kind="user_declared",
        )
        social = identities.record_external_identity(
            realm="social",
            namespace="instagram.handle",
            subject="@jordanlee",
            display_label="Jordan Lee",
            source_kind="observed",
            source_ref="instagram-profile-observation:2026-09-04",
        )

        assert first.id != second.id
        assert gmail.id != social.id
        assert identities.current_resolution(gmail.id) is None
        assert identities.current_resolution(social.id) is None

        review = identities.propose_link(gmail.id, first.id)
        assert review.state == "REVIEW_REQUIRED"
        assert review.local_reviewed_link is False
        assert second.id not in {review.person_id}
    finally:
        hq.close()


def test_observed_and_user_declared_identity_truth_remain_distinct(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Source Artist")
    try:
        identities = _identity_memory(hq)
        declared = identities.record_external_identity(
            realm="contacts",
            namespace="contacts.email",
            subject="casey@example.com",
            source_kind="USER_DECLARED",
        )
        observed = identities.record_external_identity(
            realm="gmail",
            namespace="gmail.sender",
            subject="casey@example.com",
            source_kind="OBSERVED",
            source_ref="gmail-message:msg_123",
        )

        assert declared.source_kind == "USER_DECLARED"
        assert declared.source_ref is None
        assert observed.source_kind == "OBSERVED"
        assert observed.source_ref == "gmail-message:msg_123"
        assert declared.provider_verified is False
        assert observed.provider_verified is False
        assert declared.canonical_person_proven is False
    finally:
        hq.close()


def test_observed_requires_provenance_and_kernel_cannot_self_verify_provider(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Provenance Artist")
    try:
        identities = _identity_memory(hq)

        with pytest.raises(ValidationError, match="requires source_ref"):
            identities.record_external_identity(
                realm="gmail",
                namespace="gmail.sender",
                subject="maya@example.com",
                source_kind="OBSERVED",
            )

        with pytest.raises(ValidationError, match="USER_DECLARED or OBSERVED"):
            identities.record_external_identity(
                realm="gmail",
                namespace="gmail.sender",
                subject="maya@example.com",
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider-token-that-must-not-be-trusted",
            )
    finally:
        hq.close()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("realm", None, "realm must be text"),
        ("namespace", True, "namespace must be text"),
        ("subject", 123, "subject must be text"),
        ("source_kind", False, "source_kind must be text"),
        ("realm", "UNSUPPORTED_REALM", "unsupported identity realm"),
    ],
)
def test_external_identity_semantic_inputs_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
    expected: str,
) -> None:
    root = (tmp_path / f"data-{field}-{type(value).__name__}").resolve()
    hq = HeadquartersMemory.create(root, "Boundary Artist")
    try:
        identities = _identity_memory(hq)
        kwargs: dict[str, object] = {
            "realm": "SOCIAL",
            "namespace": "instagram.handle",
            "subject": "@artist",
            "source_kind": "USER_DECLARED",
        }
        kwargs[field] = value
        with pytest.raises(ValidationError, match=expected):
            identities.record_external_identity(**kwargs)
    finally:
        hq.close()


def test_review_requires_explicit_link_or_rejection_and_blocks_parallel_path(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Review Artist")
    try:
        first = hq.people.create_person("Alex")
        second = hq.people.create_person("Alex")
        identities = _identity_memory(hq)
        external = identities.record_external_identity(
            realm="credits",
            namespace="credit.display",
            subject="Alex / producer credit #42",
            source_kind="OBSERVED",
            source_ref="credit-import:42",
        )

        pending = identities.propose_link(external.id, first.id)
        assert pending.state == "REVIEW_REQUIRED"
        assert pending.local_reviewed_link is False

        with pytest.raises(
            ValidationError,
            match="already has an unresolved or linked review",
        ):
            identities.propose_link(external.id, second.id)

        linked = identities.link(
            pending.review_id,
            reason="Artist reviewed the imported credit and chose this Person",
        )
        assert linked.state == "LINKED"
        assert linked.person_id == first.id
        assert linked.local_reviewed_link is True
        assert linked.destructive_person_merge is False
        assert linked.provider_verified is False
        assert identities.current_resolution(external.id) == linked
    finally:
        hq.close()


def test_rejection_releases_identity_for_later_review_without_erasing_history(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Reject Artist")
    try:
        first = hq.people.create_person("Morgan")
        second = hq.people.create_person("Morgan")
        identities = _identity_memory(hq)
        external = identities.record_external_identity(
            realm="opportunity",
            namespace="booking.contact",
            subject="morgan@example.com",
            source_kind="USER_DECLARED",
        )

        first_review = identities.propose_link(external.id, first.id)
        rejected = identities.reject(
            first_review.review_id,
            reason="Same name, but this is the promoter rather than the engineer",
        )
        assert rejected.state == "REJECTED"
        assert identities.current_resolution(external.id) is None

        second_review = identities.propose_link(external.id, second.id)
        assert second_review.review_id != first_review.review_id
        history = identities.reviews_for_identity(external.id)
        assert tuple(item.state for item in history) == (
            "REJECTED",
            "REVIEW_REQUIRED",
        )
        assert tuple(item.person_id for item in history) == (first.id, second.id)
    finally:
        hq.close()


def test_split_restores_uncertainty_and_preserves_original_link_event(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Split Artist")
    try:
        person = hq.people.create_person("Rae")
        identities = _identity_memory(hq)
        external = identities.record_external_identity(
            realm="collaborator",
            namespace="collab.account",
            subject="rae-remote-account",
            source_kind="OBSERVED",
            source_ref="collaboration-import:rae-remote-account",
        )
        pending = identities.propose_link(external.id, person.id)
        linked = identities.link(
            pending.review_id,
            reason="Artist confirmed the collaborator account",
        )
        split = identities.split(
            linked.review_id,
            reason="Later evidence showed the account was shared by two people",
        )

        assert split.state == "SPLIT"
        assert split.local_reviewed_link is False
        assert identities.current_resolution(external.id) is None
        events = identities.review_events(linked.review_id)
        assert tuple(event.state for event in events) == (
            "REVIEW_REQUIRED",
            "LINKED",
            "SPLIT",
        )
        assert events[1].note == "Artist confirmed the collaborator account"
        assert events[2].note == (
            "Later evidence showed the account was shared by two people"
        )
    finally:
        hq.close()


def test_invalid_review_transitions_are_refused_by_service_and_sql(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Transition Artist")
    try:
        person = hq.people.create_person("Taylor")
        identities = _identity_memory(hq)
        external = identities.record_external_identity(
            realm="social",
            namespace="social.handle",
            subject="@taylor",
            source_kind="USER_DECLARED",
        )
        review = identities.propose_link(external.id, person.id)

        with pytest.raises(ValidationError, match="only LINKED"):
            identities.split(review.review_id, reason="Cannot split before linking")

        linked = identities.link(review.review_id, reason="Confirmed locally")
        with pytest.raises(ValidationError, match="only REVIEW_REQUIRED"):
            identities.reject(linked.review_id, reason="Too late to reject")

        with pytest.raises(sqlite3.IntegrityError, match="invalid identity"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "INSERT INTO person_identity_review_events("
                    "id,review_id,state,note) VALUES(?,?,?,?)",
                    (
                        "idevent_illegal",
                        review.review_id,
                        "REJECTED",
                        "Direct SQL must not bypass the transition law",
                    ),
                )
    finally:
        hq.close()


def test_cross_profile_person_cannot_be_linked(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    first_hq = HeadquartersMemory.create(root, "First Artist")
    second_hq = HeadquartersMemory.create(root, "Second Artist")
    try:
        foreign_person = second_hq.people.create_person("Foreign Person")
        identities = _identity_memory(first_hq)
        external = identities.record_external_identity(
            realm="contacts",
            namespace="contacts.subject",
            subject="foreign@example.com",
            source_kind="USER_DECLARED",
        )
        with pytest.raises(NotFoundError, match="person not found"):
            identities.propose_link(external.id, foreign_person.id)
    finally:
        second_hq.close()
        first_hq.close()


def test_exact_external_identity_key_is_unique_without_guessing_equivalence(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Exact Identity Artist")
    try:
        identities = _identity_memory(hq)
        first = identities.record_external_identity(
            realm="social",
            namespace="Provider.Handle",
            subject="@CaseSensitive",
            source_kind="USER_DECLARED",
        )
        second = identities.record_external_identity(
            realm="social",
            namespace="provider.handle",
            subject="@casesensitive",
            source_kind="USER_DECLARED",
        )
        assert first.id != second.id
        assert first.namespace == second.namespace == "provider.handle"
        assert first.subject != second.subject

        with pytest.raises(ValidationError, match="already exists"):
            identities.record_external_identity(
                realm="SOCIAL",
                namespace="PROVIDER.HANDLE",
                subject="@CaseSensitive",
                source_kind="USER_DECLARED",
            )
    finally:
        hq.close()


def test_identity_history_survives_relaunch_and_can_be_reviewed_again(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Relaunch Artist")
    try:
        first = hq.people.create_person("Jamie One")
        second = hq.people.create_person("Jamie Two")
        identities = _identity_memory(hq)
        external = identities.record_external_identity(
            realm="gmail",
            namespace="gmail.sender",
            subject="jamie@example.com",
            source_kind="OBSERVED",
            source_ref="gmail-message:abc",
        )
        review = identities.propose_link(external.id, first.id)
        linked = identities.link(review.review_id, reason="Initial local review")
        identities.split(
            linked.review_id,
            reason="Later conversation disambiguated the two Jamies",
        )
        profile_id = hq.store.profile_id
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        identities = _identity_memory(reopened)
        restored = identities.get_external_identity(external.id)
        assert restored == external
        history = identities.reviews_for_identity(external.id)
        assert len(history) == 1
        assert history[0].state == "SPLIT"
        assert history[0].event_count == 3

        new_review = identities.propose_link(external.id, second.id)
        new_link = identities.link(
            new_review.review_id,
            reason="Artist explicitly chose the second canonical Person",
        )
        assert new_link.state == "LINKED"
        assert new_link.person_id == second.id
        assert len(identities.reviews_for_identity(external.id)) == 2
    finally:
        reopened.close()


def test_identity_operations_do_not_promote_evidence_or_mutate_people(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "No Promotion Artist")
    try:
        person = hq.people.create_person("Casey")
        people_before = hq.people.people()
        evidence_before = int(
            hq.store._conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_claims"
            ).fetchone()["n"]
        )
        identities = _identity_memory(hq)
        external = identities.record_external_identity(
            realm="credits",
            namespace="credit.subject",
            subject="credit-casey",
            source_kind="OBSERVED",
            source_ref="credits-import:casey",
        )
        review = identities.propose_link(external.id, person.id)
        identities.link(review.review_id, reason="Reviewed identity association")

        evidence_after = int(
            hq.store._conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_claims"
            ).fetchone()["n"]
        )
        assert evidence_after == evidence_before
        assert hq.people.people() == people_before
    finally:
        hq.close()


def test_identity_activity_receipts_are_append_only_visible(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Activity Artist")
    try:
        person = hq.people.create_person("Activity Person")
        identities = _identity_memory(hq)
        external = identities.record_external_identity(
            realm="contacts",
            namespace="contacts.email",
            subject="activity@example.com",
            source_kind="USER_DECLARED",
        )
        review = identities.propose_link(external.id, person.id)
        identities.link(review.review_id, reason="Reviewed locally")
        identities.split(review.review_id, reason="Association later withdrawn")

        event_types = [event.event_type for event in hq.activity.for_profile()]
        assert "EXTERNAL_IDENTITY_RECORDED" in event_types
        assert "IDENTITY_REVIEW_OPENED" in event_types
        assert "IDENTITY_REVIEW_REVIEW_REQUIRED" in event_types
        assert "IDENTITY_REVIEW_LINKED" in event_types
        assert "IDENTITY_REVIEW_SPLIT" in event_types
    finally:
        hq.close()


def test_direct_sql_cannot_rewrite_external_identity_or_review_history(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Immutable Artist")
    try:
        person = hq.people.create_person("Immutable Person")
        identities = _identity_memory(hq)
        external = identities.record_external_identity(
            realm="contacts",
            namespace="contacts.email",
            subject="immutable@example.com",
            source_kind="USER_DECLARED",
        )
        review = identities.propose_link(external.id, person.id)

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "UPDATE person_external_identities SET subject=? WHERE id=?",
                    ("rewritten@example.com", external.id),
                )

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "UPDATE person_identity_reviews SET person_id=? WHERE id=?",
                    ("person_forged", review.review_id),
                )

        origin = identities.review_events(review.review_id)[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "DELETE FROM person_identity_review_events WHERE id=?",
                    (origin.id,),
                )
    finally:
        hq.close()


def test_missing_integrity_hook_fails_closed_on_reopen(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Hook Artist")
    try:
        identities = _identity_memory(hq)
        identities.record_external_identity(
            realm="contacts",
            namespace="contacts.email",
            subject="hook@example.com",
            source_kind="USER_DECLARED",
        )
        hq.store._conn.execute(
            "DROP TRIGGER person_identity_external_immutable_update"
        )
        hq.store._conn.commit()
        profile_id = hq.store.profile_id
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        with pytest.raises(
            LineageCorruptionError,
            match="integrity hooks are incomplete",
        ):
            PersonIdentityMemory(reopened.store, reopened.people)
    finally:
        reopened.close()


def test_result_authority_fields_are_hard_false_and_not_constructor_forgeable(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Authority Artist")
    try:
        person = hq.people.create_person("Authority Person")
        identities = _identity_memory(hq)
        external = identities.record_external_identity(
            realm="contacts",
            namespace="contacts.email",
            subject="authority@example.com",
            source_kind="USER_DECLARED",
        )
        review = identities.propose_link(external.id, person.id)
        linked = identities.link(review.review_id, reason="Local review only")

        assert linked.provider_verified is False
        assert linked.destructive_person_merge is False
        assert linked.external_action_authorized is False
        assert linked.authority_effect == "UNCHANGED"

        with pytest.raises(TypeError):
            ExternalIdentity(
                sequence=1,
                id="extid_forged",
                artist_id=hq.store.primary_artist_id,
                realm="CONTACTS",
                namespace="contacts.email",
                subject="forged@example.com",
                display_label=None,
                source_kind="USER_DECLARED",
                source_ref=None,
                provider_verified=True,
            )

        with pytest.raises(TypeError):
            IdentityResolution(
                review_id="idreview_forged",
                external_identity=external,
                person_id=person.id,
                state="LINKED",
                note="forged",
                event_count=2,
                destructive_person_merge=True,
            )

        with pytest.raises(FrozenInstanceError):
            linked.external_action_authorized = True  # type: ignore[misc]
    finally:
        hq.close()
