from __future__ import annotations

import asyncio
import inspect
import json
from typing import Awaitable, Callable, Protocol

from .coordinator_gateway import DEFAULT_COORDINATOR_MCP_ENDPOINT, CoordinatorReferenceClient
from .host_session_handshake import HostSessionReferenceWorkflow
from .memory import HeadquartersMemory
from .studio_one_continuous_observer import (
    StudioOneContinuousObservationCycle,
    StudioOneContinuousObserver,
)
from .studio_one_midi_bridge import (
    DEFAULT_STUDIO_ONE_CLOCK_PORT,
    IDENTITY_SCOPE,
    StudioOneMidiBridgeError,
    StudioOneMidiClockMonitorClient,
)

SERVICE_STATES = {
    "READY",
    "WAITING_FOR_MIDI_PORT",
    "WAITING_FOR_CLOCK",
    "OBSERVING",
    "STOPPED",
}
STATUS_SCHEMA = "n0te.studio-one-observer-status/v1"


class StudioOneObserverServiceError(RuntimeError):
    """The runtime-owned Studio One observer service cannot advance safely."""


class _ObserverLike(Protocol):
    interval_seconds: float

    async def poll_once(self, *, force_discovery: bool = False): ...


class _MidiClientLike(Protocol):
    opened_port_name: str | None
    virtual_port: bool

    def open(self) -> str: ...
    def close(self) -> None: ...


CycleCallback = Callable[
    [StudioOneContinuousObservationCycle],
    object | Awaitable[object],
]


def _json_clone(value: object) -> object:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StudioOneObserverServiceError(
            "observer status contains non-JSON runtime state"
        ) from exc
    return json.loads(encoded)


