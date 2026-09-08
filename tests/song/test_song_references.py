from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from n0te.evidence import EvidenceMemory
from n0te.lineage import LineageStore
from n0te.song_references import (
    REFERENCE_KEY_PREFIX,
    ReferenceDecision,
    SongReference,
    SongReferenceError,
    SongReferenceIntegrityError,
    SongReferenceStore,
    reference_public_fields,
)


def _store(tmp_path: Path) -> tuple[LineageStore, str]:
    store = LineageStore.create(tmp_path, "Reference Artist")
    song = store.create_song("Reference Song")
    return store, song.id


def _create_reference(
    references: SongReferenceStore,
    song_id: str,
    **overrides,
) -> SongReference:
    values = {
        "song_id": song_id,
        "title": "North Star",
        "source_type": "CATALOG_RECORDING",
        "source_locator": "catalog:artist/track",
        "comparison_dimensions": ["low-end", "vocal space", "LOW END"],
        "loudness_match_policy": "MATCH_BEFORE_COMPARISON",
    }
    values.update(overrides)
    return references.create_reference(**values)


def test_reference_round_trips_through_canonical_evidence_memory(tmp_path: Path):
    store, song_id = _store(tmp_path)
    profile_id = store.profile_id
    references = SongReferenceStore(store)

    created = _create_reference(references, song_id)
    assert created.song_id == song_id
    assert created.comparison_dimensions == ("LOW_END", "VOCAL_SPACE")
    assert created.loudness_match_policy == "MATCH_BEFORE_COMPARISON"
    assert created.source_kind == "USER_DECLARED"
    store.close()

    reopened = LineageStore.open(tmp_path, profile_id)
    try:
        persisted = SongReferenceStore(reopened).references_for_song(song_id)
        assert len(persisted) == 1
        assert persisted[0] == created
    finally:
        reopened.close()


def test_reference_can_bind_exact_version_and_section_locator(tmp_path: Path):
    store, song_id = _store(tmp_path)
    try:
        version = store.create_version(song_id, label="Mix 4")
        reference = _create_reference(
            SongReferenceStore(store),
            song_id,
            version_id=version.id,
            section_locator="chorus-2",
        )
        assert reference.version_id == version.id
        assert reference.section_locator == "chorus-2"
    finally:
        store.close()


def test_cross_song_version_binding_fails_closed(tmp_path: Path):
    store, first_song_id = _store(tmp_path)
    try:
        version = store.create_version(first_song_id, label="First")
        second = store.create_song("Other Song")
        with pytest.raises(SongReferenceError, match="different Song"):
            _create_reference(
                SongReferenceStore(store),
                second.id,
                version_id=version.id,
            )
    finally:
        store.close()


def test_source_provenance_is_explicit_and_does_not_verify_rights(tmp_path: Path):
    store, song_id = _store(tmp_path)
    try:
        references = SongReferenceStore(store)
        with pytest.raises(SongReferenceError, match="requires source_ref"):
            _create_reference(
                references,
                song_id,
                source_kind="PROVIDER_VERIFIED",
            )

        observed = _create_reference(
            references,
            song_id,
            title="Local bounce",
            source_type="LOCAL_AUDIO",
            source_locator="asset:reference-bounce",
            source_kind="OBSERVED",
            source_ref="asset:abc123",
            confidence=0.9,
        )
        assert observed.source_kind == "OBSERVED"
        assert observed.source_ref == "asset:abc123"
        assert observed.confidence == 0.9
        assert not hasattr(observed, "rights_verified")
        assert not hasattr(observed, "licensed")
    finally:
        store.close()


def test_note_and_decision_bind_exact_reference_revision(tmp_path: Path):
    store, song_id = _store(tmp_path)
    try:
        references = SongReferenceStore(store)
        original = _create_reference(references, song_id)
        note = references.record_note(
            song_id,
            original.reference_id,
            "The chorus feels wide without losing the center.",
        )
        decision = references.record_decision(
            song_id,
            original.reference_id,
            "KEEP_REFERENCE",
            "Keep it for vocal-space and width decisions.",
        )

        revised = references.revise_reference(
            song_id,
            original.reference_id,
            title="North Star, narrower use",
            source_type="CATALOG_RECORDING",
            source_locator="catalog:artist/track",
            comparison_dimensions=["vocal space"],
            loudness_match_policy="DO_NOT_MATCH",
        )

        assert revised.reference_claim_id != original.reference_claim_id
        assert note.reference_claim_id == original.reference_claim_id
        assert decision.reference_claim_id == original.reference_claim_id
        assert references.reference_revision(
            song_id, original.reference_claim_id
        ) == original
        assert references.notes_for_reference(
            song_id, original.reference_id
        ) == (note,)
        assert references.decisions_for_reference(
            song_id, original.reference_id
        ) == (decision,)
    finally:
        store.close()


