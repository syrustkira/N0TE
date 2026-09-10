from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol

from .audio_engineering import EngineeringSnapshot
from .focus import FocusDimension
from .host_observation import (
    CapabilityFactInput,
    HostObservationBinding,
    HostObservationCoordinator,
    HostObservationResult,
    ShadowObservationInput,
)
from .hosts import HostRuntimeIdentity
from .shadow import HostShadowState
from .workspace import WorkspaceIdentity, WorkspaceState

HOST_SESSION_HANDSHAKE_STATUSES = {"CREATED", "REUSED", "RECONCILED"}


class HostSessionHandshakeError(RuntimeError):
    """A DAW project could not be bound safely to canonical N0TE Song/workspace identity."""


class HostSessionReferenceWorkflowError(RuntimeError):
    """A bound DAW session could not safely advance into reference discovery."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise HostSessionHandshakeError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _workflow_text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise HostSessionReferenceWorkflowError(f"{field} must not be empty")
    return text


def _workflow_strings(values: Iterable[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise HostSessionReferenceWorkflowError(f"{field} must be a sequence")
    out: list[str] = []
    for value in values:
        out.append(_workflow_text(value, field))
    return tuple(out)


class SessionReferenceClient(Protocol):
    async def discover_session(
        self,
        provider_id: str,
        binding: HostObservationBinding,
        shadow: HostShadowState,
        *,
        comparison_dimensions: Iterable[str],
        engineering_snapshot: EngineeringSnapshot | None = None,
        semantic_tags: Iterable[str] = (),
        required_features: Iterable[str] = (),
        desired_tags: Iterable[str] = (),
        feature_weights: Mapping[str, float] | None = None,
        discovery_limit: int = 12,
        result_limit: int = 3,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class HostSessionHandshakeResult:
    status: str
    binding: HostObservationBinding
    workspace: WorkspaceState

    def __post_init__(self) -> None:
        status = str(self.status).strip().upper()
        if status not in HOST_SESSION_HANDSHAKE_STATUSES:
            raise HostSessionHandshakeError(
                f"unsupported host-session handshake status: {status}"
            )
        if not isinstance(self.binding, HostObservationBinding):
            raise TypeError("binding must be HostObservationBinding")
        if not isinstance(self.workspace, WorkspaceState):
            raise TypeError("workspace must be WorkspaceState")
        if self.binding.workspace_id != self.workspace.workspace.id:
            raise HostSessionHandshakeError(
                "handshake binding/workspace identity mismatch"
            )
        if self.binding.song_id != self.workspace.workspace.song_id:
            raise HostSessionHandshakeError(
                "handshake binding/workspace crossed Song identity"
            )
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class HostSessionReferenceWorkflowResult:
    handshake: HostSessionHandshakeResult
    observation: HostObservationResult
    provider_id: str
    references: dict[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.handshake, HostSessionHandshakeResult):
            raise TypeError("handshake must be HostSessionHandshakeResult")
        if not isinstance(self.observation, HostObservationResult):
            raise TypeError("observation must be HostObservationResult")
        provider = _workflow_text(self.provider_id, "provider_id")
        if not isinstance(self.references, dict):
            raise TypeError("references must be a dict")
        if self.observation.binding != self.handshake.binding:
            raise HostSessionReferenceWorkflowError(
                "reference workflow observation crossed the handshake binding"
            )
        if self.references.get("provider_id") != provider:
            raise HostSessionReferenceWorkflowError(
                "reference workflow response provider does not match request"
            )
        if self.references.get("read_only") is not True:
            raise HostSessionReferenceWorkflowError(
                "reference workflow response is not explicitly read-only"
            )
        if self.references.get("action_authority_granted") is not False:
            raise HostSessionReferenceWorkflowError(
                "reference workflow response attempted to grant action authority"
            )
        calibration = self.references.get("session_calibration")
        if not isinstance(calibration, dict):
            raise HostSessionReferenceWorkflowError(
                "reference workflow response is missing session calibration identity"
            )
        binding = self.observation.binding
        for field in ("workspace_id", "song_id", "workspace_observation_id"):
            if calibration.get(field) != getattr(binding, field):
                raise HostSessionReferenceWorkflowError(
                    f"reference workflow response crossed session identity: {field}"
                )
        object.__setattr__(self, "provider_id", provider)


class HostSessionHandshake:
    """Bind one running DAW project to the active canonical Song safely.

    The DAW-side observer may know a project location, runtime identity and an
    optional workspace ID cached from an earlier N0TE handshake. It does not get to
    choose or rewrite canonical Song identity. First attachment uses the artist's
    already-selected active Song. Existing projects may be reused or reconciled only
    inside that Song and host family. Cross-Song or location collisions fail closed.
    """

    def __init__(self, observations: HostObservationCoordinator) -> None:
        if not isinstance(observations, HostObservationCoordinator):
            raise TypeError("observations must be HostObservationCoordinator")
        self.observations = observations
        self.workspaces = observations.workspaces

    def _active_song_id(self) -> str:
        active = self.workspaces.store.active_song()
        if active is None:
            raise HostSessionHandshakeError(
                "host session attachment requires an explicitly active Song"
            )
        return active.id

    @staticmethod
    def _validate_workspace_owner(
        workspace: WorkspaceIdentity,
        *,
        active_song_id: str,
        runtime: HostRuntimeIdentity,
    ) -> None:
        if workspace.song_id != active_song_id:
            raise HostSessionHandshakeError(
                "DAW project is already bound to a different Song; select that Song explicitly"
            )
        if workspace.host_family != runtime.family:
            raise HostSessionHandshakeError(
                "DAW project workspace belongs to a different host family"
            )

    def _resolve_existing(
        self,
        *,
        active_song_id: str,
        runtime: HostRuntimeIdentity,
        location_ref: str,
        known_workspace_id: str | None,
    ) -> WorkspaceIdentity | None:
        candidates = self.workspaces.current_candidates_at_location(location_ref)
        if len(candidates) > 1:
            raise HostSessionHandshakeError(
                "multiple current workspaces claim the same DAW project location"
            )

        if known_workspace_id is not None:
            workspace = self.workspaces.get(known_workspace_id)
            if workspace is None:
                raise HostSessionHandshakeError(
                    "known workspace ID is not present in the active N0TE profile"
                )
            self._validate_workspace_owner(
                workspace,
                active_song_id=active_song_id,
                runtime=runtime,
            )
            if candidates and candidates[0].id != workspace.id:
                raise HostSessionHandshakeError(
                    "target DAW project location is owned by another workspace"
                )
            return workspace

        if not candidates:
            return None
        workspace = candidates[0]
        self._validate_workspace_owner(
            workspace,
            active_song_id=active_song_id,
            runtime=runtime,
        )
        return workspace

    def attach(
        self,
        *,
        runtime: HostRuntimeIdentity,
        location_ref: str,
        known_workspace_id: str | None = None,
        display_name: str | None = None,
        state_fingerprint: str | None = None,
    ) -> HostSessionHandshakeResult:
        if not isinstance(runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        location = _text(location_ref, "location_ref")
        known = _optional_text(known_workspace_id, "known_workspace_id")
        display = _optional_text(display_name, "display_name")
        state_fingerprint = _optional_text(state_fingerprint, "state_fingerprint")
        active_song_id = self._active_song_id()

        workspace = self._resolve_existing(
            active_song_id=active_song_id,
            runtime=runtime,
            location_ref=location,
            known_workspace_id=known,
        )

        if workspace is None:
            workspace = self.workspaces.create(
                active_song_id,
                runtime=runtime,
                location_ref=location,
                display_name=display,
                state_fingerprint=state_fingerprint,
            )
            status = "CREATED"
        else:
            current_state = self.workspaces.state(workspace.id)
            current = current_state.current_observation
            effective_display = display if display is not None else current.display_name
            effective_state = (
                state_fingerprint
                if state_fingerprint is not None
                else current.state_fingerprint
            )
            unchanged = (
                current.location_ref == location
                and current.host_runtime_fingerprint == runtime.fingerprint
                and current.display_name == effective_display
                and current.state_fingerprint == effective_state
            )
            if unchanged:
                status = "REUSED"
            else:
                workspace = self.workspaces.reconcile_existing(
                    workspace.id,
                    song_id=active_song_id,
                    relation="SAME_OR_MOVED",
                    runtime=runtime,
                    location_ref=location,
                    display_name=effective_display,
                    state_fingerprint=effective_state,
                )
                status = "RECONCILED"

        state = self.workspaces.state(workspace.id)
        binding = self.observations.begin(
            workspace.id,
            song_id=active_song_id,
            runtime=runtime,
        )
        return HostSessionHandshakeResult(
            status=status,
            binding=binding,
            workspace=state,
        )


class HostSessionReferenceWorkflow:
    """Adapter-facing attach -> observe -> read-only reference-discovery sequence.

    Project identity and factual host observations are committed by their canonical
    owners before reference discovery. Provider/coordinator failure therefore never
    erases truthful DAW evidence. The workflow grants no DAW mutation authority and
    never persists a discovered reference merely because it was returned.
    """

    def __init__(
        self,
        observations: HostObservationCoordinator,
        reference_client: SessionReferenceClient,
    ) -> None:
        if not isinstance(observations, HostObservationCoordinator):
            raise TypeError("observations must be HostObservationCoordinator")
        if not callable(getattr(reference_client, "discover_session", None)):
            raise TypeError("reference_client must provide discover_session")
        self.observations = observations
        self.handshake = HostSessionHandshake(observations)
        self.reference_client = reference_client

    async def observe_and_discover(
        self,
        *,
        runtime: HostRuntimeIdentity,
        location_ref: str,
        provider_id: str,
        focus_evidence_ref: str,
        now_epoch_seconds: int,
        comparison_dimensions: Iterable[str],
        known_workspace_id: str | None = None,
        display_name: str | None = None,
        state_fingerprint: str | None = None,
        capabilities: tuple[CapabilityFactInput, ...] = (),
        focus_dimensions: tuple[FocusDimension, ...] = (),
        shadow: ShadowObservationInput | None = None,
        engineering_snapshot: EngineeringSnapshot | None = None,
        semantic_tags: Iterable[str] = (),
        required_features: Iterable[str] = (),
        desired_tags: Iterable[str] = (),
        feature_weights: Mapping[str, float] | None = None,
        discovery_limit: int = 12,
        result_limit: int = 3,
    ) -> HostSessionReferenceWorkflowResult:
        provider = _workflow_text(provider_id, "provider_id")
        dimensions = _workflow_strings(
            comparison_dimensions, "comparison_dimensions"
        )
        tags = _workflow_strings(semantic_tags, "semantic_tags")
        required = _workflow_strings(required_features, "required_features")
        desired = _workflow_strings(desired_tags, "desired_tags")
        weights = None if feature_weights is None else dict(feature_weights)

        attached = self.handshake.attach(
            runtime=runtime,
            location_ref=location_ref,
            known_workspace_id=known_workspace_id,
            display_name=display_name,
            state_fingerprint=state_fingerprint,
        )
        observation = self.observations.observe(
            attached.binding,
            capabilities=tuple(capabilities),
            focus_dimensions=tuple(focus_dimensions),
            focus_evidence_ref=focus_evidence_ref,
            shadow=shadow,
            now_epoch_seconds=now_epoch_seconds,
        )
        if observation.shadow.status != "CURRENT":
            raise HostSessionReferenceWorkflowError(
                "reference discovery requires a CURRENT Host Shadow; "
                "the truthful host observation remains recorded"
            )

        references = await self.reference_client.discover_session(
            provider,
            observation.binding,
            observation.shadow,
            comparison_dimensions=dimensions,
            engineering_snapshot=engineering_snapshot,
            semantic_tags=tags,
            required_features=required,
            desired_tags=desired,
            feature_weights=weights,
            discovery_limit=discovery_limit,
            result_limit=result_limit,
        )
        return HostSessionReferenceWorkflowResult(
            handshake=attached,
            observation=observation,
            provider_id=provider,
            references=references,
        )
