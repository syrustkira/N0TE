from __future__ import annotations

from dataclasses import dataclass

from .hosts import HostRuntimeIdentity
from .lineage import ValidationError
from .workspace import WorkspaceMemory

FOCUS_DIMENSIONS = {
    "TRACK",
    "CLIP_REGION",
    "TIME_RANGE",
    "MIDI_NOTES",
    "DEVICE_PLUGIN",
    "AUTOMATION",
    "PLAYHEAD",
    "ACTIVE_EDITOR",
    "SONG_SECTION",
}
FOCUS_STATES = {"OBSERVED_EXACT", "OBSERVED_AMBIGUOUS", "INFERRED", "UNKNOWN"}
_SINGLE_VALUE_DIMENSIONS = FOCUS_DIMENSIONS - {"MIDI_NOTES"}


class FocusError(RuntimeError):
    """Invalid or unsafe focus context."""


class FocusUncertainError(FocusError):
    """Requested target is stale, missing, ambiguous, inferred or otherwise unsafe."""

    def __init__(self, reason: str, dimensions: tuple[str, ...]):
        self.reason = str(reason).strip().upper()
        self.dimensions = tuple(dimensions)
        super().__init__(f"focus is {self.reason}: {', '.join(self.dimensions)}")


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise FocusError(f"{field} must not be empty")
    return text


def _dimension(value: str) -> str:
    name = _text(value, "dimension").upper().replace("-", "_").replace(" ", "_")
    if name not in FOCUS_DIMENSIONS:
        raise FocusError(f"unsupported focus dimension: {name}")
    return name


def _state(value: str) -> str:
    name = _text(value, "state").upper().replace("-", "_").replace(" ", "_")
    if name not in FOCUS_STATES:
        raise FocusError(f"unsupported focus state: {name}")
    return name


@dataclass(frozen=True)
class FocusDimension:
    dimension: str
    state: str
    refs: tuple[str, ...]
    evidence_ref: str

    def __post_init__(self) -> None:
        dimension = _dimension(self.dimension)
        state = _state(self.state)
        refs = tuple(_text(value, "focus ref") for value in self.refs)
        if len(refs) != len(set(refs)):
            raise FocusError("focus refs must be unique")
        evidence = _text(self.evidence_ref, "evidence_ref")

        if state == "UNKNOWN":
            if refs:
                raise FocusError("UNKNOWN focus must not carry candidate refs")
        elif state == "OBSERVED_AMBIGUOUS":
            if len(refs) < 2:
                raise FocusError("OBSERVED_AMBIGUOUS focus requires at least two candidate refs")
        elif state in {"OBSERVED_EXACT", "INFERRED"}:
            if not refs:
                raise FocusError(f"{state} focus requires at least one ref")

        if (
            state == "OBSERVED_EXACT"
            and dimension in _SINGLE_VALUE_DIMENSIONS
            and len(refs) != 1
        ):
            raise FocusError(f"{dimension} exact focus requires exactly one ref")

        object.__setattr__(self, "dimension", dimension)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "refs", refs)
        object.__setattr__(self, "evidence_ref", evidence)


@dataclass(frozen=True)
class FocusContext:
    workspace_id: str
    song_id: str
    workspace_observation_id: str
    host_runtime_fingerprint: str
    observation_evidence_ref: str
    dimensions: tuple[FocusDimension, ...]

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
        object.__setattr__(
            self,
            "observation_evidence_ref",
            _text(self.observation_evidence_ref, "observation_evidence_ref"),
        )
        dimensions = tuple(self.dimensions)
        if not all(isinstance(item, FocusDimension) for item in dimensions):
            raise TypeError("dimensions must contain FocusDimension values")
        names = [item.dimension for item in dimensions]
        if len(names) != len(set(names)):
            raise FocusError("FocusContext may contain each dimension at most once")
        object.__setattr__(self, "dimensions", dimensions)

    def get(self, dimension: str) -> FocusDimension | None:
        wanted = _dimension(dimension)
        return next((item for item in self.dimensions if item.dimension == wanted), None)


class FocusContextService:
    """Read-only focus gate bound to one current WorkspaceMemory observation."""

    def __init__(self, workspaces: WorkspaceMemory):
        if not isinstance(workspaces, WorkspaceMemory):
            raise TypeError("FocusContextService requires WorkspaceMemory")
        self.workspaces = workspaces

    def capture(
        self,
        workspace_id: str,
        *,
        song_id: str,
        runtime: HostRuntimeIdentity,
        observation_evidence_ref: str,
        dimensions: tuple[FocusDimension, ...] = (),
    ) -> FocusContext:
        if not isinstance(runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        state = self.workspaces.state(workspace_id)
        if state.workspace.song_id != str(song_id):
            raise ValidationError("workspace belongs to a different Song")
        if state.workspace.host_family != runtime.family:
            raise FocusError("focus runtime belongs to a different host family")
        if state.current_observation.host_runtime_fingerprint != runtime.fingerprint:
            raise FocusUncertainError("STALE_RUNTIME", ("WORKSPACE",))
        return FocusContext(
            workspace_id=state.workspace.id,
            song_id=state.workspace.song_id,
            workspace_observation_id=state.current_observation.id,
            host_runtime_fingerprint=runtime.fingerprint,
            observation_evidence_ref=observation_evidence_ref,
            dimensions=tuple(dimensions),
        )

    def validate_current(self, context: FocusContext) -> FocusContext:
        if not isinstance(context, FocusContext):
            raise TypeError("context must be FocusContext")
        state = self.workspaces.state(context.workspace_id)
        if state.workspace.song_id != context.song_id:
            raise FocusUncertainError("STALE_WORKSPACE", ("WORKSPACE",))
        if (
            state.current_observation.id != context.workspace_observation_id
            or state.current_observation.host_runtime_fingerprint
            != context.host_runtime_fingerprint
        ):
            raise FocusUncertainError("STALE_WORKSPACE", ("WORKSPACE",))
        return context

    def require_exact(
        self,
        context: FocusContext,
        *required_dimensions: str,
    ) -> tuple[FocusDimension, ...]:
        self.validate_current(context)
        requested = tuple(_dimension(value) for value in required_dimensions)
        if not requested:
            raise FocusError("require_exact requires at least one focus dimension")
        if len(requested) != len(set(requested)):
            raise FocusError("required focus dimensions must be unique")

        resolved: list[FocusDimension] = []
        unsafe: list[str] = []
        reasons: list[str] = []
        for name in requested:
            item = context.get(name)
            if item is None:
                unsafe.append(name)
                reasons.append("UNKNOWN")
                continue
            if item.state != "OBSERVED_EXACT":
                unsafe.append(name)
                reasons.append(item.state)
                continue
            resolved.append(item)

        if unsafe:
            reason = "UNSAFE_TARGET_" + "_".join(sorted(set(reasons)))
            raise FocusUncertainError(reason, tuple(unsafe))
        return tuple(resolved)

    def exact_refs(
        self,
        context: FocusContext,
        dimension: str,
    ) -> tuple[str, ...]:
        (item,) = self.require_exact(context, dimension)
        return item.refs
