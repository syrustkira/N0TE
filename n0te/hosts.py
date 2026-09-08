from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields

from .platforms import PlatformEnvironment

HOST_FAMILIES = (
    "ABLETON_LIVE",
    "FL_STUDIO",
    "LOGIC_PRO",
    "PRO_TOOLS",
    "STUDIO_ONE",
    "REAPER",
    "GENERIC_OTHER",
)

CORE_HOST_FAMILIES = HOST_FAMILIES[:-1]

TRANSLATION_MODES = {
    "NATIVE",
    "ROSETTA_2",
    "WINDOWS_X64_EMULATION",
    "WINDOWS_X86_EMULATION",
    "OTHER",
    "UNKNOWN",
}

_HOST_ALIASES = {
    "ableton live": "ABLETON_LIVE",
    "ableton": "ABLETON_LIVE",
    "fl studio": "FL_STUDIO",
    "image line fl studio": "FL_STUDIO",
    "logic pro": "LOGIC_PRO",
    "apple logic pro": "LOGIC_PRO",
    "pro tools": "PRO_TOOLS",
    "avid pro tools": "PRO_TOOLS",
    "studio one": "STUDIO_ONE",
    "presonus studio one": "STUDIO_ONE",
    "reaper": "REAPER",
    "cockos reaper": "REAPER",
    "generic other": "GENERIC_OTHER",
    "other": "GENERIC_OTHER",
}

_TRANSLATION_ALIASES = {
    "native": "NATIVE",
    "none": "NATIVE",
    "rosetta": "ROSETTA_2",
    "rosetta 2": "ROSETTA_2",
    "rosetta2": "ROSETTA_2",
    "windows x64 emulation": "WINDOWS_X64_EMULATION",
    "x64 emulation": "WINDOWS_X64_EMULATION",
    "windows x86 emulation": "WINDOWS_X86_EMULATION",
    "x86 emulation": "WINDOWS_X86_EMULATION",
    "wow64": "WINDOWS_X86_EMULATION",
    "other": "OTHER",
    "unknown": "UNKNOWN",
}

_DEFAULT_DISPLAY_NAMES = {
    "ABLETON_LIVE": "Ableton Live",
    "FL_STUDIO": "FL Studio",
    "LOGIC_PRO": "Logic Pro",
    "PRO_TOOLS": "Pro Tools",
    "STUDIO_ONE": "Studio One",
    "REAPER": "REAPER",
}

_FORBIDDEN_IDENTITY_FIELDS = {
    "rank",
    "priority",
    "preferred",
    "preference",
    "default",
    "capability",
    "support",
    "supported",
    "adapter",
    "maturity",
    "path",
    "process",
    "executable",
}


class HostIdentityError(ValueError):
    """Invalid host identity input."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise HostIdentityError(f"{field} must not be empty")
    return text


def _token(value: str, field: str) -> str:
    text = _text(value, field).casefold()
    text = re.sub(r"[_./-]+", " ", text)
    return " ".join(text.split())


def _identity_label(value: str, field: str) -> str:
    return " ".join(_text(value, field).split()).casefold()


def normalize_host_family(value: str) -> str:
    raw = _text(value, "host_family")
    canonical = raw.upper().replace("-", "_").replace(" ", "_")
    if canonical in HOST_FAMILIES:
        return canonical
    token = _token(raw, "host_family")
    try:
        return _HOST_ALIASES[token]
    except KeyError as exc:
        raise HostIdentityError(
            f"unknown host family {raw!r}; represent unlisted DAWs explicitly as GENERIC_OTHER"
        ) from exc


def normalize_translation_mode(value: str) -> str:
    raw = _text(value, "translation_mode")
    canonical = raw.upper().replace("-", "_").replace(" ", "_")
    if canonical in TRANSLATION_MODES:
        return canonical
    token = _token(raw, "translation_mode")
    try:
        return _TRANSLATION_ALIASES[token]
    except KeyError as exc:
        raise HostIdentityError(f"unknown translation mode: {raw!r}") from exc


@dataclass(frozen=True)
class HostRuntimeIdentity:
    """Descriptive active-host identity. Contains no capability or preference truth."""

    family: str
    version: str
    edition: str
    platform: PlatformEnvironment
    translation_mode: str = "NATIVE"
    display_name: str | None = None
    generic_host_label: str | None = None

    def __post_init__(self) -> None:
        family = normalize_host_family(self.family)
        version = _text(self.version, "version")
        edition = " ".join(_text(self.edition, "edition").split())
        translation = normalize_translation_mode(self.translation_mode)
        if not isinstance(self.platform, PlatformEnvironment):
            raise TypeError("platform must be PlatformEnvironment")

        display = None
        if self.display_name is not None:
            display = " ".join(_text(self.display_name, "display_name").split())

        generic = None
        if family == "GENERIC_OTHER":
            if self.generic_host_label is None:
                raise HostIdentityError(
                    "GENERIC_OTHER requires explicit generic_host_label"
                )
            generic = " ".join(
                _text(self.generic_host_label, "generic_host_label").split()
            )
        elif self.generic_host_label is not None:
            raise HostIdentityError(
                "generic_host_label is allowed only for GENERIC_OTHER"
            )

        object.__setattr__(self, "family", family)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "edition", edition)
        object.__setattr__(self, "translation_mode", translation)
        object.__setattr__(self, "display_name", display)
        object.__setattr__(self, "generic_host_label", generic)

    @classmethod
    def from_runtime_labels(
        cls,
        *,
        host_family: str,
        version: str,
        edition: str,
        os_name: str,
        machine: str,
        translation_mode: str = "NATIVE",
        display_name: str | None = None,
        generic_host_label: str | None = None,
    ) -> "HostRuntimeIdentity":
        return cls(
            family=host_family,
            version=version,
            edition=edition,
            platform=PlatformEnvironment.from_runtime_labels(os_name, machine),
            translation_mode=translation_mode,
            display_name=display_name,
            generic_host_label=generic_host_label,
        )

    @property
    def canonical_display_name(self) -> str:
        if self.family == "GENERIC_OTHER":
            assert self.generic_host_label is not None
            return self.display_name or self.generic_host_label
        return self.display_name or _DEFAULT_DISPLAY_NAMES[self.family]

    @property
    def fingerprint(self) -> str:
        payload = {
            "schema": "n0te.host-runtime/v1",
            "family": self.family,
            "version": self.version,
            "edition": self.edition.casefold(),
            "platform": {
                "os_family": self.platform.os_family,
                "architecture": self.platform.architecture,
            },
            "translation_mode": self.translation_mode,
            "generic_host_label": (
                _identity_label(self.generic_host_label, "generic_host_label")
                if self.generic_host_label is not None
                else None
            ),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def identity_payload(self) -> dict[str, object]:
        return {
            "family": self.family,
            "version": self.version,
            "edition": self.edition,
            "os_family": self.platform.os_family,
            "architecture": self.platform.architecture,
            "translation_mode": self.translation_mode,
            "generic_host_label": self.generic_host_label,
            "fingerprint": self.fingerprint,
        }


def assert_identity_contract_has_no_priority_fields() -> None:
    names = {field.name.casefold() for field in fields(HostRuntimeIdentity)}
    leaked = sorted(
        name for name in names
        if any(forbidden in name for forbidden in _FORBIDDEN_IDENTITY_FIELDS)
    )
    if leaked:
        raise HostIdentityError(
            f"host identity leaked priority/capability/runtime-location fields: {leaked}"
        )
