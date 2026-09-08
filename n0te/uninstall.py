from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Iterable

from .platforms import PlatformRoots

REMOVAL_CLASSES = {
    "RETAIN_DURABLE_DATA",
    "RETAIN_OVERLAP",
    "REMOVABLE_RUNTIME_STATE",
    "COVERED_RUNTIME_STATE",
    "BLOCKED_RUNTIME_ACTIVE",
    "BLOCKED_UNVERIFIED_PATH",
    "PLATFORM_MANAGED",
}
HELD_SERVICE_STATUS = "HELD_NOT_ACTIVE"
RUNTIME_STATES = {"STOPPED", "RUNNING", "RECOVERY_REQUIRED"}
_PROFILE_ID = re.compile(r"^prf_[0-9a-f]{32}$")


class UninstallPlanError(ValueError):
    """Unsafe or ambiguous shared uninstall-plan input."""


def _text(value: str, field: str) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise UninstallPlanError(f"{field} must not be empty")
    return text


def _path_key(path: PurePath) -> tuple[str, ...]:
    if isinstance(path, PureWindowsPath):
        return tuple(part.casefold() for part in path.parts)
    return tuple(path.parts)


def _is_same_or_within(path: PurePath, parent: PurePath) -> bool:
    p = _path_key(path)
    root = _path_key(parent)
    return len(p) >= len(root) and p[: len(root)] == root


def _overlap(a: PurePath, b: PurePath) -> bool:
    return _is_same_or_within(a, b) or _is_same_or_within(b, a)


def _validate_path(path: PurePath, *, roots: PlatformRoots, field: str) -> PurePath:
    expected = PureWindowsPath if roots.os_family == "WINDOWS" else PurePosixPath
    if not isinstance(path, expected):
        raise UninstallPlanError(
            f"{field} must use {'PureWindowsPath' if expected is PureWindowsPath else 'PurePosixPath'}"
        )
    if not path.is_absolute():
        raise UninstallPlanError(f"{field} must be absolute")
    if ".." in path.parts:
        raise UninstallPlanError(f"{field} must not contain parent traversal")
    return path


@dataclass(frozen=True)
class ResolvedPathEvidence:
    declared_path: PurePath
    resolved_path: PurePath
    is_symlink: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "evidence_ref"))
        if not isinstance(self.is_symlink, bool):
            raise UninstallPlanError("is_symlink must be boolean")
        if type(self.declared_path) is not type(self.resolved_path):
            raise UninstallPlanError("declared/resolved path types must match")
        if not self.declared_path.is_absolute() or not self.resolved_path.is_absolute():
            raise UninstallPlanError("path evidence requires absolute paths")
        if ".." in self.declared_path.parts or ".." in self.resolved_path.parts:
            raise UninstallPlanError("path evidence must not contain parent traversal")

    @property
    def canonical(self) -> bool:
        return not self.is_symlink and _path_key(self.declared_path) == _path_key(self.resolved_path)


@dataclass(frozen=True)
class RemovalEntry:
    path: PurePath
    classification: str
    reason: str
    eligible_for_removal: bool
    path_evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if self.classification not in REMOVAL_CLASSES:
            raise UninstallPlanError(
                f"invalid removal classification: {self.classification}"
            )
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if not isinstance(self.eligible_for_removal, bool):
            raise UninstallPlanError("eligible_for_removal must be boolean")
        if self.classification in {
            "RETAIN_DURABLE_DATA",
            "RETAIN_OVERLAP",
            "COVERED_RUNTIME_STATE",
            "BLOCKED_RUNTIME_ACTIVE",
            "BLOCKED_UNVERIFIED_PATH",
            "PLATFORM_MANAGED",
        } and self.eligible_for_removal:
            raise UninstallPlanError(
                f"{self.classification} paths cannot be direct removal candidates"
            )
        if self.path_evidence_ref is not None:
            object.__setattr__(
                self,
                "path_evidence_ref",
                _text(self.path_evidence_ref, "path_evidence_ref"),
            )


