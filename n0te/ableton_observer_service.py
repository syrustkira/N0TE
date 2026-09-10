from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable, Protocol

from .ableton_continuous_observer import (
    AbletonContinuousObservationCycle,
    AbletonContinuousObserver,
)
from .ableton_host_bridge import (
    DEFAULT_ABLETON_BRIDGE_ENDPOINT,
    AbletonHostBridgeError,
    AbletonRemoteScriptClient,
)
from .coordinator_gateway import (
    DEFAULT_COORDINATOR_MCP_ENDPOINT,
    CoordinatorReferenceClient,
)
from .host_session_handshake import HostSessionReferenceWorkflow
from .memory import HeadquartersMemory

SERVICE_STATES = {"READY", "WAITING_FOR_BRIDGE", "OBSERVING", "STOPPED"}


class AbletonObserverServiceError(RuntimeError):
    """The runtime-owned Ableton observer service cannot be composed or advanced safely."""


class _ObserverLike(Protocol):
    interval_seconds: float

    async def poll_once(self, *, force_discovery: bool = False): ...


CycleCallback = Callable[[AbletonContinuousObservationCycle], object | Awaitable[object]]


class AbletonObserverService:
    """Supervise continuous Ableton observation inside one owned N0TE runtime.

    The service never opens Headquarters itself. Production construction goes through
    from_runtime(), which reuses the exact Headquarters already protected by the
    ApplicationRuntime lease. The service does not create a background task by itself;
    a desktop/consumer event loop explicitly awaits run().
    """

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
            raise AbletonObserverServiceError(
                "reconnect_interval_seconds must be numeric"
            ) from exc
        if not 0.05 <= interval <= 60.0:
            raise AbletonObserverServiceError(
                "reconnect_interval_seconds must be between 0.05 and 60"
            )
        if runtime_guard is not None and not callable(runtime_guard):
            raise TypeError("runtime_guard must be callable or None")
        self.headquarters = headquarters
        self.observer = observer
        self.reconnect_interval_seconds = interval
        self._runtime_guard = runtime_guard
        self._state = "READY"
        self._latest_cycle: AbletonContinuousObservationCycle | None = None
        self._bridge_failure_count = 0
        self._last_bridge_error_class: str | None = None

    @classmethod
    def from_runtime(
        cls,
        runtime,
        *,
        provider_id: str,
        bridge_endpoint: str = DEFAULT_ABLETON_BRIDGE_ENDPOINT,
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
    ) -> "AbletonObserverService":
        # Local import keeps ApplicationRuntime independent of this service and
        # preserves its documented no-daemon/no-server lifecycle contract.
        from .app_runtime import ApplicationRuntime

        if not isinstance(runtime, ApplicationRuntime):
            raise TypeError("runtime must be ApplicationRuntime")
        if runtime.state != "RUNNING":
            raise AbletonObserverServiceError(
                "Ableton observation requires a RUNNING ApplicationRuntime"
            )
        headquarters = runtime.headquarters
        if headquarters.store.active_song() is None:
            raise AbletonObserverServiceError(
                "Ableton observation requires an explicitly active Song"
            )

        reference_client = CoordinatorReferenceClient(coordinator_endpoint)
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        observer = AbletonContinuousObserver(
            AbletonRemoteScriptClient(bridge_endpoint),
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
    def latest_cycle(self) -> AbletonContinuousObservationCycle | None:
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
            raise AbletonObserverServiceError("Ableton observer service is already running")
        self._state = "READY"

        try:
            while not stop_event.is_set():
                if not self._runtime_is_owned():
                    return
                try:
                    cycle = await self.observer.poll_once()
                except AbletonHostBridgeError as exc:
                    self._state = "WAITING_FOR_BRIDGE"
                    self._bridge_failure_count += 1
                    self._last_bridge_error_class = type(exc).__name__
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
            self._state = "STOPPED"
