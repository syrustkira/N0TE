from __future__ import annotations

import sqlite3

import pytest

from n0te.activity import ActivityLog
from n0te.collaboration_handoff import (
    ACCESS_ROLES,
    MAX_FEEDBACK_POSITION_MS,
    CollaborationHandoffMemory,
    CollaboratorFeedback,
    HandoffPackage,
)
from n0te.evidence import EvidenceMemory
from n0te.lineage import (
    LineageCorruptionError,
    LineageStore,
    NotFoundError,
    ValidationError,
)
from n0te.people import PeopleMemory


def _stack(tmp_path, artist_name: str = "Handoff Artist"):
    store = LineageStore.create(tmp_path, artist_name)
    EvidenceMemory(store)
    activity = ActivityLog(store)
    people = PeopleMemory(store)
    handoffs = CollaborationHandoffMemory(store, people)
    return store, activity, people, handoffs


def _version(store: LineageStore, song_id: str, ordinal_char: str, label: str):
    asset = store.attach_asset(
        song_id,
        name=f"{label}.wav",
        sha256=ordinal_char * 64,
        source_uri=f"fixture://{label}",
    )
    return store.create_version(
        song_id,
        label=label,
        asset_ids=(asset.id,),
        make_current=True,
    )


def _seed(tmp_path):
    store, activity, people, handoffs = _stack(tmp_path)
    song = store.create_song("Exact Song")
    person = people.create_person("Collaborator", relationship_context="Mix feedback")
    first = _version(store, song.id, "a", "v1")
    return store, activity, people, handoffs, song, person, first


def test_prepare_package_binds_exact_person_song_version_assets_and_no_external_authority(tmp_path):
    store, _, _, handoffs, song, person, version = _seed(tmp_path)
    try:
        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=version.id,
            person_id=person.id,
            access_role="review",
            label="Chorus review",
        )
        assert ACCESS_ROLES == ("VIEW", "REVIEW", "CONTRIBUTE")
        assert package.artist_id == store.primary_artist_id
        assert package.song_id == song.id
        assert package.version_id == version.id
        assert package.person_id == person.id
        assert package.access_role == "REVIEW"
        assert package.label == "Chorus review"
        assert package.state == "PREPARED"
        assert package.locally_available is True
        assert package.asset_ids == store.version_asset_ids(version.id)
        assert package.external_share_executed is False
        assert package.provider_access_granted is False
        assert package.external_revocation_verified is False
    finally:
        store.close()


def test_package_can_target_older_or_approved_version_without_conflating_latest(tmp_path):
    store, _, _, handoffs, song, person, first = _seed(tmp_path)
    try:
        store.approve_version(song.id, first.id)
        second = _version(store, song.id, "b", "v2")
        current = store.active_song()
        assert current is not None
        assert current.current_version_id == second.id
        assert current.approved_version_id == first.id

        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=first.id,
            person_id=person.id,
            access_role="VIEW",
        )
        assert package.version_id == first.id
        assert package.version_id != current.current_version_id
        assert package.version_id == current.approved_version_id
    finally:
        store.close()


def test_prepare_rejects_cross_song_version_missing_person_and_assetless_version(tmp_path):
    store, _, _, handoffs, song, person, first = _seed(tmp_path)
    try:
        other = store.create_song("Other Song")
        other_version = _version(store, other.id, "b", "other-v1")

        with pytest.raises(ValidationError):
            handoffs.prepare_package(
                song_id=song.id,
                version_id=other_version.id,
                person_id=person.id,
                access_role="VIEW",
            )
        with pytest.raises(NotFoundError):
            handoffs.prepare_package(
                song_id=song.id,
                version_id=first.id,
                person_id="person_missing",
                access_role="VIEW",
            )
        empty = store.create_version(song.id, label="No asset", make_current=False)
        with pytest.raises(ValidationError):
            handoffs.prepare_package(
                song_id=song.id,
                version_id=empty.id,
                person_id=person.id,
                access_role="VIEW",
            )
    finally:
        store.close()


