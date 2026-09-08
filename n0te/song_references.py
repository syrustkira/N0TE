from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, fields
from typing import Any, Iterable

from .evidence import EvidenceClaim, EvidenceMemory
from .lineage import LineageStore, NotFoundError, ValidationError

REFERENCE_SCHEMA_VERSION = 1
REFERENCE_KEY_PREFIX = "song.reference."
REFERENCE_NOTE_KEY_PREFIX = "song.reference-note."
REFERENCE_DECISION_KEY_PREFIX = "song.reference-decision."

SOURCE_TYPES = {"LOCAL_AUDIO", "STREAMING_LINK", "CATALOG_RECORDING", "OTHER"}
SOURCE_EVIDENCE_KINDS = {"USER_DECLARED", "OBSERVED", "PROVIDER_VERIFIED"}
LOUDNESS_MATCH_POLICIES = {
    "UNSPECIFIED",
    "MATCH_BEFORE_COMPARISON",
    "DO_NOT_MATCH",
}
REFERENCE_DECISIONS = {
    "KEEP_REFERENCE",
    "STOP_USING_REFERENCE",
    "ADJUST_REFERENCE_SCOPE",
    "INCONCLUSIVE",
}

_REF_ID = re.compile(r"^ref_[0-9a-f]{32}$")
_EVENT_ID = re.compile(r"^(note|decision)_[0-9a-f]{32}$")
_DIMENSION = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class SongReferenceError(ValueError):
    """Caller supplied invalid Song-reference semantics."""


class SongReferenceIntegrityError(RuntimeError):
    """Persisted Song-reference evidence cannot be interpreted safely."""


@dataclass(frozen=True)
class SongReference:
    reference_id: str
    reference_claim_id: str
    sequence: int
    song_id: str
    title: str
    source_type: str
    source_locator: str
    version_id: str | None
    section_locator: str | None
    comparison_dimensions: tuple[str, ...]
    loudness_match_policy: str
    source_kind: str
    source_ref: str | None
    confidence: float


@dataclass(frozen=True)
class ReferenceNote:
    note_id: str
    claim_id: str
    sequence: int
    song_id: str
    reference_id: str
    reference_claim_id: str
    text: str


@dataclass(frozen=True)
class ReferenceDecision:
    decision_id: str
    claim_id: str
    sequence: int
    song_id: str
    reference_id: str
    reference_claim_id: str
    decision: str
    reason: str


_REFERENCE_KEYS = {
    "schema_version",
    "reference_id",
    "title",
    "source_type",
    "source_locator",
    "version_id",
    "section_locator",
    "comparison_dimensions",
    "loudness_match_policy",
}
_NOTE_KEYS = {
    "schema_version",
    "note_id",
    "reference_id",
    "reference_claim_id",
    "text",
}
_DECISION_KEYS = {
    "schema_version",
    "decision_id",
    "reference_id",
    "reference_claim_id",
    "decision",
    "reason",
}


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise SongReferenceError(f"{field} must be text")
    value = value.strip()
    if not value:
        raise SongReferenceError(f"{field} must not be empty")
    if len(value) > maximum:
        raise SongReferenceError(f"{field} is too long")
    return value


def _optional_text(value: Any, field: str, maximum: int) -> str | None:
    return None if value is None else _text(value, field, maximum)


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    value = _text(value, field, 80).upper()
    if value not in allowed:
        raise SongReferenceError(f"unsupported {field}: {value}")
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool):
        raise SongReferenceError("confidence must be between 0 and 1")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise SongReferenceError("confidence must be between 0 and 1") from exc
    if not 0.0 <= value <= 1.0:
        raise SongReferenceError("confidence must be between 0 and 1")
    return value


