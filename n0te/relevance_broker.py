from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable

from .evidence_freshness import FRESHNESS_STATES

RELEVANCE_POLICY_VERSION = 1

OPERATING_CONTEXTS = ("NORMAL", "RECORDING", "LIVE")
URGENCY_LEVELS = ("NONE", "TIME_WINDOW", "DEADLINE")
# Relevance consumes the canonical freshness vocabulary; it does not own a second one.
EVIDENCE_FRESHNESS_STATES = tuple(sorted(FRESHNESS_STATES))
DISCUSSION_STATES = ("SAFE", "UNKNOWN", "UNSAFE")
TRANSACTION_STATES = ("NONE", "PENDING", "BLOCKING", "AWAITING_ARTIST")
REQUIRED_ALERT_KINDS = (
    "SAFETY",
    "TRUST_BOUNDARY",
    "CONTRADICTION",
    "STALE_CONTEXT",
    "RIGHTS_PRIVACY",
)

SURFACE_BANDS = (
    "REQUIRED_ALERT",
    "EXPLICIT_REQUEST",
    "BLOCKING",
    "DECISION_CHANGING",
    "TIME_SENSITIVE",
    "CURRENT_CONTEXT",
    "FUTURE_OPTION",
)
HOLD_BANDS = (
    "STALE_CANDIDATE_CONTEXT",
    "NEEDS_FRESH_EVIDENCE",
    "DISCUSSION_UNKNOWN",
    "BACKGROUND",
)
SUPPRESS_BANDS = ("UNSAFE_TO_DISCUSS",)
DEFER_BANDS = ("ARTIST_NOT_NOW",)
ALL_BANDS = SURFACE_BANDS + HOLD_BANDS + SUPPRESS_BANDS + DEFER_BANDS
DISPOSITIONS = ("SURFACE_NOW", "HOLD", "DEFER", "SUPPRESS")
_NORMAL_BAND_ORDER = (
    "BLOCKING",
    "DECISION_CHANGING",
    "TIME_SENSITIVE",
    "CURRENT_CONTEXT",
    "FUTURE_OPTION",
)


class RelevanceBrokerError(ValueError):
    """Invalid or unsafe Relevance Broker input."""


class UnsupportedRelevancePolicyError(RelevanceBrokerError):
    """The caller supplied a policy version this kernel cannot interpret."""


class StaleRelevanceContextError(RelevanceBrokerError):
    """The broker's own context no longer matches the current context."""


class RelevanceScopeLeakError(RelevanceBrokerError):
    """A candidate crosses an Artist/profile/Song isolation boundary."""


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise RelevanceBrokerError(f"{field_name} must be text")
    text = value.strip()
    if not text:
        raise RelevanceBrokerError(f"{field_name} must not be empty")
    return text


