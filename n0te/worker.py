from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from .platforms import ARCHITECTURES, PlatformEnvironment
from .support import SupportTarget

EXECUTION_MODES = {"NATIVE_ONLY", "ISOLATED_FOREIGN_ARCH"}
RESULT_STATES = {"SUCCEEDED", "FAILED", "CRASHED", "TIMED_OUT", "UNKNOWN"}


class WorkerEnvelopeError(ValueError):
    """Invalid worker identity, routing, request, or result input."""


def _text(value: str, field: str) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise WorkerEnvelopeError(f"{field} must not be empty")
    return text


def _hex(value: str, length: int, field: str) -> str:
    text = str(value).strip().lower()
    if len(text) != length or any(ch not in "0123456789abcdef" for ch in text):
        raise WorkerEnvelopeError(
            f"{field} must be a {length}-character hexadecimal digest"
        )
    return text


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _architectures(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({_text(value, "workload_architecture").upper() for value in values}))
    if not normalized:
        raise WorkerEnvelopeError("workload_architectures must not be empty")
    invalid = [value for value in normalized if value not in ARCHITECTURES or value == "UNKNOWN"]
    if invalid:
        raise WorkerEnvelopeError(
            f"unsupported workload_architectures: {invalid}"
        )
    return normalized


@dataclass(frozen=True)
class WorkerIdentity:
    worker_kind: str
    platform: PlatformEnvironment
    build_artifact_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_kind", _text(self.worker_kind, "worker_kind").upper())
        if not isinstance(self.platform, PlatformEnvironment):
            raise TypeError("platform must be PlatformEnvironment")
        if self.platform.os_family not in {"MACOS", "WINDOWS", "LINUX"}:
            raise WorkerEnvelopeError("worker platform must be MACOS, WINDOWS or LINUX")
        if self.platform.architecture == "UNKNOWN":
            raise WorkerEnvelopeError("worker architecture must be known")
        object.__setattr__(
            self,
            "build_artifact_sha256",
            _hex(self.build_artifact_sha256, 64, "build_artifact_sha256"),
        )

    @property
    def fingerprint(self) -> str:
        return _canonical_digest(
            {
                "worker_kind": self.worker_kind,
                "os_family": self.platform.os_family,
                "architecture": self.platform.architecture,
                "target_tier": self.platform.target_tier,
                "build_artifact_sha256": self.build_artifact_sha256,
            }
        )


@dataclass(frozen=True)
class WorkerCapability:
    capability_id: str
    job_id: str
    format_kind: str
    execution_mode: str
    workload_architectures: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _text(self.capability_id, "capability_id"))
        object.__setattr__(self, "job_id", _text(self.job_id, "job_id"))
        object.__setattr__(self, "format_kind", _text(self.format_kind, "format_kind").upper())
        if self.execution_mode not in EXECUTION_MODES:
            raise WorkerEnvelopeError(
                f"invalid execution_mode: {self.execution_mode}"
            )
        object.__setattr__(
            self,
            "workload_architectures",
            _architectures(self.workload_architectures),
        )


@dataclass(frozen=True)
class WorkerRequest:
    request_id: str
    worker_fingerprint: str
    target_fingerprint: str
    job_id: str
    format_kind: str
    workload_architecture: str
    payload_fingerprint: str
    timeout_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text(self.request_id, "request_id"))
        object.__setattr__(
            self,
            "worker_fingerprint",
            _hex(self.worker_fingerprint, 64, "worker_fingerprint"),
        )
        object.__setattr__(
            self,
            "target_fingerprint",
            _hex(self.target_fingerprint, 64, "target_fingerprint"),
        )
        object.__setattr__(self, "job_id", _text(self.job_id, "job_id"))
        object.__setattr__(self, "format_kind", _text(self.format_kind, "format_kind").upper())
        arch = _text(self.workload_architecture, "workload_architecture").upper()
        if arch not in ARCHITECTURES or arch == "UNKNOWN":
            raise WorkerEnvelopeError(
                f"unsupported workload_architecture: {arch}"
            )
        object.__setattr__(self, "workload_architecture", arch)
        object.__setattr__(
            self,
            "payload_fingerprint",
            _hex(self.payload_fingerprint, 64, "payload_fingerprint"),
        )
        if isinstance(self.timeout_ms, bool) or not isinstance(self.timeout_ms, int):
            raise WorkerEnvelopeError("timeout_ms must be an integer")
        if self.timeout_ms <= 0:
            raise WorkerEnvelopeError("timeout_ms must be positive")

    @property
    def fingerprint(self) -> str:
        return _canonical_digest(
            {
                "request_id": self.request_id,
                "worker_fingerprint": self.worker_fingerprint,
                "target_fingerprint": self.target_fingerprint,
                "job_id": self.job_id,
                "format_kind": self.format_kind,
                "workload_architecture": self.workload_architecture,
                "payload_fingerprint": self.payload_fingerprint,
                "timeout_ms": self.timeout_ms,
            }
        )


