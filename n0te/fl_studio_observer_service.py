from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .coordinator_gateway import (
    DEFAULT_COORDINATOR_MCP_ENDPOINT,
    CoordinatorReferenceClient,
)
from .fl_studio_continuous_observer import (
    FLStudioContinuousObservationCycle,
    FLStudioContinuousObserver,
)
from .fl_studio_host_bridge import FLStudioHostBridgeError, FLStudioSnapshotFileClient
from .host_session_handshake import HostSessionReferenceWorkflow
from .memory import HeadquartersMemory

SERVICE_STATES = {"READY", "WAITING_FOR_BRIDGE", "OBSERVING", "STOPPED"}
STATUS_SCHEMA = "n0te.fl-studio-observer-status/v1"


class FLStudioObserverServiceError(RuntimeError):
    """The runtime-owned FL Studio observer service cannot advance safely."""


class _ObserverLike(Protocol):
    interval_seconds: float

    async def poll_once(self, *, force_discovery: bool = False): ...


CycleCallback = Callable[
    [FLStudioContinuousObservationCycle],
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
        raise FLStudioObserverServiceError(
            "observer status contains non-JSON runtime state"
        ) from exc
    return json.loads(encoded)


class FLStudioObserverService:
    """Supervise FL snapshot observation inside one owned N0TE runtime."""

    def __init__(
        self,
        headquarters: HeadquartersMemory,
        observer: _ObserverLike,
        *,
        reconnect_interval_seconds: float = 2.0,
        runtime_guard: Callable[[], bool] | None = None,
    ) -> None:
        if not isinstance(headquarters, HeadquartersMemory):
            raise TypeError("headquarters must be HeadquartersMemory")
        if not callable(getattr(observer, "poll_once", None)):
            raise TypeError("observer must provide poll_once")
        try:
            interval = float(reconnect_interval_seconds)
        except (TypeError, ValueError) as exc:
            raise FLStudioObserverServiceError(
                "reconnect_interval_seconds must be numeric"
            ) from exc
        if not 0.05 <= interval <= 60.0:
            raise FLStudioObserverServiceError(
                "reconnect_interval_seconds must be between 0.05 and 60"
            )
        if runtime_guard is not None and not callable(runtime_guard):
            raise TypeError("runtime_guard must be callable or None")
        self.headquarters = headquarters
        self.observer = observer
        self.reconnect_interval_seconds = interval
        self._runtime_guard = runtime_guard
        self._state = "READY"
        self._latest_cycle: FLStudioContinuousObservationCycle | None = None
        self._bridge_failure_count = 0
        self._last_bridge_error_class: str | None = None

    @classmethod
    def from_runtime(
        cls,
        runtime,
        *,
        provider_id: str,
        snapshot_path: str | Path,
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
    ) -> "FLStudioObserverService":
        from .app_runtime import ApplicationRuntime

        if not isinstance(runtime, ApplicationRuntime):
            raise TypeError("runtime must be ApplicationRuntime")
        if runtime.state != "RUNNING":
            raise FLStudioObserverServiceError(
                "FL Studio observation requires a RUNNING ApplicationRuntime"
            )
        headquarters = runtime.headquarters
        if headquarters.store.active_song() is None:
            raise FLStudioObserverServiceError(
                "FL Studio observation requires an explicitly active Song"
            )

        reference_client = CoordinatorReferenceClient(coordinator_endpoint)
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        snapshot_client = FLStudioSnapshotFileClient(snapshot_path)
        observer = FLStudioContinuousObserver(
            snapshot_client,
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
            reconnect_interval_seconds=reconnect_interval_seconds,
            runtime_guard=lambda: runtime.state == "RUNNING",
        )

    @property
    def state(self) -> str:
        return self._state

    @property
    def latest_cycle(self) -> FLStudioContinuousObservationCycle | None:
        return self._latest_cycle

    @property
    def bridge_failure_count(self) -> int:
        return self._bridge_failure_count

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

    def status_projection(self) -> dict[str, object]:
        cycle = self._latest_cycle
        payload: dict[str, object] = {
            "schema": STATUS_SCHEMA,
            "service_state": self._state,
            "connected": cycle is not None and self._state == "OBSERVING",
            "bridge_failure_count": self._bridge_failure_count,
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
        mixer_track = snapshot.selected_mixer_track
        channel = snapshot.selected_channel
        window = snapshot.active_window
        payload["session"] = {
            "song_id": binding.song_id,
            "workspace_id": binding.workspace_id,
            "host_family": snapshot.runtime.family,
            "host_version": snapshot.runtime.version,
            "host_display_name": snapshot.runtime.display_name,
            "project_title": snapshot.project_title,
            "tempo_bpm": snapshot.tempo_bpm,
            "is_playing": snapshot.is_playing,
            "song_position": snapshot.song_position,
            "loop_mode": snapshot.loop_mode,
            "selected_mixer_track": (
                None
                if mixer_track is None
                else {"index": mixer_track.index, "name": mixer_track.name}
            ),
            "selected_channel": (
                None
                if channel is None
                else {"index": channel.index, "name": channel.name}
            ),
            "active_window": (
                None
                if window is None
                else {
                    "form_id": window.form_id,
                    "caption": window.caption,
                    "plugin_name": window.plugin_name,
                }
            ),
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

    async def refresh_references(self) -> FLStudioContinuousObservationCycle:
        if not self._runtime_is_owned():
            raise FLStudioObserverServiceError(
                "cannot refresh references after the owning ApplicationRuntime stopped"
            )
        try:
            cycle = await self.observer.poll_once(force_discovery=True)
        except FLStudioHostBridgeError as exc:
            self._state = "WAITING_FOR_BRIDGE"
            self._bridge_failure_count += 1
            self._last_bridge_error_class = type(exc).__name__
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
            raise FLStudioObserverServiceError("FL Studio observer service is already running")
        self._state = "READY"
        try:
            while not stop_event.is_set():
                if not self._runtime_is_owned():
                    return
                try:
                    cycle = await self.observer.poll_once()
                except FLStudioHostBridgeError as exc:
                    self._state = "WAITING_FOR_BRIDGE"
                    self._bridge_failure_count += 1
                    self._last_bridge_error_class = type(exc).__name__
                    await self._sleep_or_stop(stop_event, self.reconnect_interval_seconds)
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
            self._state = "STOPPED"
