from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .host_observation import CapabilityFactInput, ShadowObservationInput
from .host_session_handshake import (
    HostSessionReferenceWorkflow,
    HostSessionReferenceWorkflowResult,
)
from .hosts import HostRuntimeIdentity
from .shadow import ShadowEventInput

LOGIC_PROTOCOL_VERSION = 1
LOGIC_MIDI_CHANNEL = 16
LOGIC_CC_STATUS = 0xB0 | (LOGIC_MIDI_CHANNEL - 1)
LOGIC_VIRTUAL_OUT_NAME = "Logic Pro Virtual Out"

CC_SESSION_0 = 102
CC_SESSION_1 = 103
CC_SESSION_2 = 104
CC_SESSION_3 = 105
CC_TEMPO_MSB = 106
CC_TEMPO_LSB = 107
CC_FLAGS = 108
CC_METER_NUMERATOR = 109
CC_METER_DENOMINATOR = 110
CC_END = 116
CC_CHECKSUM = 117
CC_VERSION = 118
CC_START = 119

_FRAME_ORDER = (
    CC_VERSION,
    CC_SESSION_0,
    CC_SESSION_1,
    CC_SESSION_2,
    CC_SESSION_3,
    CC_TEMPO_MSB,
    CC_TEMPO_LSB,
    CC_FLAGS,
    CC_METER_NUMERATOR,
    CC_METER_DENOMINATOR,
    CC_CHECKSUM,
    CC_END,
)


class LogicMidiBridgeError(RuntimeError):
    """Logic Pro timing evidence could not be decoded or received safely."""


def _integer(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise LogicMidiBridgeError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise LogicMidiBridgeError(f"{field} must be an integer") from exc
    if number < minimum or number > maximum:
        raise LogicMidiBridgeError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return number


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise LogicMidiBridgeError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LogicMidiBridgeError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise LogicMidiBridgeError(f"{field} must be finite")
    return number


def _checksum(values: Sequence[int]) -> int:
    result = 0
    for value in values:
        result ^= _integer(value, "checksum byte", minimum=0, maximum=127)
    return result & 0x7F


@dataclass(frozen=True)
class LogicTimingFrame:
    sequence: int
    session_token: int
    tempo_bpm: float
    is_playing: bool
    is_cycling: bool
    meter_numerator: int
    meter_denominator: int
    protocol_version: int = LOGIC_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        sequence = _integer(self.sequence, "sequence", minimum=0, maximum=127)
        session_token = _integer(
            self.session_token,
            "session_token",
            minimum=0,
            maximum=0x0FFFFFFF,
        )
        protocol = _integer(
            self.protocol_version,
            "protocol_version",
            minimum=1,
            maximum=127,
        )
        if protocol != LOGIC_PROTOCOL_VERSION:
            raise LogicMidiBridgeError(
                f"unsupported Logic timing protocol version: {protocol}"
            )
        tempo = _finite(self.tempo_bpm, "tempo_bpm")
        if not 20.0 <= tempo <= 400.0:
            raise LogicMidiBridgeError("tempo_bpm must be between 20 and 400")
        if type(self.is_playing) is not bool or type(self.is_cycling) is not bool:
            raise LogicMidiBridgeError("transport flags must be bool")
        numerator = _integer(
            self.meter_numerator,
            "meter_numerator",
            minimum=1,
            maximum=127,
        )
        denominator = _integer(
            self.meter_denominator,
            "meter_denominator",
            minimum=1,
            maximum=127,
        )
        if denominator & (denominator - 1):
            raise LogicMidiBridgeError("meter_denominator must be a power of two")
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "session_token", session_token)
        object.__setattr__(self, "protocol_version", protocol)
        object.__setattr__(self, "tempo_bpm", tempo)
        object.__setattr__(self, "meter_numerator", numerator)
        object.__setattr__(self, "meter_denominator", denominator)

    @property
    def bridge_session_id(self) -> str:
        return f"logic-scripter-{self.session_token:07x}"