@dataclass(frozen=True)
class HeldServiceBoundary:
    hold_id: str
    capability: str
    status: str = HELD_SERVICE_STATUS
    promotion_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "hold_id", _text(self.hold_id, "hold_id"))
        object.__setattr__(self, "capability", _text(self.capability, "capability"))
        if self.status != HELD_SERVICE_STATUS:
            raise UninstallPlanError("held service status must remain HELD_NOT_ACTIVE")
        if self.promotion_required is not True:
            raise UninstallPlanError("held services require explicit promotion")


HELD_SERVICES = (
    HeldServiceBoundary("HOLD-001", "Accounts / identity / authentication"),
    HeldServiceBoundary("HOLD-002", "Cloud backup / sync"),
    HeldServiceBoundary("HOLD-003", "Subscriptions / billing"),
    HeldServiceBoundary("HOLD-004", "Product analytics / telemetry"),
    HeldServiceBoundary("HOLD-005", "Crash reporting / crash upload"),
    HeldServiceBoundary("HOLD-006", "DRM / activation / license enforcement"),
)


@dataclass(frozen=True)
class UninstallPlan:
    profile_id: str
    os_family: str
    runtime_state: str
    entries: tuple[RemovalEntry, ...]
    held_services: tuple[HeldServiceBoundary, ...]

    @property
    def removal_candidates(self) -> tuple[RemovalEntry, ...]:
        return tuple(entry for entry in self.entries if entry.eligible_for_removal)

    @property
    def retained(self) -> tuple[RemovalEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.classification in {"RETAIN_DURABLE_DATA", "RETAIN_OVERLAP"}
        )


