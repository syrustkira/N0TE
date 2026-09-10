from __future__ import annotations

import asyncio
import math
import statistics
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .host_observation import CapabilityFactInput, ShadowObservationInput
from .host_session_handshake import (
    HostSessionReferenceWorkflow,
    HostSessionReferenceWorkflowResult,
)
from .hosts import HostRuntimeIdentity
from .shadow import ShadowEventInput

MIDI_CLOCK = 0xF8
MIDI_START = 0xFA
MIDI_CONTINUE = 0xFB
MIDI_STOP = 0xFC
DEFAULT_STUDIO_ONE_CLOCK_PORT = "N0TE Studio One Clock"
IDENTITY_SCOPE = "MIDI_CLOCK_MONITOR_SESSION_ONLY"
_TRANSPORT_STATES = {"UNKNOWN", "PLAYING", "STOPPED"}
_MIN_CLOCK_INTERVALS = 24
_CLOCK_WINDOW = 48
_MIN_TICK_SECONDS = 60.0 / (400.0 * 24.0) * 0.75
_MAX_TICK_SECONDS = 60.0 / (20.0 * 24.0) * 1.5


class StudioOneMidiBridgeError(RuntimeError):
    """Studio One MIDI-clock evidence could not be received or trusted safely."""


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise StudioOneMidiBridgeError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StudioOneMidiBridgeError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise StudioOneMidiBridgeError(f"{field} must be finite")
    return number


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise StudioOneMidiBridgeError(f"{field} must not be empty")
    return text


def unobserved_studio_one_runtime() -> HostRuntimeIdentity:
    """Represent only Studio One identity facts MIDI Clock can support honestly."""

    return HostRuntimeIdentity.from_runtime_labels(
        host_family="STUDIO_ONE",
        version="UNOBSERVED",
        edition="Studio One",
        os_name="UNOBSERVED",
        machine="UNOBSERVED",
        translation_mode="UNKNOWN",
        display_name="Studio One",
    )


@dataclass(frozen=True)
class StudioOneClockSnapshot:
    bridge_session_id: str
    runtime: HostRuntimeIdentity
    observed_at_epoch_seconds: int
    tempo_bpm: float
    transport_state: str

    def __post_init__(self) -> None:
        session = _text(self.bridge_session_id, "bridge_session_id")
        if not isinstance(self.runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        if self.runtime.family != "STUDIO_ONE":
            raise StudioOneMidiBridgeError(
                "Studio One clock snapshot requires STUDIO_ONE runtime"
            )
        try:
            observed = int(self.observed_at_epoch_seconds)
        except (TypeError, ValueError) as exc:
            raise StudioOneMidiBridgeError(
                "observed_at_epoch_seconds must be an integer"
            ) from exc
        if observed < 0:
            raise StudioOneMidiBridgeError(
                "observed_at_epoch_seconds must be non-negative"
            )
        tempo = _finite(self.tempo_bpm, "tempo_bpm")
        if not 20.0 <= tempo <= 400.0:
            raise StudioOneMidiBridgeError(
                "tempo_bpm must be between 20 and 400"
            )
        state = _text(self.transport_state, "transport_state").upper()
        if state not in _TRANSPORT_STATES:
            raise StudioOneMidiBridgeError(
                f"unsupported transport_state: {state}"
            )
        object.__setattr__(self, "bridge_session_id", session)
        object.__setattr__(self, "observed_at_epoch_seconds", observed)
        object.__setattr__(self, "tempo_bpm", tempo)
        object.__setattr__(self, "transport_state", state)

    @property
    def identity_scope(self) -> str:
        return IDENTITY_SCOPE

    @property
    def location_ref(self) -> str:
        return f"studio-one-midi-clock-session:{self.bridge_session_id}"

    @property
    def is_playing(self) -> bool | None:
        if self.transport_state == "UNKNOWN":
            return None
        return self.transport_state == "PLAYING"

    def capabilities(self) -> tuple[CapabilityFactInput, ...]:
        base = dict(
            route_id="n0te-studio-one-midi-clock",
            route_kind="HOST_NATIVE",
            display_name="N0TE Studio One MIDI Clock Monitor",
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
                evidence_ref=f"studio-one:midi-clock:{session}:tempo",
                **base,
            )
        ]
        if self.is_playing is not None:
            facts.append(
                CapabilityFactInput(
                    capability="transport.read",
                    evidence_ref=f"studio-one:midi-clock:{session}:transport",
                    **base,
                )
            )
        return tuple(facts)

    def focus_dimensions(self) -> tuple[object, ...]:
        return ()

    def shadow(self) -> ShadowObservationInput:
        session = self.bridge_session_id
        events = [
            ShadowEventInput(
                object_kind="TEMPO",
                object_ref="tempo:main",
                field="bpm",
                action="SET",
                value=self.tempo_bpm,
                evidence_ref=f"studio-one:midi-clock:{session}:tempo",
            )
        ]
        playing = self.is_playing
        if playing is not None:
            events.append(
                ShadowEventInput(
                    object_kind="TRANSPORT",
                    object_ref="transport:main",
                    field="is_playing",
                    action="SET",
                    value=playing,
                    evidence_ref=f"studio-one:midi-clock:{session}:transport",
                )
            )
        return ShadowObservationInput(
            coverage="FULL",
            actor="EXTERNAL",
            evidence_ref=f"studio-one:midi-clock:{session}",
            events=tuple(events),
        )