def _optional_text(value: object | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _exact_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise RelevanceBrokerError(f"{field_name} must be boolean")
    return value


def _enum_text(value: object, field_name: str, allowed: Iterable[str]) -> str:
    text = (
        _required_text(value, field_name)
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if text not in allowed:
        raise RelevanceBrokerError(f"unsupported {field_name}: {text}")
    return text


def _optional_enum_text(
    value: object | None,
    field_name: str,
    allowed: Iterable[str],
) -> str | None:
    if value is None:
        return None
    return _enum_text(value, field_name, allowed)


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise RelevanceBrokerError(f"{field_name} must be a tuple of text values")
    normalized = tuple(_required_text(item, field_name) for item in value)
    if len(normalized) != len(set(normalized)):
        raise RelevanceBrokerError(f"{field_name} must not contain duplicates")
    return normalized


def _enum_tuple(
    value: object,
    field_name: str,
    allowed: Iterable[str],
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise RelevanceBrokerError(f"{field_name} must be a tuple of text values")
    normalized = tuple(_enum_text(item, field_name, allowed) for item in value)
    if len(normalized) != len(set(normalized)):
        raise RelevanceBrokerError(f"{field_name} must not contain duplicates")
    return normalized


@dataclass(frozen=True)
class RelevanceContextBinding:
    """Ephemeral exact context identity used to prevent stale arbitration.

    References are opaque identifiers owned by canonical subsystems. This object
    neither persists them nor becomes a second context store.
    """

    profile_id: str
    artist_id: str
    song_id: str | None = None
    version_id: str | None = None
    session_id: str | None = None
    focus_id: str | None = None
    workspace_id: str | None = None
    job_id: str | None = None
    purpose_key: str | None = None
    operating_context: str = "NORMAL"
    schema_version: int = RELEVANCE_POLICY_VERSION

    def __post_init__(self) -> None:
        profile_id = _required_text(self.profile_id, "profile_id")
        artist_id = _required_text(self.artist_id, "artist_id")
        song_id = _optional_text(self.song_id, "song_id")
        version_id = _optional_text(self.version_id, "version_id")
        session_id = _optional_text(self.session_id, "session_id")
        focus_id = _optional_text(self.focus_id, "focus_id")
        workspace_id = _optional_text(self.workspace_id, "workspace_id")
        job_id = _optional_text(self.job_id, "job_id")
        purpose_key = _optional_text(self.purpose_key, "purpose_key")
        operating_context = _enum_text(
            self.operating_context, "operating_context", OPERATING_CONTEXTS
        )
        if type(self.schema_version) is not int:
            raise RelevanceBrokerError("schema_version must be an integer")
        if self.schema_version != RELEVANCE_POLICY_VERSION:
            raise UnsupportedRelevancePolicyError(
                f"unsupported Relevance Broker policy version: {self.schema_version}"
            )
        if song_id is None and any(
            value is not None
            for value in (version_id, session_id, focus_id, workspace_id)
        ):
            raise RelevanceBrokerError(
                "Version, Session, Focus, and Workspace references require a Song binding"
            )
        for name, value in (
            ("profile_id", profile_id),
            ("artist_id", artist_id),
            ("song_id", song_id),
            ("version_id", version_id),
            ("session_id", session_id),
            ("focus_id", focus_id),
            ("workspace_id", workspace_id),
            ("job_id", job_id),
            ("purpose_key", purpose_key),
            ("operating_context", operating_context),
        ):
            object.__setattr__(self, name, value)

    @property
    def fingerprint(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "artist_id": self.artist_id,
            "song_id": self.song_id,
            "version_id": self.version_id,
            "session_id": self.session_id,
            "focus_id": self.focus_id,
            "workspace_id": self.workspace_id,
            "job_id": self.job_id,
            "purpose_key": self.purpose_key,
            "operating_context": self.operating_context,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RelevanceCandidate:
    """One already-resolved subject competing for the artist's attention.

    The fingerprint identifies the exact context projection that produced this
    candidate. Scope fields describe the canonical object the subject belongs to.
    """

    semantic_key: str
    surface: str
    binding_fingerprint: str
    scope_profile_id: str
    scope_artist_id: str
    scope_song_id: str | None = None
    scope_job_id: str | None = None
    purpose_keys: tuple[str, ...] = ()
    operating_contexts: tuple[str, ...] = ()
    blocks_next_step: bool = False
    protects_future_option: bool = False
    changes_next_decision: bool = False
    explicit_artist_request: bool = False
    artist_not_now: bool = False
    requires_current_evidence: bool = False
    urgency: str = "NONE"
    evidence_freshness: str = "CURRENT"
    discussion_state: str = "SAFE"
    transaction_state: str = "NONE"
    required_alert_kind: str | None = None

    def __post_init__(self) -> None:
        values = {
            "semantic_key": _required_text(self.semantic_key, "semantic_key"),
            "surface": _required_text(self.surface, "surface"),
            "binding_fingerprint": _required_text(
                self.binding_fingerprint, "binding_fingerprint"
            ),
            "scope_profile_id": _required_text(
                self.scope_profile_id, "scope_profile_id"
            ),
            "scope_artist_id": _required_text(
                self.scope_artist_id, "scope_artist_id"
            ),
            "scope_song_id": _optional_text(self.scope_song_id, "scope_song_id"),
            "scope_job_id": _optional_text(self.scope_job_id, "scope_job_id"),
            "purpose_keys": _text_tuple(self.purpose_keys, "purpose_keys"),
            "operating_contexts": _enum_tuple(
                self.operating_contexts,
                "operating_contexts",
                OPERATING_CONTEXTS,
            ),
            "urgency": _enum_text(self.urgency, "urgency", URGENCY_LEVELS),
            "evidence_freshness": _enum_text(
                self.evidence_freshness,
                "evidence_freshness",
                EVIDENCE_FRESHNESS_STATES,
            ),
            "discussion_state": _enum_text(
                self.discussion_state, "discussion_state", DISCUSSION_STATES
            ),
            "transaction_state": _enum_text(
                self.transaction_state, "transaction_state", TRANSACTION_STATES
            ),
            "required_alert_kind": _optional_enum_text(
                self.required_alert_kind,
                "required_alert_kind",
                REQUIRED_ALERT_KINDS,
            ),
        }
        for field_name in (
            "blocks_next_step",
            "protects_future_option",
            "changes_next_decision",
            "explicit_artist_request",
            "artist_not_now",
            "requires_current_evidence",
        ):
            values[field_name] = _exact_bool(getattr(self, field_name), field_name)
        for name, value in values.items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class RelevanceDecision:
    semantic_key: str
    surface: str
    disposition: str
    band: str
    reason_codes: tuple[str, ...]
    binding_fingerprint: str
    policy_version: int = field(default=RELEVANCE_POLICY_VERSION, init=False)
    authority_effect: str = field(default="UNCHANGED", init=False)
    action_authority_granted: bool = field(default=False, init=False)
    mutation_authorized: bool = field(default=False, init=False)
    external_action_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "semantic_key", _required_text(self.semantic_key, "semantic_key")
        )
        object.__setattr__(self, "surface", _required_text(self.surface, "surface"))
        object.__setattr__(
            self,
            "binding_fingerprint",
            _required_text(self.binding_fingerprint, "binding_fingerprint"),
        )
        if self.disposition not in DISPOSITIONS:
            raise RelevanceBrokerError(
                f"unsupported relevance disposition: {self.disposition}"
            )
        if self.band not in ALL_BANDS:
            raise RelevanceBrokerError(f"unsupported relevance band: {self.band}")
        reasons = _text_tuple(self.reason_codes, "reason_codes")
        if not reasons:
            raise RelevanceBrokerError("RelevanceDecision requires explainable reasons")
        object.__setattr__(self, "reason_codes", reasons)

    @property
    def surfaces_now(self) -> bool:
        return self.disposition == "SURFACE_NOW"


@dataclass(frozen=True)
class RelevanceGroup:
    """A qualitative relevance band; members are semantically tied within it."""

    band: str
    decisions: tuple[RelevanceDecision, ...]

    def __post_init__(self) -> None:
        if self.band not in SURFACE_BANDS:
            raise RelevanceBrokerError(f"unsupported surface group: {self.band}")
        if not isinstance(self.decisions, tuple) or not self.decisions:
            raise RelevanceBrokerError("surface group must contain a decision tuple")
        if not all(isinstance(item, RelevanceDecision) for item in self.decisions):
            raise RelevanceBrokerError(
                "surface group decisions must contain RelevanceDecision values"
            )
        if any(
            decision.band != self.band or not decision.surfaces_now
            for decision in self.decisions
        ):
            raise RelevanceBrokerError(
                "surface group decisions must share the group band and SURFACE_NOW disposition"
            )

    @property
    def tied(self) -> bool:
        return len(self.decisions) > 1

    @property
    def ordering_semantics(self) -> str:
        return "CANONICAL_ONLY_NOT_RELATIVE_RELEVANCE"


@dataclass(frozen=True)
class RelevanceArbitration:
    binding_fingerprint: str
    surface_groups: tuple[RelevanceGroup, ...]
    held_decisions: tuple[RelevanceDecision, ...]
    policy_version: int = field(default=RELEVANCE_POLICY_VERSION, init=False)
    authority_effect: str = field(default="UNCHANGED", init=False)
    action_authority_granted: bool = field(default=False, init=False)
    mutation_authorized: bool = field(default=False, init=False)
    external_action_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "binding_fingerprint",
            _required_text(self.binding_fingerprint, "binding_fingerprint"),
        )
        if not isinstance(self.surface_groups, tuple) or not all(
            isinstance(item, RelevanceGroup) for item in self.surface_groups
        ):
            raise RelevanceBrokerError(
                "surface_groups must be a tuple of RelevanceGroup values"
            )
        if not isinstance(self.held_decisions, tuple) or not all(
            isinstance(item, RelevanceDecision) for item in self.held_decisions
        ):
            raise RelevanceBrokerError(
                "held_decisions must be a tuple of RelevanceDecision values"
            )

    @property
    def surface_now(self) -> tuple[RelevanceDecision, ...]:
        return tuple(
            decision
            for group in self.surface_groups
            for decision in group.decisions
        )

    @property
    def has_tie(self) -> bool:
        return any(group.tied for group in self.surface_groups)


@dataclass(frozen=True)
class _Classified:
    candidate: RelevanceCandidate
    disposition: str
    band: str
    reasons: tuple[str, ...]
    mandatory: bool = False
    eligible_normal: bool = False


class RelevanceBroker:
    """Stateless, explainable arbitration over bounded canonical context."""

    def __init__(self, binding: RelevanceContextBinding):
        if not isinstance(binding, RelevanceContextBinding):
            raise TypeError("RelevanceBroker requires RelevanceContextBinding")
        self.binding = binding

    @staticmethod
    def _validate_scope(
        binding: RelevanceContextBinding,
        candidate: RelevanceCandidate,
    ) -> None:
        if candidate.scope_profile_id != binding.profile_id:
            raise RelevanceScopeLeakError("candidate belongs to a different profile")
        if candidate.scope_artist_id != binding.artist_id:
            raise RelevanceScopeLeakError("candidate belongs to a different Artist")
        if (
            binding.song_id is not None
            and candidate.scope_song_id is not None
            and candidate.scope_song_id != binding.song_id
        ):
            raise RelevanceScopeLeakError(
                "Song-bound arbitration cannot consume another Song's candidate"
            )

    @staticmethod
    def _context_reasons(
        binding: RelevanceContextBinding,
        candidate: RelevanceCandidate,
    ) -> tuple[tuple[str, ...], bool]:
        reasons: list[str] = []
        checks: list[bool] = []
        if candidate.scope_song_id is not None:
            matched = candidate.scope_song_id == binding.song_id
            reasons.append("CURRENT_SONG" if matched else "NO_ACTIVE_SONG_MATCH")
            checks.append(matched)
        if candidate.scope_job_id is not None:
            matched = candidate.scope_job_id == binding.job_id
            reasons.append("CURRENT_JOB" if matched else "DIFFERENT_JOB")
            checks.append(matched)
        if candidate.purpose_keys:
            matched = (
                binding.purpose_key is not None
                and binding.purpose_key in candidate.purpose_keys
            )
            reasons.append("CURRENT_PURPOSE" if matched else "PURPOSE_MISMATCH")
            checks.append(matched)
        if candidate.operating_contexts:
            matched = binding.operating_context in candidate.operating_contexts
            reasons.append(
                "CURRENT_OPERATING_CONTEXT"
                if matched
                else "OPERATING_CONTEXT_MISMATCH"
            )
            checks.append(matched)
        return tuple(reasons), bool(checks) and all(checks)

    @staticmethod
    def _freshness_reason(state: str) -> str | None:
        return {
            "CURRENT": None,
            "REVALIDATION_REQUIRED": "EVIDENCE_REVALIDATION_REQUIRED",
            "EXPIRED": "EVIDENCE_EXPIRED",
            "UNKNOWN": "EVIDENCE_FRESHNESS_UNKNOWN",
        }[state]

    @classmethod
    def _classify(
        cls,
        binding: RelevanceContextBinding,
        candidate: RelevanceCandidate,
    ) -> _Classified:
        cls._validate_scope(binding, candidate)
        context_reasons, context_match = cls._context_reasons(binding, candidate)
        reasons = list(context_reasons)
        freshness_reason = cls._freshness_reason(candidate.evidence_freshness)
        if freshness_reason is not None:
            reasons.append(freshness_reason)
        if candidate.transaction_state != "NONE":
            reasons.append(f"TRANSACTION_{candidate.transaction_state}")

        candidate_is_current = candidate.binding_fingerprint == binding.fingerprint

        if candidate.required_alert_kind is not None:
            reasons.append(f"REQUIRED_{candidate.required_alert_kind}")
            if not candidate_is_current:
                reasons.append("STALE_CANDIDATE_CONTEXT")
            if candidate.artist_not_now:
                reasons.append("NOT_NOW_OVERRIDDEN_FOR_REQUIRED_ALERT")
            if candidate.discussion_state != "SAFE":
                reasons.append("GUARDED_ALERT_SURFACING")
            return _Classified(
                candidate,
                "SURFACE_NOW",
                "REQUIRED_ALERT",
                tuple(dict.fromkeys(reasons)),
                mandatory=True,
            )

        if not candidate_is_current:
            reasons.append("STALE_CANDIDATE_CONTEXT")
            return _Classified(
                candidate,
                "HOLD",
                "STALE_CANDIDATE_CONTEXT",
                tuple(dict.fromkeys(reasons)),
            )

        if candidate.explicit_artist_request:
            reasons.append("EXPLICIT_ARTIST_REQUEST")
            if candidate.artist_not_now:
                reasons.append("NOT_NOW_OVERRIDDEN_FOR_EXPLICIT_REQUEST")
            if candidate.discussion_state != "SAFE":
                reasons.append("GUARDED_RESPONSE_REQUIRED")
            return _Classified(
                candidate,
                "SURFACE_NOW",
                "EXPLICIT_REQUEST",
                tuple(dict.fromkeys(reasons)),
                mandatory=True,
            )

        if candidate.artist_not_now:
            reasons.append("ARTIST_NOT_NOW")
            return _Classified(
                candidate,
                "DEFER",
                "ARTIST_NOT_NOW",
                tuple(dict.fromkeys(reasons)),
            )

        if candidate.discussion_state == "UNSAFE":
            reasons.append("UNSAFE_TO_DISCUSS")
            return _Classified(
                candidate,
                "SUPPRESS",
                "UNSAFE_TO_DISCUSS",
                tuple(dict.fromkeys(reasons)),
            )
        if candidate.discussion_state == "UNKNOWN":
            reasons.append("DISCUSSION_SAFETY_UNKNOWN")
            return _Classified(
                candidate,
                "HOLD",
                "DISCUSSION_UNKNOWN",
                tuple(dict.fromkeys(reasons)),
            )

        if (
            candidate.requires_current_evidence
            and candidate.evidence_freshness != "CURRENT"
        ):
            reasons.append("CURRENT_EVIDENCE_REQUIRED")
            return _Classified(
                candidate,
                "HOLD",
                "NEEDS_FRESH_EVIDENCE",
                tuple(dict.fromkeys(reasons)),
            )

        if candidate.blocks_next_step or candidate.transaction_state in {
            "BLOCKING",
            "AWAITING_ARTIST",
        }:
            reasons.append("BLOCKS_NEXT_STEP")
            return _Classified(
                candidate,
                "HOLD",
                "BLOCKING",
                tuple(dict.fromkeys(reasons)),
                eligible_normal=True,
            )
        if candidate.changes_next_decision:
            reasons.append("CHANGES_NEXT_DECISION")
            return _Classified(
                candidate,
                "HOLD",
                "DECISION_CHANGING",
                tuple(dict.fromkeys(reasons)),
                eligible_normal=True,
            )
        if candidate.urgency != "NONE":
            reasons.append(f"URGENCY_{candidate.urgency}")
            return _Classified(
                candidate,
                "HOLD",
                "TIME_SENSITIVE",
                tuple(dict.fromkeys(reasons)),
                eligible_normal=True,
            )
        if context_match:
            reasons.append("MATCHES_CURRENT_CONTEXT")
            return _Classified(
                candidate,
                "HOLD",
                "CURRENT_CONTEXT",
                tuple(dict.fromkeys(reasons)),
                eligible_normal=True,
            )
        if candidate.protects_future_option:
            reasons.append("PROTECTS_FUTURE_OPTION")
            return _Classified(
                candidate,
                "HOLD",
                "FUTURE_OPTION",
                tuple(dict.fromkeys(reasons)),
                eligible_normal=True,
            )

        reasons.append("BACKGROUND_ONLY")
        return _Classified(
            candidate,
            "HOLD",
            "BACKGROUND",
            tuple(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _decision(
        classified: _Classified,
        binding_fingerprint: str,
        *,
        disposition: str | None = None,
        extra_reason: str | None = None,
    ) -> RelevanceDecision:
        reasons = list(classified.reasons)
        if extra_reason is not None:
            reasons.append(extra_reason)
        return RelevanceDecision(
            semantic_key=classified.candidate.semantic_key,
            surface=classified.candidate.surface,
            disposition=classified.disposition if disposition is None else disposition,
            band=classified.band,
            reason_codes=tuple(dict.fromkeys(reasons)),
            binding_fingerprint=binding_fingerprint,
        )

    @staticmethod
    def _canonical(
        decisions: Iterable[RelevanceDecision],
    ) -> tuple[RelevanceDecision, ...]:
        return tuple(sorted(decisions, key=lambda item: (item.surface, item.semantic_key)))

    def arbitrate(
        self,
        current_binding: RelevanceContextBinding,
        candidates: Iterable[RelevanceCandidate],
    ) -> RelevanceArbitration:
        if not isinstance(current_binding, RelevanceContextBinding):
            raise TypeError("current_binding must be RelevanceContextBinding")
        if current_binding.fingerprint != self.binding.fingerprint:
            raise StaleRelevanceContextError(
                "Relevance Broker context changed; refresh context before arbitration"
            )
        try:
            candidate_values = tuple(candidates)
        except TypeError as exc:
            raise RelevanceBrokerError("candidates must be iterable") from exc
        if not all(isinstance(item, RelevanceCandidate) for item in candidate_values):
            raise TypeError("candidates must contain RelevanceCandidate values")
        keys = [item.semantic_key for item in candidate_values]
        if len(keys) != len(set(keys)):
            raise RelevanceBrokerError(
                "each arbitration candidate must have a unique semantic_key"
            )

        classified = tuple(
            self._classify(current_binding, candidate)
            for candidate in candidate_values
        )
        fingerprint = current_binding.fingerprint
        required = [item for item in classified if item.band == "REQUIRED_ALERT"]
        explicit = [item for item in classified if item.band == "EXPLICIT_REQUEST"]
        mandatory = required + explicit
        surface_groups: list[RelevanceGroup] = []
        held: list[RelevanceDecision] = []

        for band, values in (
            ("REQUIRED_ALERT", required),
            ("EXPLICIT_REQUEST", explicit),
        ):
            if values:
                decisions = self._canonical(
                    self._decision(item, fingerprint, disposition="SURFACE_NOW")
                    for item in values
                )
                surface_groups.append(RelevanceGroup(band=band, decisions=decisions))

        normal_eligible = [item for item in classified if item.eligible_normal]
        selected_normal_band: str | None = None
        if not mandatory:
            selected_normal_band = next(
                (
                    band
                    for band in _NORMAL_BAND_ORDER
                    if any(item.band == band for item in normal_eligible)
                ),
                None,
            )
            if selected_normal_band is not None:
                decisions = self._canonical(
                    self._decision(item, fingerprint, disposition="SURFACE_NOW")
                    for item in normal_eligible
                    if item.band == selected_normal_band
                )
                surface_groups.append(
                    RelevanceGroup(band=selected_normal_band, decisions=decisions)
                )

        for item in classified:
            if item.mandatory:
                continue
            if item.eligible_normal:
                if selected_normal_band == item.band and not mandatory:
                    continue
                held.append(
                    self._decision(
                        item,
                        fingerprint,
                        disposition="HOLD",
                        extra_reason=(
                            "MANDATORY_CONTEXT_ALREADY_SURFACED"
                            if mandatory
                            else "LOWER_RELEVANCE_THAN_SURFACED_GROUP"
                        ),
                    )
                )
            else:
                held.append(self._decision(item, fingerprint))

        return RelevanceArbitration(
            binding_fingerprint=fingerprint,
            surface_groups=tuple(surface_groups),
            held_decisions=self._canonical(held),
        )
