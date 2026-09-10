from __future__ import annotations

from dataclasses import dataclass

from .host_runtime import (
    ActiveToolProjection,
    ActiveToolProjectionError,
    project_active_tools,
)
from .shadow import HostShadow, HostShadowError
from .workspace import WorkspaceMemory


class DAWInspectionError(RuntimeError):
    """Current DAW evidence cannot be projected into one safe read-only view."""


@dataclass(frozen=True)
class DAWObservedField:
    name: str
    value: object
    evidence_ref: str


@dataclass(frozen=True)
class DAWObservedObject:
    object_kind: str
    object_ref: str
    fields: tuple[DAWObservedField, ...]


@dataclass(frozen=True)
class DAWInspection:
    workspace_id: str
    song_id: str
    host_family: str
    workspace_observation_id: str
    shadow_batch_id: str
    objects: tuple[DAWObservedObject, ...]
    active_tools: ActiveToolProjection
    read_only: bool = True
    action_authority_granted: bool = False

    def __post_init__(self) -> None:
        if self.read_only is not True or self.action_authority_granted is not False:
            raise DAWInspectionError(
                "DAW inspection may expose only read-only evidence without action authority"
            )


def inspect_current_daw(
    workspaces: WorkspaceMemory,
    shadow: HostShadow,
    workspace_id: str,
) -> DAWInspection:
    """Return one host-neutral view of the current verified DAW evidence.

    Host adapters remain the evidence producers. This projection neither polls a DAW
    nor mutates one. DEVICE_PLUGIN facts are normalized through project_active_tools;
    all other current Host Shadow facts are retained verbatim with provenance so a
    consumer can display current tempo, transport, selection, routing and host state
    without learning host-specific bridge schemas.
    """

    if not isinstance(workspaces, WorkspaceMemory):
        raise TypeError("workspaces must be WorkspaceMemory")
    if not isinstance(shadow, HostShadow):
        raise TypeError("shadow must be HostShadow")
    if shadow.workspaces is not workspaces:
        raise TypeError("shadow and workspaces must share WorkspaceMemory")

    try:
        tools = project_active_tools(workspaces, shadow, workspace_id)
        state = shadow.require_current(workspace_id)
    except (ActiveToolProjectionError, HostShadowError) as exc:
        raise DAWInspectionError(
            "DAW inspection requires a CURRENT verified Host Shadow"
        ) from exc

    if state.current_workspace_observation_id != tools.workspace_observation_id:
        raise DAWInspectionError(
            "DAW inspection crossed workspace observation identity"
        )
    if state.latest_batch_id != tools.shadow_batch_id:
        raise DAWInspectionError("DAW inspection crossed Host Shadow batch identity")

    grouped: dict[tuple[str, str], dict[str, DAWObservedField]] = {}
    for fact in state.facts:
        if fact.object_kind == "DEVICE_PLUGIN":
            continue
        key = (fact.object_kind, fact.object_ref)
        fields = grouped.setdefault(key, {})
        if fact.field in fields:
            raise DAWInspectionError(
                f"duplicate current DAW field: {fact.object_kind}:{fact.object_ref}:{fact.field}"
            )
        fields[fact.field] = DAWObservedField(
            name=fact.field,
            value=fact.value,
            evidence_ref=fact.evidence_ref,
        )

    objects = tuple(
        DAWObservedObject(
            object_kind=object_kind,
            object_ref=object_ref,
            fields=tuple(fields[name] for name in sorted(fields)),
        )
        for (object_kind, object_ref), fields in sorted(grouped.items())
    )

    return DAWInspection(
        workspace_id=tools.workspace_id,
        song_id=tools.song_id,
        host_family=tools.host_family,
        workspace_observation_id=tools.workspace_observation_id,
        shadow_batch_id=tools.shadow_batch_id,
        objects=objects,
        active_tools=tools,
    )
