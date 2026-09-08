from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .instance import InstanceLease
from .migration import (
    ApplicationSchemaMigrator,
    MigrationPlan,
    MigrationPlanError,
    MigrationResult,
    MigrationStep,
    MigrationValidationError,
)
from .recovery import RecoveryManager, SnapshotInfo
from .schema_program import normalize_migration_program, select_migration_chain

_BINDING_SCHEMA_VERSION = 2


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _step_to_data(step: MigrationStep) -> dict[str, object]:
    return {
        "from_version": step.from_version,
        "to_version": step.to_version,
        "description": step.description,
        "statements": list(step.statements),
    }


def _step_from_data(data: object) -> MigrationStep:
    if not isinstance(data, dict) or set(data) != {
        "from_version",
        "to_version",
        "description",
        "statements",
    }:
        raise MigrationValidationError("stored update migration step shape is invalid")
    statements = data["statements"]
    if not isinstance(statements, list) or not all(isinstance(item, str) for item in statements):
        raise MigrationValidationError("stored update migration SQL list is invalid")
    try:
        return MigrationStep(
            from_version=data["from_version"],  # type: ignore[arg-type]
            to_version=data["to_version"],  # type: ignore[arg-type]
            description=str(data["description"]),
            statements=tuple(statements),
        )
    except Exception as exc:
        raise MigrationValidationError("stored update migration step is invalid") from exc


def _steps_to_data(steps: Iterable[MigrationStep]) -> list[dict[str, object]]:
    return [_step_to_data(step) for step in normalize_migration_program(steps)]


def _steps_from_data(data: object) -> tuple[MigrationStep, ...]:
    if not isinstance(data, list):
        raise MigrationValidationError("stored schema migration program is invalid")
    try:
        return normalize_migration_program(_step_from_data(item) for item in data)
    except MigrationValidationError:
        raise
    except Exception as exc:
        raise MigrationValidationError("stored schema migration program is invalid") from exc


def _plan_to_data(plan: MigrationPlan) -> dict[str, object]:
    return {
        "migration_id": plan.migration_id,
        "profile_id": plan.profile_id,
        "source_version": plan.source_version,
        "target_version": plan.target_version,
        "source_identity_fingerprint": plan.source_identity_fingerprint,
        "source_history_fingerprint": plan.source_history_fingerprint,
        "steps": [_step_to_data(step) for step in plan.steps],
    }


def _plan_from_data(data: object) -> MigrationPlan:
    if not isinstance(data, dict) or set(data) != {
        "migration_id",
        "profile_id",
        "source_version",
        "target_version",
        "source_identity_fingerprint",
        "source_history_fingerprint",
        "steps",
    }:
        raise MigrationValidationError("stored update migration plan shape is invalid")
    raw_steps = data["steps"]
    if not isinstance(raw_steps, list):
        raise MigrationValidationError("stored update migration plan steps are invalid")
    try:
        return MigrationPlan(
            migration_id=str(data["migration_id"]),
            profile_id=str(data["profile_id"]),
            source_version=data["source_version"],  # type: ignore[arg-type]
            target_version=data["target_version"],  # type: ignore[arg-type]
            source_identity_fingerprint=str(data["source_identity_fingerprint"]),
            source_history_fingerprint=str(data["source_history_fingerprint"]),
            steps=tuple(_step_from_data(item) for item in raw_steps),
        )
    except MigrationValidationError:
        raise
    except Exception as exc:
        raise MigrationValidationError("stored update migration plan is invalid") from exc


