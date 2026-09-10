from __future__ import annotations

from pathlib import Path

import pytest

from n0te.ableton_bridge_installer import (
    AbletonBridgeInstallerError,
    default_user_library,
    install_bridge,
    resolve_user_library,
)
from n0te.platforms import PlatformEnvironment


def _source(path: Path, marker: str = "v1") -> Path:
    path.write_text(
        'ADAPTER_ID = "N0TEBridge"\n'
        f'VERSION_MARKER = "{marker}"\n'
        'def create_instance(c_instance):\n'
        '    return c_instance\n',
        encoding="utf-8",
    )
    return path


def test_default_user_library_uses_ableton_documented_macos_location(tmp_path: Path) -> None:
    platform = PlatformEnvironment.from_runtime_labels("Darwin", "arm64")
    assert default_user_library(platform, home=tmp_path) == (
        tmp_path / "Music" / "Ableton" / "User Library"
    )


def test_default_user_library_rejects_linux(tmp_path: Path) -> None:
    platform = PlatformEnvironment.from_runtime_labels("Linux", "x86_64")
    with pytest.raises(AbletonBridgeInstallerError, match="macOS and Windows"):
        default_user_library(platform, home=tmp_path)


def test_custom_user_library_must_exist_and_be_absolute(tmp_path: Path) -> None:
    platform = PlatformEnvironment.from_runtime_labels("Darwin", "arm64")
    user_library = tmp_path / "Custom Library"
    user_library.mkdir()
    assert resolve_user_library(
        explicit=user_library,
        environment={},
        platform=platform,
        home=tmp_path,
    ) == user_library

    with pytest.raises(AbletonBridgeInstallerError, match="must be absolute"):
        resolve_user_library(
            explicit="relative/library",
            environment={},
            platform=platform,
            home=tmp_path,
        )


def test_fresh_install_creates_remote_scripts_and_is_idempotent(tmp_path: Path) -> None:
    user_library = tmp_path / "User Library"
    user_library.mkdir()
    source = _source(tmp_path / "source.py")

    first = install_bridge(user_library, source=source)
    target = user_library / "Remote Scripts" / "N0TEBridge" / "__init__.py"
    assert first.status == "INSTALLED"
    assert first.restart_required is True
    assert target.read_bytes() == source.read_bytes()

    second = install_bridge(user_library, source=source)
    assert second.status == "CURRENT"
    assert second.restart_required is False
    assert second.source_sha256 == first.source_sha256


def test_install_updates_only_recognized_n0te_bridge(tmp_path: Path) -> None:
    user_library = tmp_path / "User Library"
    target_dir = user_library / "Remote Scripts" / "N0TEBridge"
    target_dir.mkdir(parents=True)
    target = _source(target_dir / "__init__.py", marker="old")
    source = _source(tmp_path / "source.py", marker="new")

    result = install_bridge(user_library, source=source)
    assert result.status == "UPDATED"
    assert result.restart_required is True
    assert target.read_bytes() == source.read_bytes()


def test_install_refuses_unrelated_existing_script_without_overwriting(tmp_path: Path) -> None:
    user_library = tmp_path / "User Library"
    target_dir = user_library / "Remote Scripts" / "N0TEBridge"
    target_dir.mkdir(parents=True)
    target = target_dir / "__init__.py"
    target.write_text("# unrelated control surface\n", encoding="utf-8")
    source = _source(tmp_path / "source.py")

    with pytest.raises(AbletonBridgeInstallerError, match="unrelated N0TEBridge"):
        install_bridge(user_library, source=source)
    assert target.read_text(encoding="utf-8") == "# unrelated control surface\n"


def test_install_refuses_unexpected_preexisting_directory_contents(tmp_path: Path) -> None:
    user_library = tmp_path / "User Library"
    target_dir = user_library / "Remote Scripts" / "N0TEBridge"
    target_dir.mkdir(parents=True)
    (target_dir / "other.py").write_text("unexpected", encoding="utf-8")
    source = _source(tmp_path / "source.py")

    with pytest.raises(AbletonBridgeInstallerError, match="unexpected files"):
        install_bridge(user_library, source=source)