class StudioOneObserverService:
    """Supervise Studio One MIDI Clock observation inside one owned N0TE runtime."""

    def __init__(
        self,
        headquarters: HeadquartersMemory,
        observer: _ObserverLike,
        midi_client: _MidiClientLike,
        *,
        reconnect_interval_seconds: float = 2.0,
        runtime_guard: Callable[[], bool] | None = None,
    ) -> None:
        if not isinstance(headquarters, HeadquartersMemory):
            raise TypeError("headquarters must be HeadquartersMemory")
        if not callable(getattr(observer, "poll_once", None)):
            raise TypeError("observer must provide poll_once")
        if not callable(getattr(midi_client, "open", None)) or not callable(
            getattr(midi_client, "close", None)
        ):
            raise TypeError("midi_client must provide open and close")
        try:
            interval = float(reconnect_interval_seconds)
        except (TypeError, ValueError) as exc:
            raise StudioOneObserverServiceError(
                "reconnect_interval_seconds must be numeric"
            ) from exc
        if not 0.05 <= interval <= 60.0:
            raise StudioOneObserverServiceError(
                "reconnect_interval_seconds must be between 0.05 and 60"
            )
        if runtime_guard is not None and not callable(runtime_guard):
            raise TypeError("runtime_guard must be callable or None")
        self.headquarters = headquarters
        self.observer = observer
        self.midi_client = midi_client
        self.reconnect_interval_seconds = interval
        self._runtime_guard = runtime_guard
        self._state = "READY"
        self._latest_cycle: StudioOneContinuousObservationCycle | None = None
        self._port_failure_count = 0
        self._clock_failure_count = 0
        self._last_bridge_error_class: str | None = None

    @classmethod
    def from_runtime(
        cls,
        runtime,
        *,
        provider_id: str,
        midi_port_name: str = DEFAULT_STUDIO_ONE_CLOCK_PORT,
        create_virtual_port: bool | None = None,
        coordinator_endpoint: str = DEFAULT_COORDINATOR_MCP_ENDPOINT,
        poll_interval_seconds: float = 1.0,
        reconnect_interval_seconds: float = 2.0,
        discovery_retry_seconds: float = 30.0,
        comparison_dimensions: tuple[str, ...] = ("tempo",),
        semantic_tags: tuple[str, ...] = (),
        required_features: tuple[str, ...] = ("tempo_bpm",),
        desired_tags: tuple[str, ...] = (),
        discovery_limit: int = 12,
        result_limit: int = 3,
    ) -> "StudioOneObserverService":
        from .app_runtime import ApplicationRuntime

        if not isinstance(runtime, ApplicationRuntime):
            raise TypeError("runtime must be ApplicationRuntime")
        if runtime.state != "RUNNING":
            raise StudioOneObserverServiceError(
                "Studio One observation requires a RUNNING ApplicationRuntime"
            )
        headquarters = runtime.headquarters
        if headquarters.store.active_song() is None:
            raise StudioOneObserverServiceError(
                "Studio One observation requires an explicitly active Song"
            )
        reference_client = CoordinatorReferenceClient(coordinator_endpoint)
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        midi_client = StudioOneMidiClockMonitorClient(
            port_name=midi_port_name,
            create_virtual_port=create_virtual_port,
        )
        observer = StudioOneContinuousObserver(
            midi_client,
            workflow,
            provider_id=provider_id,
            interval_seconds=poll_interval_seconds,
            discovery_retry_seconds=discovery_retry_seconds,
            comparison_dimensions=comparison_dimensions,
            semantic_tags=semantic_tags,
            required_features=required_features,
            desired_tags=desired_tags,
            discovery_limit=discovery_limit,
            result_limit=result_limit,
        )
        return cls(
            headquarters,
            observer,
            midi_client,
            reconnect_interval_seconds=reconnect_interval_seconds,
            runtime_guard=lambda: runtime.state == "RUNNING",
        )

    @property
    def state(self) -> str:
        return self._state

    @property
    def latest_cycle(self) -> StudioOneContinuousObservationCycle | None:
        return self._latest_cycle

    @property
    def port_failure_count(self) -> int:
        return self._port_failure_count

    @property
    def clock_failure_count(self) -> int:
        return self._clock_failure_count

    @property
    def last_bridge_error_class(self) -> str | None:
        return self._last_bridge_error_class

    def _runtime_is_owned(self) -> bool:
        if self._runtime_guard is None:
            return True
        try:
            return self._runtime_guard() is True
        except Exception:
            return False

    def _ensure_bridge_open(self) -> str:
        current = getattr(self.midi_client, "opened_port_name", None)
        return str(current) if current else str(self.midi_client.open())

    def _drop_bridge(self) -> None:
        try:
            self.midi_client.close()
        except Exception:
            pass

    def _record_port_failure(self, exc: Exception) -> None:
        self._state = "WAITING_FOR_MIDI_PORT"
        self._port_failure_count += 1
        self._last_bridge_error_class = type(exc).__name__
        self._drop_bridge()

    def _record_clock_failure(self, exc: Exception) -> None:
        self._state = "WAITING_FOR_CLOCK"
        self._clock_failure_count += 1
        self._last_bridge_error_class = type(exc).__name__

    def status_projection(self) -> dict[str, object]:
        cycle = self._latest_cycle
        payload: dict[str, object] = {
            "schema": STATUS_SCHEMA,
            "service_state": self._state,
            "connected": cycle is not None and self._state == "OBSERVING",
            "midi_port_name": getattr(self.midi_client, "opened_port_name", None),
            "virtual_port": bool(getattr(self.midi_client, "virtual_port", False)),
            "port_failure_count": self._port_failure_count,
            "clock_failure_count": self._clock_failure_count,
            "last_bridge_error_class": self._last_bridge_error_class,
            "read_only": True,
            "action_authority_granted": False,
            "session": None,
            "reference_discovery": None,
        }
        if cycle is None:
            return _json_clone(payload)  # type: ignore[return-value]
        snapshot = cycle.snapshot
        binding = cycle.observation.binding
        payload["session"] = {
            "song_id": binding.song_id,
            "workspace_id": binding.workspace_id,
            "host_family": snapshot.runtime.family,
            "host_version": snapshot.runtime.version,
            "project_identity_scope": IDENTITY_SCOPE,
            "project_name": "UNOBSERVED",
            "project_path": "UNOBSERVED",
            "focus": "UNOBSERVED",
            "tempo_bpm": snapshot.tempo_bpm,
            "transport_state": snapshot.transport_state,
            "is_playing": snapshot.is_playing,
            "observation_committed": cycle.observation_committed,
        }
        references = cycle.references
        payload["reference_discovery"] = {
            "provider_id": None if references is None else references.get("provider_id"),
            "performed": cycle.discovery_performed,
            "deferred": cycle.discovery_deferred,
            "reason": cycle.discovery_reason,
            "error_class": cycle.discovery_error_class,
            "primary": None if references is None else references.get("primary"),
            "ranked": [] if references is None else references.get("ranked", []),
        }
        return _json_clone(payload)  # type: ignore[return-value]

    async def refresh_references(self) -> StudioOneContinuousObservationCycle:
        if not self._runtime_is_owned():
            raise StudioOneObserverServiceError(
                "cannot refresh references after the owning ApplicationRuntime stopped"
            )
        try:
            await asyncio.to_thread(self._ensure_bridge_open)
        except StudioOneMidiBridgeError as exc:
            self._record_port_failure(exc)
            raise
        try:
            cycle = await self.observer.poll_once(force_discovery=True)
        except StudioOneMidiBridgeError as exc:
            self._record_clock_failure(exc)
            raise
        self._latest_cycle = cycle
        self._state = "OBSERVING"
        return cycle

    @staticmethod
    async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        on_cycle: CycleCallback | None = None,
    ) -> None:
        if not isinstance(stop_event, asyncio.Event):
            raise TypeError("stop_event must be asyncio.Event")
        if on_cycle is not None and not callable(on_cycle):
            raise TypeError("on_cycle must be callable or None")
        if self._state not in {"READY", "STOPPED"}:
            raise StudioOneObserverServiceError(
                "Studio One observer service is already running"
            )
        self._state = "READY"
        try:
            while not stop_event.is_set():
                if not self._runtime_is_owned():
                    return
                try:
                    await asyncio.to_thread(self._ensure_bridge_open)
                except StudioOneMidiBridgeError as exc:
                    self._record_port_failure(exc)
                    await self._sleep_or_stop(
                        stop_event,
                        self.reconnect_interval_seconds,
                    )
                    continue
                try:
                    cycle = await self.observer.poll_once()
                except StudioOneMidiBridgeError as exc:
                    self._record_clock_failure(exc)
                    await self._sleep_or_stop(
                        stop_event,
                        self.reconnect_interval_seconds,
                    )
                    continue
                self._latest_cycle = cycle
                self._state = "OBSERVING"
                if on_cycle is not None:
                    result = on_cycle(cycle)
                    if inspect.isawaitable(result):
                        await result
                if stop_event.is_set():
                    break
                await self._sleep_or_stop(
                    stop_event,
                    float(self.observer.interval_seconds),
                )
        finally:
            self._drop_bridge()
            self._state = "STOPPED"
