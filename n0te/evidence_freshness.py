from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .capability_evidence import CapabilityObservation

FRESHNESS_STATES = {
    "CURRENT",
    "REVALIDATION_REQUIRED",
    "EXPIRED",
    "UNKNOWN",
}
DEPENDENCY_STATES = {"CURRENT", "CHANGED", "UNKNOWN"}
DEPENDENCY_KINDS = {
    "WORKSPACE_OBSERVATION",
    "HOST_RUNTIME",
    "HOST_EDITION",
    "OS",
    "BINARY",
    "PLUGIN",
    "PROVIDER_VERSION",
    "AUTH_SCOPE",
    "PERMISSION",
    "ENTITLEMENT",
    "OTHER",
}


class EvidenceFreshnessError(ValueError):
    """Invalid or semantically unsafe evidence-freshness input."""


def _text(value: object, field: str) -> str:
    if type(value) is not str:
        raise EvidenceFreshnessError(f"{field} must be text")
    text = value.strip()
    if not text:
        raise EvidenceFreshnessError(f"{field} must not be empty")
    return text


def _optional_text(value: object | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise EvidenceFreshnessError(f"{field} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: object | None, field: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field)


def _source_current(value: object) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise EvidenceFreshnessError("source_current must be a real bool or None")
    return value


def _dependency_kind(value: object) -> str:
    text = _text(value, "dependency kind").upper().replace("-", "_").replace(" ", "_")
    if text not in DEPENDENCY_KINDS:
        raise EvidenceFreshnessError(f"unsupported dependency kind: {text}")
    return text


@dataclass(frozen=True)
class FreshnessDependency:
    """One relevant mutable dependency behind an evidence claim.

    Fingerprints are deliberately opaque. A caller may bind a capability observation
    to the exact host runtime, plug-in binary, provider scope, permission or
    entitlement it depended on without this module pretending to understand the
    provider-specific payload.
    """

    kind: str
    key: str
    observed_fingerprint: str
    current_fingerprint: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _dependency_kind(self.kind))
        object.__setattr__(self, "key", _text(self.key, "dependency key"))
        object.__setattr__(
            self,
            "observed_fingerprint",
            _text(self.observed_fingerprint, "observed_fingerprint"),
        )
        object.__setattr__(
            self,
            "current_fingerprint",
            _optional_text(self.current_fingerprint, "current_fingerprint"),
        )


@dataclass(frozen=True)
class DependencyFreshness:
    kind: str
    key: str
    state: str
    observed_fingerprint: str
    current_fingerprint: str | None


@dataclass(frozen=True)
class EvidenceFreshnessAssessment:
    state: str
    observed_at_epoch_seconds: int
    as_of_epoch_seconds: int
    age_seconds: int
    expires_at_epoch_seconds: int | None
    max_age_seconds: int | None
    source_current: bool | None
    dependency_states: tuple[DependencyFreshness, ...]
    reason_codes: tuple[str, ...]

    @property
    def usable_as_current(self) -> bool:
        return self.state == "CURRENT"

    @property
    def reverification_required(self) -> bool:
        return self.state != "CURRENT"

    @property
    def freshness_proves_correctness(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def grants_external_action_authority(self) -> bool:
        return False

    @property
    def grants_mutation_authority(self) -> bool:
        return False

    @property
    def grants_purchase_authority(self) -> bool:
        return False

    @property
    def grants_activation_authority(self) -> bool:
        return False


def _normalize_dependencies(
    dependencies: Iterable[FreshnessDependency],
) -> tuple[FreshnessDependency, ...]:
    try:
        items = tuple(dependencies)
    except TypeError as exc:
        raise EvidenceFreshnessError("dependencies must be iterable") from exc
    if any(not isinstance(item, FreshnessDependency) for item in items):
        raise EvidenceFreshnessError(
            "dependencies must contain FreshnessDependency values only"
        )
    ordered = tuple(sorted(items, key=lambda item: (item.kind, item.key)))
    seen: set[tuple[str, str]] = set()
    for item in ordered:
        identity = (item.kind, item.key)
        if identity in seen:
            raise EvidenceFreshnessError(
                f"duplicate freshness dependency: {item.kind}:{item.key}"
            )
        seen.add(identity)
    return ordered


def assess_evidence_freshness(
    *,
    observed_at_epoch_seconds: int,
    as_of_epoch_seconds: int,
    dependencies: Iterable[FreshnessDependency] = (),
    source_current: bool | None = True,
    expires_at_epoch_seconds: int | None = None,
    max_age_seconds: int | None = None,
) -> EvidenceFreshnessAssessment:
    """Assess whether old evidence may still be presented as current truth.

    This function never decides whether the underlying evidence was correct. It only
    evaluates the explicit freshness contract supplied by the caller. No default TTL
    is invented: age changes freshness only when ``max_age_seconds`` is supplied.
    """

    observed_at = _nonnegative_int(
        observed_at_epoch_seconds, "observed_at_epoch_seconds"
    )
    as_of = _nonnegative_int(as_of_epoch_seconds, "as_of_epoch_seconds")
    if as_of < observed_at:
        raise EvidenceFreshnessError(
            "as_of_epoch_seconds predates the evidence observation"
        )

    expires_at = _optional_nonnegative_int(
        expires_at_epoch_seconds, "expires_at_epoch_seconds"
    )
    if expires_at is not None and expires_at < observed_at:
        raise EvidenceFreshnessError(
            "expires_at_epoch_seconds predates the evidence observation"
        )
    max_age = _optional_nonnegative_int(max_age_seconds, "max_age_seconds")
    source_state = _source_current(source_current)
    normalized = _normalize_dependencies(dependencies)

    dependency_states: list[DependencyFreshness] = []
    changed_reasons: list[str] = []
    unknown_reasons: list[str] = []
    for item in normalized:
        if item.current_fingerprint is None:
            state = "UNKNOWN"
            unknown_reasons.append(f"DEPENDENCY_UNKNOWN:{item.kind}:{item.key}")
        elif item.current_fingerprint == item.observed_fingerprint:
            state = "CURRENT"
        else:
            state = "CHANGED"
            changed_reasons.append(f"DEPENDENCY_CHANGED:{item.kind}:{item.key}")
        dependency_states.append(
            DependencyFreshness(
                kind=item.kind,
                key=item.key,
                state=state,
                observed_fingerprint=item.observed_fingerprint,
                current_fingerprint=item.current_fingerprint,
            )
        )

    age = as_of - observed_at
    explicit_expired = expires_at is not None and as_of >= expires_at
    age_exceeded = max_age is not None and age > max_age

    reasons: list[str] = []
    if explicit_expired:
        reasons.append("EXPLICIT_EXPIRY_REACHED")
    if source_state is False:
        reasons.append("SOURCE_SUPERSEDED")
    elif source_state is None:
        unknown_reasons.append("SOURCE_CURRENT_UNKNOWN")
    reasons.extend(changed_reasons)
    if age_exceeded:
        reasons.append("MAX_AGE_EXCEEDED")
    reasons.extend(unknown_reasons)

    if explicit_expired:
        state = "EXPIRED"
    elif source_state is False or changed_reasons or age_exceeded:
        state = "REVALIDATION_REQUIRED"
    elif source_state is None or unknown_reasons:
        state = "UNKNOWN"
    else:
        state = "CURRENT"

    if state not in FRESHNESS_STATES:
        raise AssertionError(f"internal unsupported freshness state: {state}")

    return EvidenceFreshnessAssessment(
        state=state,
        observed_at_epoch_seconds=observed_at,
        as_of_epoch_seconds=as_of,
        age_seconds=age,
        expires_at_epoch_seconds=expires_at,
        max_age_seconds=max_age,
        source_current=source_state,
        dependency_states=tuple(dependency_states),
        reason_codes=tuple(reasons),
    )


def assess_capability_observation_freshness(
    observation: CapabilityObservation,
    *,
    as_of_epoch_seconds: int,
    current_workspace_observation_id: str | None,
    current_host_runtime_fingerprint: str | None,
    dependencies: Iterable[FreshnessDependency] = (),
    source_current: bool | None = True,
    expires_at_epoch_seconds: int | None = None,
    max_age_seconds: int | None = None,
) -> EvidenceFreshnessAssessment:
    """Apply the generic contract to existing exact-environment capability evidence."""

    if not isinstance(observation, CapabilityObservation):
        raise EvidenceFreshnessError(
            "observation must be an existing CapabilityObservation"
        )

    workspace_dependency = FreshnessDependency(
        kind="WORKSPACE_OBSERVATION",
        key="workspace_observation",
        observed_fingerprint=observation.workspace_observation_id,
        current_fingerprint=_optional_text(
            current_workspace_observation_id, "current_workspace_observation_id"
        ),
    )
    runtime_dependency = FreshnessDependency(
        kind="HOST_RUNTIME",
        key="host_runtime",
        observed_fingerprint=observation.host_runtime_fingerprint,
        current_fingerprint=_optional_text(
            current_host_runtime_fingerprint, "current_host_runtime_fingerprint"
        ),
    )
    extras = _normalize_dependencies(dependencies)
    return assess_evidence_freshness(
        observed_at_epoch_seconds=observation.observed_at_epoch_seconds,
        as_of_epoch_seconds=as_of_epoch_seconds,
        dependencies=(workspace_dependency, runtime_dependency, *extras),
        source_current=source_current,
        expires_at_epoch_seconds=expires_at_epoch_seconds,
        max_age_seconds=max_age_seconds,
    )
