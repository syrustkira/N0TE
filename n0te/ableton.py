from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import platform as host_platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import ableton_bridge_installer
from .ableton_host_bridge import AbletonHostBridgeError, AbletonRemoteScriptClient
from .ableton_observer_service import AbletonObserverServiceError
from .ableton_session_launcher import (
    AbletonSessionLauncherError,
    SystemProcessProbe,
    _parser as _session_parser,
    build_config,
    resolve_profile_id,
    run_session,
)
from .app_runtime import ApplicationRuntime, ApplicationRuntimeError
from .authority import AuthorityService
from .coordinator_gateway import CoordinatorGatewayError
from .host_runtime import reference_route_kind
from .host_session_handshake import HostSessionHandshake
from .hosts import HostRuntimeIdentity
from .lineage import LineageError
from .musical_plan import (
    CompiledHostAction,
    HostCompilation,
    MusicalPlan,
    MusicalPlanError,
    MusicalPlanService,
)
from .musical_transactions import MusicalTransactionService
from .platforms import PlatformEnvironment, resolve_application_roots
from .song_transactions import SongTransactionService
from .transactions import (
    CompensationResult,
    PostconditionResult,
    StepExecution,
    TransactionPlan,
    TransactionReceipt,
    TransactionSnapshot,
)

TRACK_VOLUME_STATE_SCHEMA = "n0te.ableton-selected-track-volume-state/v1"
TRACK_VOLUME_ACTION_SCHEMA = "n0te.ableton-selected-track-volume-action/v1"
TRACK_VOLUME_PAYLOAD_SCHEMA = "n0te.ableton-track-volume-payload/v1"
_TRACK_VOLUME_TOLERANCE = 0.0001
_TRACK_REF_RE = re.compile(r"^(?:track|return):[0-9]+$|^master:main$")
_ACTION_RESPONSE_MAX_BYTES = 8192


class AbletonCommandError(RuntimeError):
    """The artist-facing Ableton command cannot proceed safely."""


class AbletonTrackVolumeActionError(AbletonHostBridgeError):
    """The bounded Ableton selected-track volume action could not proceed."""


class AbletonTrackVolumeRejectedError(AbletonTrackVolumeActionError):
    """The bridge proved the action stale or invalid before applying it."""


class AbletonTrackVolumeUnknownError(AbletonTrackVolumeActionError):
    """The caller cannot prove whether a submitted action changed Live."""


