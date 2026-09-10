from __future__ import annotations

from pathlib import Path

import pytest

from n0te.fl_studio_bridge_installer import (
    FLStudioBridgeInstallerError,
    bundled_bridge_source,
    default_user_data,
    install_bridge,
    resolve_user_data,
)
from n0te.platforms import PlatformEnvironment


def _platform(os_name: str, machine: str = "x86_64") -> PlatformEnvironment:
    return PlatformEnvironment.from_runtime_labels(os_name, machine)


def _source(path: Path, marker: str = "v1") -> Path:
    path.write_text(
        "# name=N0TEBridge\n"
        'ADAPTER_ID = "N0TEBridge"\n'
        'SCHEMA = "n0te.fl-studio-observation/v1"\n'
        f'MARKER = "{marker}"\n',
        encoding="utf-8",
    )
    return path


def test_default_user_data_matches_fl_user_data_root_on_windows_and_macos(tmp_path: Path):
    expected = tmp_path / "Documents" / "Image-Line" / "FL Studio"
    assert default_user_data(_platform("Windows"), home=tmp_path) == expected
    assert default_user_data(_platform("Darwin", "arm64"), home=tmp_path) == expected

    with pytest.raises(FLStudioBridgeInstallerError, match="macOS and Windows"):
        default_user_data(_platform("Linux"), home=tmp_path)


def test_resolve_user_data_supports_explicit_and_environment_custom_locations(tmp_path: Path):
    custom = tmp_path / "custom-fl-data"
    custom.mkdir()

    assert resolve_user_data(
        explicit=custom,
        environment={},
        platform=_platform("Windows"),
        home=tmp_path,
    ) == custom
    assert resolve_user_data(
        explicit=None,
        environment={"N0TE_FL_STUDIO_USER_DATA": str(custom)},
        platform=_platform("Windows"),
        home=tmp_path,
    ) == custom

    with pytest.raises(FLStudioBridgeInstallerError, match="absolute"):
        resolve_user_data(
            explicit="relative/path",
            environment={},
            platform=_platform("Windows"),
            home=tmp_path,
        )


def test_installer_is_atomic_idempotent_and_updates_only_its_own_bridge(tmp_path: Path):
    user_data = tmp_path / "FL Studio"
    user_data.mkdir()
    source_a = _source(tmp_path / "source_a.py", "a")
    source_b = _source(tmp_path / "source_b.py", "b")

    first = install_bridge(user_data, source=source_a)
    assert first.status == "INSTALLED"
    assert first.refresh_required is True
    assert first.target_file.read_bytes() == source_a.read_bytes()
    assert first.snapshot_path == first.target_dir / "n0te_snapshot.json"
    assert not list(first.target_dir.glob("*.tmp"))

    current = install_bridge(user_data, source=source_a)
    assert current.status == "CURRENT"
    assert current.refresh_required is False

    updated = install_bridge(user_data, source=source_b)
    assert updated.status == "UPDATED"
    assert updated.target_file.read_bytes() == source_b.read_bytes()
    assert not list(updated.target_dir.glob("*.tmp"))


def test_installer_refuses_unrelated_existing_script_and_unsafe_directories(tmp_path: Path):
    user_data = tmp_path / "FL Studio"
    target_dir = user_data / "Settings" / "Hardware" / "N0TEBridge"
    target_dir.mkdir(parents=True)
    target = target_dir / "device_N0TEBridge.py"
    target.write_text("# name=N0TEBridge\nprint('not n0te')\n", encoding="utf-8")
    source = _source(tmp_path / "source.py")

    with pytest.raises(FLStudioBridgeInstallerError, match="unrelated"):
        install_bridge(user_data, source=source)

    target.unlink()
    (target_dir / "unexpected.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(FLStudioBridgeInstallerError, match="unexpected files"):
        install_bridge(user_data, source=source)


def test_installer_allows_live_snapshot_file_to_coexist_with_initial_install(tmp_path: Path):
    user_data = tmp_path / "FL Studio"
    target_dir = user_data / "Settings" / "Hardware" / "N0TEBridge"
    target_dir.mkdir(parents=True)
    snapshot = target_dir / "n0te_snapshot.json"
    snapshot.write_text('{"bridge_session_id":"live"}', encoding="utf-8")
    source = _source(tmp_path / "source.py")

    result = install_bridge(user_data, source=source)
    assert result.status == "INSTALLED"
    assert snapshot.exists()
    assert result.target_file.exists()


def test_bundled_source_points_at_actual_fl_midi_script():
    source = bundled_bridge_source()
    text = source.read_text(encoding="utf-8")
    assert source.name == "device_N0TEBridge.py"
    assert text.splitlines()[0] == "# name=N0TEBridge"
    assert 'SCHEMA = "n0te.fl-studio-observation/v1"' in text
