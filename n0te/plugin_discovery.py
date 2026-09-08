from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from .tools import TOOL_FORMAT_KINDS


DISCOVERY_FORMATS = frozenset({"VST3", "AU", "AAX", "CLAP", "LV2", "LADSPA"})
ROOT_SOURCES = frozenset({"STANDARD", "ENVIRONMENT", "EXPLICIT"})
ROOT_STATES = frozenset(
    {
        "SCANNED",
        "MISSING",
        "UNREADABLE",
        "NOT_DIRECTORY",
        "SYMLINK_SKIPPED",
        "BOUNDED_OUT",
    }
)
ARTIFACT_KINDS = frozenset({"BUNDLE", "FILE", "SYMLINK"})

_PLATFORM_FORMATS = {
    "MACOS": frozenset({"VST3", "AU", "AAX", "CLAP"}),
    "WINDOWS": frozenset({"VST3", "AAX", "CLAP"}),
    "LINUX": frozenset({"VST3", "CLAP", "LV2", "LADSPA"}),
}

_PACKAGE_SUFFIXES = {
    "VST3": (".vst3",),
    "AU": (".component",),
    "AAX": (".aaxplugin",),
    "CLAP": (".clap",),
    "LV2": (".lv2",),
    "LADSPA": (".so",),
}

_DIRECTORY_PACKAGE_FORMATS = frozenset({"AU", "AAX", "LV2"})
_FILE_PACKAGE_FORMATS = frozenset({"LADSPA"})
_RELEVANT_ENVIRONMENT_KEYS = frozenset(
    {
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "COMMONPROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES(X86)",
        "LV2_PATH",
        "LADSPA_PATH",
    }
)


class PluginDiscoveryError(ValueError):
    """Invalid discovery input or internally inconsistent discovery evidence."""


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise PluginDiscoveryError(f"{field_name} must not be empty")
    return cleaned


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 1:
        raise PluginDiscoveryError(f"{field_name} must be >= 1")
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise PluginDiscoveryError(f"{field_name} must be >= 0")
    return value


def _format_kind(value: object) -> str:
    format_kind = _text(value, "format_kind").upper()
    if format_kind not in TOOL_FORMAT_KINDS or format_kind not in DISCOVERY_FORMATS:
        raise PluginDiscoveryError(f"unsupported discovery format: {format_kind}")
    return format_kind


def _os_family(value: object) -> str:
    os_family = _text(value, "os_family").upper()
    if os_family not in _PLATFORM_FORMATS:
        raise PluginDiscoveryError(f"unsupported discovery os_family: {os_family}")
    return os_family


def runtime_os_family() -> str:
    """Return the platform family this process can safely inspect locally."""

    if sys.platform == "darwin":
        return "MACOS"
    if os.name == "nt":
        return "WINDOWS"
    if sys.platform.startswith("linux"):
        return "LINUX"
    raise PluginDiscoveryError(f"unsupported runtime platform: {sys.platform}")


def _path_text(value: object, field_name: str) -> str:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a path-like value")
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"{field_name} must be a path-like value")
    text = os.fspath(value)
    if not isinstance(text, str):
        raise TypeError(f"{field_name} must resolve to text")
    cleaned = text.strip()
    if not cleaned:
        raise PluginDiscoveryError(f"{field_name} must not be empty")
    return cleaned


def _pure_absolute(value: object, *, os_family: str, field_name: str) -> PurePath:
    text = _path_text(value, field_name)
    path: PurePath
    if os_family == "WINDOWS":
        path = PureWindowsPath(text)
    else:
        path = PurePosixPath(text)
    if not path.is_absolute():
        raise PluginDiscoveryError(f"{field_name} must be absolute for {os_family}")
    return path


def _clean_environment(environment: Mapping[str, object] | None) -> dict[str, str]:
    """Normalize only variables discovery actually consumes.

    Unrelated environment values are deliberately ignored, including empty values.
    Relevant values remain strict text and blank relevant values behave as absent.
    """

    if environment is None:
        return {}
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be a mapping")
    cleaned: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str):
            raise TypeError("environment variable names must be text")
        name = key.strip().upper()
        if not name:
            raise PluginDiscoveryError("environment variable name must not be empty")
        if name not in _RELEVANT_ENVIRONMENT_KEYS:
            continue
        if not isinstance(value, str):
            raise TypeError(f"environment[{name}] must be text")
        text = value.strip()
        if not text:
            continue
        previous = cleaned.get(name)
        if previous is not None and previous != text:
            raise PluginDiscoveryError(f"conflicting values supplied for {name}")
        cleaned[name] = text
    return cleaned