def test_only_one_prepared_package_per_person_version_and_revoke_allows_replacement(tmp_path):
    store, _, _, handoffs, song, person, version = _seed(tmp_path)
    try:
        first = handoffs.prepare_package(
            song_id=song.id,
            version_id=version.id,
            person_id=person.id,
            access_role="VIEW",
        )
        with pytest.raises(ValidationError):
            handoffs.prepare_package(
                song_id=song.id,
                version_id=version.id,
                person_id=person.id,
                access_role="REVIEW",
            )

        revoked = handoffs.revoke_package(first.id, reason="Review window ended")
        assert revoked.state == "REVOKED"
        assert revoked.locally_available is False
        assert revoked.revocation_reason == "Review window ended"
        assert revoked.external_revocation_verified is False

        replacement = handoffs.prepare_package(
            song_id=song.id,
            version_id=version.id,
            person_id=person.id,
            access_role="REVIEW",
        )
        assert replacement.id != first.id
        assert replacement.access_role == "REVIEW"
    finally:
        store.close()


def test_revocation_is_idempotent_only_for_same_durable_reason(tmp_path):
    store, _, _, handoffs, song, person, version = _seed(tmp_path)
    try:
        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=version.id,
            person_id=person.id,
            access_role="VIEW",
        )
        first = handoffs.revoke_package(package.id, reason="Superseded")
        second = handoffs.revoke_package(package.id, reason="Superseded")
        assert second == first
        with pytest.raises(ValidationError):
            handoffs.revoke_package(package.id, reason="Different reason")
    finally:
        store.close()


def test_timecoded_feedback_is_attributed_but_not_verified_or_promoted(tmp_path):
    store, _, _, handoffs, song, person, version = _seed(tmp_path)
    try:
        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=version.id,
            person_id=person.id,
            access_role="REVIEW",
        )
        before = int(
            store._conn.execute("SELECT COUNT(*) AS n FROM evidence_claims").fetchone()["n"]
        )
        feedback = handoffs.record_feedback(
            package.id,
            body="The vocal feels late here.",
            position_ms=42_250,
        )
        after = int(
            store._conn.execute("SELECT COUNT(*) AS n FROM evidence_claims").fetchone()["n"]
        )
        assert feedback.package_id == package.id
        assert feedback.person_id == person.id
        assert feedback.song_id == song.id
        assert feedback.version_id == version.id
        assert feedback.position_ms == 42_250
        assert feedback.body == "The vocal feels late here."
        assert feedback.state == "OPEN"
        assert feedback.attribution_verified is False
        assert feedback.external_message_received_verified is False
        assert feedback.artist_preference_promoted is False
        assert after == before
    finally:
        store.close()


def test_revocation_blocks_new_feedback_but_existing_feedback_can_resolve(tmp_path):
    store, _, _, handoffs, song, person, source = _seed(tmp_path)
    try:
        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=source.id,
            person_id=person.id,
            access_role="REVIEW",
        )
        feedback = handoffs.record_feedback(package.id, body="Try a shorter tail.")
        handoffs.revoke_package(package.id, reason="Package replaced")

        with pytest.raises(ValidationError):
            handoffs.record_feedback(package.id, body="Late feedback")

        response = _version(store, song.id, "b", "v2")
        resolved = handoffs.resolve_feedback(
            feedback.id,
            outcome="ADDRESSED",
            note="Shortened the tail for comparison.",
            response_version_id=response.id,
        )
        assert resolved.state == "ADDRESSED"
        assert resolved.response_version_id == response.id
    finally:
        store.close()


def test_addressed_feedback_requires_later_exact_same_song_version(tmp_path):
    store, _, _, handoffs, song, person, source = _seed(tmp_path)
    try:
        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=source.id,
            person_id=person.id,
            access_role="REVIEW",
        )
        feedback = handoffs.record_feedback(package.id, body="Change the transition.")

        with pytest.raises(ValidationError):
            handoffs.resolve_feedback(
                feedback.id,
                outcome="ADDRESSED",
                note="No actual later version.",
                response_version_id=source.id,
            )

        other_song = store.create_song("Foreign Song")
        foreign = _version(store, other_song.id, "b", "foreign-v1")
        with pytest.raises(ValidationError):
            handoffs.resolve_feedback(
                feedback.id,
                outcome="ADDRESSED",
                note="Wrong song.",
                response_version_id=foreign.id,
            )

        response = _version(store, song.id, "c", "v2")
        resolved = handoffs.resolve_feedback(
            feedback.id,
            outcome="ADDRESSED",
            note="Created a later same-Song revision.",
            response_version_id=response.id,
        )
        assert resolved.state == "ADDRESSED"
        assert resolved.response_version_id == response.id
    finally:
        store.close()


