from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Mapping

from .ableton_host_bridge import (
    AbletonHostBridgeError,
    AbletonObservationSnapshot,
    AbletonRemoteScriptClient,
)
from .host_observation import HostObservationResult
from .host_session_handshake import (
    HostSessionHandshakeResult,
    HostSessionReferenceWorkflow,
    HostSessionReferenceWorkflowResult,
)

_DISCOVERY_REASONS = {"INITIAL", "MUSICAL_CHANGE", "EXPLICIT", "RETRY"}


class AbletonContinuousObserverError(RuntimeError):
    """Continuous Ableton observation could not preserve a trustworthy session state."""


def _finite_positive(value: object, field: str, *, maximum: float) -> float:
    if isinstance(value, bool):
        raise AbletonContinuousObserverError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AbletonContinuousObserverError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise AbletonContinuousObserverError(
            f"{field} must be greater than 0 and no greater than {maximum:g}"
        )
    return number


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise AbletonContinuousObserverError(f"{field} must not be empty")
    return text


def _track_fingerprint(snapshot: AbletonObservationSnapshot) -> tuple[object, ...] | None:
    track = snapshot.selected_track
    if track is None:
        return None
    return (track.kind, track.index, track.name)


def _device_chain_fingerprint(snapshot: AbletonObservationSnapshot) -> tuple[object, ...] | None:
    if not snapshot.device_chain_observed:
        return None
    return tuple(
        (device.index, device.name, device.class_name)
        for device in snapshot.selected_track_devices
    )


def _observation_fingerprint(snapshot: AbletonObservationSnapshot) -> tuple[object, ...]:
    """Fields whose change is worth committing to canonical Host truth.

    Current song time is intentionally excluded. It remains available through the hot
    snapshot but would otherwise create an append-only Shadow batch on every poll.
    Device-chain changes are included because they alter the local Technical Twin, but
    they remain excluded from the separate reference-discovery fingerprint below.
    """

    return (
        snapshot.location_ref,
        snapshot.workspace_id,
        snapshot.runtime.fingerprint,
        snapshot.tempo_bpm,
        snapshot.is_playing,
        _track_fingerprint(snapshot),
        _device_chain_fingerprint(snapshot),
    )


def _reference_fingerprint(snapshot: AbletonObservationSnapshot) -> tuple[object, ...]:
    """Fields whose change justifies another external reference search.

    Transport, focus and device-chain changes do not trigger discovery. Tempo is
    quantized to the nearest whole BPM so tiny automation/rounding changes do not
    cause network churn.
    """

    tempo_bucket = int(math.floor(snapshot.tempo_bpm + 0.5))
    return (
        snapshot.location_ref,
        snapshot.runtime.fingerprint,
        tempo_bucket,
    )