def _supported_on(os_family: str, format_kind: str) -> bool:
    return format_kind in _PLATFORM_FORMATS[os_family]


@dataclass(frozen=True)
class PluginSearchRoot:
    """One explicitly bounded directory N0TE is permitted to inspect for one format."""

    os_family: str
    format_kind: str
    path: PurePath
    source: str

    def __post_init__(self) -> None:
        os_family = _os_family(self.os_family)
        format_kind = _format_kind(self.format_kind)
        source = _text(self.source, "source").upper()
        if source not in ROOT_SOURCES:
            raise PluginDiscoveryError(f"invalid root source: {source}")
        if not _supported_on(os_family, format_kind):
            raise PluginDiscoveryError(
                f"{format_kind} is not a discovery format for {os_family}"
            )
        expected = PureWindowsPath if os_family == "WINDOWS" else PurePosixPath
        if not isinstance(self.path, expected):
            expected_name = "PureWindowsPath" if expected is PureWindowsPath else "PurePosixPath"
            raise TypeError(f"path must use {expected_name}")
        if not self.path.is_absolute():
            raise PluginDiscoveryError("search root must be absolute")
        object.__setattr__(self, "os_family", os_family)
        object.__setattr__(self, "format_kind", format_kind)
        object.__setattr__(self, "source", source)

    @classmethod
    def explicit(
        cls,
        *,
        os_family: str,
        format_kind: str,
        path: str | os.PathLike[str],
    ) -> "PluginSearchRoot":
        normalized_os = _os_family(os_family)
        normalized_format = _format_kind(format_kind)
        return cls(
            os_family=normalized_os,
            format_kind=normalized_format,
            path=_pure_absolute(path, os_family=normalized_os, field_name="path"),
            source="EXPLICIT",
        )


@dataclass(frozen=True)
class PluginPackageObservation:
    """Filesystem/package evidence only, never license, hostability, control or Tool identity."""

    os_family: str
    format_kind: str
    package_path: PurePath
    package_name: str
    root_path: PurePath
    root_source: str
    artifact_kind: str
    discovery_state: str = field(default="DISCOVERED", init=False)
    entitlement_state: str = field(default="UNKNOWN", init=False)
    hostability_state: str = field(default="UNKNOWN", init=False)
    control_state: str = field(default="UNKNOWN", init=False)
    semantic_identity_state: str = field(default="UNRESOLVED", init=False)
    execution_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        os_family = _os_family(self.os_family)
        format_kind = _format_kind(self.format_kind)
        if not _supported_on(os_family, format_kind):
            raise PluginDiscoveryError("package format/platform mismatch")
        expected = PureWindowsPath if os_family == "WINDOWS" else PurePosixPath
        if not isinstance(self.package_path, expected) or not isinstance(
            self.root_path, expected
        ):
            raise TypeError("package_path and root_path must match os_family path type")
        if not self.package_path.is_absolute() or not self.root_path.is_absolute():
            raise PluginDiscoveryError("package and root paths must be absolute")
        artifact_kind = _text(self.artifact_kind, "artifact_kind").upper()
        if artifact_kind not in ARTIFACT_KINDS:
            raise PluginDiscoveryError(f"invalid artifact_kind: {artifact_kind}")
        root_source = _text(self.root_source, "root_source").upper()
        if root_source not in ROOT_SOURCES:
            raise PluginDiscoveryError(f"invalid root_source: {root_source}")
        object.__setattr__(self, "os_family", os_family)
        object.__setattr__(self, "format_kind", format_kind)
        object.__setattr__(self, "package_name", _text(self.package_name, "package_name"))
        object.__setattr__(self, "root_source", root_source)
        object.__setattr__(self, "artifact_kind", artifact_kind)


