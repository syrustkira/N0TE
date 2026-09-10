from __future__ import annotations

import asyncio
import json
import math
import os
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

FL_STUDIO_SNAPSHOT_SCHEMA = "n0te.fl-studio-observation/v2"
FL_STUDIO_LEGACY_SNAPSHOT_SCHEMA = "n0te.fl-studio-observation/v1"
DEFAULT_FL_STUDIO_SNAPSHOT_NAME = "n0te_snapshot.json"
_MAX_SNAPSHOT_BYTES = 65536
_MAX_PLUGIN_TEXT_CHARS = 256
_MIXER_EFFECT_SLOT_COUNT = 10
_BASE_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema",
        "adapter",
        "bridge_session_id",
        "runtime",
        "observed_at_epoch_seconds",
        "project",
        "tempo_bpm",
        "transport",
        "selected_mixer_track",
        "selected_channel",
        "active_window",
    }
)
_V2_ALLOWED_TOP_LEVEL = _BASE_ALLOWED_TOP_LEVEL | frozenset(
    {"selected_mixer_plugins", "selected_channel_generator"}
)


class FLStudioHostBridgeError(RuntimeError):
    """FL Studio read-side bridge evidence could not be trusted safely."""


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise FLStudioHostBridgeError(f"{field} must not be empty")
    return text


def _bounded_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise FLStudioHostBridgeError(f"{field} must be a string")
    text = _text(value, field)
    if len(text) > _MAX_PLUGIN_TEXT_CHARS:
        raise FLStudioHostBridgeError(
            f"{field} exceeds {_MAX_PLUGIN_TEXT_CHARS} characters"
        )
    return text


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise FLStudioHostBridgeError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FLStudioHostBridgeError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise FLStudioHostBridgeError(f"{field} must be finite")
    return number


def _integer(value: object, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise FLStudioHostBridgeError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FLStudioHostBridgeError(f"{field} must be an integer") from exc
    if minimum is not None and number < minimum:
        raise FLStudioHostBridgeError(f"{field} must be >= {minimum}")
    return number


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise FLStudioHostBridgeError(f"{field} must be an object")
    return value


def _exact_fields(payload: Mapping[str, object], allowed: frozenset[str], field: str) -> None:
    extra = sorted(set(payload) - allowed)
    if extra:
        raise FLStudioHostBridgeError(f"{field} contains unsupported fields: {extra}")


@dataclass(frozen=True)
class FLStudioNamedIndex:
    index: int
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "index", _integer(self.index, "index", minimum=0))
        object.__setattr__(self, "name", _text(self.name, "name"))


@dataclass(frozen=True)
class FLStudioActiveWindow:
    form_id: int
    caption: str
    plugin_name: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "form_id", _integer(self.form_id, "active_window.form_id"))
        object.__setattr__(self, "caption", _text(self.caption, "active_window.caption"))
        object.__setattr__(
            self,
            "plugin_name",
            _optional_text(self.plugin_name, "active_window.plugin_name"),
        )


@dataclass(frozen=True)
class FLStudioMixerPlugin:
    slot: int
    name: str

    def __post_init__(self) -> None:
        slot = _integer(self.slot, "selected_mixer_plugins.slot", minimum=0)
        if slot >= _MIXER_EFFECT_SLOT_COUNT:
            raise FLStudioHostBridgeError(
                f"selected_mixer_plugins.slot must be below {_MIXER_EFFECT_SLOT_COUNT}"
            )
        object.__setattr__(self, "slot", slot)
        object.__setattr__(self, "name", _bounded_text(self.name, "selected_mixer_plugins.name"))


@dataclass(frozen=True)
class FLStudioGeneratorPlugin:
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            _bounded_text(self.name, "selected_channel_generator.name"),
        )