def test_dismissed_feedback_cannot_claim_revision_and_resolution_is_immutable(tmp_path):
    store, _, _, handoffs, song, person, source = _seed(tmp_path)
    try:
        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=source.id,
            person_id=person.id,
            access_role="REVIEW",
        )
        feedback = handoffs.record_feedback(package.id, body="Make it brighter.")
        response = _version(store, song.id, "b", "v2")

        with pytest.raises(ValidationError):
            handoffs.resolve_feedback(
                feedback.id,
                outcome="DISMISSED",
                note="Not for this direction.",
                response_version_id=response.id,
            )

        resolved = handoffs.resolve_feedback(
            feedback.id,
            outcome="DISMISSED",
            note="Not for this direction.",
        )
        assert handoffs.resolve_feedback(
            feedback.id,
            outcome="DISMISSED",
            note="Not for this direction.",
        ) == resolved

        with pytest.raises(ValidationError):
            handoffs.resolve_feedback(
                feedback.id,
                outcome="ADDRESSED",
                note="Changed my mind.",
                response_version_id=response.id,
            )
    finally:
        store.close()


def test_relaunch_preserves_package_feedback_revision_and_activity(tmp_path):
    root = tmp_path.resolve()
    store, activity, people, handoffs = _stack(root)
    profile_id = store.profile_id
    song = store.create_song("Relaunch Song")
    person = people.create_person("Reviewer")
    source = _version(store, song.id, "a", "v1")
    checkpoint = activity.checkpoint()

    package = handoffs.prepare_package(
        song_id=song.id,
        version_id=source.id,
        person_id=person.id,
        access_role="REVIEW",
    )
    feedback = handoffs.record_feedback(
        package.id, body="Tighten this entrance.", position_ms=12_000
    )
    response = _version(store, song.id, "b", "v2")
    handoffs.resolve_feedback(
        feedback.id,
        outcome="ADDRESSED",
        note="Tightened entrance in v2.",
        response_version_id=response.id,
    )
    handoffs.revoke_package(package.id, reason="Review completed")
    store.close()

    reopened = LineageStore.open(root, profile_id)
    try:
        EvidenceMemory(reopened)
        reopened_activity = ActivityLog(reopened)
        reopened_people = PeopleMemory(reopened)
        reopened_handoffs = CollaborationHandoffMemory(reopened, reopened_people)

        durable_package = reopened_handoffs.get_package(package.id)
        durable_feedback = reopened_handoffs.get_feedback(feedback.id)
        assert durable_package is not None
        assert durable_package.state == "REVOKED"
        assert durable_package.version_id == source.id
        assert durable_feedback is not None
        assert durable_feedback.state == "ADDRESSED"
        assert durable_feedback.response_version_id == response.id
        assert durable_feedback.person_id == person.id

        events = reopened_activity.for_song(song.id, after_sequence=checkpoint)
        owned = [
            event.event_type
            for event in events
            if event.object_type in {"COLLAB_HANDOFF", "COLLAB_FEEDBACK"}
        ]
        assert owned == [
            "COLLAB_HANDOFF_PREPARED",
            "COLLAB_FEEDBACK_RECORDED",
            "COLLAB_FEEDBACK_ADDRESSED",
            "COLLAB_HANDOFF_REVOKED",
        ]
    finally:
        reopened.close()