class LogicMidiFrameAssembler:
    """Reassemble one strict N0TE frame from Logic's channel-16 CC stream."""

    def __init__(self) -> None:
        self._sequence: int | None = None
        self._values: dict[int, int] = {}
        self._next_index = 0

    def _reset(self) -> None:
        self._sequence = None
        self._values = {}
        self._next_index = 0

    def feed(self, message: Sequence[int]) -> LogicTimingFrame | None:
        if not isinstance(message, Sequence) or isinstance(message, (str, bytes)):
            raise LogicMidiBridgeError("MIDI message must be a sequence of bytes")
        if len(message) != 3:
            return None
        status = _integer(message[0], "MIDI status", minimum=0, maximum=255)
        controller = _integer(message[1], "MIDI controller", minimum=0, maximum=127)
        value = _integer(message[2], "MIDI value", minimum=0, maximum=127)
        if status != LOGIC_CC_STATUS:
            return None

        if controller == CC_START:
            self._sequence = value
            self._values = {}
            self._next_index = 0
            return None

        if self._sequence is None:
            return None

        expected = _FRAME_ORDER[self._next_index]
        if controller != expected:
            self._reset()
            raise LogicMidiBridgeError(
                "Logic timing frame is incomplete or out of order"
            )

        if controller == CC_END:
            sequence = self._sequence
            if value != sequence:
                self._reset()
                raise LogicMidiBridgeError(
                    "Logic timing frame end sequence does not match start"
                )
            try:
                frame = self._finish(sequence)
            finally:
                self._reset()
            return frame

        self._values[controller] = value
        self._next_index += 1
        return None

    def _finish(self, sequence: int) -> LogicTimingFrame:
        required = set(_FRAME_ORDER[:-1])
        if set(self._values) != required:
            raise LogicMidiBridgeError("Logic timing frame is incomplete")
        version = self._values[CC_VERSION]
        if version != LOGIC_PROTOCOL_VERSION:
            raise LogicMidiBridgeError(
                f"unsupported Logic timing protocol version: {version}"
            )
        protected = [
            sequence,
            version,
            self._values[CC_SESSION_0],
            self._values[CC_SESSION_1],
            self._values[CC_SESSION_2],
            self._values[CC_SESSION_3],
            self._values[CC_TEMPO_MSB],
            self._values[CC_TEMPO_LSB],
            self._values[CC_FLAGS],
            self._values[CC_METER_NUMERATOR],
            self._values[CC_METER_DENOMINATOR],
        ]
        if self._values[CC_CHECKSUM] != _checksum(protected):
            raise LogicMidiBridgeError("Logic timing frame checksum failed")

        session = (
            (self._values[CC_SESSION_0] << 21)
            | (self._values[CC_SESSION_1] << 14)
            | (self._values[CC_SESSION_2] << 7)
            | self._values[CC_SESSION_3]
        )
        tempo10 = (
            (self._values[CC_TEMPO_MSB] << 7)
            | self._values[CC_TEMPO_LSB]
        )
        flags = self._values[CC_FLAGS]
        if flags & ~0x03:
            raise LogicMidiBridgeError("Logic timing frame contains unsupported flags")
        return LogicTimingFrame(
            sequence=sequence,
            session_token=session,
            tempo_bpm=tempo10 / 10.0,
            is_playing=bool(flags & 0x01),
            is_cycling=bool(flags & 0x02),
            meter_numerator=self._values[CC_METER_NUMERATOR],
            meter_denominator=self._values[CC_METER_DENOMINATOR],
            protocol_version=version,
        )


