from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .evidence import EvidenceClaim, EvidenceMemory, EvidenceResolution
from .lineage import LineageStore, NotFoundError, ValidationError

MONITORING_AUTHORITY = "EVIDENCE_ONLY"
DEFAULT_MONITORING_KEYS = (
    "monitoring.output_path",
    "monitoring.listening_environment",
    "monitoring.reference_level",
    "monitoring.calibration",
    "monitoring.listener_position",
    "monitoring.translation_check",
)
SOURCE_REF_REQUIRED_KINDS = {"OBSERVED", "MEASURED"}


class MonitoringContextError(ValidationError):
    """Base error for monitoring-context contract violations."""


class StaleMonitoringContextError(MonitoringContextError):
    """Raised when a snapshot no longer matches applicable monitoring evidence."""


@dataclass(frozen=True)
class MonitoringFact:
    key: str
    status: str
    scope_kind: str | None
    scope_id: str | None
    value: Any | None
    claims: tuple[EvidenceClaim, ...]

    @property
    def claim_ids(self) -> tuple[str, ...]:
        return tuple(claim.id for claim in self.claims)

    @property
    def source_kinds(self) -> tuple[str, ...]:
        return tuple(sorted({claim.source_kind for claim in self.claims}))


@dataclass(frozen=True)
class MonitoringContext:
    """Immutable witness of the listening context for one exact Song Version.

    This object is evidence context, not calibration certification, hearing-safety
    certification, provider verification, or action authority. UNKNOWN and CONFLICT
    remain explicit so a listening judgment cannot silently become universal truth.
    """

    profile_id: str
    artist_id: str
    song_id: str
    version_id: str
    facts: tuple[MonitoringFact, ...]
    fingerprint: str
    authority: str = MONITORING_AUTHORITY

    @property
    def context_id(self) -> str:
        return f"monitoring:{self.fingerprint[:24]}"

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(fact.key for fact in self.facts)

    @property
    def status(self) -> str:
        statuses = {fact.status for fact in self.facts}
        if "CONFLICT" in statuses:
            return "CONFLICT"
        if not self.facts or statuses == {"UNKNOWN"}:
            return "UNKNOWN"
        if "UNKNOWN" in statuses:
            return "PARTIAL"
        return "RESOLVED"

    @property
    def action_authority_granted(self) -> bool:
        return False

    @property
    def universal_truth(self) -> bool:
        return False


@dataclass(frozen=True)
class MonitoringJudgmentBinding:
    """Read-only attachment between a judgment reference and its listening witness."""

    judgment_ref: str
    profile_id: str
    artist_id: str
    song_id: str
    version_id: str
    monitoring_context_id: str
    monitoring_fingerprint: str
    monitoring_keys: tuple[str, ...]
    authority: str = MONITORING_AUTHORITY

    @property
    def action_authority_granted(self) -> bool:
        return False

    @property
    def universal_truth(self) -> bool:
        return False


