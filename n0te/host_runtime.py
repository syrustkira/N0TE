from __future__ import annotations

import ctypes
import hashlib
import os
import platform as host_platform
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from .instance import ProcessIdentity
from .lineage import LineageStore
from .platforms import PlatformEnvironment
from .shadow import HostShadow, HostShadowError, ShadowFact
from .workspace import WorkspaceMemory

_PROFILE = re.compile(r"^prf_[0-9a-f]{32}$")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_ACTIVE_TOOL_COVERAGE = frozenset({"COMPLETE", "OBSERVED"})
_ACTIVE_TOOL_PARENT_KINDS = frozenset({"TRACK", "CHANNEL"})


class HostRuntimeError(RuntimeError):
    """Host-neutral N0TE runtime composition could not proceed safely."""


class ActiveToolProjectionError(HostRuntimeError):
    """Canonical active-tool facts are incomplete or internally inconsistent."""


@dataclass(frozen=True)
class ActiveTool:
    device_ref: str
    name: str
    parent_kind: str
    parent_ref: str
    position_kind: str | None
    position: int | None
    role: str | None
    class_name: str | None
    enabled: bool | None
    offline: bool | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ActiveToolScope:
    parent_kind: str
    parent_ref: str
    parent_name: str | None
    coverage: str
    expected_count: int | None
    tools: tuple[ActiveTool, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ActiveToolProjection:
    workspace_id: str
    song_id: str
    host_family: str
    workspace_observation_id: str
    shadow_batch_id: str
    scopes: tuple[ActiveToolScope, ...]


def _projection_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ActiveToolProjectionError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ActiveToolProjectionError(f"{field} must not be empty")
    return text


def _projection_optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _projection_text(value, field)


def _projection_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ActiveToolProjectionError(f"{field} must be a non-negative integer")
    return value


def _projection_optional_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise ActiveToolProjectionError(f"{field} must be bool")
    return value


def _projection_fields(
    facts: tuple[ShadowFact, ...],
    *,
    object_kind: str,
) -> dict[str, dict[str, ShadowFact]]:
    grouped: dict[str, dict[str, ShadowFact]] = {}
    for fact in facts:
        if fact.object_kind != object_kind:
            continue
        fields = grouped.setdefault(fact.object_ref, {})
        if fact.field in fields:
            raise ActiveToolProjectionError(
                f"duplicate {object_kind} field in current Host Shadow"
            )
        fields[fact.field] = fact
    return grouped


def project_active_tools(
    workspaces: WorkspaceMemory,
    shadow: HostShadow,
    workspace_id: str,
) -> ActiveToolProjection:
    """Normalize current verified DEVICE_PLUGIN facts without inventing inventory truth."""

    if not isinstance(workspaces, WorkspaceMemory):
        raise TypeError("workspaces must be WorkspaceMemory")
    if not isinstance(shadow, HostShadow):
        raise TypeError("shadow must be HostShadow")
    if shadow.workspaces is not workspaces:
        raise TypeError("shadow and workspaces must share WorkspaceMemory")

    workspace = workspaces.state(workspace_id)
    try:
        state = shadow.require_current(workspace_id)
    except HostShadowError as exc:
        raise ActiveToolProjectionError(
            "active tools require a CURRENT verified Host Shadow"
        ) from exc
    if state.current_workspace_observation_id != workspace.current_observation.id:
        raise ActiveToolProjectionError(
            "active tool projection crossed workspace observation identity"
        )
    if state.latest_batch_id is None:
        raise ActiveToolProjectionError(
            "current Host Shadow is missing its latest verified batch"
        )

    track_fields = _projection_fields(state.facts, object_kind="TRACK")
    device_fields = _projection_fields(state.facts, object_kind="DEVICE_PLUGIN")

    tools_by_scope: dict[tuple[str, str], list[ActiveTool]] = {}
    scope_evidence: dict[tuple[str, str], set[str]] = {}

    for device_ref, fields in device_fields.items():
        name_fact = fields.get("name")
        if name_fact is None:
            raise ActiveToolProjectionError(
                f"device {device_ref} is missing required name evidence"
            )
        name = _projection_text(name_fact.value, f"{device_ref}.name")

        parent_candidates: list[tuple[str, str, ShadowFact]] = []
        for field, kind in (("track_ref", "TRACK"), ("channel_ref", "CHANNEL")):
            fact = fields.get(field)
            if fact is not None:
                parent_candidates.append(
                    (
                        kind,
                        _projection_text(fact.value, f"{device_ref}.{field}"),
                        fact,
                    )
                )
        if len(parent_candidates) != 1:
            raise ActiveToolProjectionError(
                f"device {device_ref} requires exactly one parent relation"
            )
        parent_kind, parent_ref, parent_fact = parent_candidates[0]
        if parent_kind not in _ACTIVE_TOOL_PARENT_KINDS:
            raise ActiveToolProjectionError(
                f"device {device_ref} has unsupported parent kind"
            )
        if parent_kind == "TRACK" and parent_ref not in track_fields:
            raise ActiveToolProjectionError(
                f"device {device_ref} references an unobserved track"
            )

        index_fact = fields.get("index")
        slot_fact = fields.get("slot")
        if index_fact is not None and slot_fact is not None:
            raise ActiveToolProjectionError(
                f"device {device_ref} cannot carry both index and slot"
            )
        position_kind = None
        position = None
        position_fact = index_fact or slot_fact
        if position_fact is not None:
            position_kind = "INDEX" if index_fact is not None else "SLOT"
            position = _projection_nonnegative_int(
                position_fact.value,
                f"{device_ref}.{position_fact.field}",
            )

        role = _projection_optional_text(
            None if fields.get("role") is None else fields["role"].value,
            f"{device_ref}.role",
        )
        class_name = _projection_optional_text(
            None if fields.get("class_name") is None else fields["class_name"].value,
            f"{device_ref}.class_name",
        )
        enabled = _projection_optional_bool(
            None if fields.get("enabled") is None else fields["enabled"].value,
            f"{device_ref}.enabled",
        )
        offline = _projection_optional_bool(
            None if fields.get("offline") is None else fields["offline"].value,
            f"{device_ref}.offline",
        )
        evidence_refs = tuple(
            sorted({fact.evidence_ref for fact in fields.values()})
        )
        tool = ActiveTool(
            device_ref=device_ref,
            name=name,
            parent_kind=parent_kind,
            parent_ref=parent_ref,
            position_kind=position_kind,
            position=position,
            role=role,
            class_name=class_name,
            enabled=enabled,
            offline=offline,
            evidence_refs=evidence_refs,
        )
        key = (parent_kind, parent_ref)
        tools_by_scope.setdefault(key, []).append(tool)
        scope_evidence.setdefault(key, set()).update(evidence_refs)
        scope_evidence[key].add(parent_fact.evidence_ref)

    scope_keys = set(tools_by_scope)
    for track_ref, fields in track_fields.items():
        if "device_count" in fields:
            scope_keys.add(("TRACK", track_ref))

    scopes: list[ActiveToolScope] = []
    for parent_kind, parent_ref in sorted(scope_keys):
        fields = track_fields.get(parent_ref, {}) if parent_kind == "TRACK" else {}
        parent_name = None
        if "name" in fields:
            parent_name = _projection_text(
                fields["name"].value,
                f"{parent_ref}.name",
            )

        expected_count = None
        coverage = "OBSERVED"
        count_fact = fields.get("device_count")
        if count_fact is not None:
            expected_count = _projection_nonnegative_int(
                count_fact.value,
                f"{parent_ref}.device_count",
            )
            coverage = "COMPLETE"
            scope_evidence.setdefault((parent_kind, parent_ref), set()).add(
                count_fact.evidence_ref
            )
        if coverage not in _ACTIVE_TOOL_COVERAGE:
            raise ActiveToolProjectionError("unsupported active tool scope coverage")

        tools = list(tools_by_scope.get((parent_kind, parent_ref), ()))
        position_kinds = {
            tool.position_kind
            for tool in tools
            if tool.position_kind is not None
        }
        if len(position_kinds) > 1:
            raise ActiveToolProjectionError(
                f"scope {parent_ref} mixes incompatible position kinds"
            )
        positions = [
            tool.position for tool in tools if tool.position is not None
        ]
        if len(positions) != len(set(positions)):
            raise ActiveToolProjectionError(
                f"scope {parent_ref} contains duplicate device positions"
            )
        tools.sort(
            key=lambda tool: (
                tool.position is None,
                -1 if tool.position is None else tool.position,
                tool.device_ref,
            )
        )
        if expected_count is not None and expected_count != len(tools):
            raise ActiveToolProjectionError(
                f"scope {parent_ref} device_count does not match projected tools"
            )

        scopes.append(
            ActiveToolScope(
                parent_kind=parent_kind,
                parent_ref=parent_ref,
                parent_name=parent_name,
                coverage=coverage,
                expected_count=expected_count,
                tools=tuple(tools),
                evidence_refs=tuple(
                    sorted(scope_evidence.get((parent_kind, parent_ref), set()))
                ),
            )
        )

    return ActiveToolProjection(
        workspace_id=workspace.workspace.id,
        song_id=workspace.workspace.song_id,
        host_family=workspace.workspace.host_family,
        workspace_observation_id=workspace.current_observation.id,
        shadow_batch_id=state.latest_batch_id,
        scopes=tuple(scopes),
    )


def is_valid_profile_id(value: object) -> bool:
    return isinstance(value, str) and _PROFILE.fullmatch(value) is not None


def _profiles(
    data_root: Path,
    *,
    error_type: type[Exception] = HostRuntimeError,
) -> tuple[str, ...]:
    root = Path(data_root) / "profiles"
    if not root.is_dir():
        return ()
    try:
        return tuple(
            sorted(
                item.name
                for item in root.iterdir()
                if is_valid_profile_id(item.name)
                and (item / LineageStore.DB_NAME).is_file()
            )
        )
    except OSError as exc:
        raise error_type("cannot inspect N0TE profiles") from exc


def resolve_profile_id(
    data_root: Path,
    *,
    explicit: str | None,
    environment: Mapping[str, str],
    error_type: type[Exception] = HostRuntimeError,
) -> str:
    requested = (explicit or environment.get("N0TE_PROFILE_ID") or "").strip()
    candidates = _profiles(data_root, error_type=error_type)
    if requested:
        if not is_valid_profile_id(requested) or requested not in candidates:
            raise error_type(
                "configured N0TE profile does not exist or is invalid"
            )
        return requested
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise error_type("no existing N0TE profile was found")
    raise error_type("multiple N0TE profiles exist; choose --profile-id")


def reference_backend(environment: Mapping[str, str]) -> str | None:
    raw = (environment.get("N0TE_REFERENCE_SEARCH_BACKEND") or "").strip()
    if raw:
        return raw.casefold().replace("-", "_")
    if (environment.get("N0TE_REFERENCE_SEARCH_ENDPOINT") or "").strip():
        return "http_json"
    if (
        environment.get("N0TE_OPENAI_API_KEY")
        or environment.get("OPENAI_API_KEY")
        or ""
    ).strip():
        return "openai_web"
    return None


def reference_route_kind(environment: Mapping[str, str]) -> str | None:
    backend = reference_backend(environment)
    if backend == "openai_web":
        return "INTERNET"
    if backend != "http_json":
        return None
    endpoint = (environment.get("N0TE_REFERENCE_SEARCH_ENDPOINT") or "").strip()
    if not endpoint:
        return None
    host = (urlparse(endpoint).hostname or "").casefold()
    return "LOCALHOST" if host in _LOOPBACK else "INTERNET"


def resolve_provider_id(
    *,
    explicit: str | None,
    environment: Mapping[str, str],
    error_type: type[Exception] = HostRuntimeError,
) -> tuple[str, dict[str, str]]:
    child_env = dict(environment)
    requested = (
        explicit
        or environment.get("N0TE_REFERENCE_SEARCH_PROVIDER_ID")
        or ""
    ).strip()
    if requested:
        return requested, child_env
    backend = reference_backend(environment)
    if backend is None:
        raise error_type("no reference provider is configured")
    if not (environment.get("N0TE_REFERENCE_SEARCH_BACKEND") or "").strip():
        child_env["N0TE_REFERENCE_SEARCH_BACKEND"] = backend
    if backend == "openai_web":
        return "openai-web-reference-search", child_env
    if backend == "http_json":
        return "configured-reference-search", child_env
    raise error_type("provider id must be explicit for this backend")


def _coordinator_address(
    endpoint: str,
    *,
    error_type: type[Exception] = HostRuntimeError,
) -> tuple[str, int]:
    parsed = urlparse(str(endpoint).strip())
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "http"
        or host not in _LOOPBACK
        or parsed.path.rstrip("/") != "/mcp"
    ):
        raise error_type("coordinator endpoint must be loopback http /mcp")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise error_type("coordinator endpoint contains unsupported components")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise error_type("coordinator endpoint port is invalid") from exc
    return (
        "127.0.0.1" if host in {"localhost", "127.0.0.1"} else "::1",
        port,
    )