@dataclass(frozen=True)
class FLStudioObservationSnapshot:
    bridge_session_id: str
    runtime: HostRuntimeIdentity
    observed_at_epoch_seconds: int
    project_title: str
    project_changed_flag: int
    tempo_bpm: float
    is_playing: bool
    song_position: float
    loop_mode: int
    selected_mixer_track: FLStudioNamedIndex | None
    selected_channel: FLStudioNamedIndex | None
    active_window: FLStudioActiveWindow | None
    selected_mixer_plugins: tuple[FLStudioMixerPlugin, ...]
    mixer_plugin_chain_observed: bool
    selected_channel_generator: FLStudioGeneratorPlugin | None
    channel_generator_observed: bool
    adapter_id: str
    adapter_version: str

    @property
    def location_ref(self) -> str:
        return f"fl-studio-session:{self.bridge_session_id}"

    @classmethod
    def from_payload(cls, payload: object) -> "FLStudioObservationSnapshot":
        root = _object(payload, "snapshot")
        schema = root.get("schema")
        if schema == FL_STUDIO_SNAPSHOT_SCHEMA:
            _exact_fields(root, _V2_ALLOWED_TOP_LEVEL, "snapshot")
        elif schema == FL_STUDIO_LEGACY_SNAPSHOT_SCHEMA:
            _exact_fields(root, _BASE_ALLOWED_TOP_LEVEL, "snapshot")
        else:
            raise FLStudioHostBridgeError("unsupported FL Studio snapshot schema")

        adapter = _object(root.get("adapter"), "adapter")
        _exact_fields(adapter, frozenset({"id", "version"}), "adapter")

        runtime_payload = _object(root.get("runtime"), "runtime")
        _exact_fields(
            runtime_payload,
            frozenset({"host_family", "version", "edition", "os_name", "machine"}),
            "runtime",
        )
        if _text(runtime_payload.get("host_family"), "runtime.host_family").upper() != "FL_STUDIO":
            raise FLStudioHostBridgeError("FL Studio bridge may report only FL_STUDIO")
        runtime = HostRuntimeIdentity.from_runtime_labels(
            host_family="FL_STUDIO",
            version=_text(runtime_payload.get("version"), "runtime.version"),
            edition=_text(runtime_payload.get("edition"), "runtime.edition"),
            os_name=_text(runtime_payload.get("os_name"), "runtime.os_name"),
            machine=_text(runtime_payload.get("machine"), "runtime.machine"),
        )

        project = _object(root.get("project"), "project")
        _exact_fields(project, frozenset({"title", "changed_flag"}), "project")
        changed_flag = _integer(project.get("changed_flag"), "project.changed_flag", minimum=0)
        if changed_flag not in {0, 1, 2}:
            raise FLStudioHostBridgeError("project.changed_flag must be 0, 1 or 2")

        transport = _object(root.get("transport"), "transport")
        _exact_fields(
            transport,
            frozenset({"is_playing", "song_position", "loop_mode"}),
            "transport",
        )
        if type(transport.get("is_playing")) is not bool:
            raise FLStudioHostBridgeError("transport.is_playing must be bool")
        position = _finite(transport.get("song_position"), "transport.song_position")
        if not 0.0 <= position <= 1.0:
            raise FLStudioHostBridgeError("transport.song_position must be between 0 and 1")
        loop_mode = _integer(transport.get("loop_mode"), "transport.loop_mode")
        if loop_mode not in {0, 1}:
            raise FLStudioHostBridgeError("transport.loop_mode must be 0 or 1")

        tempo = _finite(root.get("tempo_bpm"), "tempo_bpm")
        if not 20.0 <= tempo <= 400.0:
            raise FLStudioHostBridgeError("tempo_bpm must be between 20 and 400")

        def named_index(field: str) -> FLStudioNamedIndex | None:
            raw = root.get(field)
            if raw is None:
                return None
            item = _object(raw, field)
            _exact_fields(item, frozenset({"index", "name"}), field)
            return FLStudioNamedIndex(
                index=_integer(item.get("index"), f"{field}.index", minimum=0),
                name=_text(item.get("name"), f"{field}.name"),
            )

        selected_mixer_track = named_index("selected_mixer_track")
        selected_channel = named_index("selected_channel")

        active_window = None
        raw_window = root.get("active_window")
        if raw_window is not None:
            window = _object(raw_window, "active_window")
            _exact_fields(
                window,
                frozenset({"form_id", "caption", "plugin_name"}),
                "active_window",
            )
            active_window = FLStudioActiveWindow(
                form_id=_integer(window.get("form_id"), "active_window.form_id"),
                caption=_text(window.get("caption"), "active_window.caption"),
                plugin_name=_optional_text(
                    window.get("plugin_name"), "active_window.plugin_name"
                ),
            )

        mixer_plugins: tuple[FLStudioMixerPlugin, ...] = ()
        mixer_chain_observed = False
        generator: FLStudioGeneratorPlugin | None = None
        generator_observed = False
        if schema == FL_STUDIO_SNAPSHOT_SCHEMA and "selected_mixer_plugins" in root:
            raw_mixer_plugins = root.get("selected_mixer_plugins")
            if selected_mixer_track is None:
                if raw_mixer_plugins is not None:
                    raise FLStudioHostBridgeError(
                        "selected_mixer_plugins must be null without a selected mixer track"
                    )
            else:
                mixer_state = _object(raw_mixer_plugins, "selected_mixer_plugins")
                _exact_fields(
                    mixer_state,
                    frozenset({"complete", "plugins"}),
                    "selected_mixer_plugins",
                )
                if type(mixer_state.get("complete")) is not bool:
                    raise FLStudioHostBridgeError(
                        "selected_mixer_plugins.complete must be bool"
                    )
                raw_plugins = mixer_state.get("plugins")
                if not isinstance(raw_plugins, list):
                    raise FLStudioHostBridgeError(
                        "selected_mixer_plugins.plugins must be an array"
                    )
                if len(raw_plugins) > _MIXER_EFFECT_SLOT_COUNT:
                    raise FLStudioHostBridgeError(
                        "selected_mixer_plugins.plugins exceeds mixer slot count"
                    )
                if not mixer_state["complete"]:
                    if raw_plugins:
                        raise FLStudioHostBridgeError(
                            "incomplete selected_mixer_plugins must not contain partial evidence"
                        )
                else:
                    parsed: list[FLStudioMixerPlugin] = []
                    seen_slots: set[int] = set()
                    for index, raw_plugin in enumerate(raw_plugins):
                        item = _object(raw_plugin, f"selected_mixer_plugins.plugins[{index}]")
                        _exact_fields(
                            item,
                            frozenset({"slot", "name"}),
                            f"selected_mixer_plugins.plugins[{index}]",
                        )
                        plugin = FLStudioMixerPlugin(
                            slot=item.get("slot"),
                            name=item.get("name"),
                        )
                        if plugin.slot in seen_slots:
                            raise FLStudioHostBridgeError(
                                "selected_mixer_plugins contains duplicate slots"
                            )
                        if parsed and plugin.slot <= parsed[-1].slot:
                            raise FLStudioHostBridgeError(
                                "selected_mixer_plugins slots must be strictly increasing"
                            )
                        seen_slots.add(plugin.slot)
                        parsed.append(plugin)
                    mixer_plugins = tuple(parsed)
                    mixer_chain_observed = True

        if schema == FL_STUDIO_SNAPSHOT_SCHEMA and "selected_channel_generator" in root:
            raw_generator = root.get("selected_channel_generator")
            if selected_channel is None:
                if raw_generator is not None:
                    raise FLStudioHostBridgeError(
                        "selected_channel_generator must be null without a selected channel"
                    )
            else:
                generator_state = _object(raw_generator, "selected_channel_generator")
                _exact_fields(
                    generator_state,
                    frozenset({"complete", "plugin"}),
                    "selected_channel_generator",
                )
                if type(generator_state.get("complete")) is not bool:
                    raise FLStudioHostBridgeError(
                        "selected_channel_generator.complete must be bool"
                    )
                raw_plugin = generator_state.get("plugin")
                if not generator_state["complete"]:
                    if raw_plugin is not None:
                        raise FLStudioHostBridgeError(
                            "incomplete selected_channel_generator must not contain partial evidence"
                        )
                else:
                    generator_observed = True
                    if raw_plugin is not None:
                        item = _object(raw_plugin, "selected_channel_generator.plugin")
                        _exact_fields(
                            item,
                            frozenset({"name"}),
                            "selected_channel_generator.plugin",
                        )
                        generator = FLStudioGeneratorPlugin(name=item.get("name"))

        return cls(
            bridge_session_id=_text(root.get("bridge_session_id"), "bridge_session_id"),
            runtime=runtime,
            observed_at_epoch_seconds=_integer(
                root.get("observed_at_epoch_seconds"),
                "observed_at_epoch_seconds",
                minimum=0,
            ),
            project_title=_text(project.get("title"), "project.title"),
            project_changed_flag=changed_flag,
            tempo_bpm=tempo,
            is_playing=transport["is_playing"],
            song_position=position,
            loop_mode=loop_mode,
            selected_mixer_track=selected_mixer_track,
            selected_channel=selected_channel,
            active_window=active_window,
            selected_mixer_plugins=mixer_plugins,
            mixer_plugin_chain_observed=mixer_chain_observed,
            selected_channel_generator=generator,
            channel_generator_observed=generator_observed,
            adapter_id=_text(adapter.get("id"), "adapter.id"),
            adapter_version=_text(adapter.get("version"), "adapter.version"),
        )

    def capabilities(self) -> tuple[CapabilityFactInput, ...]:
        base = dict(
            route_id="n0te-fl-studio-midi-script",
            route_kind="HOST_NATIVE",
            display_name="N0TE FL Studio MIDI Script",
            availability="AVAILABLE",
            evidence_kind="RUNTIME_PROBE",
            observed_at_epoch_seconds=self.observed_at_epoch_seconds,
            locality=1.0,
            privacy=1.0,
            latency=0.8,
            reversibility=1.0,
            cost_efficiency=1.0,
        )
        facts = [
            CapabilityFactInput(
                capability="tempo.read",
                evidence_ref="fl-studio:snapshot:tempo",
                **base,
            ),
            CapabilityFactInput(
                capability="transport.read",
                evidence_ref="fl-studio:snapshot:transport",
                **base,
            ),
            CapabilityFactInput(
                capability="project.metadata.read",
                evidence_ref="fl-studio:snapshot:project",
                **base,
            ),
        ]
        if self.selected_mixer_track is not None:
            facts.append(
                CapabilityFactInput(
                    capability="focus.track.read",
                    evidence_ref="fl-studio:snapshot:selected-mixer-track",
                    **base,
                )
            )
        if self.active_window is not None:
            facts.append(
                CapabilityFactInput(
                    capability="focus.active-editor.read",
                    evidence_ref="fl-studio:snapshot:active-window",
                    **base,
                )
            )
        if self.mixer_plugin_chain_observed or self.channel_generator_observed:
            facts.append(
                CapabilityFactInput(
                    capability="device.chain.read",
                    evidence_ref="fl-studio:snapshot:devices",
                    **base,
                )
            )
        return tuple(facts)

    def focus_dimensions(self) -> tuple[FocusDimension, ...]:
        dimensions: list[FocusDimension] = []
        if self.selected_mixer_track is not None:
            dimensions.append(
                FocusDimension(
                    dimension="TRACK",
                    state="OBSERVED_EXACT",
                    refs=(f"mixer-track:{self.selected_mixer_track.index}",),
                    evidence_ref="fl-studio:snapshot:selected-mixer-track",
                )
            )
        if self.active_window is not None:
            dimensions.append(
                FocusDimension(
                    dimension="ACTIVE_EDITOR",
                    state="OBSERVED_EXACT",
                    refs=(f"fl-window:{self.active_window.form_id}",),
                    evidence_ref="fl-studio:snapshot:active-window",
                )
            )
            if self.active_window.plugin_name is not None:
                dimensions.append(
                    FocusDimension(
                        dimension="DEVICE_PLUGIN",
                        state="OBSERVED_EXACT",
                        refs=(
                            f"fl-plugin:{self.active_window.form_id}:{self.active_window.plugin_name}",
                        ),
                        evidence_ref="fl-studio:snapshot:active-window",
                    )
                )
        return tuple(dimensions)

    def shadow(self) -> ShadowObservationInput:
        events = [
            ShadowEventInput(
                object_kind="TEMPO",
                object_ref="tempo:main",
                field="bpm",
                action="SET",
                value=self.tempo_bpm,
                evidence_ref="fl-studio:snapshot:tempo",
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="is_playing",
                action="SET",
                value=self.is_playing,
                evidence_ref="fl-studio:snapshot:transport",
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="song_position_normalized",
                action="SET",
                value=self.song_position,
                evidence_ref="fl-studio:snapshot:transport",
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="loop_mode",
                action="SET",
                value=self.loop_mode,
                evidence_ref="fl-studio:snapshot:transport",
            ),
        ]
        if self.selected_mixer_track is not None:
            track_ref = f"mixer-track:{self.selected_mixer_track.index}"
            events.append(
                ShadowEventInput(
                    object_kind="TRACK",
                    object_ref=track_ref,
                    field="name",
                    action="SET",
                    value=self.selected_mixer_track.name,
                    evidence_ref="fl-studio:snapshot:selected-mixer-track",
                )
            )
            if self.mixer_plugin_chain_observed:
                events.append(
                    ShadowEventInput(
                        object_kind="TRACK",
                        object_ref=track_ref,
                        field="device_count",
                        action="SET",
                        value=len(self.selected_mixer_plugins),
                        evidence_ref="fl-studio:snapshot:selected-mixer-plugins",
                    )
                )
                for plugin in self.selected_mixer_plugins:
                    device_ref = f"device:{track_ref}:slot:{plugin.slot}"
                    evidence = f"fl-studio:snapshot:mixer-plugin:{plugin.slot}"
                    events.extend(
                        (
                            ShadowEventInput(
                                object_kind="DEVICE_PLUGIN",
                                object_ref=device_ref,
                                field="track_ref",
                                action="SET",
                                value=track_ref,
                                evidence_ref=evidence,
                            ),
                            ShadowEventInput(
                                object_kind="DEVICE_PLUGIN",
                                object_ref=device_ref,
                                field="slot",
                                action="SET",
                                value=plugin.slot,
                                evidence_ref=evidence,
                            ),
                            ShadowEventInput(
                                object_kind="DEVICE_PLUGIN",
                                object_ref=device_ref,
                                field="role",
                                action="SET",
                                value="EFFECT",
                                evidence_ref=evidence,
                            ),
                            ShadowEventInput(
                                object_kind="DEVICE_PLUGIN",
                                object_ref=device_ref,
                                field="name",
                                action="SET",
                                value=plugin.name,
                                evidence_ref=evidence,
                            ),
                        )
                    )
        if self.selected_channel is not None and self.channel_generator_observed:
            channel_ref = f"channel:{self.selected_channel.index}"
            if self.selected_channel_generator is not None:
                device_ref = f"device:{channel_ref}:generator"
                events.extend(
                    (
                        ShadowEventInput(
                            object_kind="DEVICE_PLUGIN",
                            object_ref=device_ref,
                            field="channel_ref",
                            action="SET",
                            value=channel_ref,
                            evidence_ref="fl-studio:snapshot:channel-generator",
                        ),
                        ShadowEventInput(
                            object_kind="DEVICE_PLUGIN",
                            object_ref=device_ref,
                            field="role",
                            action="SET",
                            value="GENERATOR",
                            evidence_ref="fl-studio:snapshot:channel-generator",
                        ),
                        ShadowEventInput(
                            object_kind="DEVICE_PLUGIN",
                            object_ref=device_ref,
                            field="name",
                            action="SET",
                            value=self.selected_channel_generator.name,
                            evidence_ref="fl-studio:snapshot:channel-generator",
                        ),
                    )
                )
        return ShadowObservationInput(
            coverage="FULL",
            actor="EXTERNAL",
            evidence_ref=f"fl-studio:snapshot:{self.bridge_session_id}",
            events=tuple(events),
        )