@dataclass(frozen=True)
class PluginRootObservation:
    root: PluginSearchRoot
    state: str
    scanned_entries: int
    package_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.root, PluginSearchRoot):
            raise TypeError("root must be PluginSearchRoot")
        state = _text(self.state, "state").upper()
        if state not in ROOT_STATES:
            raise PluginDiscoveryError(f"invalid root state: {state}")
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "scanned_entries",
            _nonnegative_int(self.scanned_entries, "scanned_entries"),
        )
        object.__setattr__(
            self,
            "package_count",
            _nonnegative_int(self.package_count, "package_count"),
        )


@dataclass(frozen=True)
class PluginDiscoveryReport:
    roots: tuple[PluginRootObservation, ...]
    packages: tuple[PluginPackageObservation, ...]

    def __post_init__(self) -> None:
        roots = tuple(self.roots)
        packages = tuple(self.packages)
        if not all(isinstance(item, PluginRootObservation) for item in roots):
            raise TypeError("all roots must be PluginRootObservation")
        if not all(isinstance(item, PluginPackageObservation) for item in packages):
            raise TypeError("all packages must be PluginPackageObservation")
        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "packages", packages)

    @property
    def bounded_out(self) -> bool:
        return any(root.state == "BOUNDED_OUT" for root in self.roots)

    def packages_for(self, format_kind: str) -> tuple[PluginPackageObservation, ...]:
        normalized = _format_kind(format_kind)
        return tuple(item for item in self.packages if item.format_kind == normalized)


class PluginRootPlanner:
    """Pure search-root planning. No filesystem inspection or persistence occurs here."""

    @staticmethod
    def standard_roots(
        *,
        os_family: str,
        home: str | os.PathLike[str],
        environment: Mapping[str, object] | None = None,
    ) -> tuple[PluginSearchRoot, ...]:
        family = _os_family(os_family)
        env = _clean_environment(environment)
        user_home = _pure_absolute(home, os_family=family, field_name="home")
        roots: list[PluginSearchRoot] = []

        def add(format_kind: str, path: PurePath, source: str = "STANDARD") -> None:
            roots.append(
                PluginSearchRoot(
                    os_family=family,
                    format_kind=format_kind,
                    path=path,
                    source=source,
                )
            )

        if family == "MACOS":
            add("VST3", user_home / "Library" / "Audio" / "Plug-Ins" / "VST3")
            add("VST3", PurePosixPath("/Library/Audio/Plug-Ins/VST3"))
            add("VST3", PurePosixPath("/Network/Library/Audio/Plug-Ins/VST3"))
            add("AU", user_home / "Library" / "Audio" / "Plug-Ins" / "Components")
            add("AU", PurePosixPath("/Library/Audio/Plug-Ins/Components"))
            add("AU", PurePosixPath("/System/Library/Components"))
            add("CLAP", user_home / "Library" / "Audio" / "Plug-Ins" / "CLAP")
            add("CLAP", PurePosixPath("/Library/Audio/Plug-Ins/CLAP"))
            add("AAX", PurePosixPath("/Library/Application Support/Avid/Audio/Plug-Ins"))

        elif family == "WINDOWS":
            local_app_data = _pure_absolute(
                env.get("LOCALAPPDATA", str(user_home / "AppData" / "Local")),
                os_family=family,
                field_name="LOCALAPPDATA",
            )
            program_files = _pure_absolute(
                env.get("PROGRAMFILES", r"C:\Program Files"),
                os_family=family,
                field_name="PROGRAMFILES",
            )
            common_program_files = _pure_absolute(
                env.get("COMMONPROGRAMFILES", str(program_files / "Common Files")),
                os_family=family,
                field_name="COMMONPROGRAMFILES",
            )
            add("VST3", local_app_data / "Programs" / "Common" / "VST3")
            add("VST3", common_program_files / "VST3")

            program_files_x86 = env.get("PROGRAMFILES(X86)")
            if program_files_x86 is not None:
                x86 = _pure_absolute(
                    program_files_x86,
                    os_family=family,
                    field_name="PROGRAMFILES(X86)",
                )
                common_x86 = _pure_absolute(
                    env.get("COMMONPROGRAMFILES(X86)", str(x86 / "Common Files")),
                    os_family=family,
                    field_name="COMMONPROGRAMFILES(X86)",
                )
                add("VST3", common_x86 / "VST3")

            add("CLAP", common_program_files / "CLAP")
            add("CLAP", local_app_data / "Programs" / "Common" / "CLAP")
            add("AAX", common_program_files / "Avid" / "Audio" / "Plug-Ins")

        else:
            add("VST3", user_home / ".vst3")
            add("VST3", PurePosixPath("/usr/local/lib/vst3"))
            add("VST3", PurePosixPath("/usr/lib/vst3"))
            add("CLAP", user_home / ".clap")
            add("CLAP", PurePosixPath("/usr/local/lib/clap"))
            add("CLAP", PurePosixPath("/usr/lib/clap"))

            lv2_path = env.get("LV2_PATH")
            if lv2_path is not None:
                for item in _environment_paths(lv2_path, family):
                    add("LV2", item, "ENVIRONMENT")
            else:
                add("LV2", user_home / ".lv2")
                add("LV2", PurePosixPath("/usr/local/lib/lv2"))
                add("LV2", PurePosixPath("/usr/lib/lv2"))

            ladspa_path = env.get("LADSPA_PATH")
            if ladspa_path is not None:
                for item in _environment_paths(ladspa_path, family):
                    add("LADSPA", item, "ENVIRONMENT")
            else:
                add("LADSPA", PurePosixPath("/usr/local/lib/ladspa"))
                add("LADSPA", PurePosixPath("/usr/lib/ladspa"))

        return _dedupe_roots(roots)

    @staticmethod
    def combine(*groups: Iterable[PluginSearchRoot]) -> tuple[PluginSearchRoot, ...]:
        flattened: list[PluginSearchRoot] = []
        for group in groups:
            flattened.extend(group)
        return _dedupe_roots(flattened)


