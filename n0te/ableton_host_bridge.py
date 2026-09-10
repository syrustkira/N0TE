from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .focus import FocusDimension
from .host_observation import CapabilityFactInput, ShadowObservationInput
from .host_session_handshake import (
    HostSessionReferenceWorkflow,
    HostSessionReferenceWorkflowResult,
)
from .hosts import HostRuntimeIdentity
from .shadow import ShadowEventInput

ABLETON_SNAPSHOT_SCHEMA = "n0te.ableton-observation/v2"
ABLETON_LEGACY_SNAPSHOT_SCHEMA = "n0te.ableton-observation/v1"
DEFAULT_ABLETON_BRIDGE_ENDPOINT = "http://127.0.0.1:9799"
_MAX_SNAPSHOT_BYTES = 65536
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_BASE_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema",
        "adapter",
        "bridge_session_id",
        "workspace_id",
        "runtime",
        "observed_at_epoch_seconds",
        "tempo_bpm",
        "transport",
        "selected_track",
    }
)
_V2_ALLOWED_TOP_LEVEL = _BASE_ALLOWED_TOP_LEVEL | frozenset({"set_path_fingerprint"})
_SHA256_HEX = frozenset("0123456789abcdef")


class AbletonHostBridgeError(RuntimeError):
    """Ableton read-side bridge evidence could not be trusted or transported safely."""


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise AbletonHostBridgeError(f"{field} must not be empty")
    return text


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise AbletonHostBridgeError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AbletonHostBridgeError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise AbletonHostBridgeError(f"{field} must be finite")
    return number


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise AbletonHostBridgeError(f"{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AbletonHostBridgeError(f"{field} must be a non-negative integer") from exc
    if number < 0:
        raise AbletonHostBridgeError(f"{field} must be a non-negative integer")
    return number


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AbletonHostBridgeError(f"{field} must be an object")
    return value


def _exact_fields(payload: Mapping[str, object], allowed: frozenset[str], field: str) -> None:
    extra = sorted(set(payload) - allowed)
    if extra:
        raise AbletonHostBridgeError(f"{field} contains unsupported fields: {extra}")


def _sha256_fingerprint(value: object, field: str) -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    if len(text) != 64 or text != text.lower() or any(ch not in _SHA256_HEX for ch in text):
        raise AbletonHostBridgeError(f"{field} must be a lowercase SHA-256 hex digest")
    return text


def _loopback_endpoint(value: str) -> str:
    endpoint = _text(value, "endpoint")
    parsed = urlparse(endpoint)
    if parsed.scheme != "http":
        raise AbletonHostBridgeError("Ableton bridge endpoint must use http")
    if (parsed.hostname or "").casefold() not in _LOOPBACK_HOSTS:
        raise AbletonHostBridgeError("Ableton bridge endpoint must be loopback-only")
    if parsed.username is not None or parsed.password is not None:
        raise AbletonHostBridgeError("Ableton bridge endpoint must not embed credentials")
    if parsed.query or parsed.fragment or parsed.params:
        raise AbletonHostBridgeError(
            "Ableton bridge endpoint must not include params, query or fragment"
        )
    if parsed.path not in {"", "/"}:
        raise AbletonHostBridgeError("Ableton bridge endpoint must not include a path")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AbletonHostBridgeError("Ableton bridge endpoint has invalid port") from exc
    if port is None or not 1 <= port <= 65535:
        raise AbletonHostBridgeError("Ableton bridge endpoint requires an explicit valid port")
    return endpoint.rstrip("/")


@dataclass(frozen=True)
class AbletonTrackFocus:
    kind: str
    index: int | None
    name: str

    def __post_init__(self) -> None:
        kind = _text(self.kind, "selected_track.kind").upper()
        if kind not in {"TRACK", "RETURN", "MASTER"}:
            raise AbletonHostBridgeError(f"unsupported selected_track.kind: {kind}")
        index = self.index
        if kind == "MASTER":
            if index is not None:
                raise AbletonHostBridgeError("MASTER selected track must not have an index")
        else:
            index = _nonnegative_int(index, "selected_track.index")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "index", index)
        object.__setattr__(self, "name", _text(self.name, "selected_track.name"))

    @property
    def ref(self) -> str:
        if self.kind == "MASTER":
            return "master:main"
        prefix = "track" if self.kind == "TRACK" else "return"
        assert self.index is not None
        return f"{prefix}:{self.index}"