class StudioOneMidiClockTracker:
    """Derive bounded tempo and transport truth from standard MIDI realtime bytes."""

    def __init__(self) -> None:
        self._intervals: deque[float] = deque(maxlen=_CLOCK_WINDOW)
        self._last_tick: float | None = None
        self._tempo_bpm: float | None = None
        self._transport_state = "UNKNOWN"

    @property
    def tempo_bpm(self) -> float | None:
        return self._tempo_bpm

    @property
    def transport_state(self) -> str:
        return self._transport_state

    def reset_timing(self) -> None:
        self._intervals.clear()
        self._last_tick = None
        self._tempo_bpm = None

    def _estimate_tempo(self) -> float | None:
        if len(self._intervals) < _MIN_CLOCK_INTERVALS:
            return None
        values = list(self._intervals)
        median = statistics.median(values)
        if not _MIN_TICK_SECONDS <= median <= _MAX_TICK_SECONDS:
            return None
        close = [
            value
            for value in values
            if abs(value - median) <= median * 0.35
        ]
        if len(close) < max(_MIN_CLOCK_INTERVALS, int(len(values) * 0.8)):
            return None
        interval = statistics.fmean(close)
        tempo = 60.0 / (24.0 * interval)
        if not 20.0 <= tempo <= 400.0 or not math.isfinite(tempo):
            return None
        return tempo

    def feed(self, message: Sequence[int], *, monotonic_seconds: float) -> bool:
        if not isinstance(message, Sequence) or isinstance(message, (str, bytes)):
            raise StudioOneMidiBridgeError("MIDI message must be a sequence of bytes")
        if not message:
            return False
        try:
            status = int(message[0])
        except (TypeError, ValueError) as exc:
            raise StudioOneMidiBridgeError("MIDI status must be an integer") from exc
        if not 0 <= status <= 255:
            raise StudioOneMidiBridgeError("MIDI status must be between 0 and 255")
        now = _finite(monotonic_seconds, "monotonic_seconds")

        changed = False
        if status == MIDI_CLOCK and len(message) == 1:
            if self._last_tick is not None:
                delta = now - self._last_tick
                if delta <= 0:
                    self.reset_timing()
                    raise StudioOneMidiBridgeError(
                        "MIDI Clock timestamps must increase monotonically"
                    )
                if not _MIN_TICK_SECONDS <= delta <= _MAX_TICK_SECONDS:
                    self.reset_timing()
                else:
                    self._intervals.append(delta)
            self._last_tick = now
            estimate = self._estimate_tempo()
            if estimate is not None:
                if self._tempo_bpm is None or abs(estimate - self._tempo_bpm) >= 0.01:
                    changed = True
                self._tempo_bpm = estimate
            return changed

        if status in {MIDI_START, MIDI_CONTINUE} and len(message) == 1:
            if self._transport_state != "PLAYING":
                self._transport_state = "PLAYING"
                changed = True
            return changed
        if status == MIDI_STOP and len(message) == 1:
            if self._transport_state != "STOPPED":
                self._transport_state = "STOPPED"
                changed = True
            return changed
        return False


