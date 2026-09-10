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
from .studio_one_midi_bridge import (
    StudioOneClockSnapshot,
    StudioOneMidiBridgeError,
    StudioOneMidiClockMonitorClient,
)

_DISCOVERY_REASONS = {"INITIAL", "MUSICAL_CHANGE", "EXPLICIT", "RETRY"}


class StudioOneContinuousObserverError(RuntimeError):
    """Continuous Studio One timing observation lost trustworthy session state."""


def _finite_positive(value: object, field: str, *, maximum: float) -> float:
    if isinstance(value, bool):
        raise StudioOneContinuousObserverError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StudioOneContinuousObserverError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise StudioOneContinuousObserverError(
            f"{field} must be greater than 0 and no greater than {maximum:g}"
        )
    return number


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise StudioOneContinuousObserverError(f"{field} must not be empty")
    return text


def _observation_fingerprint(snapshot: StudioOneClockSnapshot) -> tuple[object, ...]:
    return (
        snapshot.location_ref,
        snapshot.runtime.fingerprint,
        round(snapshot.tempo_bpm, 2),
        snapshot.transport_state,
    )


def _reference_fingerprint(snapshot: StudioOneClockSnapshot) -> tuple[object, ...]:
    return (
        snapshot.location_ref,
        snapshot.runtime.fingerprint,
        int(math.floor(snapshot.tempo_bpm + 0.5)),
    )


@dataclass(frozen=True)
class StudioOneContinuousObservationCycle:
    sequence: int
    snapshot: StudioOneClockSnapshot
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
            raise StudioOneContinuousObserverError("sequence must be positive")
        if not isinstance(self.snapshot, StudioOneClockSnapshot):
            raise TypeError("snapshot must be StudioOneClockSnapshot")
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
        if (
            self.discovery_reason is not None
            and self.discovery_reason not in _DISCOVERY_REASONS
        ):
            raise StudioOneContinuousObserverError(
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


class StudioOneContinuousObserver:
    """Keep tempo/transport truth hot without turning MIDI Clock ticks into searches."""

    def __init__(
        self,
        client: StudioOneMidiClockMonitorClient,
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
        if not isinstance(client, StudioOneMidiClockMonitorClient):
            raise TypeError("client must be StudioOneMidiClockMonitorClient")
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
        self.comparison_dimensions = tuple(
            str(value).strip() for value in comparison_dimensions
        )
        self.semantic_tags = tuple(str(value).strip() for value in semantic_tags)
        self.required_features = tuple(
            str(value).strip() for value in required_features
        )
        self.desired_tags = tuple(str(value).strip() for value in desired_tags)
        if not self.comparison_dimensions or any(
            not value for value in self.comparison_dimensions
        ):
            raise StudioOneContinuousObserverError(
                "comparison_dimensions must contain non-empty values"
            )
        for name, values in (
            ("semantic_tags", self.semantic_tags),
            ("required_features", self.required_features),
            ("desired_tags", self.desired_tags),
        ):
            if any(not value for value in values):
                raise StudioOneContinuousObserverError(
                    f"{name} contains an empty value"
                )
        self.feature_weights = (
            None if feature_weights is None else dict(feature_weights)
        )
        self.discovery_limit = int(discovery_limit)
        self.result_limit = int(result_limit)
        if self.discovery_limit < 1 or self.result_limit < 1:
            raise StudioOneContinuousObserverError(
                "discovery_limit and result_limit must be positive"
            )
        self._clock = monotonic_clock
        self._sequence = 0
        self._latest_snapshot: StudioOneClockSnapshot | None = None
        self._latest_handshake: HostSessionHandshakeResult | None = None
        self._latest_observation: HostObservationResult | None = None
        self._latest_references: dict[str, object] | None = None
        self._last_observation_fingerprint: tuple[object, ...] | None = None
        self._last_successful_reference_fingerprint: tuple[object, ...] | None = None
        self._has_attempted_discovery = False
        self._next_discovery_retry_at = 0.0

    @property
    def latest_snapshot(self) -> StudioOneClockSnapshot | None:
        return self._latest_snapshot

    @property
    def latest_references(self) -> dict[str, object] | None:
        return (
            None
            if self._latest_references is None
            else dict(self._latest_references)
        )

    def _record_snapshot(
        self,
        snapshot: StudioOneClockSnapshot,
    ) -> tuple[HostSessionHandshakeResult, HostObservationResult]:
        attached = self.workflow.handshake.attach(
            runtime=snapshot.runtime,
            location_ref=snapshot.location_ref,
            display_name="Studio One • MIDI Clock monitor (session-only identity)",
        )
        observation = self.workflow.observations.observe(
            attached.binding,
            capabilities=snapshot.capabilities(),
            focus_dimensions=(),
            focus_evidence_ref=(
                f"studio-one:midi-clock:{snapshot.bridge_session_id}:focus-unobserved"
            ),
            shadow=snapshot.shadow(),
            now_epoch_seconds=snapshot.observed_at_epoch_seconds,
        )
        if observation.shadow.status != "CURRENT":
            raise StudioOneContinuousObserverError(
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
    ) -> StudioOneContinuousObservationCycle:
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
                    self._next_discovery_retry_at = (
                        now + self.discovery_retry_seconds
                    )
                else:
                    self._latest_references = references
                    self._last_successful_reference_fingerprint = reference_fingerprint
                    self._next_discovery_retry_at = 0.0
                    discovery_performed = True
            else:
                discovery_deferred = True

        return StudioOneContinuousObservationCycle(
            sequence=self._sequence,
            snapshot=snapshot,
            handshake=handshake,
            observation=observation,
            observation_committed=observation_changed,
            discovery_performed=discovery_performed,
            discovery_deferred=discovery_deferred,
            discovery_reason=discovery_reason,
            references=(
                None
                if self._latest_references is None
                else dict(self._latest_references)
            ),
            discovery_error_class=discovery_error_class,
        )

    async def watch(
        self,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[StudioOneContinuousObservationCycle]:
        if stop_event is not None and not isinstance(stop_event, asyncio.Event):
            raise TypeError("stop_event must be asyncio.Event or None")
        while stop_event is None or not stop_event.is_set():
            try:
                yield await self.poll_once()
            except StudioOneMidiBridgeError:
                raise
            if stop_event is None:
                await asyncio.sleep(self.interval_seconds)
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                pass
