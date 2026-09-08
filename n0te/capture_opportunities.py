from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .evidence import EvidenceClaim, EvidenceMemory
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

CAPTURE_SCHEMA_VERSION = 1
CAPTURE_KEY_PREFIX = "capture.opportunity."
CAPTURE_KINDS = {
    "PROCESS",
    "DECISION",
    "BEFORE_AFTER",
    "PERFORMANCE",
    "MILESTONE",
    "STORY",
    "OTHER",
}
CAPTURE_MEDIUMS = {"NOTE", "SCREEN", "AUDIO", "VIDEO", "PHOTO"}
CAPTURE_STATUSES = {"OPEN", "SAVED", "DISMISSED", "CAPTURED"}
TERMINAL_CAPTURE_STATUSES = {"DISMISSED", "CAPTURED"}
PROVENANCE_REQUIRED = {"OBSERVED", "MEASURED", "PROVIDER_VERIFIED", "INFERRED"}
CAPTURE_AUTHORITY = "EVIDENCE_ONLY"


@dataclass(frozen=True)
class CaptureOpportunity:
    id: str
    profile_id: str
    artist_id: str
    song_id: str | None
    version_id: str | None
    kind: str
    summary: str
    reason: str
    suggested_mediums: tuple[str, ...]
    status: str
    basis_claim_id: str
    basis_source_kind: str
    basis_source_ref: str | None
    basis_current: bool
    status_evidence_claim_id: str | None
    status_source_kind: str | None
    status_source_ref: str | None
    status_evidence_current: bool
    revision_claim_id: str
    revision_sequence: int
    authority: str = CAPTURE_AUTHORITY
    recording_authority_granted: bool = field(default=False, init=False)
    device_permission_authority_granted: bool = field(default=False, init=False)
    file_write_authority_granted: bool = field(default=False, init=False)
    publication_authority_granted: bool = field(default=False, init=False)
    provider_authority_granted: bool = field(default=False, init=False)
    external_action_authority_granted: bool = field(default=False, init=False)
    obligation_created: bool = field(default=False, init=False)
    preference_promoted: bool = field(default=False, init=False)
    worthiness_score: None = field(default=None, init=False)
    virality_score: None = field(default=None, init=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_CAPTURE_STATUSES

    @property
    def attention_state(self) -> str:
        if self.terminal:
            return "CLOSED"
        if not self.basis_current or not self.status_evidence_current:
            return "NEEDS_REVALIDATION"
        if self.status == "SAVED":
            return "SAVED"
        return "AVAILABLE"


class CaptureOpportunityService:
    """Source-bound Content Opportunity Bank over canonical EvidenceMemory.

    This service preserves *why* a moment might be worth capturing. It never
    treats that inference as objective content quality and never starts capture,
    creates tasks, requests device permissions, publishes, or grants authority.
    """

    def __init__(self, store: LineageStore, evidence: EvidenceMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("CaptureOpportunityService requires canonical LineageStore")
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError("CaptureOpportunityService requires EvidenceMemory on the same LineageStore")
        self.store = store
        self.evidence = evidence
        self._validate_existing()

    @staticmethod
    def _clean_text(value: str, field_name: str, *, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field_name} must be text")
        text = " ".join(value.split())
        if not text:
            raise ValidationError(f"{field_name} must not be empty")
        if len(text) > maximum:
            raise ValidationError(f"{field_name} is too long")
        return text

    @staticmethod
    def _normalize_kind(value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("capture opportunity kind must be text")
        kind = value.strip().upper().replace("-", "_").replace(" ", "_")
        if kind not in CAPTURE_KINDS:
            raise ValidationError(f"unsupported capture opportunity kind: {kind}")
        return kind

    @staticmethod
    def _normalize_mediums(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        if not isinstance(values, (tuple, list)):
            raise ValidationError("suggested_mediums must be a list or tuple")
        result: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValidationError("capture medium must be text")
            medium = value.strip().upper().replace("-", "_").replace(" ", "_")
            if medium not in CAPTURE_MEDIUMS:
                raise ValidationError(f"unsupported capture medium: {medium}")
            if medium not in result:
                result.append(medium)
        if not result:
            raise ValidationError("at least one suggested capture medium is required")
        return tuple(result)

    @staticmethod
    def _normalize_status(value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("capture opportunity status must be text")
        status = value.strip().upper().replace("-", "_").replace(" ", "_")
        if status not in CAPTURE_STATUSES:
            raise ValidationError(f"unsupported capture opportunity status: {status}")
        return status

    def _target_scope(
        self, *, song_id: str | None, version_id: str | None
    ) -> tuple[str, str, str | None, str | None]:
        if version_id is not None and song_id is None:
            raise ValidationError("version-scoped capture opportunity requires song_id")
        if song_id is None:
            return "ARTIST", self.store.primary_artist_id, None, None
        if not isinstance(song_id, str) or not song_id.strip():
            raise ValidationError("song_id must be non-empty text")
        song = self.store.get_song(song_id.strip())
        if song is None:
            raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")
        if song.artist_id != self.store.primary_artist_id:
            raise ValidationError("capture opportunity Song belongs to a different Artist")
        if version_id is None:
            return "SONG", song.id, song.id, None
        if not isinstance(version_id, str) or not version_id.strip():
            raise ValidationError("version_id must be non-empty text")
        version = self.store.get_version(version_id.strip())
        if version is None:
            raise NotFoundError(f"version not found: {version_id}")
        if version.song_id != song.id:
            raise ValidationError("capture opportunity version belongs to a different Song")
        return "VERSION", version.id, song.id, version.id

    def _claim_is_active(self, claim: EvidenceClaim) -> bool:
        return any(
            active.id == claim.id
            for active in self.evidence.active_claims(claim.scope_kind, claim.scope_id, claim.key)
        )

    @staticmethod
    def _require_provenance(claim: EvidenceClaim, *, purpose: str) -> None:
        if claim.source_kind in PROVENANCE_REQUIRED and not (
            isinstance(claim.source_ref, str) and claim.source_ref.strip()
        ):
            raise ValidationError(
                f"{purpose} {claim.source_kind} evidence requires source_ref provenance"
            )

    def _claim_matches_target(
        self,
        claim: EvidenceClaim,
        *,
        song_id: str | None,
        version_id: str | None,
    ) -> bool:
        if claim.scope_kind == "PROFILE":
            return claim.scope_id == self.store.profile_id
        if claim.scope_kind == "ARTIST":
            return claim.scope_id == self.store.primary_artist_id
        if claim.scope_kind == "SONG":
            return song_id is not None and claim.scope_id == song_id
        if claim.scope_kind == "VERSION":
            return version_id is not None and claim.scope_id == version_id
        return False

    def _require_basis_claim(
        self,
        claim_id: str,
        *,
        song_id: str | None,
        version_id: str | None,
    ) -> EvidenceClaim:
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise ValidationError("basis_claim_id must be non-empty text")
        claim = self.evidence.get_claim(claim_id.strip())
        if claim is None:
            raise NotFoundError(f"evidence claim not found: {claim_id}")
        if claim.key.startswith(CAPTURE_KEY_PREFIX):
            raise ValidationError("capture opportunities cannot recursively use capture opportunity evidence as their basis")
        if not self._claim_matches_target(claim, song_id=song_id, version_id=version_id):
            raise ValidationError("basis evidence scope does not match capture opportunity scope")
        if not self._claim_is_active(claim):
            raise ValidationError("capture opportunity basis must be currently active evidence")
        self._require_provenance(claim, purpose="capture basis")
        return claim

    def _require_decision_claim(
        self,
        claim_id: str,
        *,
        song_id: str | None,
        version_id: str | None,
        status: str,
    ) -> EvidenceClaim:
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise ValidationError("decision_claim_id must be non-empty text")
        claim = self.evidence.get_claim(claim_id.strip())
        if claim is None:
            raise NotFoundError(f"evidence claim not found: {claim_id}")
        if claim.key.startswith(CAPTURE_KEY_PREFIX):
            raise ValidationError("capture opportunity status requires independent decision evidence")
        if not self._claim_matches_target(claim, song_id=song_id, version_id=version_id):
            raise ValidationError("decision evidence scope does not match capture opportunity scope")
        if not self._claim_is_active(claim):
            raise ValidationError("capture opportunity decision must use currently active evidence")
        self._require_provenance(claim, purpose="capture decision")
        allowed = {"USER_DECLARED"}
        if status == "CAPTURED":
            allowed |= {"OBSERVED", "PROVIDER_VERIFIED"}
        if claim.source_kind not in allowed:
            raise ValidationError(
                f"capture status {status} requires explicit artist-declared"
                + (" or observed/provider-verified" if status == "CAPTURED" else "")
                + " evidence"
            )
        return claim

    @staticmethod
    def _opportunity_id() -> str:
        return f"capture_{uuid.uuid4().hex}"

    @staticmethod
    def _payload(
        *,
        opportunity_id: str,
        song_id: str | None,
        version_id: str | None,
        kind: str,
        summary: str,
        reason: str,
        mediums: tuple[str, ...],
        status: str,
        basis: EvidenceClaim,
        decision: EvidenceClaim | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": CAPTURE_SCHEMA_VERSION,
            "opportunity_id": opportunity_id,
            "song_id": song_id,
            "version_id": version_id,
            "kind": kind,
            "summary": summary,
            "reason": reason,
            "suggested_mediums": list(mediums),
            "status": status,
            "basis_claim_id": basis.id,
            "basis_source_kind": basis.source_kind,
            "basis_source_ref": basis.source_ref,
            "status_evidence_claim_id": None if decision is None else decision.id,
            "status_source_kind": None if decision is None else decision.source_kind,
            "status_source_ref": None if decision is None else decision.source_ref,
        }

    def _active_owned_claim_rows(self):
        return self.store._conn.execute(
            "SELECT c.seq,c.id,c.scope_kind,c.scope_id,c.key,c.value_json,c.source_kind,"
            "c.source_ref,c.confidence,c.twin_domain "
            "FROM evidence_claims c "
            "LEFT JOIN evidence_supersessions s ON s.old_claim_id=c.id "
            "WHERE c.key LIKE ? AND s.old_claim_id IS NULL ORDER BY c.seq",
            (CAPTURE_KEY_PREFIX + "%",),
        ).fetchall()

    @staticmethod
    def _claim_from_row(row) -> EvidenceClaim:
        return EvidenceClaim(
            id=str(row["id"]),
            sequence=int(row["seq"]),
            scope_kind=str(row["scope_kind"]),
            scope_id=str(row["scope_id"]),
            key=str(row["key"]),
            value=json.loads(str(row["value_json"])),
            source_kind=str(row["source_kind"]),
            source_ref=None if row["source_ref"] is None else str(row["source_ref"]),
            confidence=float(row["confidence"]),
            twin_domain=str(row["twin_domain"]),
        )

    def _parse(self, claim: EvidenceClaim, *, corruption: bool = False) -> CaptureOpportunity:
        error = LineageCorruptionError if corruption else ValidationError
        value = claim.value
        if not isinstance(value, dict):
            raise error("capture opportunity evidence payload must be an object")
        expected = {
            "schema_version",
            "opportunity_id",
            "song_id",
            "version_id",
            "kind",
            "summary",
            "reason",
            "suggested_mediums",
            "status",
            "basis_claim_id",
            "basis_source_kind",
            "basis_source_ref",
            "status_evidence_claim_id",
            "status_source_kind",
            "status_source_ref",
        }
        if set(value) != expected:
            raise error("capture opportunity evidence payload shape is invalid")
        if value["schema_version"] != CAPTURE_SCHEMA_VERSION:
            raise error("unsupported capture opportunity schema version")
        opportunity_id = value["opportunity_id"]
        if not isinstance(opportunity_id, str) or claim.key != CAPTURE_KEY_PREFIX + opportunity_id:
            raise error("capture opportunity identity/key binding is invalid")
        if claim.source_kind != "INFERRED" or not claim.source_ref:
            raise error("capture opportunity revisions must remain inferred representations with source evidence")
        song_id = value["song_id"]
        version_id = value["version_id"]
        if song_id is not None and not isinstance(song_id, str):
            raise error("capture opportunity song_id is invalid")
        if version_id is not None and not isinstance(version_id, str):
            raise error("capture opportunity version_id is invalid")
        try:
            scope_kind, scope_id, exact_song, exact_version = self._target_scope(
                song_id=song_id, version_id=version_id
            )
        except (ValidationError, NotFoundError) as exc:
            raise error(str(exc)) from exc
        if claim.scope_kind != scope_kind or claim.scope_id != scope_id:
            raise error("capture opportunity evidence scope does not match payload")
        try:
            kind = self._normalize_kind(value["kind"])
            summary = self._clean_text(value["summary"], "summary", maximum=240)
            reason = self._clean_text(value["reason"], "reason", maximum=800)
            mediums = self._normalize_mediums(value["suggested_mediums"])
            status = self._normalize_status(value["status"])
        except ValidationError as exc:
            raise error(str(exc)) from exc
        basis_id = value["basis_claim_id"]
        if not isinstance(basis_id, str) or not basis_id:
            raise error("capture opportunity basis_claim_id is invalid")
        basis = self.evidence.get_claim(basis_id)
        if basis is None:
            raise error("capture opportunity basis evidence is missing")
        if basis.key.startswith(CAPTURE_KEY_PREFIX):
            raise error("capture opportunity basis cannot be capture evidence")
        if not self._claim_matches_target(basis, song_id=exact_song, version_id=exact_version):
            raise error("capture opportunity basis evidence crosses scope")
        if value["basis_source_kind"] != basis.source_kind or value["basis_source_ref"] != basis.source_ref:
            raise error("capture opportunity basis provenance snapshot is inconsistent")
        try:
            self._require_provenance(basis, purpose="capture basis")
        except ValidationError as exc:
            raise error(str(exc)) from exc
        basis_current = self._claim_is_active(basis)

        decision_id = value["status_evidence_claim_id"]
        status_source_kind = value["status_source_kind"]
        status_source_ref = value["status_source_ref"]
        if status == "OPEN" and decision_id is None:
            if status_source_kind is not None or status_source_ref is not None:
                raise error("initial capture opportunity has impossible status provenance")
            status_current = True
        else:
            if not isinstance(decision_id, str) or not decision_id:
                raise error("capture opportunity status is missing decision evidence")
            decision = self.evidence.get_claim(decision_id)
            if decision is None:
                raise error("capture opportunity decision evidence is missing")
            if not self._claim_matches_target(decision, song_id=exact_song, version_id=exact_version):
                raise error("capture opportunity decision evidence crosses scope")
            if status_source_kind != decision.source_kind or status_source_ref != decision.source_ref:
                raise error("capture opportunity status provenance snapshot is inconsistent")
            try:
                self._require_provenance(decision, purpose="capture decision")
            except ValidationError as exc:
                raise error(str(exc)) from exc
            allowed = {"USER_DECLARED"}
            if status == "CAPTURED":
                allowed |= {"OBSERVED", "PROVIDER_VERIFIED"}
            if decision.source_kind not in allowed:
                raise error("capture opportunity status uses inadmissible decision evidence")
            status_current = self._claim_is_active(decision)

        return CaptureOpportunity(
            id=opportunity_id,
            profile_id=self.store.profile_id,
            artist_id=self.store.primary_artist_id,
            song_id=exact_song,
            version_id=exact_version,
            kind=kind,
            summary=summary,
            reason=reason,
            suggested_mediums=mediums,
            status=status,
            basis_claim_id=basis.id,
            basis_source_kind=basis.source_kind,
            basis_source_ref=basis.source_ref,
            basis_current=basis_current,
            status_evidence_claim_id=decision_id,
            status_source_kind=status_source_kind,
            status_source_ref=status_source_ref,
            status_evidence_current=status_current,
            revision_claim_id=claim.id,
            revision_sequence=claim.sequence,
        )

    def _validate_existing(self) -> None:
        try:
            seen: set[str] = set()
            for row in self._active_owned_claim_rows():
                claim = self._claim_from_row(row)
                opportunity = self._parse(claim, corruption=True)
                if opportunity.id in seen:
                    raise LineageCorruptionError("multiple current revisions exist for one capture opportunity")
                seen.add(opportunity.id)
        except LineageCorruptionError:
            raise
        except Exception as exc:
            raise LineageCorruptionError("capture opportunity evidence is unreadable or corrupt") from exc

    def all(self) -> tuple[CaptureOpportunity, ...]:
        return tuple(
            self._parse(self._claim_from_row(row))
            for row in self._active_owned_claim_rows()
        )

    def get(self, opportunity_id: str) -> CaptureOpportunity:
        if not isinstance(opportunity_id, str) or not opportunity_id.strip():
            raise ValidationError("opportunity_id must be non-empty text")
        wanted = opportunity_id.strip()
        matches = tuple(item for item in self.all() if item.id == wanted)
        if not matches:
            raise NotFoundError(f"capture opportunity not found: {wanted}")
        if len(matches) != 1:
            raise LineageCorruptionError("capture opportunity has multiple current revisions")
        return matches[0]

    def for_song(self, song_id: str) -> tuple[CaptureOpportunity, ...]:
        song = self.store.get_song(song_id)
        if song is None:
            raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")
        return tuple(item for item in self.all() if item.song_id == song.id)

    def create_opportunity(
        self,
        *,
        basis_claim_id: str,
        kind: str,
        summary: str,
        reason: str,
        suggested_mediums: tuple[str, ...] | list[str],
        song_id: str | None = None,
        version_id: str | None = None,
    ) -> CaptureOpportunity:
        scope_kind, scope_id, exact_song, exact_version = self._target_scope(
            song_id=song_id, version_id=version_id
        )
        normalized_kind = self._normalize_kind(kind)
        normalized_summary = self._clean_text(summary, "summary", maximum=240)
        normalized_reason = self._clean_text(reason, "reason", maximum=800)
        normalized_mediums = self._normalize_mediums(suggested_mediums)
        basis = self._require_basis_claim(
            basis_claim_id, song_id=exact_song, version_id=exact_version
        )
        for existing in self.all():
            if (
                existing.basis_claim_id == basis.id
                and existing.kind == normalized_kind
                and existing.summary == normalized_summary
                and existing.suggested_mediums == normalized_mediums
            ):
                raise ValidationError("duplicate capture opportunity for the same evidence and semantics")

        opportunity_id = self._opportunity_id()
        payload = self._payload(
            opportunity_id=opportunity_id,
            song_id=exact_song,
            version_id=exact_version,
            kind=normalized_kind,
            summary=normalized_summary,
            reason=normalized_reason,
            mediums=normalized_mediums,
            status="OPEN",
            basis=basis,
            decision=None,
        )
        claim = self.evidence.record_claim(
            scope_kind=scope_kind,
            scope_id=scope_id,
            key=CAPTURE_KEY_PREFIX + opportunity_id,
            value=payload,
            source_kind="INFERRED",
            source_ref=basis.id,
            confidence=basis.confidence,
            twin_domain="CREATIVE",
        )
        return self._parse(claim)

    def set_status(
        self,
        opportunity_id: str,
        *,
        status: str,
        decision_claim_id: str,
    ) -> CaptureOpportunity:
        current = self.get(opportunity_id)
        next_status = self._normalize_status(status)
        if current.terminal:
            raise ValidationError("terminal capture opportunity status is immutable")
        if next_status == current.status:
            raise ValidationError("capture opportunity status must change")
        if current.status == "OPEN" and next_status not in {"SAVED", "DISMISSED", "CAPTURED"}:
            raise ValidationError("OPEN capture opportunity may only be saved, dismissed, or captured")
        if current.status == "SAVED" and next_status not in {"OPEN", "DISMISSED", "CAPTURED"}:
            raise ValidationError("SAVED capture opportunity may only be reopened, dismissed, or captured")
        decision = self._require_decision_claim(
            decision_claim_id,
            song_id=current.song_id,
            version_id=current.version_id,
            status=next_status,
        )
        basis = self.evidence.get_claim(current.basis_claim_id)
        if basis is None:
            raise LineageCorruptionError("capture opportunity basis evidence disappeared")
        old = self.evidence.get_claim(current.revision_claim_id)
        if old is None or not self._claim_is_active(old):
            raise LineageCorruptionError("capture opportunity revision changed during status update")
        payload = self._payload(
            opportunity_id=current.id,
            song_id=current.song_id,
            version_id=current.version_id,
            kind=current.kind,
            summary=current.summary,
            reason=current.reason,
            mediums=current.suggested_mediums,
            status=next_status,
            basis=basis,
            decision=decision,
        )
        claim = self.evidence.record_claim(
            scope_kind=old.scope_kind,
            scope_id=old.scope_id,
            key=old.key,
            value=payload,
            source_kind="INFERRED",
            source_ref=decision.id,
            confidence=decision.confidence,
            twin_domain="CREATIVE",
            supersedes=(old.id,),
        )
        return self._parse(claim)
