from __future__ import annotations

from dataclasses import dataclass, field

from .capabilities import CapabilityCandidate, N0TEableJob
from .capability_evidence import CapabilityObservation
from .entitlements import ACCESS_KINDS, EntitlementSnapshot
from .evidence_freshness import EvidenceFreshnessAssessment

NEGOTIATION_STATUSES = {
    "NEGOTIABLE",
    "DEGRADED",
    "UNAVAILABLE",
    "UNKNOWN",
    "CONFLICT",
    "REVALIDATION_REQUIRED",
}


class CapabilityNegotiationError(ValueError):
    """Invalid or semantically unsafe capability-negotiation input."""


def _text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise CapabilityNegotiationError(f"{field_name} must be text")
    text = value.strip()
    if not text:
        raise CapabilityNegotiationError(f"{field_name} must not be empty")
    return text


def _strict_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise CapabilityNegotiationError(f"{field_name} must be a real bool")
    return value


def _normalize_paths(value: object, field_name: str) -> tuple["OperationDepth", ...]:
    if isinstance(value, (str, bytes)):
        raise CapabilityNegotiationError(
            f"{field_name} must contain OperationDepth values"
        )
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise CapabilityNegotiationError(
            f"{field_name} must contain OperationDepth values"
        ) from exc
    if any(not isinstance(item, OperationDepth) for item in items):
        raise CapabilityNegotiationError(
            f"{field_name} must contain OperationDepth values"
        )
    if len(items) != len(set(items)):
        raise CapabilityNegotiationError(f"{field_name} must not contain duplicates")
    return items


def _reason_tuple(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True, order=True)
class OperationDepth:
    """One exact operation/depth pair.

    Strength is intentionally not global. The artist-job request supplies its own
    strongest-to-weakest acceptable ordering, so this type never claims that one
    operation/depth is universally better than another.
    """

    operation: str
    depth: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", _text(self.operation, "path.operation"))
        object.__setattr__(self, "depth", _text(self.depth, "path.depth"))


@dataclass(frozen=True)
class CapabilityNegotiationRequest:
    """The exact artist-job and current environment being negotiated."""

    job: N0TEableJob
    profile_id: str
    subject_id: str
    workspace_observation_id: str
    environment_fingerprint: str
    acceptable_paths: tuple[OperationDepth, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.job, N0TEableJob):
            raise TypeError("request.job must be N0TEableJob")
        object.__setattr__(self, "profile_id", _text(self.profile_id, "request.profile_id"))
        object.__setattr__(self, "subject_id", _text(self.subject_id, "request.subject_id"))
        object.__setattr__(
            self,
            "workspace_observation_id",
            _text(self.workspace_observation_id, "request.workspace_observation_id"),
        )
        object.__setattr__(
            self,
            "environment_fingerprint",
            _text(self.environment_fingerprint, "request.environment_fingerprint"),
        )
        paths = _normalize_paths(self.acceptable_paths, "request.acceptable_paths")
        if not paths:
            raise CapabilityNegotiationError(
                "request.acceptable_paths must contain at least one path"
            )
        object.__setattr__(self, "acceptable_paths", paths)


@dataclass(frozen=True)
class CapabilityRouteCharacterization:
    """Evidence-bound operation/depth characterization for one discovered route."""

    observation: CapabilityObservation
    supported_paths: tuple[OperationDepth, ...]
    characterization_ref: str
    access_kind: str | None = None
    entitlement_required: bool = False
    permission_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.observation, CapabilityObservation):
            raise TypeError("characterization.observation must be CapabilityObservation")
        object.__setattr__(
            self,
            "supported_paths",
            _normalize_paths(self.supported_paths, "characterization.supported_paths"),
        )
        object.__setattr__(
            self,
            "characterization_ref",
            _text(self.characterization_ref, "characterization.characterization_ref"),
        )
        entitlement_required = _strict_bool(
            self.entitlement_required, "characterization.entitlement_required"
        )
        permission_required = _strict_bool(
            self.permission_required, "characterization.permission_required"
        )
        object.__setattr__(self, "entitlement_required", entitlement_required)
        object.__setattr__(self, "permission_required", permission_required)

        if entitlement_required or permission_required:
            if self.access_kind is None:
                raise CapabilityNegotiationError(
                    "characterization.access_kind is required when access is required"
                )
            access_kind = _text(self.access_kind, "characterization.access_kind").upper()
            if access_kind not in ACCESS_KINDS:
                raise CapabilityNegotiationError(
                    f"unsupported characterization.access_kind: {access_kind}"
                )
            object.__setattr__(self, "access_kind", access_kind)
        elif self.access_kind is not None:
            raise CapabilityNegotiationError(
                "characterization.access_kind must be omitted when access is not required"
            )


