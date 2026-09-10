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
SCRIPT_FILE_NAME = "N0TEBridge.lua"
SNAPSHOT_FILE_NAME = "n0te_snapshot.json"
RESOURCE_PATH_ENV = "N0TE_REAPER_RESOURCE_PATH"


class ReaperBridgeInstallerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReaperBridgeInstallResult:
    status: str
    resource_path: Path
    target_dir: Path
    target_file: Path
    snapshot_path: Path
    source_sha256: str

    def __post_init__(self) -> None:
        if self.status not in {"INSTALLED", "UPDATED", "CURRENT"}:
            raise ReaperBridgeInstallerError(f"invalid install status: {self.status}")


def _looks_like_resource_path(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and (
        (path / "reaper.ini").is_file() or (path / "Scripts").is_dir()
    )


def _default_candidates(
    platform: PlatformEnvironment,
    *,
    home: Path,
    environment: Mapping[str, str],
) -> tuple[Path, ...]:
    home = Path(home)
    if not home.is_absolute():
        raise ReaperBridgeInstallerError("home must be absolute")
    candidates: list[Path] = []
    if platform.os_family == "MACOS":
        candidates.append(home / "Library" / "Application Support" / "REAPER")
    elif platform.os_family == "WINDOWS":
        appdata = (environment.get("APPDATA") or "").strip()
        if appdata:
            candidates.append(Path(appdata).expanduser() / "REAPER")
        candidates.append(home / "AppData" / "Roaming" / "REAPER")
    elif platform.os_family == "LINUX":
        xdg = (environment.get("XDG_CONFIG_HOME") or "").strip()
        if xdg:
            candidates.append(Path(xdg).expanduser() / "REAPER")
        candidates.append(home / ".config" / "REAPER")
    else:
        raise ReaperBridgeInstallerError(
            "REAPER bridge installation requires macOS, Windows or Linux"
        )
    unique: list[Path] = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return tuple(unique)


def resolve_resource_path(
    *,
    explicit: str | Path | None,
    environment: Mapping[str, str],
    platform: PlatformEnvironment,
    home: Path,
) -> Path:
    raw = explicit if explicit is not None else environment.get(RESOURCE_PATH_ENV)
    if raw is not None and str(raw).strip():
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            raise ReaperBridgeInstallerError("REAPER resource path must be absolute")
        if not _looks_like_resource_path(path):
            raise ReaperBridgeInstallerError(
                "selected REAPER resource path does not look like an active REAPER resource directory"
            )
        return path

    matches = tuple(
        candidate
        for candidate in _default_candidates(
            platform,
            home=home,
            environment=environment,
        )
        if _looks_like_resource_path(candidate)
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ReaperBridgeInstallerError(
            "REAPER resource path was not found automatically; in REAPER choose "
            "Options > Show REAPER resource path, then pass that folder with --resource-path"
        )
    raise ReaperBridgeInstallerError(
        "multiple REAPER resource paths were found; pass the active one with --resource-path"
    )


def bundled_bridge_source() -> Path:
    source = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "reaper"
        / SCRIPT_FILE_NAME
    )
    if not source.is_file() or source.is_symlink():
        raise ReaperBridgeInstallerError("bundled REAPER N0TEBridge source is unavailable")
    return source


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReaperBridgeInstallerError(f"cannot read bridge file: {path}") from exc
    return digest.hexdigest()


def _existing_is_ours(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return (
        "N0TE REAPER read-side bridge" in text
        and 'local SCHEMA = "n0te.reaper-observation/v1"' in text
        and 'local ADAPTER_ID = "n0te-reaper-reascript"' in text
    )


def _ensure_real_directory(parent: Path, name: str) -> Path:
    path = parent / name
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise ReaperBridgeInstallerError(
                f"REAPER bridge install path is not a safe directory: {path}"
            )
        return path
    try:
        path.mkdir()
    except OSError as exc:
        raise ReaperBridgeInstallerError(
            f"cannot create REAPER bridge install directory: {path}"
        ) from exc
    return path


def install_bridge(
    resource_path: Path,
    *,
    source: Path | None = None,
) -> ReaperBridgeInstallResult:
    resource_path = Path(resource_path)
    if not _looks_like_resource_path(resource_path):
        raise ReaperBridgeInstallerError(
            "REAPER resource path must be an existing real resource directory"
        )
    source = Path(source) if source is not None else bundled_bridge_source()
    if not source.is_file() or source.is_symlink():
        raise ReaperBridgeInstallerError("REAPER N0TEBridge source must be a regular file")
    source_digest = _sha256(source)

    scripts = _ensure_real_directory(resource_path, "Scripts")
    target_dir = _ensure_real_directory(scripts, SCRIPT_DIR_NAME)
    target = target_dir / SCRIPT_FILE_NAME
    snapshot_path = target_dir / SNAPSHOT_FILE_NAME
    existed = target.exists()

    if existed:
        if not target.is_file() or target.is_symlink():
            raise ReaperBridgeInstallerError("N0TEBridge target file is not safe to replace")
        if _sha256(target) == source_digest:
            return ReaperBridgeInstallResult(
                "CURRENT",
                resource_path,
                target_dir,
                target,
                snapshot_path,
                source_digest,
            )
        if not _existing_is_ours(target):
            raise ReaperBridgeInstallerError(
                "an unrelated REAPER N0TEBridge.lua already exists; refusing to overwrite it"
            )
    else:
        allowed = {SNAPSHOT_FILE_NAME, ".n0te_snapshot.tmp"}
        unexpected = [item.name for item in target_dir.iterdir() if item.name not in allowed]
        if unexpected:
            raise ReaperBridgeInstallerError(
                "REAPER N0TEBridge target directory contains unexpected files; refusing to overwrite it"
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
        if isinstance(exc, ReaperBridgeInstallerError):
            raise
        raise ReaperBridgeInstallerError("REAPER N0TEBridge installation failed") from exc

    if _sha256(target) != source_digest:
        raise ReaperBridgeInstallerError("REAPER N0TEBridge verification failed after installation")
    return ReaperBridgeInstallResult(
        "UPDATED" if existed else "INSTALLED",
        resource_path,
        target_dir,
        target,
        snapshot_path,
        source_digest,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m n0te.reaper_bridge_installer")
    parser.add_argument("--resource-path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        platform = PlatformEnvironment.from_runtime_labels(
            host_platform.system(), host_platform.machine()
        )
        resource_path = resolve_resource_path(
            explicit=args.resource_path,
            environment=os.environ,
            platform=platform,
            home=Path.home(),
        )
        result = install_bridge(resource_path)
    except ReaperBridgeInstallerError as exc:
        print(f"N0TE REAPER bridge install error: {exc}", file=sys.stderr)
        return 2

    verb = "is current" if result.status == "CURRENT" else result.status.lower()
    print(f"N0TEBridge {verb} • {result.target_file}")
    print(f"N0TE REAPER snapshot • {result.snapshot_path}")
    print(
        "In REAPER open Actions, choose ReaScript: Load..., select N0TEBridge.lua, "
        "then run the N0TEBridge action."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
