from __future__ import annotations

import asyncio
import inspect
import json
from typing import Awaitable, Callable, Protocol
from urllib.request import Request, urlopen

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
from .host_runtime import ActiveToolProjectionError, project_active_tools
from .host_session_handshake import HostSessionReferenceWorkflow
from .lineage import NotFoundError
from .memory import HeadquartersMemory

SERVICE_STATES = {"READY", "WAITING_FOR_BRIDGE", "OBSERVING", "STOPPED"}
STATUS_SCHEMA = "n0te.ableton-observer-status/v1"
NOTICE_MAX_CHARS = 240
_NOTICE_RESPONSE_MAX_BYTES = 4096


class AbletonObserverServiceError(RuntimeError):
    """The runtime-owned Ableton observer service cannot be composed or advanced safely."""


class _ObserverLike(Protocol):
    interval_seconds: float

    async def poll_once(self, *, force_discovery: bool = False): ...


CycleCallback = Callable[[AbletonContinuousObservationCycle], object | Awaitable[object]]


def _json_clone(value: object) -> object:
    """Return an isolated JSON-safe value or fail closed on hidden runtime objects."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AbletonObserverServiceError(
            "observer status contains non-JSON runtime state"
        ) from exc
    return json.loads(encoded)


def _notice_text(value: object) -> str:
    if not isinstance(value, str):
        raise AbletonObserverServiceError("notice message must be a string")
    text = " ".join(value.split())
    if not text:
        raise AbletonObserverServiceError("notice message must not be empty")
    if len(text) > NOTICE_MAX_CHARS:
        raise AbletonObserverServiceError(
            f"notice message exceeds {NOTICE_MAX_CHARS} characters"
        )
    return text


class AbletonAdviceClient:
    """Display bounded N0TE advice in Live without granting musical mutation authority."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ABLETON_BRIDGE_ENDPOINT,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        validated = AbletonRemoteScriptClient(
            endpoint,
            timeout_seconds=timeout_seconds,
        )
        self.endpoint = validated.endpoint
        self.timeout_seconds = validated.timeout_seconds

    def show_notice(self, message: str) -> None:
        text = _notice_text(message)
        raw = json.dumps(
            {"message": text},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = Request(
            self.endpoint + "/notice",
            data=raw,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "N0TE/ableton-advice-bridge",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if int(response.status) != 200:
                    raise AbletonHostBridgeError(
                        f"Ableton advice bridge returned HTTP {response.status}"
                    )
                if response.headers.get_content_type() != "application/json":
                    raise AbletonHostBridgeError(
                        "Ableton advice bridge response must be application/json"
                    )
                response_raw = response.read(_NOTICE_RESPONSE_MAX_BYTES + 1)
        except AbletonHostBridgeError:
            raise
        except Exception as exc:
            raise AbletonHostBridgeError(
                "cannot deliver Ableton advice notice"
            ) from exc
        if len(response_raw) > _NOTICE_RESPONSE_MAX_BYTES:
            raise AbletonHostBridgeError("Ableton advice bridge response is oversized")
        try:
            payload = json.loads(response_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AbletonHostBridgeError(
                "Ableton advice bridge response is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {"ok"} or payload["ok"] is not True:
            raise AbletonHostBridgeError("Ableton advice bridge did not confirm display")


class AbletonObserverService:
    """Supervise continuous Ableton observation inside one owned N0TE runtime."""

    def __init__(
        self,
        headquarters: HeadquartersMemory,
        observer: _ObserverLike,
        *,
        reconnect_interval_seconds: float = 2.0,
        runtime_guard: Callable[[], bool] | None = None,
        advice_client: AbletonAdviceClient | None = None,
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
        if advice_client is not None and not isinstance(advice_client, AbletonAdviceClient):
            raise TypeError("advice_client must be AbletonAdviceClient or None")
        self.headquarters = headquarters
        self.observer = observer
        self.reconnect_interval_seconds = interval
        self._runtime_guard = runtime_guard
        self._advice_client = advice_client
        self._state = "READY"
        self._latest_cycle: AbletonContinuousObservationCycle | None = None
        self._bridge_failure_count = 0
        self._last_bridge_error_class: str | None = None
        self._notice_failure_count = 0
        self._last_notice_error_class: str | None = None

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
        live_client = AbletonRemoteScriptClient(bridge_endpoint)
        observer = AbletonContinuousObserver(
            live_client,
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
            advice_client=AbletonAdviceClient(bridge_endpoint),
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

    @property
    def notice_failure_count(self) -> int:
        return self._notice_failure_count

    @property
    def last_notice_error_class(self) -> str | None:
        return self._last_notice_error_class

    def _runtime_is_owned(self) -> bool:
        if self._runtime_guard is None:
            return True
        try:
            return self._runtime_guard() is True
        except Exception:
            return False

    def _active_tools_status(self, workspace_id: str) -> dict[str, object]:
        try:
            projection = project_active_tools(
                self.headquarters.workspaces,
                self.headquarters.shadow,
                workspace_id,
            )
        except (ActiveToolProjectionError, NotFoundError) as exc:
            return {
                "state": "UNAVAILABLE",
                "error_class": type(exc).__name__,
                "scopes": [],
            }

        scopes = []
        for scope in projection.scopes:
            scopes.append(
                {
                    "parent_kind": scope.parent_kind,
                    "parent_ref": scope.parent_ref,
                    "parent_name": scope.parent_name,
                    "coverage": scope.coverage,
                    "expected_count": scope.expected_count,
                    "tools": [
                        {
                            "device_ref": tool.device_ref,
                            "name": tool.name,
                            "position_kind": tool.position_kind,
                            "position": tool.position,
                            "role": tool.role,
                            "class_name": tool.class_name,
                            "enabled": tool.enabled,
                            "offline": tool.offline,
                        }
                        for tool in scope.tools
                    ],
                }
            )
        return {
            "state": "OBSERVED",
            "host_family": projection.host_family,
            "scopes": scopes,
        }

    def status_projection(self) -> dict[str, object]:
        """Return the consumer-safe current Ableton/N0TE state."""

        cycle = self._latest_cycle
        payload: dict[str, object] = {
            "schema": STATUS_SCHEMA,
            "service_state": self._state,
            "connected": cycle is not None and self._state == "OBSERVING",
            "bridge_failure_count": self._bridge_failure_count,
            "last_bridge_error_class": self._last_bridge_error_class,
            "advice_display": {
                "failure_count": self._notice_failure_count,
                "last_error_class": self._last_notice_error_class,
            },
            "read_only": True,
            "action_authority_granted": False,
            "session": None,
            "reference_discovery": None,
        }
        if cycle is None:
            return _json_clone(payload)  # type: ignore[return-value]

        snapshot = cycle.snapshot
        binding = cycle.observation.binding
        track = snapshot.selected_track
        payload["session"] = {
            "song_id": binding.song_id,
            "workspace_id": binding.workspace_id,
            "host_family": snapshot.runtime.family,
            "host_version": snapshot.runtime.version,
            "host_display_name": snapshot.runtime.display_name,
            "tempo_bpm": snapshot.tempo_bpm,
            "is_playing": snapshot.is_playing,
            "current_song_time": snapshot.current_song_time,
            "selected_track": (
                None
                if track is None
                else {
                    "kind": track.kind,
                    "index": track.index,
                    "name": track.name,
                    "ref": track.ref,
                }
            ),
            "observation_committed": cycle.observation_committed,
        }
        payload["active_tools"] = self._active_tools_status(binding.workspace_id)

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

    @staticmethod
    def _reference_notice(cycle: AbletonContinuousObservationCycle) -> str | None:
        if not cycle.discovery_performed or not isinstance(cycle.references, dict):
            return None
        primary = cycle.references.get("primary")
        if not isinstance(primary, dict):
            return None
        title = primary.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        return _notice_text("Reference: " + title.strip())

    async def _announce_cycle(self, cycle: AbletonContinuousObservationCycle) -> None:
        if self._advice_client is None:
            return
        message = self._reference_notice(cycle)
        if message is None:
            return
        try:
            await asyncio.to_thread(self._advice_client.show_notice, message)
        except AbletonHostBridgeError as exc:
            self._notice_failure_count += 1
            self._last_notice_error_class = type(exc).__name__

    async def refresh_references(self) -> AbletonContinuousObservationCycle:
        if not self._runtime_is_owned():
            raise AbletonObserverServiceError(
                "cannot refresh references after the owning ApplicationRuntime stopped"
            )
        try:
            cycle = await self.observer.poll_once(force_discovery=True)
        except AbletonHostBridgeError as exc:
            self._state = "WAITING_FOR_BRIDGE"
            self._bridge_failure_count += 1
            self._last_bridge_error_class = type(exc).__name__
            raise
        self._latest_cycle = cycle
        self._state = "OBSERVING"
        await self._announce_cycle(cycle)
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
                await self._announce_cycle(cycle)
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
