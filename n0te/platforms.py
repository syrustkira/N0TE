from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Mapping

PRODUCT_DIR_NAME = "N0TE"

OS_FAMILIES = {"MACOS", "WINDOWS", "LINUX", "UNSUPPORTED"}
ARCHITECTURES = {"ARM64", "X86_64", "X86_32", "ARMV7", "RISCV64", "UNKNOWN"}
TARGET_TIERS = {"CORE_TARGET", "EXTENDED_TARGET", "UNVERIFIED", "UNSUPPORTED_PLATFORM"}

_OS_ALIASES = {
    "darwin": "MACOS",
    "macos": "MACOS",
    "mac": "MACOS",
    "osx": "MACOS",
    "windows": "WINDOWS",
    "win32": "WINDOWS",
    "win64": "WINDOWS",
    "nt": "WINDOWS",
    "linux": "LINUX",
}
_ARCH_ALIASES = {
    "arm64": "ARM64",
    "aarch64": "ARM64",
    "x86_64": "X86_64",
    "amd64": "X86_64",
    "x64": "X86_64",
    "i386": "X86_32",
    "i486": "X86_32",
    "i586": "X86_32",
    "i686": "X86_32",
    "x86": "X86_32",
    "armv7": "ARMV7",
    "armv7l": "ARMV7",
    "riscv64": "RISCV64",
}
_CORE_TARGETS = {
    ("MACOS", "ARM64"),
    ("MACOS", "X86_64"),
    ("WINDOWS", "X86_64"),
    ("WINDOWS", "ARM64"),
    ("LINUX", "X86_64"),
    ("LINUX", "ARM64"),
}
_EXTENDED_TARGETS = {
    ("WINDOWS", "X86_32"),
    ("LINUX", "X86_32"),
    ("LINUX", "ARMV7"),
    ("LINUX", "RISCV64"),
}


