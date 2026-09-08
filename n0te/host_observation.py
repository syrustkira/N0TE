from __future__ import annotations

from dataclasses import dataclass

from .capabilities import CapabilityCandidate, CapabilityResolutionError, ROUTE_KINDS
from .capability_evidence import (
    CAPABILITY_AVAILABILITY,
    CAPABILITY_EVIDENCE_KINDS,
    CapabilityEnvironmentState,
    CapabilityEvidenceMemory,
    CapabilityObservation,
)
from .focus import FocusContext, FocusContextService, FocusDimension
from .hosts import HostRuntimeIdentity
from .lineage import ValidationError
from .shadow import (
    SHADOW_ACTORS,
    SHADOW_COVERAGE,
    HostShadow,
    HostShadowState,
    ShadowBatch,
    ShadowEventInput,
)
from .studio import StudioCapabilityProfile
from .workspace import WorkspaceMemory, WorkspaceState

HOST_OBSERVATION_STATUSES = {"COMPLETE", "PARTIAL"}


class HostObservationError(RuntimeError):
    """Invalid, stale or internally inconsistent host observation session."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise HostObservationError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _enum(value: str, field: str, allowed: set[str]) -> str:
    text = _text(value, field).upper().replace("-", "_").replace(" ", "_")
    if text not in allowed:
        raise HostObservationError(f"unsupported {field}: {text}")
    return text


def _score(value: float, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HostObservationError(f"{field} must be between 0 and 1") from exc
    if not 0.0 <= number <= 1.0:
        raise HostObservationError(f"{field} must be between 0 and 1")
    return number


def _nonnegative_int(value: int, field: str) -> int:
    if isinstance(value, bool):
        raise HostObservationError(f"{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise HostObservationError(f"{field} must be a non-negative integer") from exc
    if number < 0:
        raise HostObservationError(f"{field} must be a non-negative integer")
    return number


@dataclass(frozen=True)
class HostObservationBinding:
    workspace_id: str
    song_id: str
    workspace_observation_id: str
    host_runtime_fingerprint: str
    runtime: HostRuntimeIdentity

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _text(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "song_id", _text(self.song_id, "song_id"))
        object.__setattr__(
            self,
            "workspace_observation_id",
            _text(self.workspace_observation_id, "workspace_observation_id"),
        )
        object.__setattr__(
            self,
            "host_runtime_fingerprint",
            _text(self.host_runtime_fingerprint, "host_runtime_fingerprint"),
        )
        if not isinstance(self.runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        if self.runtime.fingerprint != self.host_runtime_fingerprint:
            raise HostObservationError(
                "binding runtime fingerprint does not match its HostRuntimeIdentity"
            )


@dataclass(frozen=True)
class CapabilityFactInput:
    route_id: str
    route_kind: str
    capability: str
    display_name: str
    availability: str
    evidence_kind: str
    observed_at_epoch_seconds: int
    evidence_ref: str | None = None
    brand: str | None = None
    task_fit: float = 0.5
    editability: float = 0.5
    locality: float = 0.5
    privacy: float = 0.5
    latency: float = 0.5
    reversibility: float = 0.5
    cost_efficiency: float = 0.5
    portability: float = 0.5
    paid: bool = False

    def __post_init__(self) -> None:
        route_id = _text(self.route_id, "capability.route_id")
        route_kind = _enum(self.route_kind, "capability.route_kind", ROUTE_KINDS)
        capability = _text(self.capability, "capability.capability")
        display_name = _text(self.display_name, "capability.display_name")
        availability = _enum(
            self.availability, "capability.availability", CAPABILITY_AVAILABILITY
        )
        evidence_kind = _enum(
            self.evidence_kind, "capability.evidence_kind", CAPABILITY_EVIDENCE_KINDS
        )
        evidence_ref = _optional_text(self.evidence_ref, "capability.evidence_ref")
        brand = _optional_text(self.brand, "capability.brand")
        observed_at = _nonnegative_int(
            self.observed_at_epoch_seconds, "capability.observed_at_epoch_seconds"
        )
        if type(self.paid) is not bool:
            raise HostObservationError("capability.paid must be a real bool")
        scores = {
            field: _score(getattr(self, field), f"capability.{field}")
            for field in (
                "task_fit",
                "editability",
                "locality",
                "privacy",
                "latency",
                "reversibility",
                "cost_efficiency",
                "portability",
            )
        }
        verified = availability != "UNKNOWN"
        compatible = availability != "UNAVAILABLE"
        if verified and evidence_ref is None:
            raise HostObservationError(
                "AVAILABLE/UNAVAILABLE capability fact requires evidence_ref"
            )
        try:
            CapabilityCandidate(
                candidate_id=f"preflight:{route_kind}:{route_id}:{capability}",
                route_kind=route_kind,
                capability=capability,
                display_name=display_name,
                brand=brand,
                verified=verified,
                compatible=compatible,
                evidence_ref=evidence_ref,
                evidence_age_seconds=0,
                user_preference=0.5,
                paid=self.paid,
                **scores,
            )
        except CapabilityResolutionError as exc:
            raise HostObservationError("invalid capability fact") from exc
        object.__setattr__(self, "route_id", route_id)
        object.__setattr__(self, "route_kind", route_kind)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "availability", availability)
        object.__setattr__(self, "evidence_kind", evidence_kind)
        object.__setattr__(self, "evidence_ref", evidence_ref)
        object.__setattr__(self, "brand", brand)
        object.__setattr__(self, "observed_at_epoch_seconds", observed_at)
        for field, value in scores.items():
            object.__setattr__(self, field, value)


@dataclass(frozen=True)
class ShadowObservationInput:
    coverage: str
    actor: str
    evidence_ref: str
    events: tuple[ShadowEventInput, ...] = ()

    def __post_init__(self) -> None:
        coverage = _enum(self.coverage, "shadow.coverage", SHADOW_COVERAGE)
        actor = _enum(self.actor, "shadow.actor", SHADOW_ACTORS)
        evidence_ref = _text(self.evidence_ref, "shadow.evidence_ref")
        events = tuple(self.events)
        if not all(isinstance(item, ShadowEventInput) for item in events):
            raise TypeError("shadow.events must contain ShadowEventInput values")
        if coverage == "INCREMENTAL" and not events:
            raise HostObservationError(
                "INCREMENTAL shadow observation requires at least one event"
            )
        keys = [(item.object_kind, item.object_ref, item.field) for item in events]
        if len(keys) != len(set(keys)):
            raise HostObservationError(
                "shadow observation may update each object field at most once"
            )
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "evidence_ref", evidence_ref)
        object.__setattr__(self, "events", events)


@dataclass(frozen=True)
class HostObservationResult:
    status: str
    binding: HostObservationBinding
    workspace: WorkspaceState
    capability_environment: CapabilityEnvironmentState
    studio: StudioCapabilityProfile
    focus: FocusContext
    shadow: HostShadowState
    recorded_capability_ids: tuple[str, ...]
    recorded_shadow_batch_id: str | None

    def __post_init__(self) -> None:
        status = _enum(self.status, "status", HOST_OBSERVATION_STATUSES)
        if not isinstance(self.binding, HostObservationBinding):
            raise TypeError("binding must be HostObservationBinding")
        if not isinstance(self.workspace, WorkspaceState):
            raise TypeError("workspace must be WorkspaceState")
        if not isinstance(self.capability_environment, CapabilityEnvironmentState):
            raise TypeError("capability_environment must be CapabilityEnvironmentState")
        if not isinstance(self.studio, StudioCapabilityProfile):
            raise TypeError("studio must be StudioCapabilityProfile")
        if not isinstance(self.focus, FocusContext):
            raise TypeError("focus must be FocusContext")
        if not isinstance(self.shadow, HostShadowState):
            raise TypeError("shadow must be HostShadowState")
        if status == "COMPLETE" and (
            self.shadow.status != "CURRENT" or not self.capability_environment.current
        ):
            raise HostObservationError(
                "COMPLETE host observation requires CURRENT shadow and capability truth"
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "recorded_capability_ids",
            tuple(
                _text(value, "recorded_capability_id")
                for value in self.recorded_capability_ids
            ),
        )
        object.__setattr__(
            self,
            "recorded_shadow_batch_id",
            _optional_text(self.recorded_shadow_batch_id, "recorded_shadow_batch_id"),
        )


class HostObservationCoordinator:
    """Peer-neutral read-side assembly over the canonical DAW-00A-F primitives."""

    def __init__(
        self,
        workspaces: WorkspaceMemory,
        capability_evidence: CapabilityEvidenceMemory,
        focus: FocusContextService,
        shadow: HostShadow,
    ):
        if not isinstance(workspaces, WorkspaceMemory):
            raise TypeError("workspaces must be WorkspaceMemory")
        if not isinstance(capability_evidence, CapabilityEvidenceMemory):
            raise TypeError("capability_evidence must be CapabilityEvidenceMemory")
        if not isinstance(focus, FocusContextService):
            raise TypeError("focus must be FocusContextService")
        if not isinstance(shadow, HostShadow):
            raise TypeError("shadow must be HostShadow")
        if capability_evidence.workspaces is not workspaces:
            raise TypeError("capability evidence must share WorkspaceMemory")
        if focus.workspaces is not workspaces:
            raise TypeError("focus must share WorkspaceMemory")
        if shadow.workspaces is not workspaces:
            raise TypeError("shadow must share WorkspaceMemory")
        self.workspaces = workspaces
        self.capability_evidence = capability_evidence
        self.focus = focus
        self.shadow = shadow

    def begin(
        self,
        workspace_id: str,
        *,
        song_id: str,
        runtime: HostRuntimeIdentity,
    ) -> HostObservationBinding:
        if not isinstance(runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        state = self.workspaces.state(workspace_id)
        if state.workspace.song_id != str(song_id):
            raise ValidationError("workspace belongs to a different Song")
        if state.workspace.host_family != runtime.family:
            raise HostObservationError("runtime belongs to a different host family")
        current = state.current_observation
        if current.host_runtime_fingerprint != runtime.fingerprint:
            raise HostObservationError("runtime is stale relative to the workspace")
        return HostObservationBinding(
            workspace_id=state.workspace.id,
            song_id=state.workspace.song_id,
            workspace_observation_id=current.id,
            host_runtime_fingerprint=current.host_runtime_fingerprint,
            runtime=runtime,
        )

    def _validate_binding(self, binding: HostObservationBinding) -> WorkspaceState:
        if not isinstance(binding, HostObservationBinding):
            raise TypeError("binding must be HostObservationBinding")
        state = self.workspaces.state(binding.workspace_id)
        if state.workspace.song_id != binding.song_id:
            raise HostObservationError("observation binding crossed Song identity")
        if state.workspace.host_family != binding.runtime.family:
            raise HostObservationError("observation binding crossed host family")
        current = state.current_observation
        if (
            current.id != binding.workspace_observation_id
            or current.host_runtime_fingerprint != binding.host_runtime_fingerprint
            or binding.runtime.fingerprint != binding.host_runtime_fingerprint
        ):
            raise HostObservationError(
                "host observation binding is stale relative to the current workspace"
            )
        return state

    def _preflight_capabilities(
        self,
        binding: HostObservationBinding,
        capabilities: tuple[CapabilityFactInput, ...],
        *,
        now_epoch_seconds: int,
    ) -> None:
        now = _nonnegative_int(now_epoch_seconds, "now_epoch_seconds")
        if not all(isinstance(item, CapabilityFactInput) for item in capabilities):
            raise TypeError("capabilities must contain CapabilityFactInput values")
        keys = [(item.capability, item.route_id) for item in capabilities]
        if len(keys) != len(set(keys)):
            raise HostObservationError(
                "one host observation may report each capability/route at most once"
            )
        route_kinds: dict[str, str] = {}
        for item in capabilities:
            if item.observed_at_epoch_seconds > now:
                raise HostObservationError(
                    "session time predates submitted capability evidence"
                )
            prior = route_kinds.get(item.route_id)
            if prior is not None and prior != item.route_kind:
                raise HostObservationError(
                    "one route_id cannot use multiple route kinds in one observation"
                )
            route_kinds[item.route_id] = item.route_kind

        history = self.capability_evidence.history(binding.workspace_id)
        current_history = [
            item
            for item in history
            if item.workspace_observation_id == binding.workspace_observation_id
            and item.host_runtime_fingerprint == binding.host_runtime_fingerprint
        ]
        latest_time: dict[tuple[str, str], int] = {}
        existing_kind: dict[str, str] = {}
        for item in current_history:
            if item.observed_at_epoch_seconds > now:
                raise HostObservationError(
                    "session time predates current capability evidence"
                )
            latest_time[(item.capability, item.route_id)] = max(
                latest_time.get((item.capability, item.route_id), 0),
                item.observed_at_epoch_seconds,
            )
            prior = existing_kind.get(item.route_id)
            if prior is not None and prior != item.route_kind:
                raise HostObservationError(
                    "stored route identity is internally inconsistent"
                )
            existing_kind[item.route_id] = item.route_kind
        for item in capabilities:
            if item.observed_at_epoch_seconds < latest_time.get(
                (item.capability, item.route_id), 0
            ):
                raise HostObservationError(
                    "capability observation time regressed before session commit"
                )
            prior = existing_kind.get(item.route_id)
            if prior is not None and prior != item.route_kind:
                raise HostObservationError(
                    "capability route kind conflicts with current environment history"
                )

    def _preflight_shadow(
        self,
        binding: HostObservationBinding,
        shadow: ShadowObservationInput | None,
    ) -> None:
        if shadow is None:
            return
        if not isinstance(shadow, ShadowObservationInput):
            raise TypeError("shadow must be ShadowObservationInput or None")
        if shadow.coverage == "INCREMENTAL":
            state = self.shadow.state(binding.workspace_id)
            if state.status != "CURRENT":
                raise HostObservationError(
                    "INCREMENTAL shadow observation requires a current FULL baseline"
                )
            if state.current_workspace_observation_id != binding.workspace_observation_id:
                raise HostObservationError(
                    "shadow baseline belongs to a different workspace observation"
                )

    def observe(
        self,
        binding: HostObservationBinding,
        *,
        capabilities: tuple[CapabilityFactInput, ...] = (),
        focus_dimensions: tuple[FocusDimension, ...] = (),
        focus_evidence_ref: str,
        shadow: ShadowObservationInput | None = None,
        now_epoch_seconds: int,
    ) -> HostObservationResult:
        self._validate_binding(binding)
        capabilities = tuple(capabilities)
        focus_dimensions = tuple(focus_dimensions)
        if not all(isinstance(item, FocusDimension) for item in focus_dimensions):
            raise TypeError("focus_dimensions must contain FocusDimension values")
        focus_evidence = _text(focus_evidence_ref, "focus_evidence_ref")
        now = _nonnegative_int(now_epoch_seconds, "now_epoch_seconds")
        self._preflight_capabilities(
            binding,
            capabilities,
            now_epoch_seconds=now,
        )
        self._preflight_shadow(binding, shadow)

        # Capture focus before writes. This is pure and revalidates the exact runtime.
        focus_context = self.focus.capture(
            binding.workspace_id,
            song_id=binding.song_id,
            runtime=binding.runtime,
            observation_evidence_ref=focus_evidence,
            dimensions=focus_dimensions,
        )
        if focus_context.workspace_observation_id != binding.workspace_observation_id:
            raise HostObservationError("focus capture changed observation binding")

        recorded_capabilities: list[CapabilityObservation] = []
        shadow_batch: ShadowBatch | None = None
        try:
            for item in capabilities:
                self._validate_binding(binding)
                recorded_capabilities.append(
                    self.capability_evidence.record(
                        binding.workspace_id,
                        expected_workspace_observation_id=binding.workspace_observation_id,
                        expected_host_runtime_fingerprint=binding.host_runtime_fingerprint,
                        route_id=item.route_id,
                        route_kind=item.route_kind,
                        capability=item.capability,
                        display_name=item.display_name,
                        availability=item.availability,
                        evidence_kind=item.evidence_kind,
                        observed_at_epoch_seconds=item.observed_at_epoch_seconds,
                        brand=item.brand,
                        evidence_ref=item.evidence_ref,
                        task_fit=item.task_fit,
                        editability=item.editability,
                        locality=item.locality,
                        privacy=item.privacy,
                        latency=item.latency,
                        reversibility=item.reversibility,
                        cost_efficiency=item.cost_efficiency,
                        portability=item.portability,
                        paid=item.paid,
                    )
                )
            if shadow is not None:
                self._validate_binding(binding)
                shadow_batch = self.shadow.record_batch(
                    binding.workspace_id,
                    workspace_observation_id=binding.workspace_observation_id,
                    host_runtime_fingerprint=binding.host_runtime_fingerprint,
                    coverage=shadow.coverage,
                    actor=shadow.actor,
                    evidence_ref=shadow.evidence_ref,
                    verified=True,
                    events=shadow.events,
                )
        except Exception:
            # Append-only observations already committed by their owners remain truthful,
            # but a failed multi-layer session never returns a COMPLETE/PARTIAL result.
            raise

        final_workspace = self._validate_binding(binding)
        self.focus.validate_current(focus_context)
        capability_environment = self.capability_evidence.state(binding.workspace_id)
        studio = self.capability_evidence.profile(
            binding.workspace_id, now_epoch_seconds=now
        )
        shadow_state = self.shadow.state(binding.workspace_id)
        if (
            capability_environment.workspace_observation_id
            != binding.workspace_observation_id
            or capability_environment.host_runtime_fingerprint
            != binding.host_runtime_fingerprint
            or shadow_state.current_workspace_observation_id
            != binding.workspace_observation_id
            or studio.environment_id != capability_environment.environment_id
        ):
            raise HostObservationError(
                "observation layers no longer share the same workspace binding"
            )

        status = (
            "COMPLETE"
            if shadow_state.status == "CURRENT" and capability_environment.current
            else "PARTIAL"
        )
        return HostObservationResult(
            status=status,
            binding=binding,
            workspace=final_workspace,
            capability_environment=capability_environment,
            studio=studio,
            focus=focus_context,
            shadow=shadow_state,
            recorded_capability_ids=tuple(item.id for item in recorded_capabilities),
            recorded_shadow_batch_id=None if shadow_batch is None else shadow_batch.id,
        )