@dataclass(frozen=True)
class AbletonObservationSnapshot:
    bridge_session_id: str
    workspace_id: str | None
    set_path_fingerprint: str | None
    runtime: HostRuntimeIdentity
    observed_at_epoch_seconds: int
    tempo_bpm: float
    is_playing: bool
    current_song_time: float
    selected_track: AbletonTrackFocus | None
    adapter_id: str
    adapter_version: str

    @property
    def location_ref(self) -> str:
        if self.set_path_fingerprint is not None:
            return f"ableton-set:sha256:{self.set_path_fingerprint}"
        return f"ableton-session:{self.bridge_session_id}"

    @property
    def has_durable_set_location(self) -> bool:
        return self.set_path_fingerprint is not None

    @classmethod
    def from_payload(cls, payload: object) -> "AbletonObservationSnapshot":
        root = _object(payload, "snapshot")
        schema = root.get("schema")
        if schema == ABLETON_SNAPSHOT_SCHEMA:
            _exact_fields(root, _V2_ALLOWED_TOP_LEVEL, "snapshot")
        elif schema == ABLETON_LEGACY_SNAPSHOT_SCHEMA:
            _exact_fields(root, _BASE_ALLOWED_TOP_LEVEL, "snapshot")
        else:
            raise AbletonHostBridgeError("unsupported Ableton snapshot schema")

        adapter = _object(root.get("adapter"), "adapter")
        _exact_fields(adapter, frozenset({"id", "version"}), "adapter")
        runtime_payload = _object(root.get("runtime"), "runtime")
        _exact_fields(
            runtime_payload,
            frozenset({"host_family", "version", "edition", "os_name", "machine"}),
            "runtime",
        )
        if _text(runtime_payload.get("host_family"), "runtime.host_family").upper() != "ABLETON_LIVE":
            raise AbletonHostBridgeError("Ableton bridge may report only ABLETON_LIVE")
        runtime = HostRuntimeIdentity.from_runtime_labels(
            host_family="ABLETON_LIVE",
            version=_text(runtime_payload.get("version"), "runtime.version"),
            edition=_text(runtime_payload.get("edition"), "runtime.edition"),
            os_name=_text(runtime_payload.get("os_name"), "runtime.os_name"),
            machine=_text(runtime_payload.get("machine"), "runtime.machine"),
        )

        transport = _object(root.get("transport"), "transport")
        _exact_fields(
            transport,
            frozenset({"is_playing", "current_song_time"}),
            "transport",
        )
        if type(transport.get("is_playing")) is not bool:
            raise AbletonHostBridgeError("transport.is_playing must be bool")
        song_time = _finite(transport.get("current_song_time"), "transport.current_song_time")
        if song_time < 0:
            raise AbletonHostBridgeError("transport.current_song_time must be non-negative")

        selected_payload = root.get("selected_track")
        selected: AbletonTrackFocus | None = None
        if selected_payload is not None:
            selected_obj = _object(selected_payload, "selected_track")
            _exact_fields(
                selected_obj,
                frozenset({"kind", "index", "name"}),
                "selected_track",
            )
            selected = AbletonTrackFocus(
                kind=_text(selected_obj.get("kind"), "selected_track.kind"),
                index=selected_obj.get("index"),
                name=_text(selected_obj.get("name"), "selected_track.name"),
            )

        tempo = _finite(root.get("tempo_bpm"), "tempo_bpm")
        if not 20.0 <= tempo <= 400.0:
            raise AbletonHostBridgeError("tempo_bpm must be between 20 and 400")

        workspace = _optional_text(root.get("workspace_id"), "workspace_id")
        if workspace is not None and not workspace.startswith("wsp_"):
            raise AbletonHostBridgeError("workspace_id is not a canonical N0TE workspace ID")

        set_path_fingerprint = None
        if schema == ABLETON_SNAPSHOT_SCHEMA:
            set_path_fingerprint = _sha256_fingerprint(
                root.get("set_path_fingerprint"), "set_path_fingerprint"
            )

        return cls(
            bridge_session_id=_text(root.get("bridge_session_id"), "bridge_session_id"),
            workspace_id=workspace,
            set_path_fingerprint=set_path_fingerprint,
            runtime=runtime,
            observed_at_epoch_seconds=_nonnegative_int(
                root.get("observed_at_epoch_seconds"), "observed_at_epoch_seconds"
            ),
            tempo_bpm=tempo,
            is_playing=transport["is_playing"],
            current_song_time=song_time,
            selected_track=selected,
            adapter_id=_text(adapter.get("id"), "adapter.id"),
            adapter_version=_text(adapter.get("version"), "adapter.version"),
        )

    def capabilities(self) -> tuple[CapabilityFactInput, ...]:
        base = dict(
            route_id="n0te-ableton-remote-script",
            route_kind="HOST_NATIVE",
            display_name="N0TE Ableton Remote Script",
            availability="AVAILABLE",
            evidence_kind="RUNTIME_PROBE",
            observed_at_epoch_seconds=self.observed_at_epoch_seconds,
            locality=1.0,
            privacy=1.0,
            latency=0.9,
            reversibility=1.0,
            cost_efficiency=1.0,
        )
        return (
            CapabilityFactInput(
                capability="tempo.read",
                evidence_ref="ableton:snapshot:tempo",
                **base,
            ),
            CapabilityFactInput(
                capability="transport.read",
                evidence_ref="ableton:snapshot:transport",
                **base,
            ),
            CapabilityFactInput(
                capability="focus.track.read",
                evidence_ref="ableton:snapshot:selected-track",
                **base,
            ),
        )

    def focus_dimensions(self) -> tuple[FocusDimension, ...]:
        if self.selected_track is None:
            return ()
        return (
            FocusDimension(
                dimension="TRACK",
                state="OBSERVED_EXACT",
                refs=(self.selected_track.ref,),
                evidence_ref="ableton:snapshot:selected-track",
            ),
        )

    def shadow(self) -> ShadowObservationInput:
        events = [
            ShadowEventInput(
                object_kind="TEMPO",
                object_ref="tempo:main",
                field="bpm",
                action="SET",
                value=self.tempo_bpm,
                evidence_ref="ableton:snapshot:tempo",
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="is_playing",
                action="SET",
                value=self.is_playing,
                evidence_ref="ableton:snapshot:transport",
            ),
            ShadowEventInput(
                object_kind="TRANSPORT",
                object_ref="transport:main",
                field="current_song_time",
                action="SET",
                value=self.current_song_time,
                evidence_ref="ableton:snapshot:transport",
            ),
        ]
        if self.selected_track is not None:
            events.append(
                ShadowEventInput(
                    object_kind=self.selected_track.kind,
                    object_ref=self.selected_track.ref,
                    field="name",
                    action="SET",
                    value=self.selected_track.name,
                    evidence_ref="ableton:snapshot:selected-track",
                )
            )
        return ShadowObservationInput(
            coverage="FULL",
            actor="EXTERNAL",
            evidence_ref=f"ableton:snapshot:{self.bridge_session_id}",
            events=tuple(events),
        )