def test_sql_tamper_cannot_rebind_delete_or_reopen_history(tmp_path):
    store, _, _, handoffs, song, person, source = _seed(tmp_path)
    try:
        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=source.id,
            person_id=person.id,
            access_role="REVIEW",
        )
        feedback = handoffs.record_feedback(package.id, body="One note")
        handoffs.resolve_feedback(
            feedback.id,
            outcome="DISMISSED",
            note="Artist chose another direction.",
        )
        conn = store._conn

        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE collab_handoff_packages SET access_role='CONTRIBUTE' WHERE id=?",
                (package.id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM collab_handoff_packages WHERE id=?", (package.id,))
        conn.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE collab_handoff_feedback SET state='OPEN',resolution_note=NULL "
                "WHERE id=?",
                (feedback.id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM collab_handoff_feedback WHERE id=?", (feedback.id,))
        conn.rollback()

        assert handoffs.get_package(package.id) is not None
        durable_feedback = handoffs.get_feedback(feedback.id)
        assert durable_feedback is not None
        assert durable_feedback.state == "DISMISSED"
    finally:
        store.close()


@pytest.mark.parametrize(
    "integrity_sql",
    (
        "DROP TRIGGER collab_package_delete_immutable",
        "DROP INDEX collab_one_prepared_package_per_person_version",
    ),
)
def test_missing_integrity_hook_or_index_fails_closed(integrity_sql, tmp_path):
    store, _, people, handoffs, _, _, _ = _seed(tmp_path)
    try:
        store._conn.execute(integrity_sql)
        store._conn.commit()
        with pytest.raises(LineageCorruptionError):
            CollaborationHandoffMemory(store, people)
    finally:
        store.close()


@pytest.mark.parametrize("role", ("DO_IT", "ADMIN", "OWNER", "", 1, None))
def test_access_roles_fail_closed(role, tmp_path):
    store, _, _, handoffs, song, person, version = _seed(tmp_path)
    try:
        with pytest.raises(ValidationError):
            handoffs.prepare_package(
                song_id=song.id,
                version_id=version.id,
                person_id=person.id,
                access_role=role,  # type: ignore[arg-type]
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    "position",
    (-1, MAX_FEEDBACK_POSITION_MS + 1, True, 1.5, "1000"),
)
def test_feedback_timecode_requires_bounded_integer_milliseconds(position, tmp_path):
    store, _, _, handoffs, song, person, version = _seed(tmp_path)
    try:
        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=version.id,
            person_id=person.id,
            access_role="REVIEW",
        )
        with pytest.raises(ValidationError):
            handoffs.record_feedback(
                package.id,
                body="Time-coded note",
                position_ms=position,  # type: ignore[arg-type]
            )
    finally:
        store.close()


def test_semantic_ids_are_not_coerced_from_non_text_values(tmp_path):
    store, _, _, handoffs, song, person, version = _seed(tmp_path)
    try:
        with pytest.raises(ValidationError):
            handoffs.prepare_package(
                song_id=123,  # type: ignore[arg-type]
                version_id=version.id,
                person_id=person.id,
                access_role="VIEW",
            )
        package = handoffs.prepare_package(
            song_id=song.id,
            version_id=version.id,
            person_id=person.id,
            access_role="VIEW",
        )
        with pytest.raises(ValidationError):
            handoffs.get_package(None)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            handoffs.record_feedback(package.id, body=42)  # type: ignore[arg-type]
    finally:
        store.close()


def test_result_objects_cannot_be_forged_into_external_authority_or_preference():
    package_args = dict(
        sequence=1,
        id="handoff_x",
        artist_id="artist_x",
        song_id="song_x",
        version_id="version_x",
        person_id="person_x",
        access_role="VIEW",
        label=None,
        state="PREPARED",
        revocation_reason=None,
        asset_ids=("asset_x",),
    )
    with pytest.raises(TypeError):
        HandoffPackage(**package_args, provider_access_granted=True)
    with pytest.raises(TypeError):
        HandoffPackage(**package_args, external_share_executed=True)

    feedback_args = dict(
        sequence=1,
        id="feedback_x",
        package_id="handoff_x",
        artist_id="artist_x",
        song_id="song_x",
        version_id="version_x",
        person_id="person_x",
        position_ms=None,
        body="note",
        state="OPEN",
        response_version_id=None,
        resolution_note=None,
    )
    with pytest.raises(TypeError):
        CollaboratorFeedback(**feedback_args, artist_preference_promoted=True)
    with pytest.raises(TypeError):
        CollaboratorFeedback(**feedback_args, attribution_verified=True)
