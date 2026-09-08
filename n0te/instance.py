from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Protocol

from .platforms import PlatformEnvironment, PlatformRoots, target_tier

LEASE_SCHEMA_VERSION = 2
SUPPORTED_LEASE_SCHEMA_VERSIONS = {1, LEASE_SCHEMA_VERSION}
PROCESS_STATUSES = {"ALIVE", "DEAD", "UNKNOWN"}
ACQUIRE_STATUSES = {
    "ACQUIRED",
    "ALREADY_OWNED",
    "HELD_BY_OTHER",
    "UNCERTAIN",
    "REPLACED_STALE",
}
# One opaque marker per interpreter process. Callers may provide a stronger
# launch marker, but omitting it must never collapse exact-launch identity into
# a reusable workflow/start token.
_PROCESS_LAUNCH_MARKER = uuid.uuid4().hex


class InstanceLeaseError(RuntimeError):
    """Base error for instance lease failures."""


class InstanceLeaseCorruptionError(InstanceLeaseError):
    """Persisted lease/marker state is malformed or internally inconsistent."""


class InstanceLeaseOwnershipError(InstanceLeaseError):
    """A caller attempted to release a lease it does not exactly own."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise InstanceLeaseError(f"{field} must not be empty")
    return text


def _profile(value: str) -> str:
    profile = _text(value, "profile_id")
    if "/" in profile or "\\" in profile or profile in {".", ".."}:
        raise InstanceLeaseError("profile_id must be an opaque single path component")
    return profile


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _digest(value: str, field: str) -> str:
    token = _text(value, field).lower()
    if len(token) != 64 or any(ch not in "0123456789abcdef" for ch in token):
        raise InstanceLeaseError(f"{field} must be a 64-character hexadecimal digest")
    return token


@dataclass(frozen=True, eq=False)
class ProcessIdentity:
    platform: PlatformEnvironment
    pid: int
    start_token_fingerprint: str
    launch_marker_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.platform, PlatformEnvironment):
            raise TypeError("platform must be PlatformEnvironment")
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise InstanceLeaseError("pid must be a positive integer")
        start = _digest(self.start_token_fingerprint, "start_token_fingerprint")
        launch = self.launch_marker_fingerprint
        if launch is None:
            # Direct construction without a v2 marker exists only for schema-v1
            # compatibility. New callers should use from_start_token(), whose
            # default is exact-process-launch scoped.
            launch = start
        launch = _digest(launch, "launch_marker_fingerprint")
        object.__setattr__(self, "start_token_fingerprint", start)
        object.__setattr__(self, "launch_marker_fingerprint", launch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProcessIdentity):
            return NotImplemented
        return self.same_launch(other)

    def __hash__(self) -> int:
        return hash(self.launch_fingerprint)

    @classmethod
    def from_start_token(
        cls,
        platform: PlatformEnvironment,
        *,
        pid: int,
        start_token: str,
        launch_marker: str | None = None,
    ) -> "ProcessIdentity":
        token = _text(start_token, "start_token")
        launch = (
            _PROCESS_LAUNCH_MARKER
            if launch_marker is None
            else _text(launch_marker, "launch_marker")
        )
        return cls(
            platform=platform,
            pid=pid,
            start_token_fingerprint=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            launch_marker_fingerprint=hashlib.sha256(launch.encode("utf-8")).hexdigest(),
        )

    @property
    def platform_fingerprint(self) -> str:
        return _sha256_json(
            {
                "os_family": self.platform.os_family,
                "architecture": self.platform.architecture,
            }
        )

    @property
    def fingerprint(self) -> str:
        """Reusable workflow/liveness fingerprint, not destructive ownership."""
        return _sha256_json(
            {
                "platform_fingerprint": self.platform_fingerprint,
                "pid": self.pid,
                "start_token_fingerprint": self.start_token_fingerprint,
            }
        )

    @property
    def workflow_fingerprint(self) -> str:
        return self.fingerprint

    @property
    def launch_fingerprint(self) -> str:
        return _sha256_json(
            {
                "workflow_fingerprint": self.workflow_fingerprint,
                "launch_marker_fingerprint": self.launch_marker_fingerprint,
            }
        )

    def same_workflow(self, other: "ProcessIdentity") -> bool:
        if not isinstance(other, ProcessIdentity):
            return False
        return self.workflow_fingerprint == other.workflow_fingerprint

    def same_launch(self, other: "ProcessIdentity") -> bool:
        if not isinstance(other, ProcessIdentity):
            return False
        return self.launch_fingerprint == other.launch_fingerprint

    def to_data(self) -> dict[str, object]:
        return {
            "os_family": self.platform.os_family,
            "architecture": self.platform.architecture,
            "target_tier": self.platform.target_tier,
            "pid": self.pid,
            "start_token_fingerprint": self.start_token_fingerprint,
            "launch_marker_fingerprint": self.launch_marker_fingerprint,
            "platform_fingerprint": self.platform_fingerprint,
            "fingerprint": self.fingerprint,
            "launch_fingerprint": self.launch_fingerprint,
        }

    @classmethod
    def from_data(cls, data: object) -> "ProcessIdentity":
        if not isinstance(data, dict):
            raise InstanceLeaseCorruptionError("process identity must be an object")
        v1_required = {
            "os_family",
            "architecture",
            "target_tier",
            "pid",
            "start_token_fingerprint",
            "platform_fingerprint",
            "fingerprint",
        }
        v2_required = v1_required | {"launch_marker_fingerprint", "launch_fingerprint"}
        keys = set(data)
        if keys != v1_required and keys != v2_required:
            raise InstanceLeaseCorruptionError("process identity shape is invalid")
        try:
            platform = PlatformEnvironment(
                os_family=str(data["os_family"]),
                architecture=str(data["architecture"]),
                raw_os_name=str(data["os_family"]),
                raw_machine=str(data["architecture"]),
                target_tier=str(data["target_tier"]),
            )
            process = cls(
                platform=platform,
                pid=data["pid"],  # type: ignore[arg-type]
                start_token_fingerprint=str(data["start_token_fingerprint"]),
                launch_marker_fingerprint=(
                    str(data["launch_marker_fingerprint"])
                    if "launch_marker_fingerprint" in data
                    else str(data["start_token_fingerprint"])
                ),
            )
        except Exception as exc:
            if isinstance(exc, InstanceLeaseCorruptionError):
                raise
            raise InstanceLeaseCorruptionError("process identity is invalid") from exc
        if platform.target_tier != target_tier(platform.os_family, platform.architecture):
            raise InstanceLeaseCorruptionError("process target tier is not reproducible")
        if str(data["platform_fingerprint"]) != process.platform_fingerprint:
            raise InstanceLeaseCorruptionError("process platform fingerprint mismatch")
        if str(data["fingerprint"]) != process.fingerprint:
            raise InstanceLeaseCorruptionError("process fingerprint mismatch")
        if "launch_fingerprint" in data and str(data["launch_fingerprint"]) != process.launch_fingerprint:
            raise InstanceLeaseCorruptionError("process launch fingerprint mismatch")
        return process


class ProcessProbe(Protocol):
    def status(self, process: ProcessIdentity) -> str: ...


def _probe_status(probe: ProcessProbe, process: ProcessIdentity) -> str:
    status_value = str(probe.status(process)).strip().upper()
    if status_value not in PROCESS_STATUSES:
        raise InstanceLeaseError(f"unsupported process status: {status_value}")
    return status_value


@dataclass(frozen=True)
class InstanceLease:
    profile_id: str
    process: ProcessIdentity
    lease_nonce: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _profile(self.profile_id))
        if not isinstance(self.process, ProcessIdentity):
            raise TypeError("process must be ProcessIdentity")
        nonce = _text(self.lease_nonce, "lease_nonce").lower()
        if len(nonce) != 32 or any(ch not in "0123456789abcdef" for ch in nonce):
            raise InstanceLeaseError("lease_nonce must be 32 lowercase hexadecimal characters")
        object.__setattr__(self, "lease_nonce", nonce)

    @classmethod
    def new(cls, profile_id: str, process: ProcessIdentity) -> "InstanceLease":
        return cls(_profile(profile_id), process, uuid.uuid4().hex)

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": LEASE_SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "process": self.process.to_data(),
            "lease_nonce": self.lease_nonce,
        }

    @classmethod
    def from_data(cls, data: object) -> "InstanceLease":
        if not isinstance(data, dict):
            raise InstanceLeaseCorruptionError("instance lease must be an object")
        if set(data) != {"schema_version", "profile_id", "process", "lease_nonce"}:
            raise InstanceLeaseCorruptionError("instance lease shape is invalid")
        if data["schema_version"] not in SUPPORTED_LEASE_SCHEMA_VERSIONS:
            raise InstanceLeaseCorruptionError("unsupported instance lease schema version")
        try:
            return cls(
                profile_id=str(data["profile_id"]),
                process=ProcessIdentity.from_data(data["process"]),
                lease_nonce=str(data["lease_nonce"]),
            )
        except InstanceLeaseCorruptionError:
            raise
        except Exception as exc:
            raise InstanceLeaseCorruptionError("instance lease is invalid") from exc


@dataclass(frozen=True)
class LeaseAcquireResult:
    status: str
    lease: InstanceLease | None
    previous_lease: InstanceLease | None = None

    def __post_init__(self) -> None:
        if self.status not in ACQUIRE_STATUSES:
            raise InstanceLeaseError(f"invalid acquire status: {self.status}")
        if self.status in {"ACQUIRED", "ALREADY_OWNED", "REPLACED_STALE"}:
            if self.lease is None:
                raise InstanceLeaseError(f"{self.status} requires a lease")
        if self.status == "REPLACED_STALE" and self.previous_lease is None:
            raise InstanceLeaseError("REPLACED_STALE requires previous_lease")


@dataclass(frozen=True)
class _TakeoverMarker:
    taker: ProcessIdentity
    takeover_nonce: str
    expected_previous: InstanceLease

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": LEASE_SCHEMA_VERSION,
            "taker": self.taker.to_data(),
            "takeover_nonce": self.takeover_nonce,
            "expected_previous": self.expected_previous.to_data(),
        }

    @classmethod
    def new(
        cls, taker: ProcessIdentity, previous: InstanceLease
    ) -> "_TakeoverMarker":
        return cls(taker, uuid.uuid4().hex, previous)

    @classmethod
    def from_data(cls, data: object) -> "_TakeoverMarker":
        if not isinstance(data, dict):
            raise InstanceLeaseCorruptionError("takeover marker must be an object")
        if set(data) != {
            "schema_version",
            "taker",
            "takeover_nonce",
            "expected_previous",
        }:
            raise InstanceLeaseCorruptionError("takeover marker shape is invalid")
        if data["schema_version"] not in SUPPORTED_LEASE_SCHEMA_VERSIONS:
            raise InstanceLeaseCorruptionError("unsupported takeover marker version")
        nonce = str(data["takeover_nonce"]).strip().lower()
        if len(nonce) != 32 or any(ch not in "0123456789abcdef" for ch in nonce):
            raise InstanceLeaseCorruptionError("takeover nonce is invalid")
        return cls(
            taker=ProcessIdentity.from_data(data["taker"]),
            takeover_nonce=nonce,
            expected_previous=InstanceLease.from_data(data["expected_previous"]),
        )


def semantic_lease_ref(roots: PlatformRoots, profile_id: str) -> PurePath:
    if not isinstance(roots, PlatformRoots):
        raise TypeError("roots must be PlatformRoots")
    return roots.profile_state_root(_profile(profile_id)) / "instance" / "lease.json"


class InstanceLeaseManager:
    """Shared file-lease semantics above platform-specific process probes.

    Workflow identity is reusable for liveness probing. Exact launch identity owns
    destructive lease operations. A new launch that reuses the same PID/start
    workflow marker therefore cannot inherit an older launch's release authority.
    """

    MAX_ATTEMPTS = 8
    READ_ATTEMPTS = 8
    READ_RETRY_SECONDS = 0.001

    def __init__(self, state_root: str | Path):
        root = Path(state_root)
        if not root.is_absolute():
            raise InstanceLeaseError("state_root must be absolute")
        self.state_root = root

    def _profile_dir(self, profile_id: str) -> Path:
        return self.state_root / "profiles" / _profile(profile_id) / "instance"

    def _lease_path(self, profile_id: str) -> Path:
        return self._profile_dir(profile_id) / "lease.json"

    def _marker_path(self, profile_id: str) -> Path:
        return self._profile_dir(profile_id) / "takeover.json"

    def _stale_dir(self, profile_id: str) -> Path:
        return self._profile_dir(profile_id) / "stale"

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

    @classmethod
    def _read_json(cls, path: Path) -> object:
        """Read published lease state while tolerating only bounded write exposure.

        `_write_exclusive` must create the destination before filling it, so a
        concurrent reader can briefly observe an empty/partial UTF-8 or JSON
        payload. Retry only decoding/parsing failures for a tiny bounded window.
        Stable malformed content still becomes visible corruption after the
        bound, and symlinks/non-regular files fail immediately on every attempt.
        """
        last_decode_error: Exception | None = None
        for attempt in range(cls.READ_ATTEMPTS):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                raise
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise InstanceLeaseCorruptionError(
                    f"lease state path is not a regular file: {path}"
                )
            try:
                raw = path.read_bytes().decode("utf-8")
                return json.loads(raw)
            except FileNotFoundError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                last_decode_error = exc
                if attempt + 1 < cls.READ_ATTEMPTS:
                    time.sleep(cls.READ_RETRY_SECONDS)
                    continue
                break
            except OSError as exc:
                raise InstanceLeaseCorruptionError(
                    f"lease state is unreadable or malformed: {path}"
                ) from exc
        raise InstanceLeaseCorruptionError(
            f"lease state is unreadable or malformed: {path}"
        ) from last_decode_error

    @staticmethod
    def _write_exclusive(path: Path, data: object) -> bool:
        payload = (_canonical_json(data) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            return False
        try:
            total = 0
            while total < len(payload):
                written = os.write(fd, payload[total:])
                if written <= 0:
                    raise OSError("short write while creating lease state")
                total += written
            os.fsync(fd)
        except Exception:
            try:
                os.close(fd)
            finally:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
        else:
            os.close(fd)
        InstanceLeaseManager._fsync_dir(path.parent)
        return True

    def inspect(self, profile_id: str) -> InstanceLease | None:
        path = self._lease_path(profile_id)
        try:
            data = self._read_json(path)
        except FileNotFoundError:
            return None
        lease = InstanceLease.from_data(data)
        if lease.profile_id != _profile(profile_id):
            raise InstanceLeaseCorruptionError(
                "instance lease profile does not match its path"
            )
        return lease

    def _read_marker(self, profile_id: str) -> _TakeoverMarker | None:
        path = self._marker_path(profile_id)
        try:
            return _TakeoverMarker.from_data(self._read_json(path))
        except FileNotFoundError:
            return None

    def _remove_marker_exact(self, profile_id: str, marker: _TakeoverMarker) -> None:
        current = self._read_marker(profile_id)
        if current is None:
            return
        if (
            current.takeover_nonce != marker.takeover_nonce
            or not current.taker.same_launch(marker.taker)
        ):
            raise InstanceLeaseError("takeover marker ownership changed")
        self._marker_path(profile_id).unlink()
        self._fsync_dir(self._profile_dir(profile_id))

    def _prepare_dirs(self, profile_id: str) -> None:
        directory = self._profile_dir(profile_id)
        directory.mkdir(parents=True, exist_ok=True)
        self._stale_dir(profile_id).mkdir(exist_ok=True)

    @staticmethod
    def _reused_workflow_is_stale(
        previous: ProcessIdentity,
        taker: ProcessIdentity,
    ) -> bool:
        return previous.same_workflow(taker) and not previous.same_launch(taker)

    def _complete_owned_takeover(
        self,
        profile_id: str,
        marker: _TakeoverMarker,
        probe: ProcessProbe,
    ) -> LeaseAcquireResult | None:
        expected = marker.expected_previous
        latest = self.inspect(profile_id)

        if latest is not None and latest.process.same_launch(marker.taker):
            return LeaseAcquireResult("ALREADY_OWNED", latest)

        if latest is None:
            archive = self._stale_dir(profile_id) / f"{expected.lease_nonce}.json"
            if not archive.exists():
                raise InstanceLeaseCorruptionError(
                    "takeover marker exists but prior lease is neither active nor archived"
                )
            archived = InstanceLease.from_data(self._read_json(archive))
            if archived != expected:
                raise InstanceLeaseCorruptionError(
                    "takeover marker prior lease does not match stale archive"
                )
            replacement = InstanceLease.new(profile_id, marker.taker)
            if not self._write_exclusive(self._lease_path(profile_id), replacement.to_data()):
                return None
            return LeaseAcquireResult("REPLACED_STALE", replacement, expected)

        if latest != expected:
            return None

        # A liveness probe may only know PID plus the reusable workflow marker.
        # A different exact launch marker is therefore stronger evidence for
        # destructive ownership than a reused workflow returning ALIVE.
        if self._reused_workflow_is_stale(latest.process, marker.taker):
            status = "DEAD"
        else:
            status = _probe_status(probe, latest.process)
        if status == "ALIVE":
            return LeaseAcquireResult("HELD_BY_OTHER", latest)
        if status == "UNKNOWN":
            return LeaseAcquireResult("UNCERTAIN", latest)

        archive = self._stale_dir(profile_id) / f"{latest.lease_nonce}.json"
        if not self._write_exclusive(archive, latest.to_data()):
            archived = InstanceLease.from_data(self._read_json(archive))
            if archived != latest:
                raise InstanceLeaseCorruptionError(
                    "stale archive conflicts with active takeover target"
                )
        current = self.inspect(profile_id)
        if current != latest:
            return None
        self._lease_path(profile_id).unlink()
        self._fsync_dir(self._profile_dir(profile_id))
        return self._complete_owned_takeover(profile_id, marker, probe)

    def acquire(
        self,
        profile_id: str,
        process: ProcessIdentity,
        probe: ProcessProbe,
    ) -> LeaseAcquireResult:
        profile = _profile(profile_id)
        if not isinstance(process, ProcessIdentity):
            raise TypeError("process must be ProcessIdentity")
        self._prepare_dirs(profile)
        for _ in range(self.MAX_ATTEMPTS):
            marker = self._read_marker(profile)
            if marker is not None:
                if marker.taker.same_launch(process):
                    completed = self._complete_owned_takeover(profile, marker, probe)
                    if completed is not None:
                        self._remove_marker_exact(profile, marker)
                        return completed
                    continue
                taker_status = _probe_status(probe, marker.taker)
                if taker_status == "DEAD":
                    self._remove_marker_exact(profile, marker)
                    continue
                current = self.inspect(profile)
                if current is None:
                    return LeaseAcquireResult("UNCERTAIN", marker.expected_previous)
                return LeaseAcquireResult("UNCERTAIN", current)

            current = self.inspect(profile)
            if current is None:
                lease = InstanceLease.new(profile, process)
                if self._write_exclusive(self._lease_path(profile), lease.to_data()):
                    return LeaseAcquireResult("ACQUIRED", lease)
                continue

            if current.process.same_launch(process):
                return LeaseAcquireResult("ALREADY_OWNED", current)

            if self._reused_workflow_is_stale(current.process, process):
                current_status = "DEAD"
            else:
                current_status = _probe_status(probe, current.process)
            if current_status == "ALIVE":
                return LeaseAcquireResult("HELD_BY_OTHER", current)
            if current_status == "UNKNOWN":
                return LeaseAcquireResult("UNCERTAIN", current)

            marker = _TakeoverMarker.new(process, current)
            if self._write_exclusive(self._marker_path(profile), marker.to_data()):
                completed = self._complete_owned_takeover(profile, marker, probe)
                if completed is not None:
                    self._remove_marker_exact(profile, marker)
                    return completed
            continue
        # Bounded contention/churn is an uncertainty condition, not an
        # exceptional consumer crash. Preserve any currently visible owner and
        # let ApplicationRuntime surface the existing fail-closed UNCERTAIN path.
        return LeaseAcquireResult("UNCERTAIN", self.inspect(profile))

    def release(
        self,
        profile_id: str,
        *,
        process: ProcessIdentity,
        lease_nonce: str,
    ) -> InstanceLease:
        profile = _profile(profile_id)
        current = self.inspect(profile)
        if current is None:
            raise InstanceLeaseOwnershipError("instance lease is already absent")
        if (
            not current.process.same_launch(process)
            or current.lease_nonce != str(lease_nonce).strip().lower()
        ):
            raise InstanceLeaseOwnershipError("instance lease ownership changed")
        marker = self._read_marker(profile)
        if marker is not None:
            raise InstanceLeaseOwnershipError("instance lease has an active takeover claim")
        self._lease_path(profile).unlink()
        self._fsync_dir(self._profile_dir(profile))
        return current