def _environment_paths(value: str, os_family: str) -> tuple[PurePath, ...]:
    separator = ";" if os_family == "WINDOWS" else ":"
    paths: list[PurePath] = []
    for raw in value.split(separator):
        if not raw.strip():
            continue
        paths.append(
            _pure_absolute(raw, os_family=os_family, field_name="environment search path")
        )
    if not paths:
        raise PluginDiscoveryError("environment search path must contain an absolute path")
    return tuple(paths)


def _root_key(root: PluginSearchRoot) -> tuple[str, str, str]:
    path = str(root.path)
    if root.os_family == "WINDOWS":
        path = path.casefold()
    return root.os_family, root.format_kind, path


def _dedupe_roots(roots: Iterable[PluginSearchRoot]) -> tuple[PluginSearchRoot, ...]:
    seen: set[tuple[str, str, str]] = set()
    result: list[PluginSearchRoot] = []
    for root in roots:
        if not isinstance(root, PluginSearchRoot):
            raise TypeError("all roots must be PluginSearchRoot")
        key = _root_key(root)
        if key in seen:
            continue
        seen.add(key)
        result.append(root)
    return tuple(result)


def _package_key(item: PluginPackageObservation) -> tuple[str, str, str]:
    path = str(item.package_path)
    if item.os_family == "WINDOWS":
        path = path.casefold()
    return item.os_family, item.format_kind, path


def _name_has_format_suffix(name: str, format_kind: str) -> bool:
    folded = name.casefold()
    return any(folded.endswith(suffix) for suffix in _PACKAGE_SUFFIXES[format_kind])


def _entry_artifact_kind(entry: os.DirEntry[str], format_kind: str) -> str | None:
    if not _name_has_format_suffix(entry.name, format_kind):
        return None
    try:
        if entry.is_symlink():
            return "SYMLINK"
        is_dir = entry.is_dir(follow_symlinks=False)
        is_file = entry.is_file(follow_symlinks=False)
    except OSError:
        return None

    if format_kind in _DIRECTORY_PACKAGE_FORMATS:
        return "BUNDLE" if is_dir else None
    if format_kind in _FILE_PACKAGE_FORMATS:
        return "FILE" if is_file else None
    if is_dir:
        return "BUNDLE"
    if is_file:
        return "FILE"
    return None


def _semantic_package_path(
    root: PluginSearchRoot,
    native_root: Path,
    native_package: Path,
) -> PurePath:
    relative = native_package.relative_to(native_root)
    return root.path.joinpath(*relative.parts)


