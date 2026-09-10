from __future__ import annotations

from dataclasses import dataclass

from .host_observation import (
    HostObservationBinding,
    HostObservationCoordinator,
)
from .hosts import HostRuntimeIdentity
from .workspace import WorkspaceIdentity, WorkspaceState

HOST_SESSION_HANDSHAKE_STATUSES = {"CREATED", "REUSED", "RECONCILED"}


class HostSessionHandshakeError(RuntimeError):
    """A DAW project could not be bound safely to canonical N0TE Song/workspace identity."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise HostSessionHandshakeError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


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
