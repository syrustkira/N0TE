from __future__ import annotations

import inspect
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from n0te.platforms import PlatformEnvironment, resolve_application_roots
from n0te.uninstall import (
    ApplicationRemovalPlanner,
    HELD_SERVICES,
    ResolvedPathEvidence,
    UninstallPlanError,
)


PROFILE = "prf_" + "a" * 32


def roots(os_name: str, machine: str, home: str, environment=None):
    return resolve_application_roots(
        PlatformEnvironment.from_runtime_labels(os_name, machine),
        home=home,
        environment=environment or {},
    )


def evidence(path, ref: str, *, resolved=None, symlink: bool = False):
    return ResolvedPathEvidence(
        declared_path=path,
        resolved_path=path if resolved is None else resolved,
        is_symlink=symlink,
        evidence_ref=ref,
    )


def by_path(plan):
    return {str(entry.path): entry for entry in plan.entries}


def test_macos_retains_application_support_overlap_and_only_offers_disjoint_runtime_roots() -> None:
    r = roots("macOS", "arm64", "/Users/artist")
    proof = (
        evidence(r.cache_root, "path:cache"),
        evidence(r.log_root, "path:logs"),
    )
    plan = ApplicationRemovalPlanner.plan(
        roots=r,
        profile_id=PROFILE,
        runtime_state="STOPPED",
        path_evidence=proof,
    )
    entries = by_path(plan)

    assert entries[str(r.data_root)].classification == "RETAIN_DURABLE_DATA"
    assert entries[str(r.config_root)].classification == "RETAIN_OVERLAP"
    assert entries[str(r.state_root)].classification == "RETAIN_OVERLAP"
    assert entries[str(r.cache_root)].classification == "REMOVABLE_RUNTIME_STATE"
    assert entries[str(r.log_root)].classification == "REMOVABLE_RUNTIME_STATE"
    assert {entry.path for entry in plan.removal_candidates} == {r.cache_root, r.log_root}
    assert r.profile_data_root(PROFILE) in {entry.path for entry in plan.retained}
    assert r.profile_data_root(PROFILE) / "recovery" in {entry.path for entry in plan.retained}


def test_windows_stopped_verified_runtime_roots_are_candidates_but_data_is_not() -> None:
    r = roots(
        "Windows",
        "x86_64",
        r"C:\Users\Artist",
        {
            "APPDATA": r"C:\Users\Artist\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\Artist\AppData\Local",
        },
    )
    runtime_paths = (r.config_root, r.state_root, r.cache_root, r.log_root)
    plan = ApplicationRemovalPlanner.plan(
        roots=r,
        profile_id=PROFILE,
        runtime_state="STOPPED",
        path_evidence=tuple(
            evidence(path, f"path:{index}") for index, path in enumerate(runtime_paths)
        ),
    )
    assert {entry.path for entry in plan.removal_candidates} == set(runtime_paths)
    assert all(entry.classification == "REMOVABLE_RUNTIME_STATE" for entry in plan.removal_candidates)
    assert r.data_root not in {entry.path for entry in plan.removal_candidates}


def test_linux_nested_log_root_is_covered_by_state_root_not_a_second_removal_candidate() -> None:
    r = roots("Linux", "aarch64", "/home/artist")
    proof = tuple(
        evidence(path, f"path:{index}")
        for index, path in enumerate((r.config_root, r.state_root, r.cache_root, r.log_root))
    )
    plan = ApplicationRemovalPlanner.plan(
        roots=r,
        profile_id=PROFILE,
        runtime_state="STOPPED",
        path_evidence=proof,
    )
    entries = by_path(plan)
    assert entries[str(r.state_root)].classification == "REMOVABLE_RUNTIME_STATE"
    assert entries[str(r.log_root)].classification == "COVERED_RUNTIME_STATE"
    assert r.log_root not in {entry.path for entry in plan.removal_candidates}
    assert {entry.path for entry in plan.removal_candidates} == {
        r.config_root,
        r.state_root,
        r.cache_root,
    }


@pytest.mark.parametrize("runtime_state", ["RUNNING", "RECOVERY_REQUIRED"])
def test_runtime_state_is_not_removable_until_stopped(runtime_state: str) -> None:
    r = roots("Linux", "x86_64", "/home/artist")
    plan = ApplicationRemovalPlanner.plan(
        roots=r,
        profile_id=PROFILE,
        runtime_state=runtime_state,
        path_evidence=(
            evidence(r.config_root, "path:config"),
            evidence(r.state_root, "path:state"),
            evidence(r.cache_root, "path:cache"),
        ),
    )
    assert plan.removal_candidates == ()
    assert any(entry.classification == "BLOCKED_RUNTIME_ACTIVE" for entry in plan.entries)


