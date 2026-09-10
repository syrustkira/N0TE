from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .focus import FocusDimension
from .host_observation import CapabilityFactInput, ShadowObservationInput
from .host_session_handshake import (
    HostSessionReferenceWorkflow,
    HostSessionReferenceWorkflowResult,
)
from .hosts import HostRuntimeIdentity
from .shadow import ShadowEventInput

REAPER_SNAPSHOT_SCHEMA = "n0te.reaper-observation/v1"
DEFAULT_REAPER_SNAPSHOT_NAME = "n0te_snapshot.json"
_MAX_SNAPSHOT_BYTES = 65536
_MAX_SELECTED_TRACKS = 32
_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema",
        "adapter",
        "bridge_session_id",
        "observed_at_epoch_seconds",
        "runtime",
        "project",
        "tempo_bpm",
        "transport",
        "track_count",
        "selected_tracks",
        "selection_truncated",
    }
)


class ReaperHostBridgeError(RuntimeError):
    """REAPER read-side bridge evidence could not be trusted safely."""


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ReaperHostBridgeError(f"{field} must not be empty")
    return text


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ReaperHostBridgeError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReaperHostBridgeError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ReaperHostBridgeError(f"{field} must be finite")
    return number


def _integer(
    value: object,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise ReaperHostBridgeError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ReaperHostBridgeError(f"{field} must be an integer") from exc
    if minimum is not None and number < minimum:
        raise ReaperHostBridgeError(f"{field} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ReaperHostBridgeError(f"{field} must be <= {maximum}")
    return number


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReaperHostBridgeError(f"{field} must be an object")
    return value


def _exact_fields(
    payload: Mapping[str, object],
    allowed: frozenset[str],
    field: str,
) -> None:
    extra = sorted(set(payload) - allowed)
    if extra:
        raise ReaperHostBridgeError(
            f"{field} contains unsupported fields: {extra}"
        )


@dataclass(frozen=True)
class ReaperSelectedTrack:
    index: int
    guid: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "index", _integer(self.index, "track.index", minimum=0))
        object.__setattr__(self, "guid", _text(self.guid, "track.guid"))
        object.__setattr__(self, "name", _text(self.name, "track.name"))

    @property
    def focus_ref(self) -> str:
        return f"reaper-track:{self.guid}"


@dataclass(frozen=True)
class ReaperObservationSnapshot:
    bridge_session_id: str
    runtime: HostRuntimeIdentity
    observed_at_epoch_seconds: int
    project_name: str
    project_saved: bool
    project_state_change_count: int
    tempo_bpm: float
    play_state: int
    play_position_seconds: float
    repeat_enabled: bool
    track_count: int
    selected_tracks: tuple[ReaperSelectedTrack, ...]
    selection_truncated: bool
    adapter_id: str
    adapter_version: str

    def __post_init__(self) -> None:
        session = _text(self.bridge_session_id, "bridge_session_id")
        if not isinstance(self.runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        if self.runtime.family != "REAPER":
            raise ReaperHostBridgeError("REAPER snapshot requires REAPER runtime")
        observed = _integer(
            self.observed_at_epoch_seconds,
            "observed_at_epoch_seconds",
            minimum=0,
        )
        project_name = _text(self.project_name, "project.name")
        if type(self.project_saved) is not bool:
            raise ReaperHostBridgeError("project.saved must be bool")
        state_change_count = _integer(
            self.project_state_change_count,
            "project.state_change_count",
            minimum=0,
        )
        tempo = _finite(self.tempo_bpm, "tempo_bpm")
        if not 20.0 <= tempo <= 400.0:
            raise ReaperHostBridgeError("tempo_bpm must be between 20 and 400")
        play_state = _integer(self.play_state, "transport.play_state", minimum=0, maximum=7)
        position = _finite(
            self.play_position_seconds,
            "transport.play_position_seconds",
        )
        if position < 0:
            raise ReaperHostBridgeError(
                "transport.play_position_seconds must be non-negative"
            )
        if type(self.repeat_enabled) is not bool:
            raise ReaperHostBridgeError("transport.repeat_enabled must be bool")
        track_count = _integer(self.track_count, "track_count", minimum=0)
        tracks = tuple(self.selected_tracks)
        if len(tracks) > _MAX_SELECTED_TRACKS:
            raise ReaperHostBridgeError("selected_tracks exceeds bridge bound")
        if not all(isinstance(item, ReaperSelectedTrack) for item in tracks):
            raise TypeError("selected_tracks must contain ReaperSelectedTrack values")
        if any(item.index >= track_count for item in tracks):
            raise ReaperHostBridgeError(
                "selected track index must be inside current track_count"
            )
        guids = [item.guid for item in tracks]
        if len(guids) != len(set(guids)):
            raise ReaperHostBridgeError("selected track GUIDs must be unique")
        if type(self.selection_truncated) is not bool:
            raise ReaperHostBridgeError("selection_truncated must be bool")
        if self.selection_truncated and len(tracks) != _MAX_SELECTED_TRACKS:
            raise ReaperHostBridgeError(
                "truncated selection must contain the maximum emitted track count"
            )
        object.__setattr__(self, "bridge_session_id", session)
        object.__setattr__(self, "observed_at_epoch_seconds", observed)
        object.__setattr__(self, "project_name", project_name)
        object.__setattr__(self, "project_state_change_count", state_change_count)
        object.__setattr__(self, "tempo_bpm", tempo)
        object.__setattr__(self, "play_state", play_state)
        object.__setattr__(self, "play_position_seconds", position)
        object.__setattr__(self, "track_count", track_count)
        object.__setattr__(self, "selected_tracks", tracks)
        object.__setattr__(self, "adapter_id", _text(self.adapter_id, "adapter.id"))
        object.__setattr__(self, "adapter_version", _text(self.adapter_version, "adapter.version"))

    @classmethod
    def from_payload(cls, payload: object) -> "ReaperObservationSnapshot":
        root = _object(payload, "snapshot")
        _exact_fields(root, _ALLOWED_TOP_LEVEL, "snapshot")
        if root.get("schema") != REAPER_SNAPSHOT_SCHEMA:
            raise ReaperHostBridgeError("unsupported REAPER snapshot schema")

        adapter = _object(root.get("adapter"), "adapter")
        _exact_fields(adapter, frozenset({"id", "version"}), "adapter")

        runtime_payload = _object(root.get("runtime"), "runtime")
        _exact_fields(
            runtime_payload,
            frozenset({"host_family", "version", "edition", "os_name", "machine"}),
            "runtime",
        )
        if _text(runtime_payload.get("host_family"), "runtime.host_family").upper() != "REAPER":
            raise ReaperHostBridgeError("REAPER bridge may report only REAPER")
        runtime = HostRuntimeIdentity.from_runtime_labels(
            host_family="REAPER",
            version=_text(runtime_payload.get("version"), "runtime.version"),
            edition=_text(runtime_payload.get("edition"), "runtime.edition"),
            os_name=_text(runtime_payload.get("os_name"), "runtime.os_name"),
            machine=_text(runtime_payload.get("machine"), "runtime.machine"),
        )

        project = _object(root.get("project"), "project")
        _exact_fields(
            project,
            frozenset({"name", "saved", "state_change_count"}),
            "project",
        )
        if type(project.get("saved")) is not bool:
            raise ReaperHostBridgeError("project.saved must be bool")

        transport = _object(root.get("transport"), "transport")
        _exact_fields(
            transport,
            frozenset({"play_state", "play_position_seconds", "repeat_enabled"}),
            "transport",
        )
        if type(transport.get("repeat_enabled")) is not bool:
            raise ReaperHostBridgeError("transport.repeat_enabled must be bool")

        raw_tracks = root.get("selected_tracks")
        if not isinstance(raw_tracks, list):
            raise ReaperHostBridgeError("selected_tracks must be an array")
        tracks: list[ReaperSelectedTrack] = []
        for index, raw in enumerate(raw_tracks):
            item = _object(raw, f"selected_tracks[{index}]")
            _exact_fields(
                item,
                frozenset({"index", "guid", "name"}),
                f"selected_tracks[{index}]",
            )
            tracks.append(
                ReaperSelectedTrack(
                    index=_integer(item.get("index"), f"selected_tracks[{index}].index", minimum=0),
                    guid=_text(item.get("guid"), f"selected_tracks[{index}].guid"),
                    name=_text(item.get("name"), f"selected_tracks[{index}].name"),
                )
            )
        if type(root.get("selection_truncated")) is not bool:
            raise ReaperHostBridgeError("selection_truncated must be bool")

        return cls(
            bridge_session_id=_text(root.get("bridge_session_id"), "bridge_session_id"),
            runtime=runtime,
            observed_at_epoch_seconds=_integer(
                root.get("observed_at_epoch_seconds"),
                "observed_at_epoch_seconds",
                minimum=0,
            ),
            project_name=_text(project.get("name"), "project.name"),
            project_saved=project["saved"],
            project_state_change_count=_integer(
                project.get("state_change_count"),
                "project.state_change_count",
                minimum=0,
            ),
            tempo_bpm=_finite(root.get("tempo_bpm"), "tempo_bpm"),
            play_state=_integer(
                transport.get("play_state"),
                "transport.play_state",
                minimum=0,
                maximum=7,
            ),
            play_position_seconds=_finite(
                transport.get("play_position_seconds"),
                "transport.play_position_seconds",
            ),
            repeat_enabled=transport["repeat_enabled"],
            track_count=_integer(root.get("track_count"), "track_count", minimum=0),
            selected_tracks=tuple(tracks),
            selection_truncated=root["selection_truncated"],
            adapter_id=_text(adapter.get("id"), "adapter.id"),
            adapter_version=_text(adapter.get("version"), "adapter.version"),
        )

    @property
    def location_ref(self) -> str:
        return f"reaper-session:{self.bridge_session_id}"

    @property
    def is_playing(self) -> bool:
        return bool(self.play_state & 0x01)

    @property
    def is_paused(self) -> bool:
        return bool(self.play_state & 0x02)

    @property
    def is_recording(self) -> bool:
        return bool(self.play_state & 0x04)

    def capabilities(self) -> tuple[CapabilityFactInput, ...]:
        base = dict(
            route_id="n0te-reaper-reascript",
            route_kind="HOST_NATIVE",
            display_name="N0TE REAPER ReaScript",
            availability="AVAILABLE",
            evidence_kind="RUNTIME_PROBE",
            observed_at_epoch_seconds=self.observed_at_epoch_seconds,
            locality=1.0,
            privacy=1.0,
            latency=0.9,
            reversibility=1.0,
            cost_efficiency=1.0,
        )
        session = self.bridge_session_id
        facts = [
            CapabilityFactInput(
                capability="tempo.read",
                evidence_ref=f"reaper:snapshot:{session}:tempo",
                **base,
            ),
            CapabilityFactInput(
                capability="transport.read",
                evidence_ref=f"reaper:snapshot:{session}:transport",
                **base,
            ),
            CapabilityFactInput(
                capability="project.metadata.read",
                evidence_ref=f"reaper:snapshot:{session}:project",
                **base,
            ),
            CapabilityFactInput(
                capability="project.state-change.read",
                evidence_ref=f"reaper:snapshot:{session}:project-state",
                **base,
            ),
            CapabilityFactInput(
                capability="track.count.read",
                evidence_ref=f"reaper:snapshot:{session}:tracks",
                **base,
            ),
        ]
        if self.selected_tracks:
            facts.append(
                CapabilityFactInput(
                    capability="focus.track.read",
                    evidence_ref=f"reaper:snapshot:{session}:selected-tracks",
                    **base,
                )
            )
        return tuple(facts)

    def focus_dimensions(self) -> tuple[FocusDimension, ...]:
        if not self.selected_tracks:
            return ()
        refs = tuple(item.focus_ref for item in self.selected_tracks)
        if len(refs) == 1:
            state = "OBSERVED_EXACT"
        else:
            state = "OBSERVED_AMBIGUOUS"
        return (
            FocusDimension(
                dimension="TRACK",
                state=state,
                refs=refs,
                evidence_ref=f"reaper:snapshot:{self.bridge_session_id}:selected-tracks",
            ),
        )

    def shadow(self) -> ShadowObservationInput:
        session = self.bridge_session_id
        transport_ref = f"reaper:snapshot:{session}:transport"
        events = [
            ShadowEventInput(
                object_kind="TEMPO",
                object_ref="tempo:main",
                field="bpm",
                action="SET",
                value=self.tempo_bpm,
                evidence_ref=f"reaper:snapshot:{session}:tempo",
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="is_playing",
                action="SET",
                value=self.is_playing,
                evidence_ref=transport_ref,
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="is_paused",
                action="SET",
                value=self.is_paused,
                evidence_ref=transport_ref,
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="is_recording",
                action="SET",
                value=self.is_recording,
                evidence_ref=transport_ref,
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="play_position_seconds",
                action="SET",
                value=self.play_position_seconds,
                evidence_ref=transport_ref,
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="repeat_enabled",
                action="SET",
                value=self.repeat_enabled,
                evidence_ref=transport_ref,
            ),
        ]
        for track in self.selected_tracks:
            events.append(
                ShadowEventInput(
                    object_kind="TRACK",
                    object_ref=track.focus_ref,
                    field="name",
                    action="SET",
                    value=track.name,
                    evidence_ref=f"reaper:snapshot:{session}:selected-tracks",
                )
            )
        return ShadowObservationInput(
            coverage="FULL",
            actor="EXTERNAL",
            evidence_ref=f"reaper:snapshot:{session}",
            events=tuple(events),
        )


class ReaperSnapshotFileClient:
    """Read a bounded, fresh snapshot written by N0TE's deferred REAPER script."""

    def __init__(
        self,
        snapshot_path: str | Path,
        *,
        max_age_seconds: float = 5.0,
        now_epoch_seconds: Callable[[], float] = time.time,
    ) -> None:
        path = Path(snapshot_path).expanduser()
        if not path.is_absolute():
            raise ReaperHostBridgeError("REAPER snapshot path must be absolute")
        max_age = _finite(max_age_seconds, "max_age_seconds")
        if max_age <= 0 or max_age > 60:
            raise ReaperHostBridgeError("max_age_seconds must be between 0 and 60")
        if not callable(now_epoch_seconds):
            raise TypeError("now_epoch_seconds must be callable")
        self.snapshot_path = path
        self.max_age_seconds = max_age
        self.now_epoch_seconds = now_epoch_seconds

    def fetch_snapshot(self) -> ReaperObservationSnapshot:
        path = self.snapshot_path
        try:
            if path.is_symlink():
                raise ReaperHostBridgeError("REAPER snapshot file must not be a symlink")
            stat = path.stat()
            if not path.is_file():
                raise ReaperHostBridgeError("REAPER snapshot path is not a regular file")
            if stat.st_size < 2 or stat.st_size > _MAX_SNAPSHOT_BYTES:
                raise ReaperHostBridgeError("REAPER snapshot size is invalid")
            raw = path.read_bytes()
        except ReaperHostBridgeError:
            raise
        except OSError as exc:
            raise ReaperHostBridgeError("cannot read REAPER snapshot file") from exc
        if len(raw) > _MAX_SNAPSHOT_BYTES:
            raise ReaperHostBridgeError("REAPER snapshot exceeds the snapshot bound")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReaperHostBridgeError("REAPER snapshot is not valid UTF-8 JSON") from exc
        snapshot = ReaperObservationSnapshot.from_payload(payload)
        now = _finite(self.now_epoch_seconds(), "current time")
        age = now - float(snapshot.observed_at_epoch_seconds)
        if age < -10.0:
            raise ReaperHostBridgeError(
                "REAPER snapshot timestamp is implausibly in the future"
            )
        if age > self.max_age_seconds:
            raise ReaperHostBridgeError("REAPER snapshot is stale")
        return snapshot

    async def observe_and_discover(
        self,
        workflow: HostSessionReferenceWorkflow,
        *,
        provider_id: str,
        comparison_dimensions: tuple[str, ...] = ("tempo",),
        semantic_tags: tuple[str, ...] = (),
        required_features: tuple[str, ...] = ("tempo_bpm",),
        desired_tags: tuple[str, ...] = (),
        feature_weights: Mapping[str, float] | None = None,
        discovery_limit: int = 12,
        result_limit: int = 3,
    ) -> HostSessionReferenceWorkflowResult:
        if not isinstance(workflow, HostSessionReferenceWorkflow):
            raise TypeError("workflow must be HostSessionReferenceWorkflow")
        snapshot = await asyncio.to_thread(self.fetch_snapshot)
        return await workflow.observe_and_discover(
            runtime=snapshot.runtime,
            location_ref=snapshot.location_ref,
            display_name=f"REAPER • {snapshot.project_name}",
            provider_id=provider_id,
            capabilities=snapshot.capabilities(),
            focus_dimensions=snapshot.focus_dimensions(),
            focus_evidence_ref=f"reaper:snapshot:{snapshot.bridge_session_id}:focus",
            shadow=snapshot.shadow(),
            now_epoch_seconds=snapshot.observed_at_epoch_seconds,
            comparison_dimensions=comparison_dimensions,
            semantic_tags=semantic_tags,
            required_features=required_features,
            desired_tags=desired_tags,
            feature_weights=feature_weights,
            discovery_limit=discovery_limit,
            result_limit=result_limit,
        )