class ApplicationRemovalPlanner:
    """Pure shared uninstall/data-retention policy.

    This class classifies paths only. It contains no filesystem deletion,
    package-removal, profile-purge, service activation, network or authority
    operation. Platform-specific uninstallers may consume a reviewed plan later.
    """

    @staticmethod
    def _evidence_for(
        path: PurePath,
        evidence: tuple[ResolvedPathEvidence, ...],
    ) -> ResolvedPathEvidence | None:
        matches = [item for item in evidence if _path_key(item.declared_path) == _path_key(path)]
        if len(matches) > 1:
            raise UninstallPlanError(f"duplicate path evidence for {path}")
        return None if not matches else matches[0]

    @classmethod
    def plan(
        cls,
        *,
        roots: PlatformRoots,
        profile_id: str,
        runtime_state: str,
        path_evidence: Iterable[ResolvedPathEvidence] = (),
        platform_managed_paths: Iterable[PurePath] = (),
    ) -> UninstallPlan:
        if not isinstance(roots, PlatformRoots):
            raise TypeError("roots must be PlatformRoots")
        profile = str(profile_id).strip().lower()
        if not _PROFILE_ID.fullmatch(profile):
            raise UninstallPlanError("profile_id must be a canonical prf_ identity")
        state = _text(runtime_state, "runtime_state").upper()
        if state not in RUNTIME_STATES:
            raise UninstallPlanError(f"unsupported runtime_state: {state}")

        evidence = tuple(path_evidence)
        for item in evidence:
            if not isinstance(item, ResolvedPathEvidence):
                raise TypeError("path_evidence must contain ResolvedPathEvidence")
            _validate_path(item.declared_path, roots=roots, field="declared_path")
            _validate_path(item.resolved_path, roots=roots, field="resolved_path")

        data_root = _validate_path(roots.data_root, roots=roots, field="data_root")
        profile_root = _validate_path(
            roots.profile_data_root(profile), roots=roots, field="profile_data_root"
        )
        recovery_root = _validate_path(
            profile_root / "recovery", roots=roots, field="profile_recovery_root"
        )

        entries: list[RemovalEntry] = [
            RemovalEntry(
                data_root,
                "RETAIN_DURABLE_DATA",
                "application data root contains canonical Artist/profile/Song state and is retained by default",
                False,
            ),
            RemovalEntry(
                profile_root,
                "RETAIN_DURABLE_DATA",
                "canonical profile data is Artist-owned durable creative state",
                False,
            ),
            RemovalEntry(
                recovery_root,
                "RETAIN_DURABLE_DATA",
                "recovery snapshots are retained with the Artist profile",
                False,
            ),
        ]

        runtime_roots = (
            ("config_root", roots.config_root),
            ("state_root", roots.state_root),
            ("cache_root", roots.cache_root),
            ("log_root", roots.log_root),
        )
        seen: set[tuple[str, ...]] = {
            _path_key(data_root),
            _path_key(profile_root),
            _path_key(recovery_root),
        }
        prior_runtime_paths: list[PurePath] = []
        for name, raw_path in runtime_roots:
            path = _validate_path(raw_path, roots=roots, field=name)
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            if _overlap(path, data_root):
                entries.append(
                    RemovalEntry(
                        path,
                        "RETAIN_OVERLAP",
                        f"{name} overlaps retained data_root; shared uninstall policy must bias toward retention",
                        False,
                    )
                )
                prior_runtime_paths.append(path)
                continue
            covering_parent = next(
                (parent for parent in prior_runtime_paths if _is_same_or_within(path, parent)),
                None,
            )
            if covering_parent is not None:
                entries.append(
                    RemovalEntry(
                        path,
                        "COVERED_RUNTIME_STATE",
                        f"{name} is nested under another runtime root ({covering_parent}); only the parent may be a removal candidate",
                        False,
                    )
                )
                prior_runtime_paths.append(path)
                continue
            if any(_is_same_or_within(parent, path) for parent in prior_runtime_paths):
                raise UninstallPlanError(
                    f"runtime root {name} contains a previously classified runtime root"
                )
            prior_runtime_paths.append(path)
            if state != "STOPPED":
                entries.append(
                    RemovalEntry(
                        path,
                        "BLOCKED_RUNTIME_ACTIVE",
                        f"{name} may be considered removable only after the application is STOPPED",
                        False,
                    )
                )
                continue
            physical = cls._evidence_for(path, evidence)
            if physical is None or not physical.canonical:
                entries.append(
                    RemovalEntry(
                        path,
                        "BLOCKED_UNVERIFIED_PATH",
                        f"{name} lacks canonical non-symlink physical-path evidence",
                        False,
                        None if physical is None else physical.evidence_ref,
                    )
                )
                continue
            entries.append(
                RemovalEntry(
                    path,
                    "REMOVABLE_RUNTIME_STATE",
                    f"{name} is disjoint from retained creative data, app is STOPPED and path identity is verified",
                    True,
                    physical.evidence_ref,
                )
            )

        managed_paths: list[PurePath] = []
        protected_or_runtime = [entry.path for entry in entries]
        for raw_path in tuple(platform_managed_paths):
            path = _validate_path(raw_path, roots=roots, field="platform_managed_path")
            if any(_overlap(path, known) for known in protected_or_runtime + managed_paths):
                raise UninstallPlanError(
                    "platform-managed application path overlaps retained/runtime/another package path"
                )
            managed_paths.append(path)
            physical = cls._evidence_for(path, evidence)
            if physical is None or not physical.canonical:
                entries.append(
                    RemovalEntry(
                        path,
                        "BLOCKED_UNVERIFIED_PATH",
                        "platform-managed path is not canonical/non-symlink verified",
                        False,
                        None if physical is None else physical.evidence_ref,
                    )
                )
            else:
                entries.append(
                    RemovalEntry(
                        path,
                        "PLATFORM_MANAGED",
                        "application/package path may be removed only by a later platform-specific uninstaller",
                        False,
                        physical.evidence_ref,
                    )
                )

        return UninstallPlan(
            profile_id=profile,
            os_family=roots.os_family,
            runtime_state=state,
            entries=tuple(entries),
            held_services=HELD_SERVICES,
        )