class PlatformError(ValueError):
    """Invalid or unsupported platform-bound input."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise PlatformError(f"{field} must not be empty")
    return text


def _clean_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    if environment is None:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in environment.items():
        name = str(key).strip()
        if not name:
            raise PlatformError("environment variable name must not be empty")
        text = str(value).strip()
        if text:
            cleaned[name.upper()] = text
    return cleaned


def normalize_os_family(os_name: str) -> str:
    raw = _text(os_name, "os_name").lower()
    return _OS_ALIASES.get(raw, "UNSUPPORTED")


def normalize_architecture(machine: str) -> str:
    raw = _text(machine, "machine").lower()
    return _ARCH_ALIASES.get(raw, "UNKNOWN")


def target_tier(os_family: str, architecture: str) -> str:
    if os_family not in OS_FAMILIES:
        raise PlatformError(f"invalid os_family: {os_family}")
    if architecture not in ARCHITECTURES:
        raise PlatformError(f"invalid architecture: {architecture}")
    if os_family == "UNSUPPORTED":
        return "UNSUPPORTED_PLATFORM"
    key = (os_family, architecture)
    if key in _CORE_TARGETS:
        return "CORE_TARGET"
    if key in _EXTENDED_TARGETS:
        return "EXTENDED_TARGET"
    return "UNVERIFIED"


@dataclass(frozen=True)
class PlatformEnvironment:
    os_family: str
    architecture: str
    raw_os_name: str
    raw_machine: str
    target_tier: str

    def __post_init__(self) -> None:
        if self.os_family not in OS_FAMILIES:
            raise PlatformError(f"invalid os_family: {self.os_family}")
        if self.architecture not in ARCHITECTURES:
            raise PlatformError(f"invalid architecture: {self.architecture}")
        if self.target_tier not in TARGET_TIERS:
            raise PlatformError(f"invalid target_tier: {self.target_tier}")
        if self.target_tier != target_tier(self.os_family, self.architecture):
            raise PlatformError("target_tier does not match os_family/architecture")
        object.__setattr__(self, "raw_os_name", _text(self.raw_os_name, "raw_os_name"))
        object.__setattr__(self, "raw_machine", _text(self.raw_machine, "raw_machine"))

    @classmethod
    def from_runtime_labels(cls, os_name: str, machine: str) -> "PlatformEnvironment":
        raw_os = _text(os_name, "os_name")
        raw_machine = _text(machine, "machine")
        family = normalize_os_family(raw_os)
        architecture = normalize_architecture(raw_machine)
        return cls(
            os_family=family,
            architecture=architecture,
            raw_os_name=raw_os,
            raw_machine=raw_machine,
            target_tier=target_tier(family, architecture),
        )


@dataclass(frozen=True)
class PlatformRoots:
    os_family: str
    data_root: PurePath
    config_root: PurePath
    state_root: PurePath
    cache_root: PurePath
    log_root: PurePath

    def __post_init__(self) -> None:
        if self.os_family not in {"MACOS", "WINDOWS", "LINUX"}:
            raise PlatformError("application roots require MACOS, WINDOWS or LINUX")
        expected = PureWindowsPath if self.os_family == "WINDOWS" else PurePosixPath
        for field in ("data_root", "config_root", "state_root", "cache_root", "log_root"):
            value = getattr(self, field)
            if not isinstance(value, expected):
                raise TypeError(f"{field} must use {'PureWindowsPath' if expected is PureWindowsPath else 'PurePosixPath'}")
            if not value.is_absolute():
                raise PlatformError(f"{field} must be absolute")

    def profile_data_root(self, profile_id: str) -> PurePath:
        profile = _text(profile_id, "profile_id")
        if "/" in profile or "\\" in profile or profile in {".", ".."}:
            raise PlatformError("profile_id must be an opaque single path component")
        return self.data_root / "profiles" / profile

    def profile_state_root(self, profile_id: str) -> PurePath:
        profile = _text(profile_id, "profile_id")
        if "/" in profile or "\\" in profile or profile in {".", ".."}:
            raise PlatformError("profile_id must be an opaque single path component")
        return self.state_root / "profiles" / profile


def _posix_absolute(value: str, field: str) -> PurePosixPath:
    path = PurePosixPath(_text(value, field))
    if not path.is_absolute():
        raise PlatformError(f"{field} must be an absolute path")
    return path


def _windows_absolute(value: str, field: str) -> PureWindowsPath:
    path = PureWindowsPath(_text(value, field))
    if not path.is_absolute():
        raise PlatformError(f"{field} must be an absolute Windows path")
    return path


def resolve_application_roots(
    platform: PlatformEnvironment,
    *,
    home: str,
    environment: Mapping[str, str] | None = None,
) -> PlatformRoots:
    """Purely resolve N0TE roots. Does not inspect or mutate the filesystem."""

    if not isinstance(platform, PlatformEnvironment):
        raise TypeError("platform must be PlatformEnvironment")
    env = _clean_env(environment)

    if platform.os_family == "MACOS":
        user_home = _posix_absolute(home, "home")
        support = user_home / "Library" / "Application Support" / PRODUCT_DIR_NAME
        return PlatformRoots(
            os_family="MACOS",
            data_root=support,
            config_root=support / "Config",
            state_root=support / "State",
            cache_root=user_home / "Library" / "Caches" / PRODUCT_DIR_NAME,
            log_root=user_home / "Library" / "Logs" / PRODUCT_DIR_NAME,
        )

    if platform.os_family == "WINDOWS":
        user_home = _windows_absolute(home, "home")
        roaming = _windows_absolute(
            env.get("APPDATA", str(user_home / "AppData" / "Roaming")),
            "APPDATA",
        )
        local = _windows_absolute(
            env.get("LOCALAPPDATA", str(user_home / "AppData" / "Local")),
            "LOCALAPPDATA",
        )
        return PlatformRoots(
            os_family="WINDOWS",
            data_root=local / PRODUCT_DIR_NAME / "Data",
            config_root=roaming / PRODUCT_DIR_NAME,
            state_root=local / PRODUCT_DIR_NAME / "State",
            cache_root=local / PRODUCT_DIR_NAME / "Cache",
            log_root=local / PRODUCT_DIR_NAME / "Logs",
        )

    if platform.os_family == "LINUX":
        user_home = _posix_absolute(home, "home")
        data_home = _posix_absolute(
            env.get("XDG_DATA_HOME", str(user_home / ".local" / "share")),
            "XDG_DATA_HOME",
        )
        config_home = _posix_absolute(
            env.get("XDG_CONFIG_HOME", str(user_home / ".config")),
            "XDG_CONFIG_HOME",
        )
        state_home = _posix_absolute(
            env.get("XDG_STATE_HOME", str(user_home / ".local" / "state")),
            "XDG_STATE_HOME",
        )
        cache_home = _posix_absolute(
            env.get("XDG_CACHE_HOME", str(user_home / ".cache")),
            "XDG_CACHE_HOME",
        )
        return PlatformRoots(
            os_family="LINUX",
            data_root=data_home / PRODUCT_DIR_NAME,
            config_root=config_home / PRODUCT_DIR_NAME,
            state_root=state_home / PRODUCT_DIR_NAME,
            cache_root=cache_home / PRODUCT_DIR_NAME,
            log_root=state_home / PRODUCT_DIR_NAME / "logs",
        )

    raise PlatformError(
        f"application roots are unavailable for unsupported platform family {platform.raw_os_name!r}"
    )