@dataclass(frozen=True)
class CapabilityNegotiationResult:
    status: str
    job_id: str
    profile_id: str
    subject_id: str
    route_id: str
    route_kind: str
    capability: str
    workspace_observation_id: str
    environment_fingerprint: str
    requested_paths: tuple[OperationDepth, ...]
    supported_paths: tuple[OperationDepth, ...]
    strongest_common_path: OperationDepth | None
    strongest_common_path_index: int | None
    availability: str
    access_resolution_status: str
    entitlement_state: str
    permission_state: str
    eligibility_entitlement_state: str
    eligibility_permission_state: str
    freshness_state: str
    capability_evidence_ref: str | None
    characterization_ref: str
    access_fingerprint: str | None
    candidate: CapabilityCandidate | None
    reason_codes: tuple[str, ...]
    action_authority_granted: bool = field(init=False, default=False)
    execution_authority_granted: bool = field(init=False, default=False)
    mutation_authority_granted: bool = field(init=False, default=False)
    external_action_authority_granted: bool = field(init=False, default=False)
    purchase_authority_granted: bool = field(init=False, default=False)
    activation_authority_granted: bool = field(init=False, default=False)
    provider_write_authority_granted: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        status = _text(self.status, "result.status").upper()
        if status not in NEGOTIATION_STATUSES:
            raise CapabilityNegotiationError(f"unsupported negotiation status: {status}")
        object.__setattr__(self, "status", status)
        reasons = tuple(self.reason_codes)
        if not reasons or any(type(reason) is not str or not reason.strip() for reason in reasons):
            raise CapabilityNegotiationError("result.reason_codes must contain text reasons")
        if len(reasons) != len(set(reasons)):
            raise CapabilityNegotiationError("result.reason_codes must not contain duplicates")
        if status in {"NEGOTIABLE", "DEGRADED"}:
            if self.strongest_common_path is None or self.candidate is None:
                raise CapabilityNegotiationError(
                    "rankable negotiation result requires a common path and candidate"
                )
        elif self.candidate is not None:
            raise CapabilityNegotiationError(
                "non-rankable negotiation result may not expose a ranking candidate"
            )

    @property
    def rankable(self) -> bool:
        return self.status in {"NEGOTIABLE", "DEGRADED"}

    @property
    def grants_any_authority(self) -> bool:
        return False