@dataclass(frozen=True)
class LogicTimingSnapshot:
    frame: LogicTimingFrame
    runtime: HostRuntimeIdentity
    observed_at_epoch_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.frame, LogicTimingFrame):
            raise TypeError("frame must be LogicTimingFrame")
        if not isinstance(self.runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        if self.runtime.family != "LOGIC_PRO":
            raise LogicMidiBridgeError("Logic timing snapshot requires LOGIC_PRO runtime")
        observed = _integer(
            self.observed_at_epoch_seconds,
            "observed_at_epoch_seconds",
            minimum=0,
            maximum=2**63 - 1,
        )
        object.__setattr__(self, "observed_at_epoch_seconds", observed)

    @property
    def bridge_session_id(self) -> str:
        return self.frame.bridge_session_id

    @property
    def location_ref(self) -> str:
        return f"logic-scripter-session:{self.bridge_session_id}"

    @property
    def tempo_bpm(self) -> float:
        return self.frame.tempo_bpm

    @property
    def is_playing(self) -> bool:
        return self.frame.is_playing

    @property
    def is_cycling(self) -> bool:
        return self.frame.is_cycling

    @property
    def meter_numerator(self) -> int:
        return self.frame.meter_numerator

    @property
    def meter_denominator(self) -> int:
        return self.frame.meter_denominator

    def capabilities(self) -> tuple[CapabilityFactInput, ...]:
        base = dict(
            route_id="n0te-logic-scripter-timing",
            route_kind="HOST_NATIVE",
            display_name="N0TE Logic Scripter Timing Probe",
            availability="AVAILABLE",
            evidence_kind="RUNTIME_PROBE",
            observed_at_epoch_seconds=self.observed_at_epoch_seconds,
            locality=1.0,
            privacy=1.0,
            latency=0.8,
            reversibility=1.0,
            cost_efficiency=1.0,
        )
        session = self.bridge_session_id
        return (
            CapabilityFactInput(
                capability="tempo.read",
                evidence_ref=f"logic:scripter:{session}:tempo",
                **base,
            ),
            CapabilityFactInput(
                capability="transport.read",
                evidence_ref=f"logic:scripter:{session}:transport",
                **base,
            ),
            CapabilityFactInput(
                capability="meter.read",
                evidence_ref=f"logic:scripter:{session}:meter",
                **base,
            ),
            CapabilityFactInput(
                capability="cycle.read",
                evidence_ref=f"logic:scripter:{session}:cycle",
                **base,
            ),
        )

    def focus_dimensions(self) -> tuple[object, ...]:
        return ()

    def shadow(self) -> ShadowObservationInput:
        session = self.bridge_session_id
        transport_ref = f"logic:scripter:{session}:transport"
        meter_ref = f"logic:scripter:{session}:meter"
        return ShadowObservationInput(
            coverage="FULL",
            actor="EXTERNAL",
            evidence_ref=f"logic:scripter:{session}:timing",
            events=(
                ShadowEventInput(
                    object_kind="TEMPO",
                    object_ref="tempo:main",
                    field="bpm",
                    action="SET",
                    value=self.tempo_bpm,
                    evidence_ref=f"logic:scripter:{session}:tempo",
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
                    field="is_cycling",
                    action="SET",
                    value=self.is_cycling,
                    evidence_ref=f"logic:scripter:{session}:cycle",
                ),
                ShadowEventInput(
                    object_kind="TRANSPORT",
                    object_ref="transport:main",
                    field="meter_numerator",
                    action="SET",
                    value=self.meter_numerator,
                    evidence_ref=meter_ref,
                ),
                ShadowEventInput(
                    object_kind="TRANSPORT",
                    object_ref="transport:main",
                    field="meter_denominator",
                    action="SET",
                    value=self.meter_denominator,
                    evidence_ref=meter_ref,
                ),
            ),
        )


def unobserved_logic_runtime() -> HostRuntimeIdentity:
    """Represent only the Logic identity facts this route can support honestly."""

    return HostRuntimeIdentity.from_runtime_labels(
        host_family="LOGIC_PRO",
        version="UNOBSERVED",
        edition="Logic Pro",
        os_name="Darwin",
        machine="UNOBSERVED",
        translation_mode="UNKNOWN",
        display_name="Logic Pro",
    )


class LogicMidiMonitorClient:
    """Receive N0TE timing frames from Logic Pro Virtual Out through CoreMIDI."""

    def __init__(
        self,
        *,
        port_name: str = LOGIC_VIRTUAL_OUT_NAME,
        midi_in_factory: Callable[[], object] | None = None,
        now_epoch_seconds: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        runtime: HostRuntimeIdentity | None = None,
    ) -> None:
        name = str(port_name).strip()
        if not name:
            raise LogicMidiBridgeError("port_name must not be empty")
        if midi_in_factory is not None and not callable(midi_in_factory):
            raise TypeError("midi_in_factory must be callable or None")
        if not callable(now_epoch_seconds) or not callable(monotonic_clock):
            raise TypeError("clock functions must be callable")
        runtime = runtime or unobserved_logic_runtime()
        if runtime.family != "LOGIC_PRO":
            raise LogicMidiBridgeError("runtime must represent LOGIC_PRO")
        self.port_name = name
        self._midi_in_factory = midi_in_factory
        self._now = now_epoch_seconds
        self._monotonic = monotonic_clock
        self.runtime = runtime
        self._assembler = LogicMidiFrameAssembler()
        self._condition = threading.Condition()
        self._latest: LogicTimingSnapshot | None = None
        self._last_error: LogicMidiBridgeError | None = None
        self._midi_in = None
        self._opened_port_name: str | None = None

    @property
    def opened_port_name(self) -> str | None:
        return self._opened_port_name

    def _default_midi_in(self):
        if sys.platform != "darwin":
            raise LogicMidiBridgeError(
                "Logic MIDI observation requires macOS/CoreMIDI"
            )
        try:
            import rtmidi
        except ImportError as exc:
            raise LogicMidiBridgeError(
                "python-rtmidi is required for Logic MIDI observation"
            ) from exc
        try:
            return rtmidi.MidiIn(
                rtmidi.API_MACOSX_CORE,
                name="N0TE Logic Monitor",
            )
        except Exception as exc:
            raise LogicMidiBridgeError(
                "cannot initialize the CoreMIDI input client"
            ) from exc

    def open(self) -> str:
        if self._midi_in is not None:
            raise LogicMidiBridgeError("Logic MIDI monitor is already open")
        midi_in = (
            self._midi_in_factory()
            if self._midi_in_factory is not None
            else self._default_midi_in()
        )
        try:
            ports = tuple(str(value) for value in midi_in.get_ports())
        except Exception as exc:
            raise LogicMidiBridgeError("cannot enumerate CoreMIDI input ports") from exc
        needle = self.port_name.casefold()
        matches = [
            (index, name)
            for index, name in enumerate(ports)
            if needle in name.casefold()
        ]
        if not matches:
            raise LogicMidiBridgeError(
                f"Logic MIDI destination is unavailable: {self.port_name}"
            )
        if len(matches) != 1:
            raise LogicMidiBridgeError(
                f"Logic MIDI destination is ambiguous: {self.port_name}"
            )
        index, resolved_name = matches[0]
        try:
            midi_in.open_port(index, name="N0TE Logic Monitor")
            midi_in.set_callback(self._on_midi)
        except Exception as exc:
            try:
                midi_in.close_port()
            except Exception:
                pass
            raise LogicMidiBridgeError("cannot open Logic CoreMIDI input port") from exc
        self._midi_in = midi_in
        self._opened_port_name = resolved_name
        return resolved_name

    def close(self) -> None:
        midi_in = self._midi_in
        self._midi_in = None
        self._opened_port_name = None
        if midi_in is None:
            return
        try:
            midi_in.close_port()
        except Exception as exc:
            raise LogicMidiBridgeError("cannot close Logic CoreMIDI input port") from exc

    def _on_midi(self, event, data=None) -> None:  # noqa: ARG002
        try:
            message, _delta = event
            frame = self._assembler.feed(message)
        except LogicMidiBridgeError as exc:
            with self._condition:
                self._last_error = exc
                self._condition.notify_all()
            return
        except Exception:
            with self._condition:
                self._last_error = LogicMidiBridgeError(
                    "Logic MIDI callback received an invalid event"
                )
                self._condition.notify_all()
            return
        if frame is None:
            return
        observed = int(_finite(self._now(), "current time"))
        snapshot = LogicTimingSnapshot(
            frame=frame,
            runtime=self.runtime,
            observed_at_epoch_seconds=observed,
        )
        with self._condition:
            self._latest = snapshot
            self._last_error = None
            self._condition.notify_all()

    def fetch_snapshot(
        self,
        *,
        timeout_seconds: float = 2.0,
        max_age_seconds: float = 5.0,
    ) -> LogicTimingSnapshot:
        if self._midi_in is None:
            raise LogicMidiBridgeError("Logic MIDI monitor is not open")
        timeout = _finite(timeout_seconds, "timeout_seconds")
        max_age = _finite(max_age_seconds, "max_age_seconds")
        if timeout <= 0 or timeout > 30:
            raise LogicMidiBridgeError("timeout_seconds must be between 0 and 30")
        if max_age <= 0 or max_age > 60:
            raise LogicMidiBridgeError("max_age_seconds must be between 0 and 60")
        deadline = self._monotonic() + timeout
        with self._condition:
            while True:
                now = _finite(self._now(), "current time")
                if self._latest is not None:
                    age = now - float(self._latest.observed_at_epoch_seconds)
                    if age < -10.0:
                        raise LogicMidiBridgeError(
                            "Logic timing snapshot timestamp is implausibly in the future"
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
                        raise LogicMidiBridgeError("Logic timing snapshot is stale")
                    raise LogicMidiBridgeError(
                        "no Logic timing frame was received before timeout"
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
            display_name="Logic Pro • N0TE Timing Monitor",
            provider_id=provider_id,
            capabilities=snapshot.capabilities(),
            focus_dimensions=(),
            focus_evidence_ref=(
                f"logic:scripter:{snapshot.bridge_session_id}:focus-unobserved"
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