def test_stop_using_reference_is_history_not_destructive_delete(tmp_path: Path):
    store, song_id = _store(tmp_path)
    try:
        references = SongReferenceStore(store)
        reference = _create_reference(references, song_id)
        decision = references.record_decision(
            song_id,
            reference.reference_id,
            "STOP_USING_REFERENCE",
            "It is pulling the low end away from this Song's intent.",
        )

        current = references.get_reference(song_id, reference.reference_id)
        assert current == reference
        assert decision.decision == "STOP_USING_REFERENCE"
        assert references.decisions_for_reference(
            song_id, reference.reference_id
        ) == (decision,)
    finally:
        store.close()


def test_malformed_reference_namespace_evidence_fails_closed(tmp_path: Path):
    store, song_id = _store(tmp_path)
    try:
        evidence = EvidenceMemory(store)
        reference_id = "ref_" + "a" * 32
        evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_id,
            key=f"{REFERENCE_KEY_PREFIX}{reference_id}",
            value={"schema_version": 1, "reference_id": reference_id},
            source_kind="USER_DECLARED",
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )
        with pytest.raises(SongReferenceIntegrityError):
            SongReferenceStore(store, evidence).references_for_song(song_id)
    finally:
        store.close()


def test_conflicting_active_reference_revisions_fail_closed(tmp_path: Path):
    store, song_id = _store(tmp_path)
    try:
        evidence = EvidenceMemory(store)
        reference_id = "ref_" + "b" * 32
        key = f"{REFERENCE_KEY_PREFIX}{reference_id}"
        value = {
            "schema_version": 1,
            "reference_id": reference_id,
            "title": "Conflicting reference",
            "source_type": "OTHER",
            "source_locator": "declared:one",
            "version_id": None,
            "section_locator": None,
            "comparison_dimensions": ["ARRANGEMENT"],
            "loudness_match_policy": "UNSPECIFIED",
        }
        evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_id,
            key=key,
            value=value,
            source_kind="USER_DECLARED",
            twin_domain="UNSPECIFIED",
        )
        evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_id,
            key=key,
            value={**value, "source_locator": "declared:two"},
            source_kind="USER_DECLARED",
            twin_domain="UNSPECIFIED",
        )

        references = SongReferenceStore(store, evidence)
        with pytest.raises(
            SongReferenceIntegrityError, match="conflicting active revisions"
        ):
            references.get_reference(song_id, reference_id)
        with pytest.raises(SongReferenceIntegrityError):
            references.references_for_song(song_id)
    finally:
        store.close()


def test_reference_shape_cannot_masquerade_as_target_or_execution_authority():
    public = set(reference_public_fields())
    assert public == {field.name for field in fields(SongReference)}
    forbidden = {
        "target",
        "score",
        "rating",
        "rights_verified",
        "licensed",
        "lufs",
        "gain_db",
        "mutation_authorized",
        "external_action_authorized",
        "execution_authority",
        "provider_action",
        "daw_action",
    }
    assert public.isdisjoint(forbidden)
    assert set(ReferenceDecision.__dataclass_fields__) == {
        "decision_id",
        "claim_id",
        "sequence",
        "song_id",
        "reference_id",
        "reference_claim_id",
        "decision",
        "reason",
    }


def test_invalid_dimension_and_boolean_confidence_do_not_coerce(tmp_path: Path):
    store, song_id = _store(tmp_path)
    try:
        references = SongReferenceStore(store)
        with pytest.raises(SongReferenceError):
            _create_reference(
                references,
                song_id,
                comparison_dimensions=["mix/balance"],
            )
        with pytest.raises(SongReferenceError, match="confidence"):
            _create_reference(references, song_id, confidence=True)
    finally:
        store.close()