def _dimensions(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SongReferenceError("comparison_dimensions must be a sequence")
    try:
        values = tuple(values)
    except TypeError as exc:
        raise SongReferenceError(
            "comparison_dimensions must be a sequence"
        ) from exc
    if not values or len(values) > 16:
        raise SongReferenceError(
            "comparison_dimensions must contain between 1 and 16 labels"
        )
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        token = re.sub(r"[\s-]+", "_", _text(raw, "comparison dimension", 64).upper())
        if _DIMENSION.fullmatch(token) is None:
            raise SongReferenceError(f"invalid comparison dimension: {raw}")
        if token not in seen:
            seen.add(token)
            out.append(token)
    return tuple(out)


class SongReferenceStore:
    """Durable first-class Song references over canonical EvidenceMemory.

    Source provenance says how the referenced source identity was established.
    It does not verify rights, make the reference a target, measure loudness,
    rank quality, or grant DAW/provider/action authority.

    Notes and decisions bind the exact immutable reference claim that was
    current when the artist made them. Later reference revisions cannot rewrite
    the context of an earlier judgment.
    """

    def __init__(self, store: LineageStore, evidence: EvidenceMemory | None = None):
        if not isinstance(store, LineageStore):
            raise TypeError("SongReferenceStore requires the canonical LineageStore")
        self.store = store
        self.evidence = evidence if evidence is not None else EvidenceMemory(store)
        if not isinstance(self.evidence, EvidenceMemory) or self.evidence.store is not store:
            raise TypeError(
                "SongReferenceStore EvidenceMemory must belong to the same LineageStore"
            )

    def _song(self, song_id: Any) -> str:
        song_id = _text(song_id, "song_id", 96)
        if self.store.get_song(song_id) is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        return song_id

    @staticmethod
    def _ref_id(reference_id: Any) -> str:
        reference_id = _text(reference_id, "reference_id", 96)
        if _REF_ID.fullmatch(reference_id) is None:
            raise SongReferenceError("invalid reference_id")
        return reference_id

    def _version(self, song_id: str, version_id: Any) -> str | None:
        if version_id is None:
            return None
        version_id = _text(version_id, "version_id", 96)
        version = self.store.get_version(version_id)
        if version is None:
            raise NotFoundError(f"version not found: {version_id}")
        if version.song_id != song_id:
            raise SongReferenceError("version belongs to a different Song")
        return version_id

    def _definition(
        self,
        *,
        reference_id: str,
        song_id: str,
        title: Any,
        source_type: Any,
        source_locator: Any,
        version_id: Any,
        section_locator: Any,
        comparison_dimensions: Iterable[str],
        loudness_match_policy: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": REFERENCE_SCHEMA_VERSION,
            "reference_id": reference_id,
            "title": _text(title, "title", 240),
            "source_type": _enum(source_type, "source_type", SOURCE_TYPES),
            "source_locator": _text(source_locator, "source_locator", 2048),
            "version_id": self._version(song_id, version_id),
            "section_locator": _optional_text(
                section_locator, "section_locator", 240
            ),
            "comparison_dimensions": list(_dimensions(comparison_dimensions)),
            "loudness_match_policy": _enum(
                loudness_match_policy,
                "loudness_match_policy",
                LOUDNESS_MATCH_POLICIES,
            ),
        }

    @staticmethod
    def _source(
        source_kind: Any, source_ref: Any, confidence: Any
    ) -> tuple[str, str | None, float]:
        kind = _enum(source_kind, "source_kind", SOURCE_EVIDENCE_KINDS)
        ref = _optional_text(source_ref, "source_ref", 2048)
        if kind in {"OBSERVED", "PROVIDER_VERIFIED"} and ref is None:
            raise SongReferenceError(
                f"{kind} reference source requires source_ref provenance"
            )
        return kind, ref, _confidence(confidence)

    def create_reference(
        self,
        *,
        song_id: str,
        title: str,
        source_type: str,
        source_locator: str,
        comparison_dimensions: Iterable[str],
        loudness_match_policy: str = "UNSPECIFIED",
        version_id: str | None = None,
        section_locator: str | None = None,
        source_kind: str = "USER_DECLARED",
        source_ref: str | None = None,
        confidence: float = 1.0,
    ) -> SongReference:
        song_id = self._song(song_id)
        reference_id = f"ref_{uuid.uuid4().hex}"
        value = self._definition(
            reference_id=reference_id,
            song_id=song_id,
            title=title,
            source_type=source_type,
            source_locator=source_locator,
            version_id=version_id,
            section_locator=section_locator,
            comparison_dimensions=comparison_dimensions,
            loudness_match_policy=loudness_match_policy,
        )
        kind, ref, confidence = self._source(source_kind, source_ref, confidence)
        claim = self.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song_id,
            key=f"{REFERENCE_KEY_PREFIX}{reference_id}",
            value=value,
            source_kind=kind,
            source_ref=ref,
            confidence=confidence,
            twin_domain="UNSPECIFIED",
        )
        return self._decode_reference(claim)

    def get_reference(self, song_id: str, reference_id: str) -> SongReference:
        song_id = self._song(song_id)
        reference_id = self._ref_id(reference_id)
        claims = self.evidence.active_claims(
            "SONG", song_id, f"{REFERENCE_KEY_PREFIX}{reference_id}"
        )
        if not claims:
            raise NotFoundError(f"Song reference not found: {reference_id}")
        if len(claims) != 1:
            raise SongReferenceIntegrityError(
                "Song reference has conflicting active revisions"
            )
        return self._decode_reference(claims[0])

    def references_for_song(self, song_id: str) -> tuple[SongReference, ...]:
        song_id = self._song(song_id)
        grouped: dict[str, list[EvidenceClaim]] = {}
        for claim in self.evidence.active_claims_for_scope("SONG", song_id):
            if claim.key.startswith(REFERENCE_KEY_PREFIX):
                grouped.setdefault(claim.key, []).append(claim)
        out: list[SongReference] = []
        for key, claims in grouped.items():
            if len(claims) != 1:
                raise SongReferenceIntegrityError(
                    f"Song reference has conflicting active revisions: {key}"
                )
            out.append(self._decode_reference(claims[0]))
        return tuple(sorted(out, key=lambda item: (item.sequence, item.reference_id)))

    def revise_reference(
        self,
        song_id: str,
        reference_id: str,
        *,
        title: str,
        source_type: str,
        source_locator: str,
        comparison_dimensions: Iterable[str],
        loudness_match_policy: str = "UNSPECIFIED",
        version_id: str | None = None,
        section_locator: str | None = None,
        source_kind: str = "USER_DECLARED",
        source_ref: str | None = None,
        confidence: float = 1.0,
    ) -> SongReference:
        current = self.get_reference(song_id, reference_id)
        value = self._definition(
            reference_id=current.reference_id,
            song_id=current.song_id,
            title=title,
            source_type=source_type,
            source_locator=source_locator,
            version_id=version_id,
            section_locator=section_locator,
            comparison_dimensions=comparison_dimensions,
            loudness_match_policy=loudness_match_policy,
        )
        kind, ref, confidence = self._source(source_kind, source_ref, confidence)
        claim = self.evidence.record_claim(
            scope_kind="SONG",
            scope_id=current.song_id,
            key=f"{REFERENCE_KEY_PREFIX}{current.reference_id}",
            value=value,
            source_kind=kind,
            source_ref=ref,
            confidence=confidence,
            twin_domain="UNSPECIFIED",
            supersedes=(current.reference_claim_id,),
        )
        return self._decode_reference(claim)

    def reference_revision(self, song_id: str, claim_id: str) -> SongReference:
        song_id = self._song(song_id)
        claim_id = _text(claim_id, "claim_id", 96)
        claim = self.evidence.get_claim(claim_id)
        if (
            claim is None
            or claim.scope_kind != "SONG"
            or claim.scope_id != song_id
            or not claim.key.startswith(REFERENCE_KEY_PREFIX)
        ):
            raise NotFoundError(f"reference revision not found: {claim_id}")
        return self._decode_reference(claim)

    def record_note(
        self, song_id: str, reference_id: str, text: str
    ) -> ReferenceNote:
        reference = self.get_reference(song_id, reference_id)
        note_id = f"note_{uuid.uuid4().hex}"
        claim = self.evidence.record_claim(
            scope_kind="SONG",
            scope_id=reference.song_id,
            key=f"{REFERENCE_NOTE_KEY_PREFIX}{reference.reference_id}.{note_id}",
            value={
                "schema_version": REFERENCE_SCHEMA_VERSION,
                "note_id": note_id,
                "reference_id": reference.reference_id,
                "reference_claim_id": reference.reference_claim_id,
                "text": _text(text, "text", 4000),
            },
            source_kind="USER_DECLARED",
            confidence=1.0,
            twin_domain="CREATIVE",
        )
        return self._decode_note(claim)

    def record_decision(
        self,
        song_id: str,
        reference_id: str,
        decision: str,
        reason: str,
    ) -> ReferenceDecision:
        reference = self.get_reference(song_id, reference_id)
        decision_id = f"decision_{uuid.uuid4().hex}"
        claim = self.evidence.record_claim(
            scope_kind="SONG",
            scope_id=reference.song_id,
            key=(
                f"{REFERENCE_DECISION_KEY_PREFIX}"
                f"{reference.reference_id}.{decision_id}"
            ),
            value={
                "schema_version": REFERENCE_SCHEMA_VERSION,
                "decision_id": decision_id,
                "reference_id": reference.reference_id,
                "reference_claim_id": reference.reference_claim_id,
                "decision": _enum(decision, "decision", REFERENCE_DECISIONS),
                "reason": _text(reason, "reason", 4000),
            },
            source_kind="USER_DECLARED",
            confidence=1.0,
            twin_domain="CREATIVE",
        )
        return self._decode_decision(claim)

    def notes_for_reference(
        self, song_id: str, reference_id: str
    ) -> tuple[ReferenceNote, ...]:
        reference = self.get_reference(song_id, reference_id)
        prefix = f"{REFERENCE_NOTE_KEY_PREFIX}{reference.reference_id}."
        rows = (
            self._decode_note(claim)
            for claim in self.evidence.active_claims_for_scope(
                "SONG", reference.song_id
            )
            if claim.key.startswith(prefix)
        )
        return tuple(sorted(rows, key=lambda item: (item.sequence, item.note_id)))

    def decisions_for_reference(
        self, song_id: str, reference_id: str
    ) -> tuple[ReferenceDecision, ...]:
        reference = self.get_reference(song_id, reference_id)
        prefix = f"{REFERENCE_DECISION_KEY_PREFIX}{reference.reference_id}."
        rows = (
            self._decode_decision(claim)
            for claim in self.evidence.active_claims_for_scope(
                "SONG", reference.song_id
            )
            if claim.key.startswith(prefix)
        )
        return tuple(
            sorted(rows, key=lambda item: (item.sequence, item.decision_id))
        )

    def _decode_reference(self, claim: EvidenceClaim) -> SongReference:
        try:
            if claim.scope_kind != "SONG" or not claim.key.startswith(
                REFERENCE_KEY_PREFIX
            ):
                raise SongReferenceIntegrityError("invalid Song reference scope/key")
            reference_id = claim.key[len(REFERENCE_KEY_PREFIX) :]
            if _REF_ID.fullmatch(reference_id) is None:
                raise SongReferenceIntegrityError("invalid Song reference identity")
            if claim.twin_domain != "UNSPECIFIED":
                raise SongReferenceIntegrityError("invalid reference Twin domain")
            if claim.source_kind not in SOURCE_EVIDENCE_KINDS:
                raise SongReferenceIntegrityError(
                    "unsupported reference source evidence kind"
                )
            if (
                claim.source_kind in {"OBSERVED", "PROVIDER_VERIFIED"}
                and not claim.source_ref
            ):
                raise SongReferenceIntegrityError(
                    "observed/provider reference lacks provenance"
                )
            value = claim.value
            if not isinstance(value, dict) or set(value) != _REFERENCE_KEYS:
                raise SongReferenceIntegrityError("invalid Song reference value shape")
            if value["schema_version"] != REFERENCE_SCHEMA_VERSION:
                raise SongReferenceIntegrityError(
                    "unsupported Song reference schema version"
                )
            if value["reference_id"] != reference_id:
                raise SongReferenceIntegrityError(
                    "Song reference identity disagrees with evidence key"
                )
            dimensions = _dimensions(value["comparison_dimensions"])
            if list(dimensions) != value["comparison_dimensions"]:
                raise SongReferenceIntegrityError(
                    "stored comparison dimensions are not canonical"
                )
            return SongReference(
                reference_id=reference_id,
                reference_claim_id=claim.id,
                sequence=claim.sequence,
                song_id=claim.scope_id,
                title=_text(value["title"], "title", 240),
                source_type=_enum(value["source_type"], "source_type", SOURCE_TYPES),
                source_locator=_text(
                    value["source_locator"], "source_locator", 2048
                ),
                version_id=self._version(claim.scope_id, value["version_id"]),
                section_locator=_optional_text(
                    value["section_locator"], "section_locator", 240
                ),
                comparison_dimensions=dimensions,
                loudness_match_policy=_enum(
                    value["loudness_match_policy"],
                    "loudness_match_policy",
                    LOUDNESS_MATCH_POLICIES,
                ),
                source_kind=claim.source_kind,
                source_ref=claim.source_ref,
                confidence=_confidence(claim.confidence),
            )
        except SongReferenceIntegrityError:
            raise
        except (SongReferenceError, NotFoundError, ValidationError) as exc:
            raise SongReferenceIntegrityError(
                "persisted Song reference evidence is invalid"
            ) from exc

    def _decode_note(self, claim: EvidenceClaim) -> ReferenceNote:
        try:
            value = self._event_value(claim, _NOTE_KEYS, "note")
            reference = self.reference_revision(
                claim.scope_id, value["reference_claim_id"]
            )
            if reference.reference_id != value["reference_id"]:
                raise SongReferenceIntegrityError("reference note binding is invalid")
            return ReferenceNote(
                note_id=value["note_id"],
                claim_id=claim.id,
                sequence=claim.sequence,
                song_id=claim.scope_id,
                reference_id=reference.reference_id,
                reference_claim_id=reference.reference_claim_id,
                text=_text(value["text"], "text", 4000),
            )
        except SongReferenceIntegrityError:
            raise
        except (SongReferenceError, NotFoundError, ValidationError) as exc:
            raise SongReferenceIntegrityError(
                "persisted reference note is invalid"
            ) from exc

    def _decode_decision(self, claim: EvidenceClaim) -> ReferenceDecision:
        try:
            value = self._event_value(claim, _DECISION_KEYS, "decision")
            reference = self.reference_revision(
                claim.scope_id, value["reference_claim_id"]
            )
            if reference.reference_id != value["reference_id"]:
                raise SongReferenceIntegrityError(
                    "reference decision binding is invalid"
                )
            return ReferenceDecision(
                decision_id=value["decision_id"],
                claim_id=claim.id,
                sequence=claim.sequence,
                song_id=claim.scope_id,
                reference_id=reference.reference_id,
                reference_claim_id=reference.reference_claim_id,
                decision=_enum(value["decision"], "decision", REFERENCE_DECISIONS),
                reason=_text(value["reason"], "reason", 4000),
            )
        except SongReferenceIntegrityError:
            raise
        except (SongReferenceError, NotFoundError, ValidationError) as exc:
            raise SongReferenceIntegrityError(
                "persisted reference decision is invalid"
            ) from exc

    @staticmethod
    def _event_value(
        claim: EvidenceClaim, keys: set[str], kind: str
    ) -> dict[str, Any]:
        try:
            if (
                claim.scope_kind != "SONG"
                or claim.source_kind != "USER_DECLARED"
                or claim.twin_domain != "CREATIVE"
            ):
                raise SongReferenceIntegrityError(
                    f"reference {kind} provenance is invalid"
                )
            value = claim.value
            if not isinstance(value, dict) or set(value) != keys:
                raise SongReferenceIntegrityError(
                    f"reference {kind} value shape is invalid"
                )
            if value["schema_version"] != REFERENCE_SCHEMA_VERSION:
                raise SongReferenceIntegrityError(
                    f"unsupported reference {kind} schema version"
                )
            event_id = value[f"{kind}_id"]
            if (
                not isinstance(event_id, str)
                or _EVENT_ID.fullmatch(event_id) is None
                or not event_id.startswith(f"{kind}_")
            ):
                raise SongReferenceIntegrityError(
                    f"reference {kind} identity is invalid"
                )
            prefix = (
                REFERENCE_NOTE_KEY_PREFIX
                if kind == "note"
                else REFERENCE_DECISION_KEY_PREFIX
            )
            if claim.key != f"{prefix}{value['reference_id']}.{event_id}":
                raise SongReferenceIntegrityError(
                    f"reference {kind} key disagrees with value"
                )
            return value
        except SongReferenceIntegrityError:
            raise
        except (KeyError, TypeError) as exc:
            raise SongReferenceIntegrityError(
                f"persisted reference {kind} is invalid"
            ) from exc


def reference_public_fields() -> tuple[str, ...]:
    """Stable inspection helper for anti-conflation tests/adapters."""
    return tuple(field.name for field in fields(SongReference))