class MonitoringContextService:
    """Build exact-Version listening-context witnesses over canonical EvidenceMemory.

    The service owns no second database and does not discover hardware. Recording a
    fact delegates to EvidenceMemory, preserving source kind and exact scope. A
    snapshot resolves each monitoring key using EvidenceMemory's existing
    VERSION -> SONG -> ARTIST -> PROFILE specificity contract.
    """

    def __init__(self, store: LineageStore, evidence: EvidenceMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("MonitoringContextService requires LineageStore")
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError(
                "MonitoringContextService requires EvidenceMemory for the same LineageStore"
            )
        self.store = store
        self.evidence = evidence

    @staticmethod
    def _normalize_key(key: str) -> str:
        key = str(key).strip()
        if not key.startswith("monitoring.") or len(key) <= len("monitoring."):
            raise MonitoringContextError(
                "monitoring evidence key must use the monitoring.* namespace"
            )
        return key

    @classmethod
    def _normalize_keys(cls, keys: Iterable[str]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(cls._normalize_key(key) for key in keys))
        if not normalized:
            raise MonitoringContextError("at least one monitoring evidence key is required")
        return normalized

    def _validate_song_version(self, song_id: str, version_id: str) -> tuple[str, str]:
        song_id = str(song_id).strip()
        version_id = str(version_id).strip()
        song = self.store.get_song(song_id)
        if song is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        if song.artist_id != self.store.primary_artist_id:
            raise MonitoringContextError("Song belongs to a different Artist")
        version = self.store.get_version(version_id)
        if version is None:
            raise NotFoundError(f"version not found: {version_id}")
        if version.song_id != song_id:
            raise MonitoringContextError("version belongs to a different Song")
        return song_id, version_id

    def record_fact(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        key: str,
        value: Any,
        source_kind: str,
        source_ref: str | None = None,
        confidence: float = 1.0,
        supersedes: Iterable[str] = (),
    ) -> EvidenceClaim:
        key = self._normalize_key(key)
        source_kind = str(source_kind).strip().upper()
        normalized_ref = None if source_ref is None else str(source_ref).strip()
        if source_kind == "PROVIDER_VERIFIED":
            raise MonitoringContextError(
                "MonitoringContextService cannot self-issue PROVIDER_VERIFIED evidence; "
                "consume a verifier-backed canonical Evidence claim instead"
            )
        if source_kind in SOURCE_REF_REQUIRED_KINDS and not normalized_ref:
            raise MonitoringContextError(
                f"{source_kind} monitoring evidence requires a source_ref"
            )
        return self.evidence.record_claim(
            scope_kind=scope_kind,
            scope_id=scope_id,
            key=key,
            value=value,
            source_kind=source_kind,
            source_ref=normalized_ref,
            confidence=confidence,
            twin_domain="TECHNICAL",
            supersedes=supersedes,
        )

    @staticmethod
    def _fact_from_resolution(resolution: EvidenceResolution) -> MonitoringFact:
        return MonitoringFact(
            key=resolution.key,
            status=resolution.status,
            scope_kind=resolution.scope_kind,
            scope_id=resolution.scope_id,
            value=resolution.value,
            claims=resolution.claims,
        )

    @staticmethod
    def _claim_payload(claim: EvidenceClaim) -> dict[str, Any]:
        return {
            "id": claim.id,
            "sequence": claim.sequence,
            "scope_kind": claim.scope_kind,
            "scope_id": claim.scope_id,
            "key": claim.key,
            "value": claim.value,
            "source_kind": claim.source_kind,
            "source_ref": claim.source_ref,
            "confidence": claim.confidence,
            "twin_domain": claim.twin_domain,
        }

    @classmethod
    def _fingerprint(
        cls,
        *,
        profile_id: str,
        artist_id: str,
        song_id: str,
        version_id: str,
        facts: tuple[MonitoringFact, ...],
    ) -> str:
        payload = {
            "schema": 1,
            "profile_id": profile_id,
            "artist_id": artist_id,
            "song_id": song_id,
            "version_id": version_id,
            "facts": [
                {
                    "key": fact.key,
                    "status": fact.status,
                    "scope_kind": fact.scope_kind,
                    "scope_id": fact.scope_id,
                    "value": fact.value,
                    "claims": [cls._claim_payload(claim) for claim in fact.claims],
                }
                for fact in facts
            ],
        }
        try:
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MonitoringContextError(
                "monitoring context contains non-canonical evidence data"
            ) from exc
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _context_fingerprint(cls, context: MonitoringContext) -> str:
        return cls._fingerprint(
            profile_id=context.profile_id,
            artist_id=context.artist_id,
            song_id=context.song_id,
            version_id=context.version_id,
            facts=context.facts,
        )

    def snapshot(
        self,
        *,
        song_id: str,
        version_id: str,
        keys: Iterable[str] = DEFAULT_MONITORING_KEYS,
    ) -> MonitoringContext:
        song_id, version_id = self._validate_song_version(song_id, version_id)
        normalized_keys = self._normalize_keys(keys)
        facts = tuple(
            self._fact_from_resolution(
                self.evidence.resolve_for_song(
                    song_id=song_id,
                    version_id=version_id,
                    key=key,
                )
            )
            for key in normalized_keys
        )
        fingerprint = self._fingerprint(
            profile_id=self.store.profile_id,
            artist_id=self.store.primary_artist_id,
            song_id=song_id,
            version_id=version_id,
            facts=facts,
        )
        return MonitoringContext(
            profile_id=self.store.profile_id,
            artist_id=self.store.primary_artist_id,
            song_id=song_id,
            version_id=version_id,
            facts=facts,
            fingerprint=fingerprint,
        )

    def is_current(self, context: MonitoringContext) -> bool:
        if not isinstance(context, MonitoringContext):
            raise TypeError("context must be MonitoringContext")
        if (
            context.profile_id != self.store.profile_id
            or context.artist_id != self.store.primary_artist_id
            or context.authority != MONITORING_AUTHORITY
        ):
            return False
        if self._context_fingerprint(context) != context.fingerprint:
            return False
        current = self.snapshot(
            song_id=context.song_id,
            version_id=context.version_id,
            keys=context.keys,
        )
        return current.fingerprint == context.fingerprint

    def assert_current(self, context: MonitoringContext) -> MonitoringContext:
        if not self.is_current(context):
            raise StaleMonitoringContextError(
                "monitoring context is stale, altered, or belongs to a different profile"
            )
        return context

    def bind_judgment(
        self,
        *,
        judgment_ref: str,
        context: MonitoringContext,
    ) -> MonitoringJudgmentBinding:
        judgment_ref = str(judgment_ref).strip()
        if not judgment_ref:
            raise MonitoringContextError("judgment_ref must not be empty")
        self.assert_current(context)
        return MonitoringJudgmentBinding(
            judgment_ref=judgment_ref,
            profile_id=context.profile_id,
            artist_id=context.artist_id,
            song_id=context.song_id,
            version_id=context.version_id,
            monitoring_context_id=context.context_id,
            monitoring_fingerprint=context.fingerprint,
            monitoring_keys=context.keys,
        )

    def binding_is_current(self, binding: MonitoringJudgmentBinding) -> bool:
        if not isinstance(binding, MonitoringJudgmentBinding):
            raise TypeError("binding must be MonitoringJudgmentBinding")
        if (
            binding.profile_id != self.store.profile_id
            or binding.artist_id != self.store.primary_artist_id
            or binding.authority != MONITORING_AUTHORITY
        ):
            return False
        current = self.snapshot(
            song_id=binding.song_id,
            version_id=binding.version_id,
            keys=binding.monitoring_keys,
        )
        return current.fingerprint == binding.monitoring_fingerprint