class SystemProcessProbe:
    """Conservative liveness proof that rejects PID reuse by start token."""

    def __init__(
        self,
        platform: PlatformEnvironment | None = None,
        *,
        error_type: type[Exception] = HostRuntimeError,
    ) -> None:
        self.platform = platform or PlatformEnvironment.from_runtime_labels(
            host_platform.system(),
            host_platform.machine(),
        )
        self.error_type = error_type

    @staticmethod
    def _posix_start(pid: int) -> tuple[str, str | None]:
        try:
            done = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            return "UNKNOWN", None
        if done.returncode != 0:
            return "DEAD", None
        token = " ".join(done.stdout.split())
        return ("ALIVE", token) if token else ("DEAD", None)

    @staticmethod
    def _windows_start(pid: int) -> tuple[str, str | None]:
        if os.name != "nt":
            return "UNKNOWN", None
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return (
                ("DEAD", None)
                if int(kernel32.GetLastError()) == 87
                else ("UNKNOWN", None)
            )
        try:
            values = [ctypes.c_ulonglong() for _ in range(4)]
            ok = kernel32.GetProcessTimes(
                handle,
                *(ctypes.byref(value) for value in values),
            )
            return (
                ("ALIVE", str(int(values[0].value)))
                if ok
                else ("UNKNOWN", None)
            )
        finally:
            kernel32.CloseHandle(handle)

    def _raw_start(self, pid: int) -> tuple[str, str | None]:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return "DEAD", None
        if self.platform.os_family in {"MACOS", "LINUX"}:
            return self._posix_start(pid)
        if self.platform.os_family == "WINDOWS":
            return self._windows_start(pid)
        return "UNKNOWN", None

    def _token(self, raw: str) -> str:
        return f"{self.platform.os_family}:{raw}"

    def current_process(self) -> ProcessIdentity:
        status, raw = self._raw_start(os.getpid())
        if status != "ALIVE" or raw is None:
            raise self.error_type(
                "cannot prove current N0TE process start identity"
            )
        return ProcessIdentity.from_start_token(
            self.platform,
            pid=os.getpid(),
            start_token=self._token(raw),
        )

    def status(self, process: ProcessIdentity) -> str:
        if process.platform.os_family != self.platform.os_family:
            return "UNKNOWN"
        status, raw = self._raw_start(process.pid)
        if status != "ALIVE" or raw is None:
            return status
        digest = hashlib.sha256(self._token(raw).encode()).hexdigest()
        return (
            "ALIVE"
            if digest == process.start_token_fingerprint
            else "DEAD"
        )


