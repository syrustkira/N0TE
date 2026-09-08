from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .app_runtime import ApplicationRuntime
from .artifacts import (
    ManifestAuthenticityEvidence,
    ReleaseArtifactVerifier,
    ReleaseManifest,
)
from .instance import InstanceLease, InstanceLeaseManager, ProcessIdentity, ProcessProbe
from .lineage import LineageStore
from .memory import HeadquartersMemory
from .recovery import RecoveryManager, SnapshotInfo
from .support import SupportTarget

UPDATE_SCHEMA_VERSION = 1
UPDATE_STATES = {
    "PREPARED",
    "INSTALLING",
    "VALIDATING",
    "ROLLING_BACK",
    "RESTORING",
    "FINALIZING",
    "SUCCEEDED",
    "FAILED_SAFE",
    "ROLLED_BACK",
    "RECOVERY_REQUIRED",
}
SETTLED_UPDATE_STATES = {"SUCCEEDED", "FAILED_SAFE", "ROLLED_BACK"}
TERMINAL_UPDATE_STATES = SETTLED_UPDATE_STATES | {"RECOVERY_REQUIRED"}
IN_FLIGHT_UPDATE_STATES = {
    "INSTALLING",
    "VALIDATING",
    "ROLLING_BACK",
    "RESTORING",
    "FINALIZING",
}
PACKAGE_ACTIONS = {"INSTALL", "ROLLBACK"}
PACKAGE_INSTALL_STATES = {"SUCCEEDED", "FAILED_SAFE", "FAILED_CHANGED", "UNKNOWN"}
PACKAGE_ROLLBACK_STATES = {"SUCCEEDED", "FAILED", "UNKNOWN"}
_ALLOWED_TRANSITIONS = {
    "PREPARED": {"INSTALLING", "RECOVERY_REQUIRED"},
    "INSTALLING": {"VALIDATING", "ROLLING_BACK", "FINALIZING", "RECOVERY_REQUIRED"},
    "VALIDATING": {"ROLLING_BACK", "FINALIZING", "RECOVERY_REQUIRED"},
    "ROLLING_BACK": {"RESTORING", "RECOVERY_REQUIRED"},
    "RESTORING": {"FINALIZING", "RECOVERY_REQUIRED"},
    "FINALIZING": {"SUCCEEDED", "FAILED_SAFE", "ROLLED_BACK", "RECOVERY_REQUIRED"},
    "SUCCEEDED": set(),
    "FAILED_SAFE": set(),
    "ROLLED_BACK": set(),
    "RECOVERY_REQUIRED": set(),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UPDATE_ID = re.compile(r"^upd_[0-9a-f]{32}$")
_PROFILE_ID = re.compile(r"^prf_[0-9a-f]{32}$")


class ApplicationUpdateError(RuntimeError):
    """Base failure for application update orchestration."""


class UpdateRejectedError(ApplicationUpdateError):
    """The update could not safely enter package mutation."""


class UpdateExecuteOnceError(ApplicationUpdateError):
    """A prepared update is no longer eligible for first execution."""


class UpdateJournalCorruptionError(ApplicationUpdateError):
    """Durable update state is malformed or internally inconsistent."""


class UpdateBusyError(ApplicationUpdateError):
    """Another process already owns update work for this profile."""


def _text(value: str, field: str) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise ApplicationUpdateError(f"{field} must not be empty")
    return text


def _sha256(value: str, field: str) -> str:
    text = str(value).strip().lower()
    if not _SHA256.fullmatch(text):
        raise ApplicationUpdateError(f"{field} must be a lowercase SHA-256")
    return text


def _profile(value: str) -> str:
    profile = str(value).strip()
    if not _PROFILE_ID.fullmatch(profile):
        raise ApplicationUpdateError("profile_id must be a canonical local profile identity")
    return profile


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_root(path: str | Path) -> Path:
    root = Path(path)
    if not root.is_absolute():
        raise ApplicationUpdateError("data_root must be absolute")
    return Path(os.path.abspath(os.path.normpath(str(root))))


def _root_fingerprint(path: str | Path) -> str:
    return hashlib.sha256(os.fsencode(str(_normalized_root(path)))).hexdigest()


@dataclass(frozen=True)
class UpdatePlan:
    update_id: str
    profile_id: str
    current_release_id: str
    target_release_id: str
    target_version: str
    manifest_fingerprint: str
    artifact_id: str
    artifact_sha256: str
    target_fingerprint: str
    data_root_fingerprint: str
    snapshot_sha256: str
    snapshot_logical_sha256: str
    snapshot_size_bytes: int
    snapshot_lineage_schema_version: str

    def __post_init__(self) -> None:
        update = str(self.update_id).strip().lower()
        if not _UPDATE_ID.fullmatch(update):
            raise ApplicationUpdateError("invalid update_id")
        object.__setattr__(self, "update_id", update)
        object.__setattr__(self, "profile_id", _profile(self.profile_id))
        for field in (
            "current_release_id",
            "target_release_id",
            "target_version",
            "artifact_id",
            "snapshot_lineage_schema_version",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        for field in (
            "manifest_fingerprint",
            "artifact_sha256",
            "target_fingerprint",
            "data_root_fingerprint",
            "snapshot_sha256",
            "snapshot_logical_sha256",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        if (
            isinstance(self.snapshot_size_bytes, bool)
            or not isinstance(self.snapshot_size_bytes, int)
            or self.snapshot_size_bytes <= 0
        ):
            raise ApplicationUpdateError("snapshot_size_bytes must be a positive integer")

    def to_data(self) -> dict[str, object]:
        return {
            "update_id": self.update_id,
            "profile_id": self.profile_id,
            "current_release_id": self.current_release_id,
            "target_release_id": self.target_release_id,
            "target_version": self.target_version,
            "manifest_fingerprint": self.manifest_fingerprint,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "target_fingerprint": self.target_fingerprint,
            "data_root_fingerprint": self.data_root_fingerprint,
            "snapshot_sha256": self.snapshot_sha256,
            "snapshot_logical_sha256": self.snapshot_logical_sha256,
            "snapshot_size_bytes": self.snapshot_size_bytes,
            "snapshot_lineage_schema_version": self.snapshot_lineage_schema_version,
        }

    @classmethod
    def from_data(cls, data: object) -> "UpdatePlan":
        if not isinstance(data, dict):
            raise UpdateJournalCorruptionError("update plan must be an object")
        if set(data) != {
            "update_id",
            "profile_id",
            "current_release_id",
            "target_release_id",
            "target_version",
            "manifest_fingerprint",
            "artifact_id",
            "artifact_sha256",
            "target_fingerprint",
            "data_root_fingerprint",
            "snapshot_sha256",
            "snapshot_logical_sha256",
            "snapshot_size_bytes",
            "snapshot_lineage_schema_version",
        }:
            raise UpdateJournalCorruptionError("update plan shape is invalid")
        try:
            return cls(**data)  # type: ignore[arg-type]
        except ApplicationUpdateError as exc:
            raise UpdateJournalCorruptionError("update plan is invalid") from exc

    @property
    def fingerprint(self) -> str:
        return _digest_json(self.to_data())


@dataclass(frozen=True)
class PackageActionRequest:
    update_id: str
    plan_fingerprint: str
    action: str
    profile_id: str
    current_release_id: str
    target_release_id: str
    artifact_id: str
    artifact_sha256: str
    target_fingerprint: str

    def __post_init__(self) -> None:
        update = str(self.update_id).strip().lower()
        if not _UPDATE_ID.fullmatch(update):
            raise ApplicationUpdateError("invalid package request update_id")
        object.__setattr__(self, "update_id", update)
        object.__setattr__(
            self, "plan_fingerprint", _sha256(self.plan_fingerprint, "plan_fingerprint")
        )
        action = _text(self.action, "action").upper()
        if action not in PACKAGE_ACTIONS:
            raise ApplicationUpdateError(f"invalid package action: {action}")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "profile_id", _profile(self.profile_id))
        for field in ("current_release_id", "target_release_id", "artifact_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(
            self, "artifact_sha256", _sha256(self.artifact_sha256, "artifact_sha256")
        )
        object.__setattr__(
            self,
            "target_fingerprint",
            _sha256(self.target_fingerprint, "target_fingerprint"),
        )

    @property
    def fingerprint(self) -> str:
        return _digest_json(
            {
                "update_id": self.update_id,
                "plan_fingerprint": self.plan_fingerprint,
                "action": self.action,
                "profile_id": self.profile_id,
                "current_release_id": self.current_release_id,
                "target_release_id": self.target_release_id,
                "artifact_id": self.artifact_id,
                "artifact_sha256": self.artifact_sha256,
                "target_fingerprint": self.target_fingerprint,
            }
        )


@dataclass(frozen=True)
class PackageActionReceipt:
    request_fingerprint: str
    action: str
    state: str
    evidence_ref: str
    resulting_release_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_fingerprint",
            _sha256(self.request_fingerprint, "request_fingerprint"),
        )
        action = _text(self.action, "action").upper()
        if action not in PACKAGE_ACTIONS:
            raise ApplicationUpdateError(f"invalid package receipt action: {action}")
        object.__setattr__(self, "action", action)
        state = _text(self.state, "state").upper()
        allowed = PACKAGE_INSTALL_STATES if action == "INSTALL" else PACKAGE_ROLLBACK_STATES
        if state not in allowed:
            raise ApplicationUpdateError(f"invalid {action} receipt state: {state}")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "evidence_ref"))
        if self.resulting_release_id is not None:
            object.__setattr__(
                self,
                "resulting_release_id",
                _text(self.resulting_release_id, "resulting_release_id"),
            )

    def to_data(self) -> dict[str, str | None]:
        return {
            "request_fingerprint": self.request_fingerprint,
            "action": self.action,
            "state": self.state,
            "evidence_ref": self.evidence_ref,
            "resulting_release_id": self.resulting_release_id,
        }

    @classmethod
    def from_data(cls, data: object) -> "PackageActionReceipt":
        if not isinstance(data, dict) or set(data) != {
            "request_fingerprint",
            "action",
            "state",
            "evidence_ref",
            "resulting_release_id",
        }:
            raise UpdateJournalCorruptionError("package receipt shape is invalid")
        try:
            return cls(**data)  # type: ignore[arg-type]
        except ApplicationUpdateError as exc:
            raise UpdateJournalCorruptionError("package receipt is invalid") from exc