@dataclass(frozen=True)
class UpdateMigrationBinding:
    update_id: str
    update_plan_fingerprint: str
    manifest_fingerprint: str
    target_release_id: str
    rollback_snapshot_sha256: str
    rollback_snapshot_size_bytes: int
    schema_program: tuple[MigrationStep, ...]
    migration_plan: MigrationPlan

    def __post_init__(self) -> None:
        update_id = str(self.update_id).strip()
        target_release = str(self.target_release_id).strip()
        if not update_id or not target_release:
            raise MigrationValidationError("update migration binding identity is incomplete")
        object.__setattr__(self, "update_id", update_id)
        object.__setattr__(self, "target_release_id", target_release)
        for field in (
            "update_plan_fingerprint",
            "manifest_fingerprint",
            "rollback_snapshot_sha256",
        ):
            value = str(getattr(self, field)).strip().lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise MigrationValidationError(f"{field} must be a lowercase SHA-256")
            object.__setattr__(self, field, value)
        if (
            isinstance(self.rollback_snapshot_size_bytes, bool)
            or not isinstance(self.rollback_snapshot_size_bytes, int)
            or self.rollback_snapshot_size_bytes <= 0
        ):
            raise MigrationValidationError("rollback snapshot size must be positive")
        if not isinstance(self.migration_plan, MigrationPlan):
            raise TypeError("migration_plan must be MigrationPlan")
        try:
            program = normalize_migration_program(self.schema_program)
        except Exception as exc:
            raise MigrationValidationError("update schema program is invalid") from exc
        object.__setattr__(self, "schema_program", program)
        if self.migration_plan.profile_id == "":
            raise MigrationValidationError("migration plan profile identity is missing")
        expected = select_migration_chain(
            source_version=self.migration_plan.source_version,
            target_version=self.migration_plan.target_version,
            program=program,
        )
        if expected != self.migration_plan.steps:
            raise MigrationValidationError(
                "selected migration plan does not match authenticated schema program"
            )

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": _BINDING_SCHEMA_VERSION,
            "update_id": self.update_id,
            "update_plan_fingerprint": self.update_plan_fingerprint,
            "manifest_fingerprint": self.manifest_fingerprint,
            "target_release_id": self.target_release_id,
            "rollback_snapshot_sha256": self.rollback_snapshot_sha256,
            "rollback_snapshot_size_bytes": self.rollback_snapshot_size_bytes,
            "schema_program": _steps_to_data(self.schema_program),
            "migration_plan": _plan_to_data(self.migration_plan),
        }

    @property
    def fingerprint(self) -> str:
        return _digest(self.payload())

    @classmethod
    def from_payload(cls, data: object) -> "UpdateMigrationBinding":
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "update_id",
            "update_plan_fingerprint",
            "manifest_fingerprint",
            "target_release_id",
            "rollback_snapshot_sha256",
            "rollback_snapshot_size_bytes",
            "schema_program",
            "migration_plan",
        }:
            raise MigrationValidationError("update migration binding shape is invalid")
        if data["schema_version"] != _BINDING_SCHEMA_VERSION:
            raise MigrationValidationError("unsupported update migration binding version")
        try:
            return cls(
                update_id=str(data["update_id"]),
                update_plan_fingerprint=str(data["update_plan_fingerprint"]),
                manifest_fingerprint=str(data["manifest_fingerprint"]),
                target_release_id=str(data["target_release_id"]),
                rollback_snapshot_sha256=str(data["rollback_snapshot_sha256"]),
                rollback_snapshot_size_bytes=data["rollback_snapshot_size_bytes"],  # type: ignore[arg-type]
                schema_program=_steps_from_data(data["schema_program"]),
                migration_plan=_plan_from_data(data["migration_plan"]),
            )
        except MigrationValidationError:
            raise
        except Exception as exc:
            raise MigrationValidationError("update migration binding is invalid") from exc


class UpdateMigrationBindingStore:
    """Crash-consistent durable binding between one update and one schema plan."""

    def __init__(self, state_root: str | Path):
        root = Path(state_root)
        if not root.is_absolute():
            raise MigrationPlanError("state_root must be absolute")
        self.root = Path(os.path.abspath(os.path.normpath(str(root)))) / "update-schema"

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

    def _path(self, update_id: str) -> Path:
        update = str(update_id).strip()
        if not update or "/" in update or "\\" in update:
            raise MigrationValidationError("invalid update_id for migration binding")
        return self.root / f"{update}.json"

    def create(self, binding: UpdateMigrationBinding) -> None:
        if not isinstance(binding, UpdateMigrationBinding):
            raise TypeError("binding must be UpdateMigrationBinding")
        if self.root.is_symlink():
            raise MigrationValidationError("update schema state root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise MigrationValidationError("update schema state root is not a real directory")
        final = self._path(binding.update_id)
        if final.exists() or final.is_symlink():
            raise MigrationValidationError("update migration binding already exists")
        payload = binding.payload()
        envelope = {
            "payload": payload,
            "binding_fingerprint": binding.fingerprint,
            "integrity_sha256": _digest(payload),
        }
        encoded = (_canonical_json(envelope) + "\n").encode("utf-8")
        temp = self.root / f".{binding.update_id}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp, flags, 0o600)
        try:
            total = 0
            while total < len(encoded):
                written = os.write(fd, encoded[total:])
                if written <= 0:
                    raise OSError("short write while persisting update migration binding")
                total += written
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temp, final)
            self._fsync_dir(self.root)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def read(self, update_id: str) -> UpdateMigrationBinding:
        path = self._path(update_id)
        if path.is_symlink() or not path.is_file():
            raise MigrationValidationError("update migration binding is missing or not a real file")
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise MigrationValidationError("update migration binding is unreadable") from exc
        if not isinstance(envelope, dict) or set(envelope) != {
            "payload",
            "binding_fingerprint",
            "integrity_sha256",
        }:
            raise MigrationValidationError("update migration binding envelope is invalid")
        payload = envelope["payload"]
        if str(envelope["integrity_sha256"]) != _digest(payload):
            raise MigrationValidationError("update migration binding integrity mismatch")
        binding = UpdateMigrationBinding.from_payload(payload)
        if str(envelope["binding_fingerprint"]) != binding.fingerprint:
            raise MigrationValidationError("update migration binding fingerprint mismatch")
        return binding