class AbletonRemoteScriptClient:
    """Read a bounded, GET-only session snapshot from N0TE's Live Remote Script."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ABLETON_BRIDGE_ENDPOINT,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        self.endpoint = _loopback_endpoint(endpoint)
        timeout = _finite(timeout_seconds, "timeout_seconds")
        if timeout <= 0 or timeout > 30:
            raise AbletonHostBridgeError("timeout_seconds must be between 0 and 30")
        self.timeout_seconds = timeout

    def fetch_snapshot(self) -> AbletonObservationSnapshot:
        request = Request(
            self.endpoint + "/snapshot",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "N0TE/ableton-read-bridge"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if int(response.status) != 200:
                    raise AbletonHostBridgeError(
                        f"Ableton bridge returned HTTP {response.status}"
                    )
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise AbletonHostBridgeError(
                        "Ableton bridge response must be application/json"
                    )
                length_header = response.headers.get("Content-Length")
                if length_header is not None:
                    try:
                        length = int(length_header)
                    except ValueError as exc:
                        raise AbletonHostBridgeError(
                            "Ableton bridge Content-Length is invalid"
                        ) from exc
                    if length < 0 or length > _MAX_SNAPSHOT_BYTES:
                        raise AbletonHostBridgeError(
                            "Ableton bridge response exceeds the snapshot bound"
                        )
                raw = response.read(_MAX_SNAPSHOT_BYTES + 1)
        except AbletonHostBridgeError:
            raise
        except Exception as exc:
            raise AbletonHostBridgeError("cannot read Ableton loopback snapshot") from exc
        if len(raw) > _MAX_SNAPSHOT_BYTES:
            raise AbletonHostBridgeError("Ableton bridge response exceeds the snapshot bound")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AbletonHostBridgeError("Ableton bridge response is not valid UTF-8 JSON") from exc
        return AbletonObservationSnapshot.from_payload(payload)

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
            known_workspace_id=snapshot.workspace_id,
            display_name="Ableton Live Set",
            provider_id=provider_id,
            capabilities=snapshot.capabilities(),
            focus_dimensions=snapshot.focus_dimensions(),
            focus_evidence_ref=f"ableton:snapshot:{snapshot.bridge_session_id}:focus",
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
