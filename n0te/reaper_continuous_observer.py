from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Mapping

from .host_observation import HostObservationResult
from .host_session_handshake import (
    HostSessionHandshakeResult,
    HostSessionReferenceWorkflow,
    HostSessionReferenceWorkflowResult,
)
from .reaper_host_bridge import (
    ReaperHostBridgeError,
    ReaperObservationSnapshot,
    ReaperSnapshotFileClient,
)

_DISCOVERY_REASONS = {"INITIAL", "MUSICAL_CHANGE", "EXPLICIT", "RETRY"}


class ReaperContinuousObserverError(RuntimeError):
    """Continuous REAPER observation could not preserve trustworthy session state."""


def _finite_positive(value: object, field: str, *, maximum: float) -> float:
    if isinstance(value, bool):
        raise ReaperContinuousObserverError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReaperContinuousObserverError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise ReaperContinuousObserverError(
            f"{field} must be greater than 0 and no greater than {maximum:g}"
        )
    return number


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ReaperContinuousObserverError(f"{field} must not be empty")
    return text


def _selected_tracks_fingerprint(snapshot: ReaperObservationSnapshot) -> tuple[tuple[object, ...], ...]:
    return tuple((track.index, track.guid, track.name) for track in snapshot.selected_tracks)


def _observation_fingerprint(snapshot: ReaperObservationSnapshot) -> tuple[object, ...]:
    """Only facts that map to canonical identity/focus/Shadow trigger a commit.

    Play position, project state-change count and total track count remain hot snapshot
    telemetry. They are intentionally excluded so polling does not create append-only
    history for facts N0TE cannot yet represent semantically in Host Shadow.
    """

    return (
        snapshot.location_ref,
        snapshot.runtime.fingerprint,
        snapshot.project_name,
        snapshot.project_saved,
        snapshot.tempo_bpm,
        snapshot.play_state,
        snapshot.repeat_enabled,
        _selected_tracks_fingerprint(snapshot),
    )


def _reference_fingerprint(snapshot: ReaperObservationSnapshot) -> tuple[object, ...]:
    return (
        snapshot.location_ref,
        snapshot.runtime.fingerprint,
        int(math.floor(snapshot.tempo_bpm + 0.5)),
    )