class PackageDriver(Protocol):
    def perform(
        self,
        request: PackageActionRequest,
        artifact_bytes: bytes | None,
    ) -> PackageActionReceipt: ...


@dataclass(frozen=True)
class UpdateResult:
    state: str
    plan: UpdatePlan
    install_receipt: PackageActionReceipt | None
    rollback_receipt: PackageActionReceipt | None
    restored_sha256: str | None
    reason: str

    def __post_init__(self) -> None:
        if self.state not in TERMINAL_UPDATE_STATES:
            raise ApplicationUpdateError(f"invalid terminal update state: {self.state}")
        if self.restored_sha256 is not None:
            object.__setattr__(
                self,
                "restored_sha256",
                _sha256(self.restored_sha256, "restored_sha256"),
            )
        object.__setattr__(self, "reason", _text(self.reason, "reason"))


@dataclass(frozen=True)
class UpdateStatus:
    state: str
    plan: UpdatePlan
    install_receipt: PackageActionReceipt | None
    rollback_receipt: PackageActionReceipt | None
    restored_sha256: str | None
    reason: str | None
    requires_recovery: bool
    retry_allowed: bool


@dataclass
class _Journal:
    plan: UpdatePlan
    state: str
    history: list[dict[str, str]]
    install_receipt: PackageActionReceipt | None = None
    rollback_receipt: PackageActionReceipt | None = None
    restored_sha256: str | None = None
    reason: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": UPDATE_SCHEMA_VERSION,
            "plan": self.plan.to_data(),
            "plan_fingerprint": self.plan.fingerprint,
            "state": self.state,
            "history": self.history,
            "install_receipt": (
                None if self.install_receipt is None else self.install_receipt.to_data()
            ),
            "rollback_receipt": (
                None if self.rollback_receipt is None else self.rollback_receipt.to_data()
            ),
            "restored_sha256": self.restored_sha256,
            "reason": self.reason,
        }