class FLStudioSnapshotFileClient:
    """Read a bounded, fresh snapshot written atomically by N0TE's FL MIDI script."""

    def __init__(
        self,
        snapshot_path: str | Path,
        *,
        max_age_seconds: float = 5.0,
        now_epoch_seconds: Callable[[], float] = time.time,
    ) -> None:
        path = Path(snapshot_path).expanduser()
        if not path.is_absolute():
            raise FLStudioHostBridgeError("FL Studio snapshot path must be absolute")
        max_age = _finite(max_age_seconds, "max_age_seconds")
        if max_age <= 0 or max_age > 60:
            raise FLStudioHostBridgeError("max_age_seconds must be between 0 and 60")
        if not callable(now_epoch_seconds):
            raise TypeError("now_epoch_seconds must be callable")
        self.snapshot_path = path
        self.max_age_seconds = max_age
        self.now_epoch_seconds = now_epoch_seconds

    def fetch_snapshot(self) -> FLStudioObservationSnapshot:
        path = self.snapshot_path
        try:
            if path.is_symlink():
                raise FLStudioHostBridgeError("FL Studio snapshot file must not be a symlink")
            stat = path.stat()
            if not path.is_file():
                raise FLStudioHostBridgeError("FL Studio snapshot path is not a regular file")
            if stat.st_size < 2 or stat.st_size > _MAX_SNAPSHOT_BYTES:
                raise FLStudioHostBridgeError("FL Studio snapshot size is invalid")
            raw = path.read_bytes()
        except FLStudioHostBridgeError:
            raise
        except OSError as exc:
            raise FLStudioHostBridgeError("cannot read FL Studio snapshot file") from exc
        if len(raw) > _MAX_SNAPSHOT_BYTES:
            raise FLStudioHostBridgeError("FL Studio snapshot exceeds the snapshot bound")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FLStudioHostBridgeError("FL Studio snapshot is not valid UTF-8 JSON") from exc
        snapshot = FLStudioObservationSnapshot.from_payload(payload)
        now = _finite(self.now_epoch_seconds(), "current time")
        age = now - float(snapshot.observed_at_epoch_seconds)
        if age < -10.0:
            raise FLStudioHostBridgeError("FL Studio snapshot timestamp is implausibly in the future")
        if age > self.max_age_seconds:
            raise FLStudioHostBridgeError("FL Studio snapshot is stale")
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
            display_name=f"FL Studio • {snapshot.project_title}",
            provider_id=provider_id,
            capabilities=snapshot.capabilities(),
            focus_dimensions=snapshot.focus_dimensions(),
            focus_evidence_ref=f"fl-studio:snapshot:{snapshot.bridge_session_id}:focus",
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