@dataclass(frozen=True)
class ReaperContinuousObservationCycle:
    sequence: int
    snapshot: ReaperObservationSnapshot
    handshake: HostSessionHandshakeResult
    observation: HostObservationResult
    observation_committed: bool
    discovery_performed: bool
    discovery_deferred: bool
    discovery_reason: str | None
    references: dict[str, object] | None
    discovery_error_class: str | None

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ReaperContinuousObserverError("sequence must be positive")
        if not isinstance(self.snapshot, ReaperObservationSnapshot):
            raise TypeError("snapshot must be ReaperObservationSnapshot")
        if not isinstance(self.handshake, HostSessionHandshakeResult):
            raise TypeError("handshake must be HostSessionHandshakeResult")
        if not isinstance(self.observation, HostObservationResult):
            raise TypeError("observation must be HostObservationResult")
        for field in (
            "observation_committed",
            "discovery_performed",
            "discovery_deferred",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be bool")
        if self.discovery_reason is not None and self.discovery_reason not in _DISCOVERY_REASONS:
            raise ReaperContinuousObserverError(
                f"unsupported discovery reason: {self.discovery_reason}"
            )
        if self.references is not None and not isinstance(self.references, dict):
            raise TypeError("references must be a dict or None")
        if self.discovery_error_class is not None:
            object.__setattr__(
                self,
                "discovery_error_class",
                _text(self.discovery_error_class, "discovery_error_class"),
            )


class ReaperContinuousObserver:
    """Keep N0TE aware of REAPER without turning deferred snapshots into search spam."""

    def __init__(
        self,
        client: ReaperSnapshotFileClient,
        workflow: HostSessionReferenceWorkflow,
        *,
        provider_id: str,
        interval_seconds: float = 1.0,
        discovery_retry_seconds: float = 30.0,
        comparison_dimensions: tuple[str, ...] = ("tempo",),
        semantic_tags: tuple[str, ...] = (),
        required_features: tuple[str, ...] = ("tempo_bpm",),
        desired_tags: tuple[str, ...] = (),
        feature_weights: Mapping[str, float] | None = None,
        discovery_limit: int = 12,
        result_limit: int = 3,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(client, ReaperSnapshotFileClient):
            raise TypeError("client must be ReaperSnapshotFileClient")
        if not isinstance(workflow, HostSessionReferenceWorkflow):
            raise TypeError("workflow must be HostSessionReferenceWorkflow")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        self.client = client
        self.workflow = workflow
        self.provider_id = _text(provider_id, "provider_id")
        self.interval_seconds = _finite_positive(
            interval_seconds,
            "interval_seconds",
            maximum=60.0,
        )
        self.discovery_retry_seconds = _finite_positive(
            discovery_retry_seconds,
            "discovery_retry_seconds",
            maximum=3600.0,
        )
        self.comparison_dimensions = tuple(str(v).strip() for v in comparison_dimensions)
        self.semantic_tags = tuple(str(v).strip() for v in semantic_tags)
        self.required_features = tuple(str(v).strip() for v in required_features)
        self.desired_tags = tuple(str(v).strip() for v in desired_tags)
        if not self.comparison_dimensions or any(not v for v in self.comparison_dimensions):
            raise ReaperContinuousObserverError(
                "comparison_dimensions must contain non-empty values"
            )
        for name, values in (
            ("semantic_tags", self.semantic_tags),
            ("required_features", self.required_features),
            ("desired_tags", self.desired_tags),
        ):
            if any(not value for value in values):
                raise ReaperContinuousObserverError(f"{name} contains an empty value")
        self.feature_weights = None if feature_weights is None else dict(feature_weights)
        self.discovery_limit = int(discovery_limit)
        self.result_limit = int(result_limit)
        if self.discovery_limit < 1 or self.result_limit < 1:
            raise ReaperContinuousObserverError(
                "discovery_limit and result_limit must be positive"
            )
        self._clock = monotonic_clock
        self._sequence = 0
        self._latest_snapshot: ReaperObservationSnapshot | None = None
        self._latest_handshake: HostSessionHandshakeResult | None = None
        self._latest_observation: HostObservationResult | None = None
        self._latest_references: dict[str, object] | None = None
        self._last_observation_fingerprint: tuple[object, ...] | None = None
        self._last_successful_reference_fingerprint: tuple[object, ...] | None = None
        self._has_attempted_discovery = False
        self._next_discovery_retry_at = 0.0

    @property
    def latest_snapshot(self) -> ReaperObservationSnapshot | None:
        return self._latest_snapshot

    @property
    def latest_references(self) -> dict[str, object] | None:
        return None if self._latest_references is None else dict(self._latest_references)

    def _record_snapshot(
        self,
        snapshot: ReaperObservationSnapshot,
    ) -> tuple[HostSessionHandshakeResult, HostObservationResult]:
        attached = self.workflow.handshake.attach(
            runtime=snapshot.runtime,
            location_ref=snapshot.location_ref,
            display_name=f"REAPER • {snapshot.project_name}",
        )
        observation = self.workflow.observations.observe(
            attached.binding,
            capabilities=snapshot.capabilities(),
            focus_dimensions=snapshot.focus_dimensions(),
            focus_evidence_ref=f"reaper:snapshot:{snapshot.bridge_session_id}:focus",
            shadow=snapshot.shadow(),
            now_epoch_seconds=snapshot.observed_at_epoch_seconds,
        )
        if observation.shadow.status != "CURRENT":
            raise ReaperContinuousObserverError(
                "continuous observation requires a CURRENT Host Shadow"
            )
        return attached, observation

    async def _discover(
        self,
        handshake: HostSessionHandshakeResult,
        observation: HostObservationResult,
    ) -> dict[str, object]:
        references = await self.workflow.reference_client.discover_session(
            self.provider_id,
            observation.binding,
            observation.shadow,
            comparison_dimensions=self.comparison_dimensions,
            semantic_tags=self.semantic_tags,
            required_features=self.required_features,
            desired_tags=self.desired_tags,
            feature_weights=self.feature_weights,
            discovery_limit=self.discovery_limit,
            result_limit=self.result_limit,
        )
        validated = HostSessionReferenceWorkflowResult(
            handshake=handshake,
            observation=observation,
            provider_id=self.provider_id,
            references=references,
        )
        return dict(validated.references)

    async def poll_once(
        self,
        *,
        force_discovery: bool = False,
    ) -> ReaperContinuousObservationCycle:
        if type(force_discovery) is not bool:
            raise TypeError("force_discovery must be bool")
        snapshot = await asyncio.to_thread(self.client.fetch_snapshot)
        self._latest_snapshot = snapshot
        self._sequence += 1

        observation_fingerprint = _observation_fingerprint(snapshot)
        observation_changed = (
            self._latest_observation is None
            or observation_fingerprint != self._last_observation_fingerprint
        )
        if observation_changed:
            handshake, observation = self._record_snapshot(snapshot)
            self._latest_handshake = handshake
            self._latest_observation = observation
            self._last_observation_fingerprint = observation_fingerprint
        else:
            handshake = self._latest_handshake
            observation = self._latest_observation
            assert handshake is not None and observation is not None

        reference_fingerprint = _reference_fingerprint(snapshot)
        if force_discovery:
            discovery_reason = "EXPLICIT"
        elif self._last_successful_reference_fingerprint == reference_fingerprint:
            discovery_reason = None
        elif not self._has_attempted_discovery:
            discovery_reason = "INITIAL"
        elif self._last_successful_reference_fingerprint is None:
            discovery_reason = "RETRY"
        else:
            discovery_reason = "MUSICAL_CHANGE"

        discovery_performed = False
        discovery_deferred = False
        discovery_error_class = None
        now = float(self._clock())
        if discovery_reason is not None:
            eligible = force_discovery or now >= self._next_discovery_retry_at
            if eligible:
                self._has_attempted_discovery = True
                try:
                    references = await self._discover(handshake, observation)
                except Exception as exc:
                    discovery_error_class = type(exc).__name__
                    self._next_discovery_retry_at = now + self.discovery_retry_seconds
                else:
                    self._latest_references = references
                    self._last_successful_reference_fingerprint = reference_fingerprint
                    self._next_discovery_retry_at = 0.0
                    discovery_performed = True
            else:
                discovery_deferred = True

        return ReaperContinuousObservationCycle(
            sequence=self._sequence,
            snapshot=snapshot,
            handshake=handshake,
            observation=observation,
            observation_committed=observation_changed,
            discovery_performed=discovery_performed,
            discovery_deferred=discovery_deferred,
            discovery_reason=discovery_reason,
            references=None if self._latest_references is None else dict(self._latest_references),
            discovery_error_class=discovery_error_class,
        )

    async def watch(
        self,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[ReaperContinuousObservationCycle]:
        if stop_event is not None and not isinstance(stop_event, asyncio.Event):
            raise TypeError("stop_event must be asyncio.Event or None")
        while stop_event is None or not stop_event.is_set():
            try:
                yield await self.poll_once()
            except ReaperHostBridgeError:
                raise
            if stop_event is None:
                await asyncio.sleep(self.interval_seconds)
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