@dataclass(frozen=True)
class AbletonContinuousObservationCycle:
    sequence: int
    snapshot: AbletonObservationSnapshot
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
            raise AbletonContinuousObserverError("sequence must be positive")
        if not isinstance(self.snapshot, AbletonObservationSnapshot):
            raise TypeError("snapshot must be AbletonObservationSnapshot")
        if not isinstance(self.handshake, HostSessionHandshakeResult):
            raise TypeError("handshake must be HostSessionHandshakeResult")
        if not isinstance(self.observation, HostObservationResult):
            raise TypeError("observation must be HostObservationResult")
        for field in ("observation_committed", "discovery_performed", "discovery_deferred"):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be bool")
        if self.discovery_reason is not None and self.discovery_reason not in _DISCOVERY_REASONS:
            raise AbletonContinuousObserverError(
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


class AbletonContinuousObserver:
    """Keep N0TE aware of Live without turning polling into provider spam.

    The loop has three deliberately different cadences:

    * Live snapshots are fetched every poll and retained hot in memory.
    * Canonical Host observation is appended only when meaningful host truth changes.
    * Reference discovery runs only for a new Set/runtime, a meaningful tempo change,
      an explicit request, or a backoff-governed retry after provider failure.

    It grants no DAW mutation authority and performs no writes inside Live.
    """

    def __init__(
        self,
        client: AbletonRemoteScriptClient,
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
        if not isinstance(client, AbletonRemoteScriptClient):
            raise TypeError("client must be AbletonRemoteScriptClient")
        if not isinstance(workflow, HostSessionReferenceWorkflow):
            raise TypeError("workflow must be HostSessionReferenceWorkflow")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        self.client = client
        self.workflow = workflow
        self.provider_id = _text(provider_id, "provider_id")
        self.interval_seconds = _finite_positive(
            interval_seconds, "interval_seconds", maximum=60.0
        )
        self.discovery_retry_seconds = _finite_positive(
            discovery_retry_seconds, "discovery_retry_seconds", maximum=3600.0
        )
        self.comparison_dimensions = tuple(str(v).strip() for v in comparison_dimensions)
        self.semantic_tags = tuple(str(v).strip() for v in semantic_tags)
        self.required_features = tuple(str(v).strip() for v in required_features)
        self.desired_tags = tuple(str(v).strip() for v in desired_tags)
        if not self.comparison_dimensions or any(not v for v in self.comparison_dimensions):
            raise AbletonContinuousObserverError(
                "comparison_dimensions must contain non-empty values"
            )
        for name, values in (
            ("semantic_tags", self.semantic_tags),
            ("required_features", self.required_features),
            ("desired_tags", self.desired_tags),
        ):
            if any(not value for value in values):
                raise AbletonContinuousObserverError(f"{name} contains an empty value")
        self.feature_weights = None if feature_weights is None else dict(feature_weights)
        self.discovery_limit = int(discovery_limit)
        self.result_limit = int(result_limit)
        if self.discovery_limit < 1 or self.result_limit < 1:
            raise AbletonContinuousObserverError(
                "discovery_limit and result_limit must be positive"
            )
        self._clock = monotonic_clock
        self._sequence = 0
        self._latest_snapshot: AbletonObservationSnapshot | None = None
        self._latest_handshake: HostSessionHandshakeResult | None = None
        self._latest_observation: HostObservationResult | None = None
        self._latest_references: dict[str, object] | None = None
        self._last_observation_fingerprint: tuple[object, ...] | None = None
        self._last_successful_reference_fingerprint: tuple[object, ...] | None = None
        self._has_attempted_discovery = False
        self._next_discovery_retry_at = 0.0

    @property
    def latest_snapshot(self) -> AbletonObservationSnapshot | None:
        return self._latest_snapshot

    @property
    def latest_references(self) -> dict[str, object] | None:
        return None if self._latest_references is None else dict(self._latest_references)

    def _known_workspace_id(self, snapshot: AbletonObservationSnapshot) -> str | None:
        if snapshot.workspace_id is not None:
            return snapshot.workspace_id
        if not snapshot.has_durable_set_location:
            return None
        candidates = self.workflow.handshake.workspaces.current_candidates_at_location(
            snapshot.session_location_ref
        )
        if len(candidates) > 1:
            raise AbletonHostBridgeError(
                "multiple current workspaces claim this Ableton document session"
            )
        if not candidates:
            return None
        return candidates[0].id

    def _record_snapshot(
        self,
        snapshot: AbletonObservationSnapshot,
    ) -> tuple[HostSessionHandshakeResult, HostObservationResult]:
        attached = self.workflow.handshake.attach(
            runtime=snapshot.runtime,
            location_ref=snapshot.location_ref,
            known_workspace_id=self._known_workspace_id(snapshot),
            display_name="Ableton Live Set",
        )
        observation = self.workflow.observations.observe(
            attached.binding,
            capabilities=snapshot.capabilities(),
            focus_dimensions=snapshot.focus_dimensions(),
            focus_evidence_ref=f"ableton:snapshot:{snapshot.bridge_session_id}:focus",
            shadow=snapshot.shadow(),
            now_epoch_seconds=snapshot.observed_at_epoch_seconds,
        )
        if observation.shadow.status != "CURRENT":
            raise AbletonContinuousObserverError(
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
    ) -> AbletonContinuousObservationCycle:
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

        return AbletonContinuousObservationCycle(
            sequence=self._sequence,
            snapshot=snapshot,
            handshake=handshake,
            observation=observation,
            observation_committed=observation_changed,
            discovery_performed=discovery_performed,
            discovery_deferred=discovery_deferred,
            discovery_reason=discovery_reason,
            references=(
                None if self._latest_references is None else dict(self._latest_references)
            ),
            discovery_error_class=discovery_error_class,
        )

    async def watch(
        self,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[AbletonContinuousObservationCycle]:
        if stop_event is not None and not isinstance(stop_event, asyncio.Event):
            raise TypeError("stop_event must be asyncio.Event or None")
        while stop_event is None or not stop_event.is_set():
            yield await self.poll_once()
            if stop_event is None:
                await asyncio.sleep(self.interval_seconds)
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