def _finite_unit(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise AbletonTrackVolumeActionError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AbletonTrackVolumeActionError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise AbletonTrackVolumeActionError(f"{field} must be between 0 and 1")
    return number


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise AbletonTrackVolumeActionError(f"{field} must not be empty")
    return text


def _track_ref(value: object) -> str:
    ref = _text(value, "track_ref")
    if _TRACK_REF_RE.fullmatch(ref) is None:
        raise AbletonTrackVolumeActionError(f"unsupported Ableton track ref: {ref}")
    return ref


def _json_fingerprint(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class AbletonTrackVolumeState:
    bridge_session_id: str
    track_ref: str
    normalized: float
    parameter_enabled: bool
    parameter_minimum: float
    parameter_maximum: float

    @classmethod
    def from_payload(cls, payload: object) -> "AbletonTrackVolumeState":
        if not isinstance(payload, dict):
            raise AbletonTrackVolumeActionError("track-volume state must be an object")
        expected = {
            "schema",
            "bridge_session_id",
            "track_ref",
            "normalized",
            "parameter_enabled",
            "parameter_minimum",
            "parameter_maximum",
        }
        if set(payload) != expected:
            raise AbletonTrackVolumeActionError(
                "track-volume state contains unsupported or missing fields"
            )
        if payload.get("schema") != TRACK_VOLUME_STATE_SCHEMA:
            raise AbletonTrackVolumeActionError("unsupported track-volume state schema")
        if type(payload.get("parameter_enabled")) is not bool:
            raise AbletonTrackVolumeActionError("parameter_enabled must be bool")
        try:
            minimum = float(payload.get("parameter_minimum"))
            maximum = float(payload.get("parameter_maximum"))
        except (TypeError, ValueError) as exc:
            raise AbletonTrackVolumeActionError("parameter range must be numeric") from exc
        if not math.isfinite(minimum) or not math.isfinite(maximum) or not maximum > minimum:
            raise AbletonTrackVolumeActionError("parameter range is invalid")
        return cls(
            bridge_session_id=_text(payload.get("bridge_session_id"), "bridge_session_id"),
            track_ref=_track_ref(payload.get("track_ref")),
            normalized=_finite_unit(payload.get("normalized"), "normalized"),
            parameter_enabled=payload["parameter_enabled"],
            parameter_minimum=minimum,
            parameter_maximum=maximum,
        )

    @property
    def fingerprint(self) -> str:
        return _json_fingerprint(
            {
                "schema": TRACK_VOLUME_STATE_SCHEMA,
                "bridge_session_id": self.bridge_session_id,
                "track_ref": self.track_ref,
                "normalized": self.normalized,
                "parameter_enabled": self.parameter_enabled,
                "parameter_minimum": self.parameter_minimum,
                "parameter_maximum": self.parameter_maximum,
            }
        )


@dataclass(frozen=True)
class AbletonTrackVolumeActionResult:
    bridge_session_id: str
    action_id: str
    track_ref: str
    before_normalized: float
    after_normalized: float

    @classmethod
    def from_payload(cls, payload: object) -> "AbletonTrackVolumeActionResult":
        if not isinstance(payload, dict):
            raise AbletonTrackVolumeUnknownError("track-volume response must be an object")
        expected = {
            "ok",
            "schema",
            "bridge_session_id",
            "action_id",
            "track_ref",
            "before_normalized",
            "after_normalized",
        }
        if set(payload) != expected or payload.get("ok") is not True:
            raise AbletonTrackVolumeUnknownError(
                "track-volume success response has an invalid shape"
            )
        if payload.get("schema") != TRACK_VOLUME_ACTION_SCHEMA:
            raise AbletonTrackVolumeUnknownError(
                "track-volume success response has an unsupported schema"
            )
        return cls(
            bridge_session_id=_text(payload.get("bridge_session_id"), "bridge_session_id"),
            action_id=_text(payload.get("action_id"), "action_id"),
            track_ref=_track_ref(payload.get("track_ref")),
            before_normalized=_finite_unit(payload.get("before_normalized"), "before_normalized"),
            after_normalized=_finite_unit(payload.get("after_normalized"), "after_normalized"),
        )

    @property
    def fingerprint(self) -> str:
        return _json_fingerprint(
            {
                "schema": TRACK_VOLUME_ACTION_SCHEMA,
                "bridge_session_id": self.bridge_session_id,
                "action_id": self.action_id,
                "track_ref": self.track_ref,
                "before_normalized": self.before_normalized,
                "after_normalized": self.after_normalized,
            }
        )


class AbletonTrackVolumeActionClient:
    """Strict loopback transport for N0TEBridge's single reversible setter."""

    def __init__(self, endpoint: str = "http://127.0.0.1:9799", *, timeout_seconds: float = 2.0) -> None:
        validated = AbletonRemoteScriptClient(endpoint=endpoint, timeout_seconds=timeout_seconds)
        self.endpoint = validated.endpoint
        self.timeout_seconds = validated.timeout_seconds

    @property
    def action_endpoint(self) -> str:
        return self.endpoint + "/action/selected-track-volume"

    @staticmethod
    def _decode_json(raw: bytes, *, unknown: bool) -> object:
        if len(raw) > _ACTION_RESPONSE_MAX_BYTES:
            message = "Ableton action response exceeds the bounded size"
            if unknown:
                raise AbletonTrackVolumeUnknownError(message)
            raise AbletonTrackVolumeActionError(message)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if unknown:
                raise AbletonTrackVolumeUnknownError(
                    "Ableton action response is not valid UTF-8 JSON"
                ) from exc
            raise AbletonTrackVolumeActionError(
                "Ableton action state is not valid UTF-8 JSON"
            ) from exc

    def fetch_state(self) -> AbletonTrackVolumeState:
        request = Request(
            self.action_endpoint,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "N0TE/ableton-action"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if int(response.status) != 200:
                    raise AbletonTrackVolumeActionError(
                        f"Ableton action state returned HTTP {response.status}"
                    )
                raw = response.read(_ACTION_RESPONSE_MAX_BYTES + 1)
        except AbletonTrackVolumeActionError:
            raise
        except Exception as exc:
            raise AbletonTrackVolumeActionError(
                "cannot read Ableton selected-track volume state"
            ) from exc
        return AbletonTrackVolumeState.from_payload(self._decode_json(raw, unknown=False))

    @staticmethod
    def _error_message(error: HTTPError) -> str:
        try:
            payload = json.loads(error.read(_ACTION_RESPONSE_MAX_BYTES).decode("utf-8"))
        except Exception:
            return f"HTTP {error.code}"
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            return payload["error"]
        return f"HTTP {error.code}"

    def set_selected_track_volume(
        self,
        *,
        bridge_session_id: str,
        action_id: str,
        track_ref: str,
        expected_normalized: float,
        desired_normalized: float,
    ) -> AbletonTrackVolumeActionResult:
        body = {
            "schema": TRACK_VOLUME_ACTION_SCHEMA,
            "bridge_session_id": _text(bridge_session_id, "bridge_session_id"),
            "action_id": _text(action_id, "action_id"),
            "track_ref": _track_ref(track_ref),
            "expected_normalized": _finite_unit(expected_normalized, "expected_normalized"),
            "desired_normalized": _finite_unit(desired_normalized, "desired_normalized"),
        }
        raw_body = json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        request = Request(
            self.action_endpoint,
            data=raw_body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "N0TE/ableton-action",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if int(response.status) != 200:
                    raise AbletonTrackVolumeUnknownError(
                        f"Ableton action returned HTTP {response.status}"
                    )
                raw = response.read(_ACTION_RESPONSE_MAX_BYTES + 1)
        except HTTPError as exc:
            message = self._error_message(exc)
            if exc.code in {400, 403, 404, 405, 409}:
                raise AbletonTrackVolumeRejectedError(message) from exc
            raise AbletonTrackVolumeUnknownError(message) from exc
        except AbletonTrackVolumeActionError:
            raise
        except Exception as exc:
            raise AbletonTrackVolumeUnknownError(
                "Ableton action response was lost; change state is unknown"
            ) from exc
        result = AbletonTrackVolumeActionResult.from_payload(
            self._decode_json(raw, unknown=True)
        )
        if (
            result.bridge_session_id != body["bridge_session_id"]
            or result.action_id != body["action_id"]
            or result.track_ref != body["track_ref"]
            or abs(result.before_normalized - body["expected_normalized"])
            > _TRACK_VOLUME_TOLERANCE
            or abs(result.after_normalized - body["desired_normalized"])
            > _TRACK_VOLUME_TOLERANCE
        ):
            raise AbletonTrackVolumeUnknownError(
                "Ableton action response does not match the submitted mutation"
            )
        return result


@dataclass(frozen=True)
class AbletonTrackVolumePayload:
    track_ref: str
    target_normalized: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "track_ref", _track_ref(self.track_ref))
        object.__setattr__(
            self,
            "target_normalized",
            float(f"{_finite_unit(self.target_normalized, 'target_normalized'):.6f}"),
        )

    def document(self) -> dict[str, object]:
        return {
            "schema": TRACK_VOLUME_PAYLOAD_SCHEMA,
            "track_ref": self.track_ref,
            "target_normalized": self.target_normalized,
        }

    @property
    def digest(self) -> str:
        return _json_fingerprint(self.document())

    @property
    def payload_ref(self) -> str:
        raw = json.dumps(
            self.document(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return f"ableton-track-volume:v1:{encoded}:sha256:{self.digest}"

    @classmethod
    def from_ref(cls, payload_ref: str) -> "AbletonTrackVolumePayload":
        ref = _text(payload_ref, "payload_ref")
        prefix = "ableton-track-volume:v1:"
        marker = ":sha256:"
        if not ref.startswith(prefix) or marker not in ref:
            raise AbletonTrackVolumeActionError("unsupported Ableton volume payload ref")
        encoded, claimed = ref[len(prefix):].rsplit(marker, 1)
        if len(claimed) != 64 or any(ch not in "0123456789abcdef" for ch in claimed):
            raise AbletonTrackVolumeActionError("Ableton volume payload digest is invalid")
        try:
            padding = "=" * ((4 - len(encoded) % 4) % 4)
            document = json.loads(
                base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
            )
        except Exception as exc:
            raise AbletonTrackVolumeActionError("Ableton volume payload ref is invalid") from exc
        if not isinstance(document, dict) or set(document) != {
            "schema",
            "track_ref",
            "target_normalized",
        }:
            raise AbletonTrackVolumeActionError("Ableton volume payload document is invalid")
        if document.get("schema") != TRACK_VOLUME_PAYLOAD_SCHEMA:
            raise AbletonTrackVolumeActionError("unsupported Ableton volume payload schema")
        payload = cls(
            track_ref=document.get("track_ref"),
            target_normalized=document.get("target_normalized"),
        )
        if payload.digest != claimed or payload.payload_ref != ref:
            raise AbletonTrackVolumeActionError(
                "Ableton volume payload ref failed content-address verification"
            )
        return payload


class AbletonSelectedTrackVolumeCompiler:
    """Compile one portable TRACK target into one content-addressed Live action."""

    compiler_id = "n0te-ableton-selected-track-volume"
    compiler_version = "1"
    host_family = "ABLETON_LIVE"

    def __init__(self, target_normalized: float) -> None:
        self.target_normalized = _finite_unit(target_normalized, "target_normalized")

    def compile(self, plan: MusicalPlan, runtime: HostRuntimeIdentity) -> HostCompilation:
        if not isinstance(plan, MusicalPlan):
            raise TypeError("plan must be MusicalPlan")
        if not isinstance(runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        if runtime.family != self.host_family:
            raise MusicalPlanError("Ableton volume compiler requires ABLETON_LIVE runtime")
        target = plan.target("TRACK")
        if target is None or len(target.refs) != 1:
            raise MusicalPlanError("Ableton volume compiler requires one exact TRACK target")
        track = _track_ref(target.refs[0])
        if track in plan.locked_element_refs:
            raise MusicalPlanError("Ableton volume compiler refuses a locked track")
        if plan.editable_element_refs and track not in plan.editable_element_refs:
            raise MusicalPlanError(
                "Ableton volume compiler target is outside editable_element_refs"
            )
        payload = AbletonTrackVolumePayload(track, self.target_normalized)
        digest = payload.digest
        action = CompiledHostAction(
            action_id=f"ableton-track-volume-{digest[:16]}",
            route_kind="HOST_NATIVE",
            capability="track.level.set",
            payload_ref=payload.payload_ref,
            postcondition_ref=f"ableton-track-volume-postcondition:sha256:{digest}",
            compensatable=True,
        )
        return HostCompilation(
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            compiler_id=self.compiler_id,
            compiler_version=self.compiler_version,
            host_family=self.host_family,
            host_runtime_fingerprint=runtime.fingerprint,
            actions=(action,),
            evidence_ref=f"ableton:compiler:selected-track-volume:{digest}",
        )


class AbletonSelectedTrackVolumeDriver:
    """Real CompiledActionDriver for one preconditioned Live mixer-volume change."""

    def __init__(
        self,
        action_client: AbletonTrackVolumeActionClient,
        *,
        observation_client: AbletonRemoteScriptClient | None = None,
    ) -> None:
        if not isinstance(action_client, AbletonTrackVolumeActionClient):
            raise TypeError("action_client must be AbletonTrackVolumeActionClient")
        self.action_client = action_client
        self.observation_client = observation_client or AbletonRemoteScriptClient(
            endpoint=action_client.endpoint,
            timeout_seconds=action_client.timeout_seconds,
        )
        self._snapshot_records: dict[str, dict[str, object]] = {}
        self._action_snapshot_refs: dict[str, str] = {}
        self._verified_states: dict[str, AbletonTrackVolumeState] = {}

    @staticmethod
    def _payload(action: CompiledHostAction) -> AbletonTrackVolumePayload:
        if not isinstance(action, CompiledHostAction):
            raise TypeError("action must be CompiledHostAction")
        if (
            action.route_kind != "HOST_NATIVE"
            or action.capability != "track.level.set"
            or action.compensatable is not True
        ):
            raise AbletonTrackVolumeActionError(
                "driver accepts only compensatable HOST_NATIVE track.level.set"
            )
        payload = AbletonTrackVolumePayload.from_ref(action.payload_ref)
        expected_post = f"ableton-track-volume-postcondition:sha256:{payload.digest}"
        if action.postcondition_ref != expected_post:
            raise AbletonTrackVolumeActionError(
                "compiled volume action postcondition does not match its payload"
            )
        return payload

    def prepare_snapshot(
        self,
        transaction_plan: TransactionPlan,
        musical_plan: MusicalPlan,
        compilation: HostCompilation,
    ) -> TransactionSnapshot:
        if len(compilation.actions) != 1:
            raise AbletonTrackVolumeActionError(
                "selected-track volume driver requires exactly one compiled action"
            )
        action = compilation.actions[0]
        payload = self._payload(action)
        observation = self.observation_client.fetch_snapshot()
        if observation.runtime.fingerprint != compilation.host_runtime_fingerprint:
            raise AbletonTrackVolumeActionError(
                "Ableton runtime changed before transaction snapshot"
            )
        if observation.selected_track is None or observation.selected_track.ref != payload.track_ref:
            raise AbletonTrackVolumeActionError(
                "Ableton selected track changed before transaction snapshot"
            )
        state = self.action_client.fetch_state()
        if state.bridge_session_id != observation.bridge_session_id or state.track_ref != payload.track_ref:
            raise AbletonTrackVolumeActionError(
                "Ableton action state crossed the observed Song/track identity"
            )
        if not state.parameter_enabled:
            raise AbletonTrackVolumeActionError(
                "Ableton selected-track volume parameter is disabled"
            )
        material = {
            "schema": "n0te.ableton-track-volume-transaction-snapshot/v1",
            "transaction_id": transaction_plan.transaction_id,
            "operation_id": transaction_plan.operation_id,
            "plan_id": musical_plan.plan_id,
            "compilation_plan_id": compilation.plan_id,
            "bridge_session_id": state.bridge_session_id,
            "track_ref": state.track_ref,
            "original_normalized": state.normalized,
            "target_normalized": payload.target_normalized,
            "state_fingerprint": state.fingerprint,
        }
        digest = _json_fingerprint(material)
        snapshot_ref = f"ableton:track-volume-snapshot:sha256:{digest}"
        self._snapshot_records[snapshot_ref] = material
        self._action_snapshot_refs[action.action_id] = snapshot_ref
        return TransactionSnapshot(
            transaction_plan.transaction_id,
            transaction_plan.operation_id,
            snapshot_ref,
            f"sha256:{digest}",
            f"ableton:evidence:snapshot:{digest}",
        )

    def _record_for_action(self, action: CompiledHostAction) -> dict[str, object]:
        try:
            snapshot_ref = self._action_snapshot_refs[action.action_id]
            return self._snapshot_records[snapshot_ref]
        except KeyError as exc:
            raise AbletonTrackVolumeActionError(
                "compiled action has no prepared host snapshot"
            ) from exc

    def execute_action(self, action: CompiledHostAction) -> StepExecution:
        payload = self._payload(action)
        record = self._record_for_action(action)
        try:
            result = self.action_client.set_selected_track_volume(
                bridge_session_id=str(record["bridge_session_id"]),
                action_id=action.action_id,
                track_ref=payload.track_ref,
                expected_normalized=float(record["original_normalized"]),
                desired_normalized=payload.target_normalized,
            )
        except AbletonTrackVolumeRejectedError:
            return StepExecution(
                action.action_id,
                "FAILED",
                "NOT_APPLIED",
                f"ableton:evidence:action-rejected:{action.action_id}",
            )
        except AbletonTrackVolumeUnknownError:
            return StepExecution(
                action.action_id,
                "UNKNOWN",
                "UNKNOWN",
                f"ableton:evidence:action-unknown:{action.action_id}",
            )
        return StepExecution(
            action.action_id,
            "SUCCEEDED",
            "APPLIED",
            f"ableton:evidence:action-applied:{action.action_id}",
            f"sha256:{result.fingerprint}",
        )

    def verify_action(
        self,
        action: CompiledHostAction,
        execution: StepExecution,
    ) -> PostconditionResult:
        payload = self._payload(action)
        record = self._record_for_action(action)
        try:
            state = self.action_client.fetch_state()
        except AbletonTrackVolumeActionError:
            return PostconditionResult(
                action.action_id,
                action.postcondition_ref,
                "UNKNOWN",
                f"ableton:evidence:verify-unknown:{action.action_id}",
            )
        if state.bridge_session_id != record["bridge_session_id"] or state.track_ref != payload.track_ref:
            return PostconditionResult(
                action.action_id,
                action.postcondition_ref,
                "UNKNOWN",
                f"ableton:evidence:verify-identity-drift:{action.action_id}",
            )
        if abs(state.normalized - payload.target_normalized) > _TRACK_VOLUME_TOLERANCE:
            return PostconditionResult(
                action.action_id,
                action.postcondition_ref,
                "FAILED",
                f"ableton:evidence:verify-mismatch:{action.action_id}",
            )
        self._verified_states[action.action_id] = state
        return PostconditionResult(
            action.action_id,
            action.postcondition_ref,
            "SATISFIED",
            f"ableton:evidence:verify-satisfied:{action.action_id}:{state.fingerprint}",
        )

    def compensate_action(
        self,
        action: CompiledHostAction,
        snapshot: TransactionSnapshot,
    ) -> CompensationResult:
        payload = self._payload(action)
        record = self._snapshot_records.get(snapshot.snapshot_ref)
        if record is None:
            return CompensationResult(
                action.action_id,
                snapshot.snapshot_ref,
                "UNKNOWN",
                f"ableton:evidence:compensation-snapshot-missing:{action.action_id}",
            )
        try:
            self.action_client.set_selected_track_volume(
                bridge_session_id=str(record["bridge_session_id"]),
                action_id=f"compensate:{action.action_id}",
                track_ref=payload.track_ref,
                expected_normalized=payload.target_normalized,
                desired_normalized=float(record["original_normalized"]),
            )
        except AbletonTrackVolumeRejectedError:
            return CompensationResult(
                action.action_id,
                snapshot.snapshot_ref,
                "FAILED",
                f"ableton:evidence:compensation-rejected:{action.action_id}",
            )
        except AbletonTrackVolumeUnknownError:
            return CompensationResult(
                action.action_id,
                snapshot.snapshot_ref,
                "UNKNOWN",
                f"ableton:evidence:compensation-unknown:{action.action_id}",
            )
        try:
            restored = self.action_client.fetch_state()
        except AbletonTrackVolumeActionError:
            return CompensationResult(
                action.action_id,
                snapshot.snapshot_ref,
                "UNKNOWN",
                f"ableton:evidence:compensation-verify-unknown:{action.action_id}",
            )
        if (
            restored.bridge_session_id != record["bridge_session_id"]
            or restored.track_ref != payload.track_ref
            or abs(restored.normalized - float(record["original_normalized"]))
            > _TRACK_VOLUME_TOLERANCE
        ):
            return CompensationResult(
                action.action_id,
                snapshot.snapshot_ref,
                "FAILED",
                f"ableton:evidence:compensation-not-restored:{action.action_id}",
            )
        return CompensationResult(
            action.action_id,
            snapshot.snapshot_ref,
            "RESTORED",
            f"ableton:evidence:compensation-restored:{action.action_id}:{restored.fingerprint}",
        )

    def success_receipt(
        self,
        transaction_plan: TransactionPlan,
        musical_plan: MusicalPlan,
        compilation: HostCompilation,
        snapshot: TransactionSnapshot,
    ) -> TransactionReceipt:
        if len(compilation.actions) != 1:
            raise AbletonTrackVolumeActionError(
                "selected-track volume receipt requires one compiled action"
            )
        action = compilation.actions[0]
        state = self._verified_states.get(action.action_id)
        if state is None:
            raise AbletonTrackVolumeActionError(
                "cannot issue success receipt before verified host readback"
            )
        material = {
            "schema": "n0te.ableton-track-volume-receipt/v1",
            "transaction_id": transaction_plan.transaction_id,
            "operation_id": transaction_plan.operation_id,
            "plan_fingerprint": musical_plan.fingerprint,
            "compilation_plan_id": compilation.plan_id,
            "snapshot_ref": snapshot.snapshot_ref,
            "final_state_fingerprint": state.fingerprint,
        }
        digest = _json_fingerprint(material)
        return TransactionReceipt(
            transaction_plan.transaction_id,
            transaction_plan.operation_id,
            snapshot.snapshot_ref,
            f"ableton:track-volume-receipt:sha256:{digest}",
            f"ableton:evidence:receipt:{digest}",
            f"sha256:{digest}",
        )


@dataclass(frozen=True)
class AbletonTryVolumeConfig:
    data_root: Path
    state_root: Path
    profile_id: str
    bridge_endpoint: str
    target_normalized: float

    def __post_init__(self) -> None:
        data = Path(self.data_root)
        state = Path(self.state_root)
        if not data.is_absolute() or not state.is_absolute():
            raise AbletonCommandError("try-volume roots must be absolute")
        profile = str(self.profile_id).strip()
        if not profile:
            raise AbletonCommandError("profile_id must not be empty")
        target = float(self.target_normalized)
        if not math.isfinite(target) or not 0.0 <= target <= 1.0:
            raise AbletonCommandError("target normalized volume must be between 0 and 1")
        endpoint = AbletonRemoteScriptClient(endpoint=self.bridge_endpoint).endpoint
        object.__setattr__(self, "data_root", data)
        object.__setattr__(self, "state_root", state)
        object.__setattr__(self, "profile_id", profile)
        object.__setattr__(self, "bridge_endpoint", endpoint)
        object.__setattr__(self, "target_normalized", float(f"{target:.6f}"))


def _try_volume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m n0te.ableton try-volume")
    parser.add_argument("target", type=float, help="normalized selected-track volume from 0 to 1")
    parser.add_argument("--profile-id")
    parser.add_argument("--data-root")
    parser.add_argument("--state-root")
    parser.add_argument("--bridge-endpoint", default="http://127.0.0.1:9799")
    return parser


def _build_try_volume_config(
    args: argparse.Namespace,
    *,
    environment: Mapping[str, str],
) -> AbletonTryVolumeConfig:
    platform = PlatformEnvironment.from_runtime_labels(
        host_platform.system(), host_platform.machine()
    )
    roots = resolve_application_roots(
        platform,
        home=str(Path.home()),
        environment=environment,
    )
    data = Path(args.data_root).expanduser() if args.data_root else Path(str(roots.data_root))
    state = Path(args.state_root).expanduser() if args.state_root else Path(str(roots.state_root))
    profile = resolve_profile_id(
        data,
        explicit=args.profile_id,
        environment=environment,
    )
    return AbletonTryVolumeConfig(
        data_root=data,
        state_root=state,
        profile_id=profile,
        bridge_endpoint=args.bridge_endpoint,
        target_normalized=args.target,
    )


def _known_workspace_for_snapshot(headquarters, snapshot) -> str | None:
    if snapshot.workspace_id is not None:
        return snapshot.workspace_id
    if not snapshot.has_durable_set_location:
        return None
    candidates = headquarters.workspaces.current_candidates_at_location(
        snapshot.session_location_ref
    )
    if len(candidates) > 1:
        raise AbletonCommandError(
            "multiple current workspaces claim this Ableton document session"
        )
    return None if not candidates else candidates[0].id


def _observe_volume_context(
    headquarters,
    observation_client: AbletonRemoteScriptClient,
    action_client: AbletonTrackVolumeActionClient,
    *,
    expected_bridge_session_id: str | None = None,
    expected_track_ref: str | None = None,
    expected_normalized: float | None = None,
):
    snapshot = observation_client.fetch_snapshot()
    selected = snapshot.selected_track
    if selected is None:
        raise AbletonCommandError(
            "try-volume requires one selected Ableton track, return, or master track"
        )
    state = action_client.fetch_state()
    if state.bridge_session_id != snapshot.bridge_session_id or state.track_ref != selected.ref:
        raise AbletonCommandError(
            "Ableton action state does not match the observed Live Song/selected track"
        )
    if not state.parameter_enabled:
        raise AbletonCommandError("selected-track volume parameter is disabled")
    if expected_bridge_session_id is not None and snapshot.bridge_session_id != expected_bridge_session_id:
        raise AbletonCommandError(
            "the Ableton Live Set changed during audition; refusing to act on a different Song"
        )
    if expected_track_ref is not None and selected.ref != expected_track_ref:
        raise AbletonCommandError(
            "the selected Ableton track changed during audition; refusing to act on another target"
        )
    if expected_normalized is not None and abs(state.normalized - expected_normalized) > _TRACK_VOLUME_TOLERANCE:
        raise AbletonCommandError(
            "the selected-track volume changed during audition; refusing to overwrite newer artist/manual state"
        )

    attached = HostSessionHandshake(headquarters.host_observation).attach(
        runtime=snapshot.runtime,
        location_ref=snapshot.location_ref,
        known_workspace_id=_known_workspace_for_snapshot(headquarters, snapshot),
        display_name="Ableton Live Set",
    )
    observed = headquarters.host_observation.observe(
        attached.binding,
        capabilities=snapshot.capabilities(),
        focus_dimensions=snapshot.focus_dimensions(),
        focus_evidence_ref=f"ableton:try-volume:{snapshot.bridge_session_id}:focus",
        shadow=snapshot.shadow(),
        now_epoch_seconds=snapshot.observed_at_epoch_seconds,
    )
    if observed.status != "COMPLETE" or observed.shadow.status != "CURRENT":
        raise AbletonCommandError(
            "try-volume requires a complete current Ableton host observation"
        )
    return snapshot, state, observed


def _build_volume_transaction(headquarters, snapshot, observed, target: float, *, phase: str):
    track = snapshot.selected_track
    assert track is not None
    plans = MusicalPlanService(headquarters.focus)
    plan = plans.prepare(
        observed.focus,
        artist_intent=(
            "Try the proposed selected-track level and compare by ear."
            if phase == "TRY"
            else "Restore the exact pre-try selected-track level after audition."
        ),
        required_dimensions=("TRACK",),
        desired_change=(
            f"{phase.title()} Ableton {track.name} ({track.ref}) mixer volume at normalized {target:.6f}."
        ),
        constraints=("Preserve every other observed Live target and parameter.",),
        editable_element_refs=(track.ref,),
        verification_refs=(f"verify:ableton:selected-track-volume:{target:.6f}",),
        provenance_refs=(
            f"artist-cli:ableton:{phase.casefold()}:{snapshot.bridge_session_id}",
        ),
    )
    compilation = plans.compile(
        plan,
        observed.focus,
        snapshot.runtime,
        AbletonSelectedTrackVolumeCompiler(target),
    )
    transactions = MusicalTransactionService(
        plans,
        SongTransactionService(headquarters.transactions),
    )
    intent = transactions.preview(plan, observed.focus, compilation)
    return plans, plan, compilation, transactions, intent


def _confirmation(input_fn: Callable[[str], str], prompt: str) -> str:
    try:
        return str(input_fn(prompt)).strip()
    except EOFError as exc:
        raise AbletonCommandError(
            "interactive confirmation ended before an artist decision was recorded"
        ) from exc


def _render_preview(
    output: Callable[[str], None],
    *,
    phase: str,
    track_name: str,
    track_ref: str,
    current: float,
    target: float,
    intent,
) -> str:
    token = f"{phase} {intent.intent_fingerprint[:12]}"
    output(f"N0TE {phase} preview • {track_name} • {track_ref}")
    output(f"current normalized volume: {current:.6f}")
    output(f"proposed normalized volume: {target:.6f}")
    output(f"authority: {intent.action_class} • intent {intent.intent_fingerprint}")
    output(f"Type exactly: {token}")
    return token


def _execute_volume_transaction(
    *,
    transactions: MusicalTransactionService,
    plan: MusicalPlan,
    context,
    compilation: HostCompilation,
    intent,
    token: str,
    phase: str,
    action_client: AbletonTrackVolumeActionClient,
    observation_client: AbletonRemoteScriptClient,
):
    approval = AuthorityService.bind_approval(
        intent,
        f"artist-cli:{phase.casefold()}:typed:{token}",
    )
    prepared = transactions.prepare(
        plan,
        context,
        compilation,
        approval,
        idempotency_key=f"artist-cli:{phase.casefold()}:{approval.approval_id}",
        claim_evidence_ref=f"artist-cli:{phase.casefold()}:approval:{approval.approval_id}",
    )
    driver = AbletonSelectedTrackVolumeDriver(
        action_client,
        observation_client=observation_client,
    )
    return transactions.run(prepared, plan, context, compilation, driver)


def _result_code(result, output: Callable[[str], None], *, phase: str) -> int:
    if result.status == "COMPLETE":
        return 0
    if result.status == "COMPENSATED":
        output(
            f"N0TE {phase} did not complete; the known applied change was restored when possible."
        )
        return 2
    output(
        f"N0TE {phase} outcome is uncertain. Inspect Ableton Live before any retry; recovery review is required."
    )
    return 3


def run_try_volume(
    config: AbletonTryVolumeConfig,
    *,
    process_probe=None,
    runtime_factory=ApplicationRuntime,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> int:
    if not isinstance(config, AbletonTryVolumeConfig):
        raise TypeError("config must be AbletonTryVolumeConfig")
    probe = process_probe or SystemProcessProbe()
    runtime = runtime_factory(data_root=config.data_root, state_root=config.state_root)
    launch = runtime.launch(
        profile_id=config.profile_id,
        process=probe.current_process(),
        probe=probe,
    )
    if launch.status != "STARTED":
        reason = f" ({launch.reason})" if getattr(launch, "reason", None) else ""
        raise AbletonCommandError(
            f"cannot own N0TE profile runtime: {launch.status}{reason}"
        )

    primary_error = None
    try:
        headquarters = runtime.headquarters
        song = headquarters.store.active_song()
        if song is None:
            raise AbletonCommandError("selected profile has no active Song")
        observation_client = AbletonRemoteScriptClient(config.bridge_endpoint)
        action_client = AbletonTrackVolumeActionClient(config.bridge_endpoint)
        snapshot, state, observed = _observe_volume_context(
            headquarters,
            observation_client,
            action_client,
        )
        track = snapshot.selected_track
        assert track is not None
        original = state.normalized
        target = config.target_normalized
        if abs(original - target) <= _TRACK_VOLUME_TOLERANCE:
            output(
                f"N0TE TRY • {track.name} is already at normalized {target:.6f}; no mutation or approval created."
            )
            return 0

        _, plan, compilation, transactions, intent = _build_volume_transaction(
            headquarters,
            snapshot,
            observed,
            target,
            phase="TRY",
        )
        apply_token = _render_preview(
            output,
            phase="APPLY",
            track_name=track.name,
            track_ref=track.ref,
            current=original,
            target=target,
            intent=intent,
        )
        typed = _confirmation(
            input_fn,
            f"Type {apply_token} to apply the try, anything else cancels: ",
        )
        if typed != apply_token:
            output("N0TE TRY cancelled • Ableton Live was not changed.")
            return 0

        result = _execute_volume_transaction(
            transactions=transactions,
            plan=plan,
            context=observed.focus,
            compilation=compilation,
            intent=intent,
            token=typed,
            phase="TRY",
            action_client=action_client,
            observation_client=observation_client,
        )
        code = _result_code(result, output, phase="TRY")
        if code:
            return code
        output(
            f"N0TE TRY applied and verified • {track.name}: {original:.6f} → {target:.6f}"
        )

        while True:
            decision = _confirmation(
                input_fn,
                "Hear it in Ableton Live. Type KEEP to keep the tried level or RESTORE to return to the captured original: ",
            ).upper()
            if decision in {"KEEP", "RESTORE"}:
                break
            output("Decision not recorded. Type exactly KEEP or RESTORE.")

        if decision == "KEEP":
            output(
                f"N0TE DECIDE • KEEP • verified tried level {target:.6f} remains on {track.name}."
            )
            return 0

        restore_snapshot, restore_state, restore_observed = _observe_volume_context(
            headquarters,
            observation_client,
            action_client,
            expected_bridge_session_id=snapshot.bridge_session_id,
            expected_track_ref=track.ref,
            expected_normalized=target,
        )
        _, restore_plan, restore_compilation, restore_transactions, restore_intent = (
            _build_volume_transaction(
                headquarters,
                restore_snapshot,
                restore_observed,
                original,
                phase="RESTORE",
            )
        )
        restore_token = _render_preview(
            output,
            phase="RESTORE",
            track_name=track.name,
            track_ref=track.ref,
            current=restore_state.normalized,
            target=original,
            intent=restore_intent,
        )
        typed_restore = _confirmation(
            input_fn,
            f"Type {restore_token} to restore the captured original, anything else leaves the tried level in place: ",
        )
        if typed_restore != restore_token:
            output(
                f"N0TE RESTORE cancelled • tried level {target:.6f} remains on {track.name}."
            )
            return 0

        restore_result = _execute_volume_transaction(
            transactions=restore_transactions,
            plan=restore_plan,
            context=restore_observed.focus,
            compilation=restore_compilation,
            intent=restore_intent,
            token=typed_restore,
            phase="RESTORE",
            action_client=action_client,
            observation_client=observation_client,
        )
        code = _result_code(restore_result, output, phase="RESTORE")
        if code:
            return code
        output(
            f"N0TE DECIDE • RESTORED • {track.name}: {target:.6f} → {original:.6f}"
        )
        return 0
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        quit_result = runtime.quit()
        if primary_error is None and quit_result.status != "STOPPED":
            raise AbletonCommandError(
                "N0TE runtime could not release its profile lease cleanly"
            )


def run_try_volume_command(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> int:
    config = _build_try_volume_config(
        _try_volume_parser().parse_args(list(argv)),
        environment=environment,
    )
    return run_try_volume(config, input_fn=input_fn, output=output)


def _reference_route_kind(environment: Mapping[str, str]) -> str | None:
    return reference_route_kind(environment)


def validate_network_preflight(environment: Mapping[str, str]) -> None:
    """Require an explicit connected choice before internet discovery."""
    if _reference_route_kind(environment) != "INTERNET":
        return
    mode = (environment.get("N0TE_NETWORK_MODE") or "OFFLINE").strip().upper()
    if mode != "CONNECTED":
        raise AbletonCommandError(
            "internet reference discovery is configured while N0TE network policy is not CONNECTED; "
            "set N0TE_NETWORK_MODE=CONNECTED to allow this session"
        )


def run_command(argv: Sequence[str], *, environment: Mapping[str, str]) -> int:
    validate_network_preflight(environment)
    parser = _session_parser()
    parser.prog = "python -m n0te.ableton"
    args = parser.parse_args(list(argv))
    config, coordinator_env = build_config(args, environment=environment)
    return run_session(config, coordinator_environment=coordinator_env)


def _split_command(argv: Sequence[str]) -> tuple[str, list[str]]:
    args = list(argv)
    if args and args[0] == "install":
        return "install", args[1:]
    if args and args[0] == "try-volume":
        return "try-volume", args[1:]
    if args and args[0] == "run":
        return "run", args[1:]
    return "run", args


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    command, rest = _split_command(sys.argv[1:] if argv is None else argv)
    if command == "install":
        return ableton_bridge_installer.main(rest)

    env = dict(os.environ if environment is None else environment)
    try:
        if command == "try-volume":
            return run_try_volume_command(rest, environment=env)
        return run_command(rest, environment=env)
    except AbletonTrackVolumeUnknownError as exc:
        print(f"N0TE Ableton action outcome uncertain: {exc}", file=sys.stderr)
        print("Inspect Ableton Live before retrying this action.", file=sys.stderr)
        return 3
    except AbletonHostBridgeError as exc:
        print(f"N0TE Ableton session error: {exc}", file=sys.stderr)
        print(
            "Install or refresh the Live bridge with: python -m n0te.ableton install",
            file=sys.stderr,
        )
        return 2
    except (
        AbletonCommandError,
        AbletonSessionLauncherError,
        AbletonObserverServiceError,
        ApplicationRuntimeError,
        CoordinatorGatewayError,
        LineageError,
        MusicalPlanError,
    ) as exc:
        print(f"N0TE Ableton session error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