class StudioOneMidiClockMonitorClient:
    """Receive Studio One's supported External Instrument MIDI Clock output."""

    def __init__(
        self,
        *,
        port_name: str = DEFAULT_STUDIO_ONE_CLOCK_PORT,
        midi_in_factory: Callable[[], object] | None = None,
        create_virtual_port: bool | None = None,
        now_epoch_seconds: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        runtime: HostRuntimeIdentity | None = None,
        session_id: str | None = None,
    ) -> None:
        self.port_name = _text(port_name, "port_name")
        if midi_in_factory is not None and not callable(midi_in_factory):
            raise TypeError("midi_in_factory must be callable or None")
        if create_virtual_port is not None and type(create_virtual_port) is not bool:
            raise TypeError("create_virtual_port must be bool or None")
        if not callable(now_epoch_seconds) or not callable(monotonic_clock):
            raise TypeError("clock functions must be callable")
        runtime = runtime or unobserved_studio_one_runtime()
        if runtime.family != "STUDIO_ONE":
            raise StudioOneMidiBridgeError("runtime must represent STUDIO_ONE")
        self.runtime = runtime
        self._midi_in_factory = midi_in_factory
        self.create_virtual_port = (
            sys.platform != "win32"
            if create_virtual_port is None
            else create_virtual_port
        )
        self._now = now_epoch_seconds
        self._monotonic = monotonic_clock
        self.bridge_session_id = (
            "studio-one-clock-" + uuid.uuid4().hex
            if session_id is None
            else _text(session_id, "session_id")
        )
        self._tracker = StudioOneMidiClockTracker()
        self._condition = threading.Condition()
        self._latest: StudioOneClockSnapshot | None = None
        self._last_error: StudioOneMidiBridgeError | None = None
        self._midi_in = None
        self._opened_port_name: str | None = None
        self._virtual_port = False

    @property
    def opened_port_name(self) -> str | None:
        return self._opened_port_name

    @property
    def virtual_port(self) -> bool:
        return self._virtual_port

    def _default_midi_in(self):
        try:
            import rtmidi
        except ImportError as exc:
            raise StudioOneMidiBridgeError(
                "python-rtmidi is required for Studio One MIDI Clock observation"
            ) from exc
        try:
            return rtmidi.MidiIn(name="N0TE Studio One Monitor")
        except Exception as exc:
            raise StudioOneMidiBridgeError(
                "cannot initialize the system MIDI input client"
            ) from exc

    def open(self) -> str:
        if self._midi_in is not None:
            raise StudioOneMidiBridgeError(
                "Studio One MIDI Clock monitor is already open"
            )
        midi_in = (
            self._midi_in_factory()
            if self._midi_in_factory is not None
            else self._default_midi_in()
        )
        try:
            ports = tuple(str(value) for value in midi_in.get_ports())
        except Exception as exc:
            raise StudioOneMidiBridgeError(
                "cannot enumerate system MIDI input ports"
            ) from exc
        needle = self.port_name.casefold()
        matches = [
            (index, name)
            for index, name in enumerate(ports)
            if needle in name.casefold()
        ]
        try:
            if len(matches) == 1:
                index, resolved = matches[0]
                midi_in.open_port(index, name="N0TE Studio One Monitor")
                self._virtual_port = False
            elif len(matches) > 1:
                raise StudioOneMidiBridgeError(
                    f"Studio One MIDI destination is ambiguous: {self.port_name}"
                )
            elif self.create_virtual_port:
                midi_in.open_virtual_port(self.port_name)
                resolved = self.port_name
                self._virtual_port = True
            else:
                raise StudioOneMidiBridgeError(
                    f"Studio One MIDI destination is unavailable: {self.port_name}; "
                    "create a local loopback MIDI port with that name or pass --port-name"
                )
            ignore_types = getattr(midi_in, "ignore_types", None)
            if not callable(ignore_types):
                raise StudioOneMidiBridgeError(
                    "MIDI input backend cannot enable timing messages"
                )
            ignore_types(sysex=True, timing=False, active_sense=True)
            midi_in.set_callback(self._on_midi)
        except Exception as exc:
            try:
                midi_in.close_port()
            except Exception:
                pass
            if isinstance(exc, StudioOneMidiBridgeError):
                raise
            raise StudioOneMidiBridgeError(
                "cannot open Studio One MIDI Clock input"
            ) from exc
        self._midi_in = midi_in
        self._opened_port_name = resolved
        return resolved

    def close(self) -> None:
        midi_in = self._midi_in
        self._midi_in = None
        self._opened_port_name = None
        self._virtual_port = False
        if midi_in is None:
            return
        try:
            midi_in.close_port()
        except Exception as exc:
            raise StudioOneMidiBridgeError(
                "cannot close Studio One MIDI Clock input"
            ) from exc

    def _publish(self) -> None:
        tempo = self._tracker.tempo_bpm
        if tempo is None:
            return
        snapshot = StudioOneClockSnapshot(
            bridge_session_id=self.bridge_session_id,
            runtime=self.runtime,
            observed_at_epoch_seconds=int(_finite(self._now(), "current time")),
            tempo_bpm=tempo,
            transport_state=self._tracker.transport_state,
        )
        with self._condition:
            self._latest = snapshot
            self._last_error = None
            self._condition.notify_all()

    def _on_midi(self, event, data=None) -> None:  # noqa: ARG002
        try:
            message, _delta = event
            changed = self._tracker.feed(
                message,
                monotonic_seconds=self._monotonic(),
            )
            if int(message[0]) == MIDI_CLOCK or changed:
                self._publish()
        except StudioOneMidiBridgeError as exc:
            with self._condition:
                self._last_error = exc
                self._condition.notify_all()
        except Exception:
            with self._condition:
                self._last_error = StudioOneMidiBridgeError(
                    "Studio One MIDI callback received an invalid event"
                )
                self._condition.notify_all()

    def fetch_snapshot(
        self,
        *,
        timeout_seconds: float = 2.0,
        max_age_seconds: float = 5.0,
    ) -> StudioOneClockSnapshot:
        if self._midi_in is None:
            raise StudioOneMidiBridgeError(
                "Studio One MIDI Clock monitor is not open"
            )
        timeout = _finite(timeout_seconds, "timeout_seconds")
        max_age = _finite(max_age_seconds, "max_age_seconds")
        if timeout <= 0 or timeout > 30:
            raise StudioOneMidiBridgeError(
                "timeout_seconds must be between 0 and 30"
            )
        if max_age <= 0 or max_age > 60:
            raise StudioOneMidiBridgeError(
                "max_age_seconds must be between 0 and 60"
            )
        deadline = self._monotonic() + timeout
        with self._condition:
            while True:
                now = _finite(self._now(), "current time")
                if self._latest is not None:
                    age = now - float(self._latest.observed_at_epoch_seconds)
                    if age < -10.0:
                        raise StudioOneMidiBridgeError(
                            "Studio One timing snapshot timestamp is implausibly in the future"
                        )
                    if age <= max_age:
                        return self._latest
                if self._last_error is not None:
                    error = self._last_error
                    self._last_error = None
                    raise error
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    if self._latest is not None:
                        raise StudioOneMidiBridgeError(
                            "Studio One timing snapshot is stale"
                        )
                    raise StudioOneMidiBridgeError(
                        "no stable Studio One MIDI Clock was received before timeout"
                    )
                self._condition.wait(timeout=remaining)

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
            display_name="Studio One • MIDI Clock monitor (session-only identity)",
            provider_id=provider_id,
            capabilities=snapshot.capabilities(),
            focus_dimensions=(),
            focus_evidence_ref=(
                f"studio-one:midi-clock:{snapshot.bridge_session_id}:focus-unobserved"
            ),
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
