from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from .platforms import (
    normalize_architecture,
    normalize_os_family,
    target_tier,
)

POLICY_TIERS = {"CORE", "EXTENDED"}
EVIDENCE_STATES = {"ACCEPTED", "LEGACY_ACCEPTED", "EXPERIMENTAL", "KNOWN_BREAK"}
DERIVED_STATES = EVIDENCE_STATES | {"UNVERIFIED"}

_DEFAULT_POLICY_REF = "PLATFORM_SUPPORT_MATRIX"
_DEFAULT_VERSION_POLICY = "CAPABILITY_EVIDENCE_DRIVEN"


class SupportEnvelopeError(ValueError):
    """Invalid support policy/evidence input."""


def _text(value: str, field: str) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise SupportEnvelopeError(f"{field} must not be empty")
    return text


def _tags(values: Iterable[str]) -> tuple[str, ...]:
    normalized = {_text(value, "scope_tag") for value in values}
    return tuple(sorted(normalized))


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SupportTarget:
    os_family: str
    architecture: str
    policy_tier: str
    policy_ref: str = _DEFAULT_POLICY_REF
    os_version_policy: str = _DEFAULT_VERSION_POLICY
    scope_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.os_family not in {"MACOS", "WINDOWS", "LINUX"}:
            raise SupportEnvelopeError(
                "support target requires MACOS, WINDOWS or LINUX"
            )
        if self.architecture not in {
            "ARM64",
            "X86_64",
            "X86_32",
            "ARMV7",
            "RISCV64",
        }:
            raise SupportEnvelopeError(
                f"unsupported support-target architecture: {self.architecture}"
            )
        if self.policy_tier not in POLICY_TIERS:
            raise SupportEnvelopeError(f"invalid policy_tier: {self.policy_tier}")
        object.__setattr__(self, "policy_ref", _text(self.policy_ref, "policy_ref"))
        object.__setattr__(
            self,
            "os_version_policy",
            _text(self.os_version_policy, "os_version_policy"),
        )
        object.__setattr__(self, "scope_tags", _tags(self.scope_tags))

        platform_tier = target_tier(self.os_family, self.architecture)
        expected = {
            "CORE_TARGET": "CORE",
            "EXTENDED_TARGET": "EXTENDED",
        }.get(platform_tier)
        if expected is None:
            raise SupportEnvelopeError(
                "target is not admitted by the canonical platform target policy"
            )
        if self.policy_tier != expected:
            raise SupportEnvelopeError(
                f"{self.os_family}/{self.architecture} is canonical {expected}, "
                f"not {self.policy_tier}"
            )

    @classmethod
    def from_runtime_labels(
        cls,
        *,
        os_name: str,
        machine: str,
        policy_ref: str = _DEFAULT_POLICY_REF,
        os_version_policy: str = _DEFAULT_VERSION_POLICY,
        scope_tags: Iterable[str] = (),
    ) -> "SupportTarget":
        os_family = normalize_os_family(os_name)
        architecture = normalize_architecture(machine)
        tier = target_tier(os_family, architecture)
        if tier not in {"CORE_TARGET", "EXTENDED_TARGET"}:
            raise SupportEnvelopeError(
                f"runtime labels do not resolve to an admitted support target: "
                f"{os_name!r}/{machine!r}"
            )
        return cls(
            os_family=os_family,
            architecture=architecture,
            policy_tier="CORE" if tier == "CORE_TARGET" else "EXTENDED",
            policy_ref=policy_ref,
            os_version_policy=os_version_policy,
            scope_tags=tuple(scope_tags),
        )

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "os_family": self.os_family,
                "architecture": self.architecture,
                "policy_tier": self.policy_tier,
                "policy_ref": self.policy_ref,
                "os_version_policy": self.os_version_policy,
                "scope_tags": self.scope_tags,
            }
        )

    @property
    def target_id(self) -> str:
        suffix = self.fingerprint[:12]
        return (
            f"support:{self.os_family.lower()}:{self.architecture.lower()}:"
            f"{self.policy_tier.lower()}:{suffix}"
        )


@dataclass(frozen=True)
class SupportEvidence:
    target_fingerprint: str
    state: str
    evidence_ref: str
    known_break_reason: str | None = None
    upstream_limitation: str | None = None

    def __post_init__(self) -> None:
        fingerprint = str(self.target_fingerprint).strip().lower()
        if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
            raise SupportEnvelopeError(
                "target_fingerprint must be a 64-character hexadecimal digest"
            )
        object.__setattr__(self, "target_fingerprint", fingerprint)
        if self.state not in EVIDENCE_STATES:
            raise SupportEnvelopeError(f"invalid support evidence state: {self.state}")
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "evidence_ref"))

        if self.state == "KNOWN_BREAK":
            if self.known_break_reason is None:
                raise SupportEnvelopeError(
                    "KNOWN_BREAK requires an exact known_break_reason"
                )
            object.__setattr__(
                self,
                "known_break_reason",
                _text(self.known_break_reason, "known_break_reason"),
            )
        elif self.known_break_reason is not None:
            raise SupportEnvelopeError(
                "known_break_reason is only valid for KNOWN_BREAK evidence"
            )

        if self.state == "LEGACY_ACCEPTED":
            if self.upstream_limitation is None:
                raise SupportEnvelopeError(
                    "LEGACY_ACCEPTED requires an upstream_limitation"
                )
            object.__setattr__(
                self,
                "upstream_limitation",
                _text(self.upstream_limitation, "upstream_limitation"),
            )
        elif self.upstream_limitation is not None:
            raise SupportEnvelopeError(
                "upstream_limitation is only valid for LEGACY_ACCEPTED evidence"
            )


