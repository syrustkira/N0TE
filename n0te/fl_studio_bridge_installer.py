from __future__ import annotations

import argparse
import hashlib
import os
import platform as host_platform
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .platforms import PlatformEnvironment

SCRIPT_DIR_NAME = "N0TEBridge"
SCRIPT_FILE_NAME = "device_N0TEBridge.py"
SNAPSHOT_FILE_NAME = "n0te_snapshot.json"
USER_DATA_ENV = "N0TE_FL_STUDIO_USER_DATA"


class FLStudioBridgeInstallerError(RuntimeError):
    pass


@dataclass(frozen=True)
class FLStudioBridgeInstallResult:
    status: str
    user_data: Path
    target_dir: Path
    target_file: Path
    snapshot_path: Path
    source_sha256: str
    refresh_required: bool

    def __post_init__(self) -> None:
        if self.status not in {"INSTALLED", "UPDATED", "CURRENT"}:
            raise FLStudioBridgeInstallerError(f"invalid install status: {self.status}")


def default_user_data(platform: PlatformEnvironment, *, home: Path) -> Path:
    home = Path(home)
    if not home.is_absolute():
        raise FLStudioBridgeInstallerError("home must be absolute")
    if platform.os_family in {"MACOS", "WINDOWS"}:
        return home / "Documents" / "Image-Line" / "FL Studio"
    raise FLStudioBridgeInstallerError(
        "FL Studio bridge installation is supported only on macOS and Windows"
    )


def resolve_user_data(
    *,
    explicit: str | Path | None,
    environment: Mapping[str, str],
    platform: PlatformEnvironment,
    home: Path,
) -> Path:
    raw = explicit if explicit is not None else environment.get(USER_DATA_ENV)
    if raw is None or not str(raw).strip():
        path = default_user_data(platform, home=home)
    else:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            raise FLStudioBridgeInstallerError("FL Studio User data path must be absolute")
    if not path.is_dir():
        raise FLStudioBridgeInstallerError(
            "FL Studio User data folder was not found at the selected path; "
            "pass --user-data if FL Studio uses a custom User data folder"
        )
    if path.is_symlink():
        raise FLStudioBridgeInstallerError("FL Studio User data folder must not be a symlink")
    return path


def bundled_bridge_source() -> Path:
    source = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "fl_studio"
        / SCRIPT_DIR_NAME
        / SCRIPT_FILE_NAME
    )
    if not source.is_file() or source.is_symlink():
        raise FLStudioBridgeInstallerError("bundled FL Studio N0TEBridge source is unavailable")
    return source


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise FLStudioBridgeInstallerError(f"cannot read bridge file: {path}") from exc
    return digest.hexdigest()


def _existing_is_ours(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    return (
        first_line == "# name=N0TEBridge"
        and 'ADAPTER_ID = "N0TEBridge"' in text
        and 'SCHEMA = "n0te.fl-studio-observation/v1"' in text
    )


def _ensure_real_directory(parent: Path, name: str) -> Path:
    path = parent / name
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise FLStudioBridgeInstallerError(
                f"FL Studio bridge install path is not a safe directory: {path}"
            )
        return path
    try:
        path.mkdir()
    except OSError as exc:
        raise FLStudioBridgeInstallerError(
            f"cannot create FL Studio bridge install directory: {path}"
        ) from exc
    return path


def install_bridge(
    user_data: Path,
    *,
    source: Path | None = None,
) -> FLStudioBridgeInstallResult:
    user_data = Path(user_data)
    if not user_data.is_dir() or user_data.is_symlink():
        raise FLStudioBridgeInstallerError(
            "FL Studio User data folder must be an existing real directory"
        )
    source = Path(source) if source is not None else bundled_bridge_source()
    if not source.is_file() or source.is_symlink():
        raise FLStudioBridgeInstallerError("FL Studio N0TEBridge source must be a regular file")
    source_digest = _sha256(source)

    settings = _ensure_real_directory(user_data, "Settings")
    hardware = _ensure_real_directory(settings, "Hardware")
    target_dir = _ensure_real_directory(hardware, SCRIPT_DIR_NAME)
    target = target_dir / SCRIPT_FILE_NAME
    snapshot_path = target_dir / SNAPSHOT_FILE_NAME
    existed = target.exists()

    if existed:
        if not target.is_file() or target.is_symlink():
            raise FLStudioBridgeInstallerError("N0TEBridge target file is not safe to replace")
        if _sha256(target) == source_digest:
            return FLStudioBridgeInstallResult(
                "CURRENT",
                user_data,
                target_dir,
                target,
                snapshot_path,
                source_digest,
                False,
            )
        if not _existing_is_ours(target):
            raise FLStudioBridgeInstallerError(
                "an unrelated FL Studio N0TEBridge script already exists; refusing to overwrite it"
            )
    else:
        unexpected = [item.name for item in target_dir.iterdir() if item.name != SNAPSHOT_FILE_NAME]
        if unexpected:
            raise FLStudioBridgeInstallerError(
                "N0TEBridge target directory contains unexpected files; refusing to overwrite it"
            )

    temp = target_dir / ("." + SCRIPT_FILE_NAME + ".n0te-" + uuid.uuid4().hex + ".tmp")
    try:
        data = source.read_bytes()
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except Exception as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, FLStudioBridgeInstallerError):
            raise
        raise FLStudioBridgeInstallerError("FL Studio N0TEBridge installation failed") from exc

    if _sha256(target) != source_digest:
        raise FLStudioBridgeInstallerError("FL Studio N0TEBridge verification failed after installation")
    return FLStudioBridgeInstallResult(
        "UPDATED" if existed else "INSTALLED",
        user_data,
        target_dir,
        target,
        snapshot_path,
        source_digest,
        True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m n0te.fl_studio_bridge_installer")
    parser.add_argument("--user-data")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        platform = PlatformEnvironment.from_runtime_labels(
            host_platform.system(), host_platform.machine()
        )
        user_data = resolve_user_data(
            explicit=args.user_data,
            environment=os.environ,
            platform=platform,
            home=Path.home(),
        )
        result = install_bridge(user_data)
    except FLStudioBridgeInstallerError as exc:
        print(f"N0TE FL Studio bridge install error: {exc}", file=sys.stderr)
        return 2

    if result.status == "CURRENT":
        print(f"N0TEBridge is current • {result.target_dir}")
    else:
        print(f"N0TEBridge {result.status.lower()} • {result.target_dir}")
        print(
            "In FL Studio MIDI Settings, refresh MIDI scripts and select N0TEBridge for an input port."
        )
    print(f"N0TE FL snapshot • {result.snapshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