class CoordinatorProcessSupervisor:
    """Reuse or own one loopback coordinator without leaking host semantics."""

    def __init__(
        self,
        endpoint: str,
        *,
        environment: Mapping[str, str],
        popen_factory=subprocess.Popen,
        error_type: type[Exception] = HostRuntimeError,
    ) -> None:
        self.error_type = error_type
        self.host, self.port = _coordinator_address(
            endpoint,
            error_type=error_type,
        )
        self.environment = dict(environment)
        self._popen_factory = popen_factory
        self._process = None
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned

    def _listening(self) -> bool:
        try:
            with socket.create_connection(
                (self.host, self.port),
                timeout=0.15,
            ):
                return True
        except OSError:
            return False

    def start(self) -> str:
        if self._listening():
            return "REUSED"
        if reference_backend(self.environment) is None:
            raise self.error_type(
                "cannot self-start coordinator without a reference provider"
            )
        env = dict(self.environment)
        env.update(
            N0TE_MCP_TRANSPORT="streamable-http",
            N0TE_MCP_HOST=self.host,
            N0TE_MCP_PORT=str(self.port),
        )
        try:
            process = self._popen_factory(
                [sys.executable, "-m", "n0te.coordinator_mcp"],
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise self.error_type(
                "cannot start local N0TE coordinator"
            ) from exc
        self._process = process
        self._owned = True
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._process = None
                self._owned = False
                raise self.error_type(
                    "local N0TE coordinator exited during startup"
                )
            if self._listening():
                return "STARTED"
            time.sleep(0.05)
        self.stop()
        raise self.error_type(
            "local N0TE coordinator did not become reachable"
        )

    def stop(self) -> None:
        process, owned = self._process, self._owned
        self._process = None
        self._owned = False
        if not owned or process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)