@dataclass(frozen=True)
class TargetSupportStatus:
    target: SupportTarget
    state: str
    evidence_ref: str | None
    known_break_reason: str | None
    upstream_limitation: str | None

    def __post_init__(self) -> None:
        if self.state not in DERIVED_STATES:
            raise SupportEnvelopeError(f"invalid derived support state: {self.state}")


@dataclass(frozen=True)
class SupportBlocker:
    target_id: str
    target_fingerprint: str
    state: str
    reason: str
    evidence_ref: str | None


class SupportEnvelope:
    """Pure support-policy/evidence projection.

    Policy says what N0TE intends to support. Evidence says what exact target
    has actually passed, is legacy-accepted, remains experimental, or has a
    named known break. Target declaration alone never becomes acceptance.
    """

    def __init__(
        self,
        targets: Iterable[SupportTarget],
        evidence: Iterable[SupportEvidence] = (),
    ):
        target_list = tuple(sorted(tuple(targets), key=lambda target: target.target_id))
        if not target_list:
            raise SupportEnvelopeError("support envelope requires at least one target")
        by_fingerprint: dict[str, SupportTarget] = {}
        for target in target_list:
            if not isinstance(target, SupportTarget):
                raise TypeError("targets must contain SupportTarget values")
            if target.fingerprint in by_fingerprint:
                raise SupportEnvelopeError(
                    f"duplicate support target: {target.target_id}"
                )
            by_fingerprint[target.fingerprint] = target

        evidence_list = tuple(evidence)
        by_target: dict[str, SupportEvidence] = {}
        for item in evidence_list:
            if not isinstance(item, SupportEvidence):
                raise TypeError("evidence must contain SupportEvidence values")
            if item.target_fingerprint not in by_fingerprint:
                raise SupportEnvelopeError(
                    "support evidence references a target outside this envelope"
                )
            if item.target_fingerprint in by_target:
                raise SupportEnvelopeError(
                    "multiple support evidence receipts for one target are ambiguous"
                )
            by_target[item.target_fingerprint] = item

        self._targets = target_list
        self._by_fingerprint = by_fingerprint
        self._evidence = by_target

    @property
    def targets(self) -> tuple[SupportTarget, ...]:
        return self._targets

    def status(self, target: SupportTarget) -> TargetSupportStatus:
        if not isinstance(target, SupportTarget):
            raise TypeError("target must be SupportTarget")
        owned = self._by_fingerprint.get(target.fingerprint)
        if owned != target:
            raise SupportEnvelopeError("target is not part of this support envelope")
        evidence = self._evidence.get(target.fingerprint)
        if evidence is None:
            return TargetSupportStatus(
                target=target,
                state="UNVERIFIED",
                evidence_ref=None,
                known_break_reason=None,
                upstream_limitation=None,
            )
        return TargetSupportStatus(
            target=target,
            state=evidence.state,
            evidence_ref=evidence.evidence_ref,
            known_break_reason=evidence.known_break_reason,
            upstream_limitation=evidence.upstream_limitation,
        )

    def statuses(self) -> tuple[TargetSupportStatus, ...]:
        return tuple(self.status(target) for target in self._targets)

    def customer_mode_blockers(self) -> tuple[SupportBlocker, ...]:
        blockers: list[SupportBlocker] = []
        for status in self.statuses():
            target = status.target
            if target.policy_tier != "CORE":
                continue
            if status.state in {"ACCEPTED", "LEGACY_ACCEPTED"}:
                continue
            if status.state == "KNOWN_BREAK":
                reason = f"known break: {status.known_break_reason}"
            elif status.state == "EXPERIMENTAL":
                reason = "core target remains experimental"
            else:
                reason = "core target has no acceptance evidence"
            blockers.append(
                SupportBlocker(
                    target_id=target.target_id,
                    target_fingerprint=target.fingerprint,
                    state=status.state,
                    reason=reason,
                    evidence_ref=status.evidence_ref,
                )
            )
        return tuple(blockers)

    def extended_findings(self) -> tuple[TargetSupportStatus, ...]:
        return tuple(
            status
            for status in self.statuses()
            if status.target.policy_tier == "EXTENDED"
            and status.state not in {"ACCEPTED", "LEGACY_ACCEPTED"}
        )

    def evidence_for(self, target: SupportTarget) -> SupportEvidence | None:
        self.status(target)
        return self._evidence.get(target.fingerprint)


def default_architecture_targets() -> tuple[SupportTarget, ...]:
    entries = (
        ("MACOS", "ARM64"),
        ("MACOS", "X86_64"),
        ("WINDOWS", "X86_64"),
        ("WINDOWS", "ARM64"),
        ("LINUX", "X86_64"),
        ("LINUX", "ARM64"),
        ("WINDOWS", "X86_32"),
        ("LINUX", "X86_32"),
        ("LINUX", "ARMV7"),
        ("LINUX", "RISCV64"),
    )
    return tuple(
        SupportTarget(
            os_family=os_family,
            architecture=architecture,
            policy_tier=(
                "CORE"
                if target_tier(os_family, architecture) == "CORE_TARGET"
                else "EXTENDED"
            ),
        )
        for os_family, architecture in entries
    )


def default_support_envelope(
    evidence: Iterable[SupportEvidence] = (),
) -> SupportEnvelope:
    return SupportEnvelope(default_architecture_targets(), evidence)