class UpdateBoundSchemaMigrator(ApplicationSchemaMigrator):
    """Schema migrator that reuses an update's existing maintenance hold/snapshot.

    The authenticated release supplies a full ordered migration program. Planning
    inspects this profile's current semantic schema and derives only the contiguous
    subset it needs. Read-only planning intentionally does not treat a persisted
    runtime lease as authoritative liveness. Execution receives the already-acquired
    update hold, verifies exact ownership, and reuses the update's pre-package
    snapshot rather than overwriting canonical recovery state mid-transaction.
    """

    def __init__(
        self,
        data_root: str | Path,
        state_root: str | Path,
        *,
        rollback_snapshot_sha256: str,
        rollback_snapshot_size_bytes: int,
    ):
        super().__init__(data_root, state_root)
        snapshot_sha = str(rollback_snapshot_sha256).strip().lower()
        if len(snapshot_sha) != 64 or any(ch not in "0123456789abcdef" for ch in snapshot_sha):
            raise MigrationPlanError("rollback_snapshot_sha256 must be a lowercase SHA-256")
        if (
            isinstance(rollback_snapshot_size_bytes, bool)
            or not isinstance(rollback_snapshot_size_bytes, int)
            or rollback_snapshot_size_bytes <= 0
        ):
            raise MigrationPlanError("rollback_snapshot_size_bytes must be positive")
        self.rollback_snapshot_sha256 = snapshot_sha
        self.rollback_snapshot_size_bytes = rollback_snapshot_size_bytes
        self._bound_snapshot: SnapshotInfo | None = None

    def prepare_read_only(
        self,
        *,
        profile_id: str,
        target_version: int,
        steps: Iterable[MigrationStep],
    ) -> MigrationPlan:
        if isinstance(target_version, bool) or not isinstance(target_version, int):
            raise MigrationPlanError("target_version must be an integer")
        if target_version < 1:
            raise MigrationPlanError("target_version must be positive")
        source = self._inspect_path(self._db_path(profile_id), profile_id)
        selected = select_migration_chain(
            source_version=source.application_version,
            target_version=target_version,
            program=steps,
        )
        return MigrationPlan(
            migration_id=f"mig_{uuid.uuid4().hex}",
            profile_id=profile_id,
            source_version=source.application_version,
            target_version=target_version,
            source_identity_fingerprint=source.identity_fingerprint,
            source_history_fingerprint=source.history_fingerprint,
            steps=selected,
        )

    def _existing_rollback_snapshot(self, profile_id: str) -> SnapshotInfo:
        snapshot = RecoveryManager.inspect_snapshot(self.data_root, profile_id)
        if snapshot.sha256 != self.rollback_snapshot_sha256:
            raise MigrationValidationError("update rollback snapshot hash changed before schema migration")
        if snapshot.size_bytes != self.rollback_snapshot_size_bytes:
            raise MigrationValidationError("update rollback snapshot size changed before schema migration")
        return snapshot

    def _create_execution_snapshot(self, plan: MigrationPlan) -> SnapshotInfo:
        if self._bound_snapshot is None:
            raise MigrationValidationError("update-bound rollback snapshot was not established")
        latest = self._existing_rollback_snapshot(plan.profile_id)
        if latest.sha256 != self._bound_snapshot.sha256:
            raise MigrationValidationError("update rollback snapshot changed during schema migration")
        return latest

    def migrate_under_maintenance(
        self,
        plan: MigrationPlan,
        *,
        maintenance_lease: InstanceLease,
    ) -> MigrationResult:
        if not isinstance(plan, MigrationPlan):
            raise TypeError("plan must be MigrationPlan")
        if not isinstance(maintenance_lease, InstanceLease):
            raise TypeError("maintenance_lease must be InstanceLease")
        if maintenance_lease.profile_id != plan.profile_id:
            raise MigrationValidationError("update maintenance hold belongs to a different profile")
        current_hold = self._leases.inspect(plan.profile_id)
        if current_hold != maintenance_lease:
            raise MigrationValidationError("exact update maintenance hold is not owned")
        current = self._inspect_path(self._db_path(plan.profile_id), plan.profile_id)
        self._require_prepared_state(plan, current)
        snapshot = self._existing_rollback_snapshot(plan.profile_id)
        if plan.target_version == plan.source_version:
            return MigrationResult(
                "NO_CHANGE",
                plan,
                plan.source_version,
                snapshot.sha256,
                "update target uses current semantic schema; rollback snapshot remains exact and no migration write is required",
            )
        self._bound_snapshot = snapshot
        try:
            return self._migrate_owned(plan, maintenance_lease)
        finally:
            self._bound_snapshot = None
