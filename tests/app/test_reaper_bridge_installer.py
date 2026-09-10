from __future__ import annotations

from pathlib import Path

import pytest

from n0te.platforms import PlatformEnvironment
from n0te.reaper_bridge_installer import (
    ReaperBridgeInstallerError,
    install_bridge,
    resolve_resource_path,
)


def _resource(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "reaper.ini").write_text("[reaper]\n", encoding="utf-8")
    return root


def _source(path: Path, marker="v1") -> Path:
    path.write_text(
        "-- N0TE REAPER read-side bridge.\n"
        'local SCHEMA = "n0te.reaper-observation/v1"\n'
        'local ADAPTER_ID = "n0te-reaper-reascript"\n'
        f"-- {marker}\n",
        encoding="utf-8",
    )
    return path


def test_resolve_resource_path_uses_explicit_or_unique_existing_default(tmp_path: Path):
    platform = PlatformEnvironment.from_runtime_labels("Linux", "x86_64")
    explicit = _resource(tmp_path / "custom")
    assert resolve_resource_path(
        explicit=explicit,
        environment={},
        platform=platform,
        home=tmp_path,
    ) == explicit

    default = _resource(tmp_path / ".config" / "REAPER")
    assert resolve_resource_path(
        explicit=None,
        environment={},
        platform=platform,
        home=tmp_path,
    ) == default


def test_resolve_resource_path_fails_closed_when_not_found(tmp_path: Path):
    platform = PlatformEnvironment.from_runtime_labels("Linux", "x86_64")
    with pytest.raises(ReaperBridgeInstallerError, match="Show REAPER resource path"):
        resolve_resource_path(
            explicit=None,
            environment={},
            platform=platform,
            home=tmp_path,
        )


def test_install_is_atomic_idempotent_and_returns_snapshot_path(tmp_path: Path):
    resource = _resource(tmp_path / "resource")
    source = _source(tmp_path / "source.lua")

    first = install_bridge(resource, source=source)
    assert first.status == "INSTALLED"
    assert first.target_file == resource / "Scripts" / "N0TEBridge" / "N0TEBridge.lua"
    assert first.snapshot_path == resource / "Scripts" / "N0TEBridge" / "n0te_snapshot.json"
    assert first.target_file.read_bytes() == source.read_bytes()
    assert not list(first.target_dir.glob("*.tmp"))

    second = install_bridge(resource, source=source)
    assert second.status == "CURRENT"

    updated_source = _source(tmp_path / "source-v2.lua", marker="v2")
    third = install_bridge(resource, source=updated_source)
    assert third.status == "UPDATED"
    assert third.target_file.read_bytes() == updated_source.read_bytes()


def test_installer_refuses_unrelated_existing_target(tmp_path: Path):
    resource = _resource(tmp_path / "resource")
    target_dir = resource / "Scripts" / "N0TEBridge"
    target_dir.mkdir(parents=True)
    target = target_dir / "N0TEBridge.lua"
    target.write_text("-- somebody else's script\n", encoding="utf-8")
    source = _source(tmp_path / "source.lua")

    with pytest.raises(ReaperBridgeInstallerError, match="unrelated"):
        install_bridge(resource, source=source)
    assert target.read_text(encoding="utf-8") == "-- somebody else's script\n"


def test_installer_refuses_unexpected_directory_contents(tmp_path: Path):
    resource = _resource(tmp_path / "resource")
    target_dir = resource / "Scripts" / "N0TEBridge"
    target_dir.mkdir(parents=True)
    (target_dir / "unknown.txt").write_text("keep me", encoding="utf-8")
    source = _source(tmp_path / "source.lua")

    with pytest.raises(ReaperBridgeInstallerError, match="unexpected files"):
        install_bridge(resource, source=source)
