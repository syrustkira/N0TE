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

SCRIPT_NAME = "N0TEBridge"
REMOTE_SCRIPTS_DIR = "Remote Scripts"
USER_LIBRARY_ENV = "N0TE_ABLETON_USER_LIBRARY"


class AbletonBridgeInstallerError(RuntimeError):
    pass


@dataclass(frozen=True)
class BridgeInstallResult:
    status: str
    user_library: Path
    target_dir: Path
    source_sha256: str
    restart_required: bool

    def __post_init__(self) -> None:
        if self.status not in {"INSTALLED", "UPDATED", "CURRENT"}:
            raise AbletonBridgeInstallerError(f"invalid install status: {self.status}")


def default_user_library(platform: PlatformEnvironment, *, home: Path) -> Path:
    home = Path(home)
    if not home.is_absolute():
        raise AbletonBridgeInstallerError("home must be absolute")
    if platform.os_family == "MACOS":
        return home / "Music" / "Ableton" / "User Library"
    if platform.os_family == "WINDOWS":
        return home / "Documents" / "Ableton" / "User Library"
    raise AbletonBridgeInstallerError(
        "Ableton Live bridge installation is supported only on macOS and Windows"
    )


def resolve_user_library(
    *,
    explicit: str | Path | None,
    environment: Mapping[str, str],
    platform: PlatformEnvironment,
    home: Path,
) -> Path:
    raw = explicit if explicit is not None else environment.get(USER_LIBRARY_ENV)
    if raw is None or not str(raw).strip():
        path = default_user_library(platform, home=home)
    else:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            raise AbletonBridgeInstallerError("Ableton User Library path must be absolute")
    if not path.is_dir():
        raise AbletonBridgeInstallerError(
            "Ableton User Library was not found at the selected path; "
            "pass --user-library if Live uses a custom User Library"
        )
    if path.is_symlink():
        raise AbletonBridgeInstallerError("Ableton User Library must not be a symlink")
    return path


def bundled_bridge_source() -> Path:
    try:
        from integrations.ableton import N0TEBridge
    except Exception as exc:
        raise AbletonBridgeInstallerError("bundled N0TEBridge source is unavailable") from exc
    source = Path(str(getattr(N0TEBridge, "__file__", ""))).resolve()
    if source.name != "__init__.py" or not source.is_file():
        raise AbletonBridgeInstallerError("bundled N0TEBridge source is invalid")
    return source


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AbletonBridgeInstallerError(f"cannot read bridge file: {path}") from exc
    return digest.hexdigest()


def _existing_is_ours(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return 'ADAPTER_ID = "N0TEBridge"' in text and "def create_instance(c_instance):" in text


def install_bridge(
    user_library: Path,
    *,
    source: Path | None = None,
) -> BridgeInstallResult:
    user_library = Path(user_library)
    if not user_library.is_dir() or user_library.is_symlink():
        raise AbletonBridgeInstallerError("Ableton User Library must be an existing real directory")
    source = Path(source) if source is not None else bundled_bridge_source()
    if not source.is_file() or source.is_symlink():
        raise AbletonBridgeInstallerError("N0TEBridge source must be a regular file")
    source_digest = _sha256(source)

    remote_scripts = user_library / REMOTE_SCRIPTS_DIR
    try:
        remote_scripts.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise AbletonBridgeInstallerError("cannot create Ableton Remote Scripts directory") from exc
    if not remote_scripts.is_dir() or remote_scripts.is_symlink():
        raise AbletonBridgeInstallerError("Ableton Remote Scripts path must be a real directory")

    target_dir = remote_scripts / SCRIPT_NAME
    target = target_dir / "__init__.py"
    existed = target.exists()
    if target_dir.exists():
        if not target_dir.is_dir() or target_dir.is_symlink():
            raise AbletonBridgeInstallerError("N0TEBridge install target is not a safe directory")
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise AbletonBridgeInstallerError("N0TEBridge target file is not safe to replace")
            if _sha256(target) == source_digest:
                return BridgeInstallResult("CURRENT", user_library, target_dir, source_digest, False)
            if not _existing_is_ours(target):
                raise AbletonBridgeInstallerError(
                    "an unrelated N0TEBridge script already exists; refusing to overwrite it"
                )
        elif any(target_dir.iterdir()):
            raise AbletonBridgeInstallerError(
                "N0TEBridge target directory contains unexpected files; refusing to overwrite it"
            )
    else:
        try:
            target_dir.mkdir()
        except OSError as exc:
            raise AbletonBridgeInstallerError("cannot create N0TEBridge target directory") from exc

    temp = target_dir / (".__init__.py.n0te-" + uuid.uuid4().hex + ".tmp")
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
        if isinstance(exc, AbletonBridgeInstallerError):
            raise
        raise AbletonBridgeInstallerError("N0TEBridge installation failed") from exc

    if _sha256(target) != source_digest:
        raise AbletonBridgeInstallerError("N0TEBridge verification failed after installation")
    return BridgeInstallResult(
        "UPDATED" if existed else "INSTALLED",
        user_library,
        target_dir,
        source_digest,
        True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m n0te.ableton_bridge_installer")
    parser.add_argument("--user-library")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        platform = PlatformEnvironment.from_runtime_labels(
            host_platform.system(), host_platform.machine()
        )
        user_library = resolve_user_library(
            explicit=args.user_library,
            environment=os.environ,
            platform=platform,
            home=Path.home(),
        )
        result = install_bridge(user_library)
    except AbletonBridgeInstallerError as exc:
        print(f"N0TE Ableton bridge install error: {exc}", file=sys.stderr)
        return 2
    if result.status == "CURRENT":
        print(f"N0TEBridge is current • {result.target_dir}")
    else:
        print(f"N0TEBridge {result.status.lower()} • {result.target_dir}")
        print("Restart Ableton Live, then select N0TEBridge as the Control Surface in Live's MIDI settings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())