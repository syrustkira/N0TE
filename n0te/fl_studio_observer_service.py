from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
import uuid
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
NOTICE_SCHEMA = "n0te.fl-studio-notice/v1"
NOTICE_FILE_NAME = "n0te_notice.json"
NOTICE_MAX_CHARS = 240


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


def _notice_text(value: object) -> str:
    if not isinstance(value, str):
        raise FLStudioObserverServiceError("notice message must be a string")
    text = " ".join(value.split())
    if not text:
        raise FLStudioObserverServiceError("notice message must not be empty")
    if len(text) > NOTICE_MAX_CHARS:
        raise FLStudioObserverServiceError(
            f"notice message exceeds {NOTICE_MAX_CHARS} characters"
        )
    return text


class FLStudioAdviceFileClient:
    """Write one bounded, session-bound UI hint for the FL MIDI script to consume."""

    def __init__(self, snapshot_path: str | Path) -> None:
        snapshot = Path(snapshot_path).expanduser()
        if not snapshot.is_absolute():
            raise FLStudioObserverServiceError("FL Studio snapshot path must be absolute")
        parent = snapshot.parent
        if parent.exists() and (not parent.is_dir() or parent.is_symlink()):
            raise FLStudioObserverServiceError("FL Studio bridge directory is unsafe")
        self.notice_path = parent / NOTICE_FILE_NAME

    def show_notice(
        self,
        message: str,
        *,
        bridge_session_id: str,
        created_at_epoch_seconds: int | None = None,
    ) -> None:
        text = _notice_text(message)
        session = str(bridge_session_id).strip()
        if not session:
            raise FLStudioObserverServiceError("bridge_session_id must not be empty")
        created = int(time.time()) if created_at_epoch_seconds is None else int(created_at_epoch_seconds)
        payload = {
            "schema": NOTICE_SCHEMA,
            "bridge_session_id": session,
            "notice_id": uuid.uuid4().hex,
            "created_at_epoch_seconds": created,
            "message": text,
        }
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > 4096:
            raise FLStudioObserverServiceError("notice payload is oversized")
        parent = self.notice_path.parent
        if not parent.is_dir() or parent.is_symlink():
            raise FLStudioObserverServiceError("FL Studio bridge directory is unavailable")
        if self.notice_path.exists() and self.notice_path.is_symlink():
            raise FLStudioObserverServiceError("FL Studio notice path must not be a symlink")
        temp = parent / ("." + NOTICE_FILE_NAME + ".n0te-" + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.notice_path)
        except Exception as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, FLStudioObserverServiceError):
                raise
            raise FLStudioObserverServiceError("cannot deliver FL Studio advice notice") from exc


class FLStudioObserverService:
    """Supervise FL snapshot observation inside one owned N0TE runtime."""

    def __init__(
        self,
        headquarters: HeadquartersMemory,
        observer: _ObserverLike,
        *,
        reconnect_interval_seconds: float = 2.0,
        runtime_guard: Callable[[], bool] | None = None,
        advice_client: FLStudioAdviceFileClient | None = None,
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
        if advice_client is not None and not isinstance(advice_client, FLStudioAdviceFileClient):
            raise TypeError("advice_client must be FLStudioAdviceFileClient or None")
        self.headquarters = headquarters
        self.observer = observer
        self.reconnect_interval_seconds = interval
        self._runtime_guard = runtime_guard
        self._advice_client = advice_client
        self._state = "READY"
        self._latest_cycle: FLStudioContinuousObservationCycle | None = None
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
            advice_client=FLStudioAdviceFileClient(snapshot_path),
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

    def status_projection(self) -> dict[str, object]:
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

    @staticmethod
    def _reference_notice(cycle: FLStudioContinuousObservationCycle) -> str | None:
        if not cycle.discovery_performed or not isinstance(cycle.references, dict):
            return None
        primary = cycle.references.get("primary")
        if not isinstance(primary, dict):
            return None
        title = primary.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        return _notice_text("Reference: " + title.strip())

    async def _announce_cycle(self, cycle: FLStudioContinuousObservationCycle) -> None:
        if self._advice_client is None:
            return
        message = self._reference_notice(cycle)
        if message is None:
            return
        try:
            await asyncio.to_thread(
                self._advice_client.show_notice,
                message,
                bridge_session_id=cycle.snapshot.bridge_session_id,
            )
        except FLStudioObserverServiceError as exc:
            self._notice_failure_count += 1
            self._last_notice_error_class = type(exc).__name__

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