class PluginDiscoveryScanner:
    """Bounded local package discovery with no loading, execution or identity inference.

    Only caller-supplied PluginSearchRoot objects are inspected. Recognized package
    directories are terminal package boundaries, directory symlinks are never
    recursively followed, and the scanner cannot inspect roots for another OS family.
    """

    def __init__(self, *, max_entries_per_root: int = 20_000, max_depth: int = 8):
        self.max_entries_per_root = _positive_int(
            max_entries_per_root, "max_entries_per_root"
        )
        self.max_depth = _nonnegative_int(max_depth, "max_depth")

    def scan(self, roots: Iterable[PluginSearchRoot]) -> PluginDiscoveryReport:
        planned = _dedupe_roots(tuple(roots))
        current_os = runtime_os_family()
        root_observations: list[PluginRootObservation] = []
        packages: list[PluginPackageObservation] = []
        seen_packages: set[tuple[str, str, str]] = set()

        for root in planned:
            if root.os_family != current_os:
                raise PluginDiscoveryError(
                    f"cannot inspect a {root.os_family} plug-in root from a {current_os} runtime"
                )
            observation, found = self._scan_root(root)
            root_observations.append(observation)
            for item in found:
                key = _package_key(item)
                if key in seen_packages:
                    continue
                seen_packages.add(key)
                packages.append(item)

        packages.sort(
            key=lambda item: (
                item.format_kind,
                str(item.package_path).casefold()
                if item.os_family == "WINDOWS"
                else str(item.package_path),
            )
        )
        return PluginDiscoveryReport(tuple(root_observations), tuple(packages))

    def _scan_root(
        self, root: PluginSearchRoot
    ) -> tuple[PluginRootObservation, tuple[PluginPackageObservation, ...]]:
        native_root = Path(str(root.path))
        try:
            metadata = os.lstat(native_root)
        except FileNotFoundError:
            return self._root_result(root, "MISSING", 0, ()), ()
        except OSError:
            return self._root_result(root, "UNREADABLE", 0, ()), ()

        if stat.S_ISLNK(metadata.st_mode):
            return self._root_result(root, "SYMLINK_SKIPPED", 0, ()), ()
        if not stat.S_ISDIR(metadata.st_mode):
            return self._root_result(root, "NOT_DIRECTORY", 0, ()), ()

        queue: list[tuple[Path, int]] = [(native_root, 0)]
        found: list[PluginPackageObservation] = []
        scanned_entries = 0
        state = "SCANNED"
        bounded = False

        while queue and not bounded:
            directory, depth = queue.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if scanned_entries >= self.max_entries_per_root:
                            state = "BOUNDED_OUT"
                            bounded = True
                            break
                        scanned_entries += 1

                        artifact_kind = _entry_artifact_kind(entry, root.format_kind)
                        if artifact_kind is not None:
                            native_package = Path(entry.path)
                            found.append(
                                PluginPackageObservation(
                                    os_family=root.os_family,
                                    format_kind=root.format_kind,
                                    package_path=_semantic_package_path(
                                        root, native_root, native_package
                                    ),
                                    package_name=entry.name,
                                    root_path=root.path,
                                    root_source=root.source,
                                    artifact_kind=artifact_kind,
                                )
                            )
                            # A recognized package directory is an evidence boundary.
                            # Never descend into it looking for more plug-ins.
                            continue

                        try:
                            if entry.is_symlink():
                                continue
                            is_directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            if state == "SCANNED":
                                state = "UNREADABLE"
                            continue

                        if is_directory and depth < self.max_depth:
                            queue.append((Path(entry.path), depth + 1))
            except OSError:
                if state != "BOUNDED_OUT":
                    state = "UNREADABLE"

        result = tuple(found)
        return self._root_result(root, state, scanned_entries, result), result

    @staticmethod
    def _root_result(
        root: PluginSearchRoot,
        state: str,
        scanned_entries: int,
        packages: tuple[PluginPackageObservation, ...],
    ) -> PluginRootObservation:
        return PluginRootObservation(
            root=root,
            state=state,
            scanned_entries=scanned_entries,
            package_count=len(packages),
        )
