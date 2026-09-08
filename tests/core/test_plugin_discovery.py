from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

import pytest

from n0te.plugin_discovery import (
    PluginDiscoveryError,
    PluginDiscoveryScanner,
    PluginRootObservation,
    PluginSearchRoot,
    runtime_os_family,
)


def _root(tmp_path, *, format_kind: str = "VST3") -> PluginSearchRoot:
    return PluginSearchRoot.explicit(
        os_family=runtime_os_family(),
        format_kind=format_kind,
        path=tmp_path,
    )


def test_discovers_packages_without_turning_presence_into_authority(tmp_path) -> None:
    root_path = tmp_path / "plugins"
    vendor = root_path / "Vendor"
    bundle = vendor / "Alpha.vst3"
    nested = bundle / "Contents" / "ShouldNotAppear.vst3"
    bundle.mkdir(parents=True)
    nested.mkdir(parents=True)
    file_plugin = root_path / "Beta.vst3"
    file_plugin.parent.mkdir(parents=True, exist_ok=True)
    file_plugin.write_bytes(b"not-loaded")
    (root_path / "notes.txt").write_text("ignore me", encoding="utf-8")

    report = PluginDiscoveryScanner().scan((_root(root_path),))

    names = {item.package_name for item in report.packages}
    assert names == {"Alpha.vst3", "Beta.vst3"}
    assert "ShouldNotAppear.vst3" not in names
    assert report.roots[0].state == "SCANNED"
    assert report.roots[0].package_count == 2
    for package in report.packages:
        assert package.discovery_state == "DISCOVERED"
        assert package.entitlement_state == "UNKNOWN"
        assert package.hostability_state == "UNKNOWN"
        assert package.control_state == "UNKNOWN"
        assert package.semantic_identity_state == "UNRESOLVED"
        assert package.execution_authorized is False


def test_missing_and_non_directory_roots_are_distinct(tmp_path) -> None:
    missing = tmp_path / "missing"
    missing_report = PluginDiscoveryScanner().scan((_root(missing),))
    assert missing_report.roots[0].state == "MISSING"
    assert missing_report.packages == ()

    file_root = tmp_path / "not-a-directory"
    file_root.write_text("x", encoding="utf-8")
    file_report = PluginDiscoveryScanner().scan((_root(file_root),))
    assert file_report.roots[0].state == "NOT_DIRECTORY"
    assert file_report.packages == ()


def test_entry_budget_reports_bounded_out_instead_of_silently_finishing(tmp_path) -> None:
    root_path = tmp_path / "plugins"
    root_path.mkdir()
    for index in range(4):
        (root_path / f"item-{index}.txt").write_text("x", encoding="utf-8")

    report = PluginDiscoveryScanner(max_entries_per_root=1).scan((_root(root_path),))

    assert report.bounded_out is True
    assert report.roots[0].state == "BOUNDED_OUT"
    assert report.roots[0].scanned_entries == 1


def test_depth_budget_controls_directory_descent(tmp_path) -> None:
    root_path = tmp_path / "plugins"
    plugin = root_path / "Vendor" / "Deep.vst3"
    plugin.mkdir(parents=True)
    root = _root(root_path)

    shallow = PluginDiscoveryScanner(max_depth=0).scan((root,))
    assert shallow.packages == ()

    one_level = PluginDiscoveryScanner(max_depth=1).scan((root,))
    assert {item.package_name for item in one_level.packages} == {"Deep.vst3"}


def test_directory_symlinks_are_never_recursive_scan_roots(tmp_path) -> None:
    root_path = tmp_path / "plugins"
    root_path.mkdir()
    external = tmp_path / "external"
    hidden = external / "Hidden.vst3"
    hidden.mkdir(parents=True)
    linked_directory = root_path / "linked-vendor"
    linked_package = root_path / "Linked.vst3"

    try:
        linked_directory.symlink_to(external, target_is_directory=True)
        linked_package.symlink_to(hidden, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    report = PluginDiscoveryScanner().scan((_root(root_path),))

    names = {item.package_name for item in report.packages}
    assert "Hidden.vst3" not in names
    assert names == {"Linked.vst3"}
    linked = report.packages[0]
    assert linked.artifact_kind == "SYMLINK"
    assert linked.execution_authorized is False


def test_symlink_root_is_skipped_without_following_target(tmp_path) -> None:
    target = tmp_path / "target"
    (target / "Inside.vst3").mkdir(parents=True)
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    report = PluginDiscoveryScanner().scan((_root(linked_root),))

    assert report.roots[0].state == "SYMLINK_SKIPPED"
    assert report.packages == ()


def test_duplicate_roots_do_not_duplicate_observations_or_packages(tmp_path) -> None:
    root_path = tmp_path / "plugins"
    (root_path / "Only.vst3").mkdir(parents=True)
    root = _root(root_path)

    report = PluginDiscoveryScanner().scan((root, root))

    assert len(report.roots) == 1
    assert len(report.packages) == 1


def test_scan_budget_and_observation_counts_reject_boolean_coercion(tmp_path) -> None:
    with pytest.raises(TypeError):
        PluginDiscoveryScanner(max_entries_per_root=True)
    with pytest.raises(TypeError):
        PluginDiscoveryScanner(max_depth=False)

    root = _root(tmp_path)
    with pytest.raises(TypeError):
        PluginRootObservation(root=root, state="SCANNED", scanned_entries=True, package_count=0)
    with pytest.raises(TypeError):
        PluginRootObservation(root=root, state="SCANNED", scanned_entries=0, package_count=False)


def test_explicit_roots_require_absolute_paths_and_supported_format_platform_pairs() -> None:
    family = runtime_os_family()
    with pytest.raises(PluginDiscoveryError):
        PluginSearchRoot.explicit(
            os_family=family,
            format_kind="VST3",
            path="relative/plugins",
        )

    if family == "LINUX":
        with pytest.raises(PluginDiscoveryError):
            PluginSearchRoot.explicit(
                os_family="LINUX",
                format_kind="AU",
                path="/tmp/plugins",
            )
    elif family == "WINDOWS":
        with pytest.raises(PluginDiscoveryError):
            PluginSearchRoot.explicit(
                os_family="WINDOWS",
                format_kind="LV2",
                path=r"C:\Plugins",
            )
    else:
        with pytest.raises(PluginDiscoveryError):
            PluginSearchRoot.explicit(
                os_family="MACOS",
                format_kind="LV2",
                path="/tmp/plugins",
            )


def test_scanner_refuses_to_reinterpret_foreign_platform_paths() -> None:
    family = runtime_os_family()
    if family == "WINDOWS":
        foreign = PluginSearchRoot(
            os_family="MACOS",
            format_kind="VST3",
            path=PurePosixPath("/Library/Audio/Plug-Ins/VST3"),
            source="STANDARD",
        )
    else:
        foreign = PluginSearchRoot(
            os_family="WINDOWS",
            format_kind="VST3",
            path=PureWindowsPath(r"C:\Program Files\Common Files\VST3"),
            source="STANDARD",
        )

    with pytest.raises(PluginDiscoveryError, match="cannot inspect"):
        PluginDiscoveryScanner().scan((foreign,))