class ApplicationUpdateCoordinator:
    """Durable update transaction above replaceable platform package drivers.

    Artifact trust, one profile, a pre-update creative snapshot, a profile-scoped
    maintenance lease and post-install Headquarters validation are all required.
    Ambiguous outcomes remain explicit recovery work and are never retried or
    rolled back blindly.
    """

    def __init__(
        self,
        *,
        state_root: str | Path,
        memory_opener: Callable[[str | Path, str], HeadquartersMemory] = HeadquartersMemory.open,
    ):
        root = Path(state_root)
        if not root.is_absolute():
            raise ApplicationUpdateError("state_root must be absolute")
        if not callable(memory_opener):
            raise TypeError("memory_opener must be callable")
        self.state_root = root
        self._memory_opener = memory_opener
        self._leases = InstanceLeaseManager(root)

    @property
    def updates_root(self) -> Path:
        return self.state_root / "updates"

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

    def _prepare_updates_dir(self) -> None:
        root = self.updates_root
        if root.is_symlink():
            raise UpdateJournalCorruptionError("updates state root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise UpdateJournalCorruptionError("updates state root is not a real directory")

    def _journal_path(self, update_id: str) -> Path:
        update = str(update_id).strip().lower()
        if not _UPDATE_ID.fullmatch(update):
            raise ApplicationUpdateError("invalid update_id")
        return self.updates_root / f"{update}.json"

    @staticmethod
    def _journal_integrity(payload: dict[str, object]) -> str:
        return _digest_json(payload)

    @staticmethod
    def _request(plan: UpdatePlan, action: str) -> PackageActionRequest:
        return PackageActionRequest(
            update_id=plan.update_id,
            plan_fingerprint=plan.fingerprint,
            action=action,
            profile_id=plan.profile_id,
            current_release_id=plan.current_release_id,
            target_release_id=plan.target_release_id,
            artifact_id=plan.artifact_id,
            artifact_sha256=plan.artifact_sha256,
            target_fingerprint=plan.target_fingerprint,
        )

    @classmethod
    def _validate_receipt(
        cls,
        receipt: PackageActionReceipt,
        request: PackageActionRequest,
    ) -> PackageActionReceipt:
        if not isinstance(receipt, PackageActionReceipt):
            raise ApplicationUpdateError("package driver returned invalid receipt type")
        if receipt.action != request.action:
            raise ApplicationUpdateError("package receipt action does not match request")
        if receipt.request_fingerprint != request.fingerprint:
            raise ApplicationUpdateError("package receipt belongs to a different request")
        if request.action == "INSTALL":
            if receipt.state == "SUCCEEDED" and receipt.resulting_release_id != request.target_release_id:
                raise ApplicationUpdateError(
                    "successful install receipt does not prove the target release"
                )
            if receipt.state == "FAILED_SAFE" and receipt.resulting_release_id != request.current_release_id:
                raise ApplicationUpdateError(
                    "safe install failure does not prove the original release remained installed"
                )
        elif receipt.state == "SUCCEEDED" and receipt.resulting_release_id != request.current_release_id:
            raise ApplicationUpdateError(
                "successful rollback receipt does not prove the original release"
            )
        return receipt

    @classmethod
    def _validate_journal_semantics(cls, journal: _Journal) -> None:
        if journal.state not in UPDATE_STATES:
            raise UpdateJournalCorruptionError("update journal state is invalid")
        states = [entry.get("state") for entry in journal.history]
        if not states or states[0] != "PREPARED" or states[-1] != journal.state:
            raise UpdateJournalCorruptionError("update journal history endpoints are invalid")
        for previous, current in zip(states, states[1:]):
            if current not in _ALLOWED_TRANSITIONS.get(str(previous), set()):
                raise UpdateJournalCorruptionError(
                    f"illegal update journal transition: {previous}->{current}"
                )
        install = journal.install_receipt
        rollback = journal.rollback_receipt
        if install is not None:
            try:
                cls._validate_receipt(install, cls._request(journal.plan, "INSTALL"))
            except ApplicationUpdateError as exc:
                raise UpdateJournalCorruptionError(
                    "stored install receipt is inconsistent with the plan"
                ) from exc
        if rollback is not None:
            try:
                cls._validate_receipt(rollback, cls._request(journal.plan, "ROLLBACK"))
            except ApplicationUpdateError as exc:
                raise UpdateJournalCorruptionError(
                    "stored rollback receipt is inconsistent with the plan"
                ) from exc
        if journal.restored_sha256 is not None and journal.restored_sha256 != journal.plan.snapshot_sha256:
            raise UpdateJournalCorruptionError(
                "restored creative-state hash differs from the prepared snapshot"
            )
        state = journal.state
        if state == "PREPARED":
            if install is not None or rollback is not None or journal.restored_sha256 is not None:
                raise UpdateJournalCorruptionError("PREPARED update carries execution evidence")
        elif state == "INSTALLING":
            if rollback is not None or journal.restored_sha256 is not None:
                raise UpdateJournalCorruptionError("INSTALLING update carries later-phase evidence")
        elif state == "VALIDATING":
            if install is None or install.state != "SUCCEEDED" or rollback is not None:
                raise UpdateJournalCorruptionError("VALIDATING update lacks successful install evidence")
        elif state == "ROLLING_BACK":
            if install is None or install.state not in {"SUCCEEDED", "FAILED_CHANGED"}:
                raise UpdateJournalCorruptionError("ROLLING_BACK update lacks changed-package evidence")
            if rollback is not None or journal.restored_sha256 is not None:
                raise UpdateJournalCorruptionError("ROLLING_BACK update carries later-phase evidence")
        elif state == "RESTORING":
            if (
                install is None
                or install.state not in {"SUCCEEDED", "FAILED_CHANGED"}
                or rollback is None
                or rollback.state != "SUCCEEDED"
            ):
                raise UpdateJournalCorruptionError("RESTORING update lacks successful rollback evidence")
        elif state == "FINALIZING":
            known = (
                install is not None
                and (
                    install.state == "FAILED_SAFE"
                    or (install.state == "SUCCEEDED" and rollback is None)
                    or (
                        install.state in {"SUCCEEDED", "FAILED_CHANGED"}
                        and rollback is not None
                        and rollback.state == "SUCCEEDED"
                        and journal.restored_sha256 == journal.plan.snapshot_sha256
                    )
                )
            )
            if not known:
                raise UpdateJournalCorruptionError("FINALIZING outcome is not fully evidenced")
        elif state == "SUCCEEDED":
            if install is None or install.state != "SUCCEEDED" or rollback is not None:
                raise UpdateJournalCorruptionError("SUCCEEDED update lacks target-install evidence")
            if journal.restored_sha256 is not None:
                raise UpdateJournalCorruptionError("SUCCEEDED update restored old creative state")
        elif state == "FAILED_SAFE":
            if install is None or install.state != "FAILED_SAFE":
                raise UpdateJournalCorruptionError("FAILED_SAFE update lacks unchanged-package evidence")
            if rollback is not None or journal.restored_sha256 is not None:
                raise UpdateJournalCorruptionError("FAILED_SAFE update carries compensation evidence")
        elif state == "ROLLED_BACK":
            if (
                install is None
                or install.state not in {"SUCCEEDED", "FAILED_CHANGED"}
                or rollback is None
                or rollback.state != "SUCCEEDED"
                or journal.restored_sha256 != journal.plan.snapshot_sha256
            ):
                raise UpdateJournalCorruptionError("ROLLED_BACK update lacks exact compensation evidence")
        if state in TERMINAL_UPDATE_STATES and not journal.reason:
            raise UpdateJournalCorruptionError("terminal update state requires a reason")

    def _write_journal(self, journal: _Journal, *, create: bool = False) -> None:
        self._validate_journal_semantics(journal)
        self._prepare_updates_dir()
        path = self._journal_path(journal.plan.update_id)
        if path.is_symlink():
            raise UpdateJournalCorruptionError("update journal must not be a symlink")
        if create and path.exists():
            raise UpdateJournalCorruptionError("update journal already exists")
        payload = journal.payload()
        envelope = dict(payload)
        envelope["integrity_sha256"] = self._journal_integrity(payload)
        encoded = (_canonical_json(envelope) + "\n").encode("utf-8")
        temp = self.updates_root / f".{journal.plan.update_id}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp, flags, 0o600)
        try:
            total = 0
            while total < len(encoded):
                written = os.write(fd, encoded[total:])
                if written <= 0:
                    raise OSError("short write while persisting update journal")
                total += written
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temp, path)
            self._fsync_dir(self.updates_root)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_journal(self, update_id: str) -> _Journal:
        path = self._journal_path(update_id)
        if path.is_symlink() or not path.is_file():
            raise UpdateJournalCorruptionError("update journal is missing or not a real file")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise UpdateJournalCorruptionError("update journal is unreadable") from exc
        if not isinstance(data, dict):
            raise UpdateJournalCorruptionError("update journal must be an object")
        integrity = data.pop("integrity_sha256", None)
        if not isinstance(integrity, str) or not _SHA256.fullmatch(integrity):
            raise UpdateJournalCorruptionError("update journal integrity digest is invalid")
        if self._journal_integrity(data) != integrity:
            raise UpdateJournalCorruptionError("update journal integrity mismatch")
        if set(data) != {
            "schema_version",
            "plan",
            "plan_fingerprint",
            "state",
            "history",
            "install_receipt",
            "rollback_receipt",
            "restored_sha256",
            "reason",
        } or data["schema_version"] != UPDATE_SCHEMA_VERSION:
            raise UpdateJournalCorruptionError("update journal shape/version is invalid")
        plan = UpdatePlan.from_data(data["plan"])
        if data["plan_fingerprint"] != plan.fingerprint:
            raise UpdateJournalCorruptionError("update journal plan fingerprint mismatch")
        history_data = data["history"]
        if not isinstance(history_data, list) or not history_data:
            raise UpdateJournalCorruptionError("update journal history is invalid")
        history: list[dict[str, str]] = []
        for entry in history_data:
            if not isinstance(entry, dict) or set(entry) != {"state", "evidence"}:
                raise UpdateJournalCorruptionError("update journal history entry is invalid")
            state = str(entry["state"])
            if state not in UPDATE_STATES:
                raise UpdateJournalCorruptionError("update journal history state is invalid")
            try:
                evidence = _text(str(entry["evidence"]), "history evidence")
            except ApplicationUpdateError as exc:
                raise UpdateJournalCorruptionError("update journal history evidence is invalid") from exc
            history.append({"state": state, "evidence": evidence})
        try:
            install = (
                None
                if data["install_receipt"] is None
                else PackageActionReceipt.from_data(data["install_receipt"])
            )
            rollback = (
                None
                if data["rollback_receipt"] is None
                else PackageActionReceipt.from_data(data["rollback_receipt"])
            )
            restored = (
                None
                if data["restored_sha256"] is None
                else _sha256(str(data["restored_sha256"]), "restored_sha256")
            )
            reason = None if data["reason"] is None else _text(str(data["reason"]), "reason")
        except ApplicationUpdateError as exc:
            raise UpdateJournalCorruptionError("update journal evidence fields are invalid") from exc
        journal = _Journal(
            plan=plan,
            state=str(data["state"]),
            history=history,
            install_receipt=install,
            rollback_receipt=rollback,
            restored_sha256=restored,
            reason=reason,
        )
        self._validate_journal_semantics(journal)
        return journal

    def _transition(
        self,
        update_id: str,
        *,
        expected: set[str],
        new_state: str,
        evidence: str,
        install_receipt: PackageActionReceipt | None = None,
        rollback_receipt: PackageActionReceipt | None = None,
        restored_sha256: str | None = None,
        reason: str | None = None,
    ) -> _Journal:
        journal = self._read_journal(update_id)
        if journal.state not in expected:
            raise UpdateExecuteOnceError(
                f"update state is {journal.state}, expected one of {sorted(expected)}"
            )
        if new_state not in _ALLOWED_TRANSITIONS[journal.state]:
            raise ApplicationUpdateError(
                f"illegal update transition: {journal.state}->{new_state}"
            )
        journal.state = new_state
        journal.history.append(
            {"state": new_state, "evidence": _text(evidence, "evidence")}
        )
        if install_receipt is not None:
            journal.install_receipt = install_receipt
        if rollback_receipt is not None:
            journal.rollback_receipt = rollback_receipt
        if restored_sha256 is not None:
            journal.restored_sha256 = _sha256(restored_sha256, "restored_sha256")
        if reason is not None:
            journal.reason = _text(reason, "reason")
        self._write_journal(journal)
        return journal

    @staticmethod
    def _verify_artifact(
        *,
        plan: UpdatePlan | None,
        manifest: ReleaseManifest,
        artifact_id: str,
        artifact_bytes: bytes,
        target: SupportTarget,
        authenticity: ManifestAuthenticityEvidence,
    ):
        if not isinstance(artifact_bytes, (bytes, bytearray, memoryview)):
            raise TypeError("artifact_bytes must be bytes-like")
        verification = ReleaseArtifactVerifier.verify(
            manifest=manifest,
            artifact_id=artifact_id,
            artifact_bytes=bytes(artifact_bytes),
            expected_target=target,
            authenticity=authenticity,
        )
        if verification.status != "READY" or verification.actual_sha256 is None:
            raise UpdateRejectedError(
                f"release artifact is not eligible for update: {verification.status}"
            )
        if plan is not None:
            if manifest.fingerprint != plan.manifest_fingerprint:
                raise UpdateRejectedError("manifest fingerprint differs from prepared update")
            if manifest.release_id != plan.target_release_id or manifest.version != plan.target_version:
                raise UpdateRejectedError("manifest release identity differs from prepared update")
            if artifact_id != plan.artifact_id:
                raise UpdateRejectedError("artifact_id differs from prepared update")
            if target.fingerprint != plan.target_fingerprint:
                raise UpdateRejectedError("target differs from prepared update")
            if verification.actual_sha256 != plan.artifact_sha256:
                raise UpdateRejectedError("artifact bytes differ from prepared update")
        return verification

    @staticmethod
    def _logical_database_fingerprint(path: Path, expected_profile_id: str) -> str:
        if path.is_symlink() or not path.is_file():
            raise UpdateRejectedError("canonical database is missing or not a real file")
        conn: sqlite3.Connection | None = None
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.execute("PRAGMA query_only=ON")
            quick = conn.execute("PRAGMA quick_check").fetchone()
            if quick is None or str(quick[0]) != "ok":
                raise UpdateRejectedError("canonical database integrity check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise UpdateRejectedError("canonical database foreign-key check failed")
            rows = dict(
                conn.execute(
                    "SELECT key,value FROM metadata WHERE key IN ('profile_id','schema_version')"
                ).fetchall()
            )
            if str(rows.get("profile_id", "")) != expected_profile_id:
                raise UpdateRejectedError("canonical database profile identity mismatch")
            digest = hashlib.sha256()
            for line in conn.iterdump():
                digest.update(line.encode("utf-8"))
                digest.update(b"\n")
            return digest.hexdigest()
        except UpdateRejectedError:
            raise
        except sqlite3.DatabaseError as exc:
            raise UpdateRejectedError("canonical database could not be fingerprinted") from exc
        finally:
            if conn is not None:
                conn.close()

    @classmethod
    def _snapshot_logical_fingerprint(cls, snapshot: SnapshotInfo) -> str:
        return cls._logical_database_fingerprint(snapshot.path, snapshot.profile_id)

    @classmethod
    def _verify_snapshot_and_live(cls, plan: UpdatePlan, data_root: Path) -> None:
        if _root_fingerprint(data_root) != plan.data_root_fingerprint:
            raise UpdateRejectedError("data_root differs from the prepared update")
        snapshot = RecoveryManager.inspect_snapshot(data_root, plan.profile_id)
        if (
            snapshot.sha256 != plan.snapshot_sha256
            or snapshot.size_bytes != plan.snapshot_size_bytes
            or snapshot.lineage_schema_version != plan.snapshot_lineage_schema_version
        ):
            raise UpdateRejectedError("prepared creative-state snapshot changed or disappeared")
        if cls._snapshot_logical_fingerprint(snapshot) != plan.snapshot_logical_sha256:
            raise UpdateRejectedError("prepared snapshot logical state changed")
        live = data_root / "profiles" / plan.profile_id / LineageStore.DB_NAME
        if cls._logical_database_fingerprint(live, plan.profile_id) != plan.snapshot_logical_sha256:
            raise UpdateRejectedError(
                "live creative state changed after update preparation; prepare a new update"
            )

    @staticmethod
    def _coordination_lease_id(profile_id: str) -> str:
        return f"__app_update_{_profile(profile_id)}__"

    def _acquire_coordination(
        self,
        profile_id: str,
        process: ProcessIdentity,
        probe: ProcessProbe,
    ) -> InstanceLease:
        result = self._leases.acquire(
            self._coordination_lease_id(profile_id), process, probe
        )
        if result.status in {"ALREADY_OWNED", "HELD_BY_OTHER", "UNCERTAIN"}:
            raise UpdateBusyError(
                f"profile update ownership unavailable: {result.status}"
            )
        if result.status not in {"ACQUIRED", "REPLACED_STALE"} or result.lease is None:
            raise UpdateBusyError(
                f"unexpected profile update ownership state: {result.status}"
            )
        return result.lease

    def _release_coordination(
        self, profile_id: str, process: ProcessIdentity, lease: InstanceLease
    ) -> None:
        self._leases.release(
            self._coordination_lease_id(profile_id),
            process=process,
            lease_nonce=lease.lease_nonce,
        )

    def _acquire_profile_hold(
        self,
        profile_id: str,
        process: ProcessIdentity,
        probe: ProcessProbe,
    ) -> InstanceLease:
        result = self._leases.acquire(_profile(profile_id), process, probe)
        if result.status in {"ALREADY_OWNED", "HELD_BY_OTHER", "UNCERTAIN"}:
            raise UpdateBusyError(
                f"profile runtime ownership unavailable for update maintenance: {result.status}"
            )
        if result.status not in {"ACQUIRED", "REPLACED_STALE"} or result.lease is None:
            raise UpdateBusyError(
                f"unexpected profile maintenance ownership state: {result.status}"
            )
        return result.lease

    def _release_profile_hold(
        self, profile_id: str, process: ProcessIdentity, lease: InstanceLease
    ) -> None:
        self._leases.release(
            _profile(profile_id),
            process=process,
            lease_nonce=lease.lease_nonce,
        )

    def _blocking_updates(self, profile_id: str) -> tuple[UpdateStatus, ...]:
        self._prepare_updates_dir()
        statuses: list[UpdateStatus] = []
        for path in sorted(self.updates_root.iterdir()):
            if path.name.startswith("."):
                continue
            if path.is_symlink() or not path.is_file():
                raise UpdateJournalCorruptionError(
                    "update state contains a non-regular journal candidate"
                )
            if not _UPDATE_ID.fullmatch(path.stem) or path.suffix != ".json":
                raise UpdateJournalCorruptionError("update journal filename is invalid")
            status = self.status(path.stem)
            if status.plan.profile_id == profile_id and status.state not in SETTLED_UPDATE_STATES:
                statuses.append(status)
        return tuple(statuses)

    def prepare(
        self,
        *,
        runtime: ApplicationRuntime,
        current_release_id: str,
        manifest: ReleaseManifest,
        artifact_id: str,
        artifact_bytes: bytes,
        target: SupportTarget,
        authenticity: ManifestAuthenticityEvidence,
        process: ProcessIdentity,
        probe: ProcessProbe,
    ) -> UpdatePlan:
        if not isinstance(runtime, ApplicationRuntime):
            raise TypeError("runtime must be ApplicationRuntime")
        if runtime.state != "RUNNING" or runtime.profile_id is None:
            raise UpdateRejectedError(
                "update preparation requires a RUNNING owned profile runtime"
            )
        if not isinstance(process, ProcessIdentity):
            raise TypeError("process must be ProcessIdentity")
        runtime_process = getattr(runtime, "_process", None)
        if (
            not isinstance(runtime_process, ProcessIdentity)
            or runtime_process.fingerprint != process.fingerprint
        ):
            raise UpdateRejectedError(
                "update preparation process does not own the running profile runtime"
            )
        profile_id = _profile(runtime.profile_id)
        data_root = _normalized_root(runtime.data_root)
        current_release = _text(current_release_id, "current_release_id")
        verification = self._verify_artifact(
            plan=None,
            manifest=manifest,
            artifact_id=artifact_id,
            artifact_bytes=artifact_bytes,
            target=target,
            authenticity=authenticity,
        )
        coordination = self._acquire_coordination(profile_id, process, probe)
        plan: UpdatePlan | None = None
        primary_error: BaseException | None = None
        try:
            if self._blocking_updates(profile_id):
                raise UpdateRejectedError(
                    "profile already has unresolved or prepared update work"
                )
            snapshot = RecoveryManager(runtime.headquarters.store).create_snapshot()
            snapshot_logical = self._snapshot_logical_fingerprint(snapshot)
            quit_result = runtime.quit()
            if quit_result.status != "STOPPED":
                raise UpdateRejectedError(
                    f"runtime could not reach STOPPED before update preparation: {quit_result.status}"
                )
            live = data_root / "profiles" / profile_id / LineageStore.DB_NAME
            if self._logical_database_fingerprint(live, profile_id) != snapshot_logical:
                raise UpdateRejectedError(
                    "creative state changed while update preparation was stopping the runtime"
                )
            plan = UpdatePlan(
                update_id=f"upd_{uuid.uuid4().hex}",
                profile_id=profile_id,
                current_release_id=current_release,
                target_release_id=manifest.release_id,
                target_version=manifest.version,
                manifest_fingerprint=manifest.fingerprint,
                artifact_id=artifact_id,
                artifact_sha256=verification.actual_sha256,
                target_fingerprint=target.fingerprint,
                data_root_fingerprint=_root_fingerprint(data_root),
                snapshot_sha256=snapshot.sha256,
                snapshot_logical_sha256=snapshot_logical,
                snapshot_size_bytes=snapshot.size_bytes,
                snapshot_lineage_schema_version=snapshot.lineage_schema_version,
            )
            self._write_journal(
                _Journal(
                    plan=plan,
                    state="PREPARED",
                    history=[
                        {
                            "state": "PREPARED",
                            "evidence": f"snapshot:{snapshot.sha256}",
                        }
                    ],
                ),
                create=True,
            )
        except BaseException as exc:
            primary_error = exc
        release_error: BaseException | None = None
        try:
            self._release_coordination(profile_id, process, coordination)
        except BaseException as exc:
            release_error = exc
        if release_error is not None and plan is not None:
            try:
                self._transition(
                    plan.update_id,
                    expected={"PREPARED"},
                    new_state="RECOVERY_REQUIRED",
                    evidence="prepare:coordination-release-failed",
                    reason=(
                        "no package mutation started, but profile update ownership "
                        f"could not be released after preparation: {release_error}"
                    ),
                )
            except Exception:
                pass
            raise UpdateRejectedError(
                f"update {plan.update_id} requires recovery after preparation cleanup failed"
            ) from release_error
        if primary_error is not None:
            if release_error is not None:
                raise UpdateRejectedError(
                    f"update preparation failed and update ownership cleanup also failed: {release_error}"
                ) from primary_error
            raise primary_error
        if release_error is not None:
            raise UpdateRejectedError(
                f"profile update ownership cleanup failed during preparation: {release_error}"
            ) from release_error
        if plan is None:
            raise ApplicationUpdateError("update preparation produced no plan")
        return plan

    def _to_result(self, journal: _Journal) -> UpdateResult:
        if journal.state not in TERMINAL_UPDATE_STATES:
            raise ApplicationUpdateError("journal is not terminal")
        return UpdateResult(
            journal.state,
            journal.plan,
            journal.install_receipt,
            journal.rollback_receipt,
            journal.restored_sha256,
            journal.reason or f"update ended in {journal.state}",
        )

    def _mark_recovery(
        self,
        plan: UpdatePlan,
        *,
        expected: set[str],
        evidence: str,
        reason: str,
        install_receipt: PackageActionReceipt | None = None,
        rollback_receipt: PackageActionReceipt | None = None,
    ) -> _Journal:
        return self._transition(
            plan.update_id,
            expected=expected,
            new_state="RECOVERY_REQUIRED",
            evidence=evidence,
            reason=reason,
            install_receipt=install_receipt,
            rollback_receipt=rollback_receipt,
        )

    def _finish_recovery(
        self,
        journal: _Journal,
        *,
        process: ProcessIdentity,
        coordination: InstanceLease,
        profile_hold: InstanceLease | None,
        retain_profile_hold: bool,
    ) -> UpdateResult:
        if profile_hold is not None and not retain_profile_hold:
            try:
                self._release_profile_hold(journal.plan.profile_id, process, profile_hold)
            except Exception:
                retain_profile_hold = True
        try:
            self._release_coordination(journal.plan.profile_id, process, coordination)
        except Exception:
            pass
        return self._to_result(journal)

    def _finalize(
        self,
        plan: UpdatePlan,
        *,
        process: ProcessIdentity,
        coordination: InstanceLease,
        profile_hold: InstanceLease,
        terminal_state: str,
        terminal_evidence: str,
    ) -> UpdateResult:
        if terminal_state not in SETTLED_UPDATE_STATES:
            raise ApplicationUpdateError("final terminal state must be settled")
        try:
            self._release_profile_hold(plan.profile_id, process, profile_hold)
        except Exception as exc:
            journal = self._mark_recovery(
                plan,
                expected={"FINALIZING"},
                evidence="finalize:profile-hold-release-failed",
                reason=(
                    "package/creative-state outcome is known, but the maintenance "
                    f"profile hold could not be released: {exc}"
                ),
            )
            try:
                self._release_coordination(plan.profile_id, process, coordination)
            except Exception:
                pass
            return self._to_result(journal)
        try:
            self._release_coordination(plan.profile_id, process, coordination)
        except Exception as exc:
            journal = self._mark_recovery(
                plan,
                expected={"FINALIZING"},
                evidence="finalize:coordination-release-failed",
                reason=(
                    "package/creative-state outcome is known, but profile update "
                    f"coordination could not be released: {exc}"
                ),
            )
            return self._to_result(journal)
        terminal = self._transition(
            plan.update_id,
            expected={"FINALIZING"},
            new_state=terminal_state,
            evidence=terminal_evidence,
        )
        return self._to_result(terminal)

    def _compensate(
        self,
        plan: UpdatePlan,
        *,
        data_root: Path,
        driver: PackageDriver,
        process: ProcessIdentity,
        coordination: InstanceLease,
        profile_hold: InstanceLease,
        starting_state: str,
        install_receipt: PackageActionReceipt,
        reason: str,
    ) -> UpdateResult:
        self._transition(
            plan.update_id,
            expected={starting_state},
            new_state="ROLLING_BACK",
            evidence=f"compensate:{reason}",
            install_receipt=install_receipt,
            reason=reason,
        )
        rollback_request = self._request(plan, "ROLLBACK")
        try:
            rollback_receipt = self._validate_receipt(
                driver.perform(rollback_request, None), rollback_request
            )
        except Exception as exc:
            journal = self._mark_recovery(
                plan,
                expected={"ROLLING_BACK"},
                evidence="rollback:exception",
                reason=f"package rollback outcome is ambiguous: {exc}",
                install_receipt=install_receipt,
            )
            return self._finish_recovery(
                journal,
                process=process,
                coordination=coordination,
                profile_hold=profile_hold,
                retain_profile_hold=True,
            )
        if rollback_receipt.state != "SUCCEEDED":
            journal = self._mark_recovery(
                plan,
                expected={"ROLLING_BACK"},
                evidence=f"rollback:{rollback_receipt.evidence_ref}",
                reason=f"package rollback did not succeed: {rollback_receipt.state}",
                install_receipt=install_receipt,
                rollback_receipt=rollback_receipt,
            )
            return self._finish_recovery(
                journal,
                process=process,
                coordination=coordination,
                profile_hold=profile_hold,
                retain_profile_hold=True,
            )
        self._transition(
            plan.update_id,
            expected={"ROLLING_BACK"},
            new_state="RESTORING",
            evidence=f"rollback:{rollback_receipt.evidence_ref}",
            install_receipt=install_receipt,
            rollback_receipt=rollback_receipt,
            reason=reason,
        )
        try:
            restored = RecoveryManager.restore_snapshot(
                data_root, plan.profile_id, expected_sha256=plan.snapshot_sha256
            )
        except Exception as exc:
            journal = self._mark_recovery(
                plan,
                expected={"RESTORING"},
                evidence="restore:exception",
                reason=f"creative-state snapshot restore failed: {exc}",
                install_receipt=install_receipt,
                rollback_receipt=rollback_receipt,
            )
            return self._finish_recovery(
                journal,
                process=process,
                coordination=coordination,
                profile_hold=profile_hold,
                retain_profile_hold=True,
            )
        if restored.installed_sha256 != plan.snapshot_sha256:
            journal = self._mark_recovery(
                plan,
                expected={"RESTORING"},
                evidence="restore:hash-mismatch",
                reason="restored creative state does not match the prepared snapshot",
                install_receipt=install_receipt,
                rollback_receipt=rollback_receipt,
            )
            return self._finish_recovery(
                journal,
                process=process,
                coordination=coordination,
                profile_hold=profile_hold,
                retain_profile_hold=True,
            )
        self._transition(
            plan.update_id,
            expected={"RESTORING"},
            new_state="FINALIZING",
            evidence=f"restore:{restored.installed_sha256}",
            install_receipt=install_receipt,
            rollback_receipt=rollback_receipt,
            restored_sha256=restored.installed_sha256,
            reason=reason,
        )
        return self._finalize(
            plan,
            process=process,
            coordination=coordination,
            profile_hold=profile_hold,
            terminal_state="ROLLED_BACK",
            terminal_evidence="finalize:rolled-back",
        )

    def _validate_installed_headquarters(self, data_root: Path, profile_id: str) -> None:
        headquarters: HeadquartersMemory | None = None
        try:
            headquarters = self._memory_opener(data_root, profile_id)
            if not isinstance(headquarters, HeadquartersMemory):
                raise UpdateRejectedError("memory_opener did not return HeadquartersMemory")
            if headquarters.store.profile_id != profile_id:
                raise UpdateRejectedError("updated Headquarters opened a different profile")
            quick = headquarters.store._conn.execute("PRAGMA quick_check").fetchone()
            foreign = headquarters.store._conn.execute("PRAGMA foreign_key_check").fetchall()
            if quick is None or str(quick[0]) != "ok" or foreign:
                raise UpdateRejectedError("post-update canonical database integrity check failed")
        finally:
            if headquarters is not None:
                headquarters.close()

    def apply(
        self,
        *,
        plan: UpdatePlan,
        data_root: str | Path,
        manifest: ReleaseManifest,
        artifact_bytes: bytes,
        target: SupportTarget,
        authenticity: ManifestAuthenticityEvidence,
        driver: PackageDriver,
        process: ProcessIdentity,
        probe: ProcessProbe,
    ) -> UpdateResult:
        if not isinstance(plan, UpdatePlan):
            raise TypeError("plan must be UpdatePlan")
        if not isinstance(process, ProcessIdentity):
            raise TypeError("process must be ProcessIdentity")
        data = _normalized_root(data_root)
        journal = self._read_journal(plan.update_id)
        if journal.plan != plan:
            raise UpdateRejectedError("supplied update plan differs from durable journal")
        if journal.state != "PREPARED":
            raise UpdateExecuteOnceError(
                f"update is execute-once and durable state is already {journal.state}"
            )
        self._verify_artifact(
            plan=plan,
            manifest=manifest,
            artifact_id=plan.artifact_id,
            artifact_bytes=artifact_bytes,
            target=target,
            authenticity=authenticity,
        )
        coordination = self._acquire_coordination(plan.profile_id, process, probe)
        profile_hold: InstanceLease | None = None
        try:
            current = self._read_journal(plan.update_id)
            if current.state != "PREPARED" or current.plan != plan:
                raise UpdateExecuteOnceError("update changed while execution ownership was acquired")
            profile_hold = self._acquire_profile_hold(plan.profile_id, process, probe)
            self._verify_snapshot_and_live(plan, data)
            self._transition(
                plan.update_id,
                expected={"PREPARED"},
                new_state="INSTALLING",
                evidence="install:started",
            )
            install_request = self._request(plan, "INSTALL")
            try:
                install_receipt = self._validate_receipt(
                    driver.perform(install_request, bytes(artifact_bytes)), install_request
                )
            except Exception as exc:
                recovery = self._mark_recovery(
                    plan,
                    expected={"INSTALLING"},
                    evidence="install:exception",
                    reason=f"package install outcome is ambiguous: {exc}",
                )
                return self._finish_recovery(
                    recovery,
                    process=process,
                    coordination=coordination,
                    profile_hold=profile_hold,
                    retain_profile_hold=True,
                )
            if install_receipt.state == "UNKNOWN":
                recovery = self._mark_recovery(
                    plan,
                    expected={"INSTALLING"},
                    evidence=f"install:{install_receipt.evidence_ref}",
                    reason="package install outcome is UNKNOWN; blind retry and rollback are forbidden",
                    install_receipt=install_receipt,
                )
                return self._finish_recovery(
                    recovery,
                    process=process,
                    coordination=coordination,
                    profile_hold=profile_hold,
                    retain_profile_hold=True,
                )
            if install_receipt.state == "FAILED_SAFE":
                self._transition(
                    plan.update_id,
                    expected={"INSTALLING"},
                    new_state="FINALIZING",
                    evidence=f"install:{install_receipt.evidence_ref}",
                    install_receipt=install_receipt,
                    reason="package driver proved install failed before changing package state",
                )
                return self._finalize(
                    plan,
                    process=process,
                    coordination=coordination,
                    profile_hold=profile_hold,
                    terminal_state="FAILED_SAFE",
                    terminal_evidence="finalize:failed-safe",
                )
            if install_receipt.state == "FAILED_CHANGED":
                return self._compensate(
                    plan,
                    data_root=data,
                    driver=driver,
                    process=process,
                    coordination=coordination,
                    profile_hold=profile_hold,
                    starting_state="INSTALLING",
                    install_receipt=install_receipt,
                    reason="package install failed after changing package state",
                )
            self._transition(
                plan.update_id,
                expected={"INSTALLING"},
                new_state="VALIDATING",
                evidence=f"install:{install_receipt.evidence_ref}",
                install_receipt=install_receipt,
            )
            validation_failure: str | None = None
            try:
                self._validate_installed_headquarters(data, plan.profile_id)
            except Exception as exc:
                validation_failure = f"post-update Headquarters validation failed: {exc}"
            if validation_failure is not None:
                return self._compensate(
                    plan,
                    data_root=data,
                    driver=driver,
                    process=process,
                    coordination=coordination,
                    profile_hold=profile_hold,
                    starting_state="VALIDATING",
                    install_receipt=install_receipt,
                    reason=validation_failure,
                )
            self._transition(
                plan.update_id,
                expected={"VALIDATING"},
                new_state="FINALIZING",
                evidence="validation:headquarters-open-integrity-close",
                install_receipt=install_receipt,
                reason="package install and canonical Headquarters validation succeeded",
            )
            return self._finalize(
                plan,
                process=process,
                coordination=coordination,
                profile_hold=profile_hold,
                terminal_state="SUCCEEDED",
                terminal_evidence="finalize:succeeded",
            )
        except (UpdateRejectedError, UpdateBusyError, UpdateExecuteOnceError):
            if profile_hold is not None:
                try:
                    self._release_profile_hold(plan.profile_id, process, profile_hold)
                except Exception:
                    pass
            try:
                self._release_coordination(plan.profile_id, process, coordination)
            except Exception:
                pass
            raise
        except Exception as exc:
            current = self._read_journal(plan.update_id)
            if current.state in IN_FLIGHT_UPDATE_STATES:
                try:
                    current = self._mark_recovery(
                        plan,
                        expected={current.state},
                        evidence="coordinator:unexpected-exception",
                        reason=f"update coordinator failed after mutation may have started: {exc}",
                        install_receipt=current.install_receipt,
                        rollback_receipt=current.rollback_receipt,
                    )
                except Exception:
                    pass
            if current.state == "RECOVERY_REQUIRED":
                return self._finish_recovery(
                    current,
                    process=process,
                    coordination=coordination,
                    profile_hold=profile_hold,
                    retain_profile_hold=True,
                )
            if profile_hold is not None:
                try:
                    self._release_profile_hold(plan.profile_id, process, profile_hold)
                except Exception:
                    pass
            try:
                self._release_coordination(plan.profile_id, process, coordination)
            except Exception:
                pass
            raise

    def status(self, update_id: str) -> UpdateStatus:
        journal = self._read_journal(update_id)
        in_flight = journal.state in IN_FLIGHT_UPDATE_STATES
        return UpdateStatus(
            journal.state,
            journal.plan,
            journal.install_receipt,
            journal.rollback_receipt,
            journal.restored_sha256,
            journal.reason,
            requires_recovery=(in_flight or journal.state == "RECOVERY_REQUIRED"),
            retry_allowed=(journal.state == "PREPARED"),
        )