def test_stopped_but_unverified_or_symlink_runtime_path_is_not_removable() -> None:
    r = roots("Linux", "x86_64", "/home/artist")
    plan = ApplicationRemovalPlanner.plan(
        roots=r,
        profile_id=PROFILE,
        runtime_state="STOPPED",
        path_evidence=(evidence(r.cache_root, "path:cache", symlink=True),),
    )
    entries = by_path(plan)
    assert entries[str(r.cache_root)].classification == "BLOCKED_UNVERIFIED_PATH"
    assert entries[str(r.config_root)].classification == "BLOCKED_UNVERIFIED_PATH"
    assert plan.removal_candidates == ()


def test_platform_managed_binary_is_descriptive_only_and_requires_exact_path_evidence() -> None:
    r = roots("Linux", "x86_64", "/home/artist")
    app_path = PurePosixPath("/opt/N0TE/n0te")
    plan = ApplicationRemovalPlanner.plan(
        roots=r,
        profile_id=PROFILE,
        runtime_state="STOPPED",
        path_evidence=(evidence(app_path, "package:path"),),
        platform_managed_paths=(app_path,),
    )
    entry = by_path(plan)[str(app_path)]
    assert entry.classification == "PLATFORM_MANAGED"
    assert entry.eligible_for_removal is False


def test_platform_managed_path_cannot_overlap_retained_or_runtime_roots() -> None:
    r = roots("Linux", "x86_64", "/home/artist")
    with pytest.raises(UninstallPlanError):
        ApplicationRemovalPlanner.plan(
            roots=r,
            profile_id=PROFILE,
            runtime_state="STOPPED",
            platform_managed_paths=(r.profile_data_root(PROFILE) / "fake-app",),
        )
    with pytest.raises(UninstallPlanError):
        ApplicationRemovalPlanner.plan(
            roots=r,
            profile_id=PROFILE,
            runtime_state="STOPPED",
            platform_managed_paths=(r.cache_root / "fake-app",),
        )


def test_duplicate_or_mismatched_path_evidence_fails_closed() -> None:
    r = roots("Linux", "x86_64", "/home/artist")
    duplicate = (
        evidence(r.cache_root, "path:one"),
        evidence(r.cache_root, "path:two"),
    )
    with pytest.raises(UninstallPlanError):
        ApplicationRemovalPlanner.plan(
            roots=r,
            profile_id=PROFILE,
            runtime_state="STOPPED",
            path_evidence=duplicate,
        )

    alias = ResolvedPathEvidence(
        declared_path=r.cache_root,
        resolved_path=PurePosixPath("/tmp/not-the-cache"),
        is_symlink=False,
        evidence_ref="path:retargeted",
    )
    plan = ApplicationRemovalPlanner.plan(
        roots=r,
        profile_id=PROFILE,
        runtime_state="STOPPED",
        path_evidence=(alias,),
    )
    assert by_path(plan)[str(r.cache_root)].classification == "BLOCKED_UNVERIFIED_PATH"


def test_windows_path_evidence_uses_windows_case_semantics() -> None:
    r = roots(
        "Windows",
        "x86_64",
        r"C:\Users\Artist",
        {"LOCALAPPDATA": r"C:\Users\Artist\AppData\Local"},
    )
    declared = r.cache_root
    resolved = PureWindowsPath(str(declared).upper())
    plan = ApplicationRemovalPlanner.plan(
        roots=r,
        profile_id=PROFILE,
        runtime_state="STOPPED",
        path_evidence=(evidence(declared, "path:case", resolved=resolved),),
    )
    assert by_path(plan)[str(r.cache_root)].classification == "REMOVABLE_RUNTIME_STATE"


def test_profile_scope_is_canonical_and_cannot_escape_data_root() -> None:
    r = roots("Linux", "x86_64", "/home/artist")
    for bad in ("artist", "../escape", "prf_short", "prf_" + "g" * 32):
        with pytest.raises(UninstallPlanError):
            ApplicationRemovalPlanner.plan(
                roots=r,
                profile_id=bad,
                runtime_state="STOPPED",
            )


def test_held_services_are_preserved_inactive_with_explicit_promotion_requirement() -> None:
    r = roots("Linux", "x86_64", "/home/artist")
    plan = ApplicationRemovalPlanner.plan(
        roots=r,
        profile_id=PROFILE,
        runtime_state="STOPPED",
    )
    assert plan.held_services == HELD_SERVICES
    assert [item.hold_id for item in plan.held_services] == [
        "HOLD-001",
        "HOLD-002",
        "HOLD-003",
        "HOLD-004",
        "HOLD-005",
        "HOLD-006",
    ]
    assert all(item.status == "HELD_NOT_ACTIVE" for item in plan.held_services)
    assert all(item.promotion_required is True for item in plan.held_services)


def test_planner_has_no_execution_or_destructive_public_surface() -> None:
    public_callables = {
        name
        for name, value in inspect.getmembers(ApplicationRemovalPlanner, callable)
        if not name.startswith("_")
    }
    assert public_callables == {"plan"}
    banned = {
        "delete",
        "remove",
        "purge",
        "unlink",
        "rmtree",
        "execute",
        "install",
        "uninstall",
        "activate",
        "upload",
        "send",
    }
    assert not (public_callables & banned)
