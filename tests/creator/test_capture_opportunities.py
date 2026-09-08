from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from n0te.capture_opportunities import CaptureOpportunityService
from n0te.lineage import LineageCorruptionError, ValidationError
from n0te.memory import HeadquartersMemory


def _service(hq: HeadquartersMemory) -> CaptureOpportunityService:
    return CaptureOpportunityService(hq.store, hq.evidence)


def _claim(
    hq: HeadquartersMemory,
    *,
    key: str,
    value: object,
    song_id: str | None = None,
    version_id: str | None = None,
    source_kind: str = "USER_DECLARED",
    source_ref: str | None = None,
    supersedes: tuple[str, ...] = (),
):
    if version_id is not None:
        scope_kind = "VERSION"
        scope_id = version_id
    elif song_id is not None:
        scope_kind = "SONG"
        scope_id = song_id
    else:
        scope_kind = "ARTIST"
        scope_id = hq.store.primary_artist_id
    return hq.evidence.record_claim(
        scope_kind=scope_kind,
        scope_id=scope_id,
        key=key,
        value=value,
        source_kind=source_kind,
        source_ref=source_ref,
        confidence=1.0,
        twin_domain="CREATIVE",
        supersedes=supersedes,
    )


def test_song_capture_opportunity_preserves_basis_and_grants_no_authority(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Capture Artist")
    try:
        song = hq.store.create_song("Capture Song")
        basis = _claim(
            hq,
            key="creator.work.breakthrough",
            value={"event": "chorus arrangement clicked"},
            song_id=song.id,
            source_kind="OBSERVED",
            source_ref="session:chorus-pass-4",
        )
        opportunity = _service(hq).create_opportunity(
            basis_claim_id=basis.id,
            kind="PROCESS",
            summary="Show the chorus before and after the arrangement change",
            reason="The work evidence marks a concrete creative turning point.",
            suggested_mediums=["screen", "audio", "screen"],
            song_id=song.id,
        )

        assert opportunity.song_id == song.id
        assert opportunity.version_id is None
        assert opportunity.kind == "PROCESS"
        assert opportunity.status == "OPEN"
        assert opportunity.basis_claim_id == basis.id
        assert opportunity.basis_source_kind == "OBSERVED"
        assert opportunity.basis_source_ref == "session:chorus-pass-4"
        assert opportunity.suggested_mediums == ("SCREEN", "AUDIO")
        assert opportunity.attention_state == "AVAILABLE"
        assert opportunity.worthiness_score is None
        assert opportunity.virality_score is None
        assert opportunity.recording_authority_granted is False
        assert opportunity.device_permission_authority_granted is False
        assert opportunity.file_write_authority_granted is False
        assert opportunity.publication_authority_granted is False
        assert opportunity.provider_authority_granted is False
        assert opportunity.external_action_authority_granted is False
        assert opportunity.obligation_created is False
        assert opportunity.preference_promoted is False
    finally:
        hq.close()


def test_exact_version_scope_rejects_other_song_or_version_basis(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Version Artist")
    try:
        song_a = hq.store.create_song("A")
        version_a = hq.store.create_version(song_a.id, label="A1")
        version_a2 = hq.store.create_version(song_a.id, label="A2")
        song_b = hq.store.create_song("B")
        version_b = hq.store.create_version(song_b.id, label="B1")
        basis_a = _claim(
            hq,
            key="creator.work.version-a",
            value={"moment": "vocal comp"},
            version_id=version_a.id,
        )
        service = _service(hq)

        with pytest.raises(ValidationError, match="scope does not match"):
            service.create_opportunity(
                basis_claim_id=basis_a.id,
                kind="DECISION",
                summary="Explain the vocal comp decision",
                reason="Exact Version evidence exists.",
                suggested_mediums=["NOTE"],
                song_id=song_a.id,
                version_id=version_a2.id,
            )
        with pytest.raises(ValidationError, match="different Song"):
            service.create_opportunity(
                basis_claim_id=basis_a.id,
                kind="DECISION",
                summary="Explain the vocal comp decision",
                reason="Wrong Song must fail closed.",
                suggested_mediums=["NOTE"],
                song_id=song_b.id,
                version_id=version_a.id,
            )

        basis_b = _claim(
            hq,
            key="creator.work.version-b",
            value={"moment": "performance"},
            version_id=version_b.id,
        )
        opportunity = service.create_opportunity(
            basis_claim_id=basis_b.id,
            kind="PERFORMANCE",
            summary="Preserve the performance moment",
            reason="Exact Version evidence is available.",
            suggested_mediums=["AUDIO"],
            song_id=song_b.id,
            version_id=version_b.id,
        )
        assert opportunity.version_id == version_b.id
    finally:
        hq.close()


def test_nondeclared_basis_requires_provenance(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Provenance Artist")
    try:
        song = hq.store.create_song("Song")
        for source_kind in ("OBSERVED", "MEASURED", "PROVIDER_VERIFIED", "INFERRED"):
            basis = _claim(
                hq,
                key=f"creator.work.{source_kind.lower()}",
                value={"source": source_kind},
                song_id=song.id,
                source_kind=source_kind,
            )
            with pytest.raises(ValidationError, match="requires source_ref provenance"):
                _service(hq).create_opportunity(
                    basis_claim_id=basis.id,
                    kind="STORY",
                    summary=f"Potential {source_kind} story",
                    reason="Provenance is intentionally missing.",
                    suggested_mediums=["NOTE"],
                    song_id=song.id,
                )
    finally:
        hq.close()


def test_duplicate_semantic_opportunity_is_rejected_even_after_dismissal(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "No Spam Artist")
    try:
        song = hq.store.create_song("Song")
        basis = _claim(
            hq,
            key="creator.work.milestone",
            value={"event": "first full arrangement"},
            song_id=song.id,
        )
        service = _service(hq)
        opportunity = service.create_opportunity(
            basis_claim_id=basis.id,
            kind="MILESTONE",
            summary="Capture the first complete arrangement",
            reason="This is a concrete work milestone.",
            suggested_mediums=["SCREEN"],
            song_id=song.id,
        )
        dismiss = _claim(
            hq,
            key="creator.capture.choice.dismiss",
            value={"choice": "dismiss"},
            song_id=song.id,
        )
        dismissed = service.set_status(
            opportunity.id, status="DISMISSED", decision_claim_id=dismiss.id
        )
        assert dismissed.terminal is True
        assert dismissed.preference_promoted is False

        with pytest.raises(ValidationError, match="duplicate capture opportunity"):
            service.create_opportunity(
                basis_claim_id=basis.id,
                kind="MILESTONE",
                summary="Capture the first complete arrangement",
                reason="A different reason must not bypass semantic dedupe.",
                suggested_mediums=["SCREEN"],
                song_id=song.id,
            )
    finally:
        hq.close()


def test_save_and_dismiss_require_explicit_artist_declared_decision_evidence(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Choice Artist")
    try:
        song = hq.store.create_song("Song")
        basis = _claim(
            hq,
            key="creator.work.choice",
            value={"event": "sound design experiment"},
            song_id=song.id,
        )
        service = _service(hq)
        opportunity = service.create_opportunity(
            basis_claim_id=basis.id,
            kind="PROCESS",
            summary="Save the sound-design process as a content idea",
            reason="The process may be useful to revisit.",
            suggested_mediums=["SCREEN"],
            song_id=song.id,
        )
        inferred = _claim(
            hq,
            key="creator.capture.inferred-choice",
            value={"choice": "save"},
            song_id=song.id,
            source_kind="INFERRED",
            source_ref="model:local-choice-guess",
        )
        with pytest.raises(ValidationError, match="explicit artist-declared"):
            service.set_status(
                opportunity.id, status="SAVED", decision_claim_id=inferred.id
            )

        save = _claim(
            hq,
            key="creator.capture.choice.save",
            value={"choice": "save"},
            song_id=song.id,
        )
        saved = service.set_status(
            opportunity.id, status="SAVED", decision_claim_id=save.id
        )
        assert saved.status == "SAVED"
        assert saved.attention_state == "SAVED"
        assert saved.preference_promoted is False

        reopen = _claim(
            hq,
            key="creator.capture.choice.reopen",
            value={"choice": "reopen"},
            song_id=song.id,
        )
        reopened = service.set_status(
            opportunity.id, status="OPEN", decision_claim_id=reopen.id
        )
        assert reopened.status == "OPEN"
    finally:
        hq.close()


def test_captured_confirmation_does_not_mean_published_or_authorized(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Captured Artist")
    try:
        song = hq.store.create_song("Song")
        basis = _claim(
            hq,
            key="creator.work.performance",
            value={"event": "live rehearsal take"},
            song_id=song.id,
            source_kind="OBSERVED",
            source_ref="rehearsal:take-3",
        )
        service = _service(hq)
        opportunity = service.create_opportunity(
            basis_claim_id=basis.id,
            kind="PERFORMANCE",
            summary="Preserve the rehearsal performance moment",
            reason="A real performance event was observed.",
            suggested_mediums=["VIDEO", "AUDIO"],
            song_id=song.id,
        )
        captured_receipt = _claim(
            hq,
            key="creator.capture.receipt",
            value={"captured": True},
            song_id=song.id,
            source_kind="PROVIDER_VERIFIED",
            source_ref="capture-provider:receipt-22",
        )
        captured = service.set_status(
            opportunity.id,
            status="CAPTURED",
            decision_claim_id=captured_receipt.id,
        )

        assert captured.status == "CAPTURED"
        assert captured.status_source_kind == "PROVIDER_VERIFIED"
        assert captured.attention_state == "CLOSED"
        assert captured.recording_authority_granted is False
        assert captured.publication_authority_granted is False
        assert captured.provider_authority_granted is False
        assert captured.external_action_authority_granted is False
    finally:
        hq.close()


def test_superseded_basis_marks_open_or_saved_opportunity_needs_revalidation(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Stale Basis Artist")
    try:
        song = hq.store.create_song("Song")
        basis = _claim(
            hq,
            key="creator.work.before-after",
            value={"state": "before"},
            song_id=song.id,
        )
        service = _service(hq)
        opportunity = service.create_opportunity(
            basis_claim_id=basis.id,
            kind="BEFORE_AFTER",
            summary="Show the before/after transformation",
            reason="The original state is source-bound.",
            suggested_mediums=["AUDIO"],
            song_id=song.id,
        )
        replacement = _claim(
            hq,
            key=basis.key,
            value={"state": "corrected-before"},
            song_id=song.id,
            supersedes=(basis.id,),
        )
        assert replacement.id != basis.id

        refreshed = service.get(opportunity.id)
        assert refreshed.basis_claim_id == basis.id
        assert refreshed.basis_current is False
        assert refreshed.status == "OPEN"
        assert refreshed.attention_state == "NEEDS_REVALIDATION"
    finally:
        hq.close()


def test_superseded_save_decision_marks_saved_opportunity_needs_revalidation(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Stale Choice Artist")
    try:
        song = hq.store.create_song("Song")
        basis = _claim(
            hq,
            key="creator.work.saved",
            value={"event": "production trick"},
            song_id=song.id,
        )
        service = _service(hq)
        opportunity = service.create_opportunity(
            basis_claim_id=basis.id,
            kind="PROCESS",
            summary="Save the production trick",
            reason="It came from actual Song work.",
            suggested_mediums=["SCREEN"],
            song_id=song.id,
        )
        save = _claim(
            hq,
            key="creator.capture.save-revision",
            value={"choice": "save"},
            song_id=song.id,
        )
        saved = service.set_status(opportunity.id, status="SAVED", decision_claim_id=save.id)
        assert saved.attention_state == "SAVED"
        _claim(
            hq,
            key=save.key,
            value={"choice": "correction"},
            song_id=song.id,
            supersedes=(save.id,),
        )
        stale = service.get(opportunity.id)
        assert stale.status == "SAVED"
        assert stale.status_evidence_current is False
        assert stale.attention_state == "NEEDS_REVALIDATION"
    finally:
        hq.close()


def test_terminal_status_is_immutable(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Terminal Artist")
    try:
        basis = _claim(
            hq,
            key="creator.work.story",
            value={"moment": "artist story"},
        )
        service = _service(hq)
        opportunity = service.create_opportunity(
            basis_claim_id=basis.id,
            kind="STORY",
            summary="Preserve the story idea",
            reason="The artist explicitly described the moment.",
            suggested_mediums=["NOTE"],
        )
        dismiss = _claim(
            hq,
            key="creator.capture.dismiss-terminal",
            value={"choice": "dismiss"},
        )
        service.set_status(opportunity.id, status="DISMISSED", decision_claim_id=dismiss.id)
        retry = _claim(
            hq,
            key="creator.capture.retry",
            value={"choice": "save"},
        )
        with pytest.raises(ValidationError, match="terminal capture opportunity status is immutable"):
            service.set_status(opportunity.id, status="SAVED", decision_claim_id=retry.id)
    finally:
        hq.close()


def test_capture_opportunity_survives_restart_without_new_store(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Restart Capture Artist")
    profile_id = hq.store.profile_id
    try:
        song = hq.store.create_song("Song")
        basis = _claim(
            hq,
            key="creator.work.restart",
            value={"moment": "session story"},
            song_id=song.id,
        )
        opportunity = _service(hq).create_opportunity(
            basis_claim_id=basis.id,
            kind="STORY",
            summary="Remember the session story",
            reason="It is bound to the Song evidence.",
            suggested_mediums=["NOTE"],
            song_id=song.id,
        )
        opportunity_id = opportunity.id
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        service = _service(reopened)
        restored = service.get(opportunity_id)
        assert restored.id == opportunity_id
        assert restored.song_id == song.id
        assert restored.status == "OPEN"
        assert len(service.for_song(song.id)) == 1
        tables = {
            str(row["name"])
            for row in reopened.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert not any(name.startswith("capture_") for name in tables)
    finally:
        reopened.close()


def test_owned_namespace_malformed_evidence_fails_closed(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Malformed Artist")
    try:
        hq.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=hq.store.primary_artist_id,
            key="capture.opportunity.fake",
            value={"schema_version": 1, "opportunity_id": "fake"},
            source_kind="INFERRED",
            source_ref="claim_missing",
            confidence=1.0,
            twin_domain="CREATIVE",
        )
        with pytest.raises(LineageCorruptionError, match="payload shape is invalid"):
            _service(hq)
    finally:
        hq.close()


def test_canonical_evidence_immutability_prevents_revision_rewrite(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Immutable Capture Artist")
    try:
        basis = _claim(
            hq,
            key="creator.work.immutable",
            value={"moment": "session"},
        )
        opportunity = _service(hq).create_opportunity(
            basis_claim_id=basis.id,
            kind="OTHER",
            summary="Preserve a work moment",
            reason="A bounded actual-work basis exists.",
            suggested_mediums=["NOTE"],
        )
        with pytest.raises(sqlite3.IntegrityError, match="evidence claims are immutable"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "UPDATE evidence_claims SET value_json='{}' WHERE id=?",
                    (opportunity.revision_claim_id,),
                )
    finally:
        hq.close()