class CapabilityNegotiator:
    """Pure pre-ranking interoperability negotiation for one exact route.

    Availability, operation/depth support, entitlement/permission truth and
    freshness stay separate. Descriptive access truth is retained, while only the
    entitlement service's conservative eligibility projection may make access
    negotiable. The result exposes an existing CapabilityCandidate only after the
    exact case is legitimately rankable. CapabilityResolver still owns ranking and
    execution authority remains owned elsewhere.
    """

    @staticmethod
    def _validate_bindings(
        request: CapabilityNegotiationRequest,
        characterization: CapabilityRouteCharacterization,
        freshness: EvidenceFreshnessAssessment,
        access: EntitlementSnapshot | None,
    ) -> None:
        observation = characterization.observation
        if observation.capability != request.job.capability:
            raise CapabilityNegotiationError(
                "route capability does not match the requested job capability"
            )
        if freshness.observed_at_epoch_seconds != observation.observed_at_epoch_seconds:
            raise CapabilityNegotiationError(
                "freshness assessment does not describe the route observation time"
            )

        dependency_map = {
            (item.kind, item.key): item for item in freshness.dependency_states
        }
        workspace_dependency = dependency_map.get(
            ("WORKSPACE_OBSERVATION", "workspace_observation")
        )
        runtime_dependency = dependency_map.get(("HOST_RUNTIME", "host_runtime"))
        if workspace_dependency is None or runtime_dependency is None:
            raise CapabilityNegotiationError(
                "freshness assessment must bind workspace observation and host runtime"
            )
        if workspace_dependency.observed_fingerprint != observation.workspace_observation_id:
            raise CapabilityNegotiationError(
                "freshness workspace evidence does not describe the route observation"
            )
        if workspace_dependency.current_fingerprint != request.workspace_observation_id:
            raise CapabilityNegotiationError(
                "freshness workspace current state does not match the negotiation case"
            )
        if runtime_dependency.observed_fingerprint != observation.host_runtime_fingerprint:
            raise CapabilityNegotiationError(
                "freshness runtime evidence does not describe the route observation"
            )
        if runtime_dependency.current_fingerprint != request.environment_fingerprint:
            raise CapabilityNegotiationError(
                "freshness runtime current state does not match the negotiation case"
            )

        access_required = (
            characterization.entitlement_required
            or characterization.permission_required
        )
        if not access_required:
            if access is not None:
                raise CapabilityNegotiationError(
                    "access evidence must be omitted when the route requires no access gate"
                )
            return
        if access is None:
            return
        if access.profile_id != request.profile_id:
            raise CapabilityNegotiationError(
                "access evidence does not match the requested profile"
            )
        if access.route_id != observation.route_id:
            raise CapabilityNegotiationError(
                "access evidence does not match the characterized route"
            )
        if access.capability != request.job.capability:
            raise CapabilityNegotiationError(
                "access evidence does not match the requested capability"
            )
        if access.access_kind != characterization.access_kind:
            raise CapabilityNegotiationError(
                "access evidence does not match the characterized access kind"
            )

    @staticmethod
    def negotiate(
        request: CapabilityNegotiationRequest,
        characterization: CapabilityRouteCharacterization,
        *,
        freshness: EvidenceFreshnessAssessment,
        access: EntitlementSnapshot | None = None,
    ) -> CapabilityNegotiationResult:
        if not isinstance(request, CapabilityNegotiationRequest):
            raise TypeError("request must be CapabilityNegotiationRequest")
        if not isinstance(characterization, CapabilityRouteCharacterization):
            raise TypeError(
                "characterization must be CapabilityRouteCharacterization"
            )
        if not isinstance(freshness, EvidenceFreshnessAssessment):
            raise TypeError("freshness must be EvidenceFreshnessAssessment")
        if access is not None and not isinstance(access, EntitlementSnapshot):
            raise TypeError("access must be EntitlementSnapshot or None")

        CapabilityNegotiator._validate_bindings(
            request, characterization, freshness, access
        )
        observation = characterization.observation
        supported = set(characterization.supported_paths)
        common_index: int | None = None
        common_path: OperationDepth | None = None
        for index, path in enumerate(request.acceptable_paths):
            if path in supported:
                common_index = index
                common_path = path
                break

        reasons: list[str] = ["ROUTE_CHARACTERIZATION_BOUND"]
        hard_unavailable = False
        conflict = False
        revalidation_required = False
        unknown = False

        if observation.availability == "AVAILABLE":
            reasons.append("CAPABILITY_AVAILABLE")
        elif observation.availability == "UNAVAILABLE":
            reasons.append("CAPABILITY_UNAVAILABLE")
            hard_unavailable = True
        else:
            reasons.append("CAPABILITY_AVAILABILITY_UNKNOWN")
            unknown = True

        if freshness.state == "CURRENT":
            reasons.append("EVIDENCE_CURRENT")
        elif freshness.state in {"REVALIDATION_REQUIRED", "EXPIRED"}:
            reasons.append(f"EVIDENCE_{freshness.state}")
            revalidation_required = True
        else:
            reasons.append("EVIDENCE_FRESHNESS_UNKNOWN")
            unknown = True
        reasons.extend(f"FRESHNESS:{reason}" for reason in freshness.reason_codes)

        access_required = (
            characterization.entitlement_required
            or characterization.permission_required
        )
        access_resolution_status = "NOT_REQUIRED"
        entitlement_state = "NOT_REQUIRED"
        permission_state = "NOT_REQUIRED"
        eligibility_entitlement_state = "NOT_REQUIRED"
        eligibility_permission_state = "NOT_REQUIRED"
        access_fingerprint: str | None = None

        if not access_required:
            reasons.append("ACCESS_NOT_REQUIRED")
        elif access is None:
            access_resolution_status = "UNKNOWN"
            entitlement_state = (
                "UNKNOWN" if characterization.entitlement_required else "NOT_REQUIRED"
            )
            permission_state = (
                "UNKNOWN" if characterization.permission_required else "NOT_REQUIRED"
            )
            eligibility_entitlement_state = entitlement_state
            eligibility_permission_state = permission_state
            reasons.append("ACCESS_EVIDENCE_MISSING")
            unknown = True
        else:
            access_resolution_status = access.resolution_status
            entitlement_state = access.entitlement_state
            permission_state = access.permission_state
            eligibility_entitlement_state = access.eligibility_entitlement_state
            eligibility_permission_state = access.eligibility_permission_state
            access_fingerprint = access.fingerprint

            if access.resolution_status == "CONFLICT":
                reasons.append("ACCESS_CONFLICT")
                conflict = True
            elif access.resolution_status == "UNKNOWN":
                reasons.append("ACCESS_RESOLUTION_UNKNOWN")
                unknown = True
            else:
                reasons.append("ACCESS_RESOLVED")

            if access.validity_state == "EXPIRED":
                reasons.append("ACCESS_EXPIRED")
                revalidation_required = True
            elif access.validity_state == "UNKNOWN":
                reasons.append("ACCESS_VALIDITY_UNKNOWN")
                unknown = True
            else:
                reasons.append("ACCESS_CURRENT")

            if characterization.entitlement_required:
                if eligibility_entitlement_state == "DENIED":
                    reasons.append("ENTITLEMENT_DENIED")
                    hard_unavailable = True
                elif eligibility_entitlement_state == "UNKNOWN":
                    reasons.append("ENTITLEMENT_ELIGIBILITY_UNKNOWN")
                    unknown = True
                elif eligibility_entitlement_state == "NOT_REQUIRED":
                    reasons.append("ENTITLEMENT_REQUIREMENT_CONFLICT")
                    conflict = True
                else:
                    reasons.append("ENTITLEMENT_ELIGIBLE_GRANTED")

            if characterization.permission_required:
                if eligibility_permission_state == "DENIED":
                    reasons.append("PERMISSION_DENIED")
                    hard_unavailable = True
                elif eligibility_permission_state == "UNKNOWN":
                    reasons.append("PERMISSION_ELIGIBILITY_UNKNOWN")
                    unknown = True
                elif eligibility_permission_state == "NOT_REQUIRED":
                    reasons.append("PERMISSION_REQUIREMENT_CONFLICT")
                    conflict = True
                else:
                    reasons.append("PERMISSION_ELIGIBLE_GRANTED")

        if common_path is None:
            reasons.append("NO_COMMON_OPERATION_DEPTH")
            hard_unavailable = True
        elif common_index == 0:
            reasons.append("STRONGEST_REQUESTED_PATH_SUPPORTED")
        else:
            reasons.append("STRONGEST_COMMON_PATH_IS_DEGRADED")

        if hard_unavailable:
            status = "UNAVAILABLE"
        elif conflict:
            status = "CONFLICT"
        elif revalidation_required:
            status = "REVALIDATION_REQUIRED"
        elif unknown:
            status = "UNKNOWN"
        elif common_index is not None and common_index > 0:
            status = "DEGRADED"
        else:
            status = "NEGOTIABLE"

        candidate = None
        if status in {"NEGOTIABLE", "DEGRADED"}:
            candidate = observation.to_candidate(
                now_epoch_seconds=freshness.as_of_epoch_seconds
            )
            reasons.append("RANKING_CANDIDATE_EXPOSED_AFTER_NEGOTIATION")
        else:
            reasons.append("NO_RANKING_CANDIDATE")
        reasons.append("NEGOTIATION_ONLY_NO_ACTION_AUTHORITY")

        return CapabilityNegotiationResult(
            status=status,
            job_id=request.job.id,
            profile_id=request.profile_id,
            subject_id=request.subject_id,
            route_id=observation.route_id,
            route_kind=observation.route_kind,
            capability=request.job.capability,
            workspace_observation_id=request.workspace_observation_id,
            environment_fingerprint=request.environment_fingerprint,
            requested_paths=request.acceptable_paths,
            supported_paths=characterization.supported_paths,
            strongest_common_path=common_path,
            strongest_common_path_index=common_index,
            availability=observation.availability,
            access_resolution_status=access_resolution_status,
            entitlement_state=entitlement_state,
            permission_state=permission_state,
            eligibility_entitlement_state=eligibility_entitlement_state,
            eligibility_permission_state=eligibility_permission_state,
            freshness_state=freshness.state,
            capability_evidence_ref=observation.evidence_ref,
            characterization_ref=characterization.characterization_ref,
            access_fingerprint=access_fingerprint,
            candidate=candidate,
            reason_codes=_reason_tuple(reasons),
        )