@dataclass(frozen=True)
class WorkerRoute:
    worker_fingerprint: str
    request_fingerprint: str
    execution_mode: str
    foreign_architecture: bool


@dataclass(frozen=True)
class WorkerResult:
    request_fingerprint: str
    worker_fingerprint: str
    state: str
    evidence_ref: str
    result_fingerprint: str | None = None
    receipt_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_fingerprint",
            _hex(self.request_fingerprint, 64, "request_fingerprint"),
        )
        object.__setattr__(
            self,
            "worker_fingerprint",
            _hex(self.worker_fingerprint, 64, "worker_fingerprint"),
        )
        if self.state not in RESULT_STATES:
            raise WorkerEnvelopeError(f"invalid worker result state: {self.state}")
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "evidence_ref"))
        if self.state == "SUCCEEDED":
            if self.result_fingerprint is None or self.receipt_ref is None:
                raise WorkerEnvelopeError(
                    "SUCCEEDED requires result_fingerprint and receipt_ref"
                )
            object.__setattr__(
                self,
                "result_fingerprint",
                _hex(self.result_fingerprint, 64, "result_fingerprint"),
            )
            object.__setattr__(self, "receipt_ref", _text(self.receipt_ref, "receipt_ref"))
        else:
            if self.result_fingerprint is not None or self.receipt_ref is not None:
                raise WorkerEnvelopeError(
                    "non-SUCCEEDED result cannot carry success result/receipt fields"
                )


class WorkerEnvelope:
    """Pure worker route/result validation; performs no subprocess or IPC work."""

    @staticmethod
    def plan(
        *,
        worker: WorkerIdentity,
        capability: WorkerCapability,
        request: WorkerRequest,
        target: SupportTarget,
    ) -> WorkerRoute:
        if not isinstance(worker, WorkerIdentity):
            raise TypeError("worker must be WorkerIdentity")
        if not isinstance(capability, WorkerCapability):
            raise TypeError("capability must be WorkerCapability")
        if not isinstance(request, WorkerRequest):
            raise TypeError("request must be WorkerRequest")
        if not isinstance(target, SupportTarget):
            raise TypeError("target must be SupportTarget")

        if request.worker_fingerprint != worker.fingerprint:
            raise WorkerEnvelopeError("request is bound to a different worker")
        if request.target_fingerprint != target.fingerprint:
            raise WorkerEnvelopeError("request is bound to a different platform target")
        if worker.platform.os_family != target.os_family:
            raise WorkerEnvelopeError("worker OS family differs from application target")
        if request.job_id != capability.job_id:
            raise WorkerEnvelopeError("request job is not provided by worker capability")
        if request.format_kind != capability.format_kind:
            raise WorkerEnvelopeError("request format is not provided by worker capability")
        if request.workload_architecture not in capability.workload_architectures:
            raise WorkerEnvelopeError(
                "workload architecture is not declared by worker capability"
            )

        foreign = worker.platform.architecture != target.architecture
        if capability.execution_mode == "NATIVE_ONLY":
            if foreign:
                raise WorkerEnvelopeError(
                    "NATIVE_ONLY worker architecture differs from application target"
                )
            if request.workload_architecture != target.architecture:
                raise WorkerEnvelopeError(
                    "NATIVE_ONLY workload architecture differs from application target"
                )
        else:
            if request.workload_architecture != worker.platform.architecture:
                raise WorkerEnvelopeError(
                    "isolated foreign-architecture workload must match worker architecture"
                )
        return WorkerRoute(
            worker_fingerprint=worker.fingerprint,
            request_fingerprint=request.fingerprint,
            execution_mode=capability.execution_mode,
            foreign_architecture=foreign,
        )

    @staticmethod
    def validate_result(
        *,
        worker: WorkerIdentity,
        request: WorkerRequest,
        result: WorkerResult,
    ) -> WorkerResult:
        if not isinstance(worker, WorkerIdentity):
            raise TypeError("worker must be WorkerIdentity")
        if not isinstance(request, WorkerRequest):
            raise TypeError("request must be WorkerRequest")
        if not isinstance(result, WorkerResult):
            raise TypeError("result must be WorkerResult")
        if result.worker_fingerprint != worker.fingerprint:
            raise WorkerEnvelopeError("result belongs to a different worker")
        if result.request_fingerprint != request.fingerprint:
            raise WorkerEnvelopeError("result belongs to a different request")
        return result
