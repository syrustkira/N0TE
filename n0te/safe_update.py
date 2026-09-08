from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .artifacts import ReleaseManifest
from .instance import ProcessIdentity
from .memory import HeadquartersMemory
from .migration import MigrationStep
from .schema_program import (
    migration_steps_fingerprint,
    normalize_migration_program,
    select_migration_chain,
)
from .update import (
    ApplicationUpdateCoordinator as _BaseApplicationUpdateCoordinator,
    ApplicationUpdateError,
    UpdatePlan,
    UpdateRejectedError,
    UpdateResult,
)
from .update_migration import (
    UpdateBoundSchemaMigrator,
    UpdateMigrationBinding,
    UpdateMigrationBindingStore,
)


class _ValidationDatabaseOwnershipUncertain(BaseException):
    """Bypass automatic compensation when the validation database may still be open."""


class _SchemaMigrationRecoveryRequired(BaseException):
    """Bypass blind package rollback when schema/maintenance outcome is ambiguous."""


@dataclass(frozen=True)
class _SchemaApplyContext:
    plan: UpdatePlan
    manifest: ReleaseManifest
    process: ProcessIdentity
    binding: UpdateMigrationBinding


def _canonical_data_root(path: str | Path) -> Path:
    root = Path(path)
    if not root.is_absolute():
        raise ApplicationUpdateError("data_root must be absolute")
    lexical = Path(os.path.abspath(os.path.normpath(str(root))))
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UpdateRejectedError(
            "data_root must exist and resolve to a real directory"
        ) from exc
    if resolved != lexical:
        raise UpdateRejectedError(
            "data_root must not traverse a symlink or filesystem alias during update"
        )
    if not resolved.is_dir():
        raise UpdateRejectedError("data_root must resolve to a directory")
    return resolved


class ApplicationUpdateCoordinator(_BaseApplicationUpdateCoordinator):
    """Consumer-safe APP-01C/E update coordinator.

    The base coordinator owns package mutation, update journaling, maintenance
    holds and package/data compensation. This guard requires the authenticated
    release manifest to declare the target semantic schema and the full ordered
    migration program, derives the exact contiguous subset needed by this profile,
    durably binds it to the prepared update, and executes it inside the same
    maintenance hold after package installation but before Headquarters validation.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._migration_bindings = UpdateMigrationBindingStore(self.state_root)
        self._schema_apply_context: _SchemaApplyContext | None = None

    def prepare(
        self,
        *,
        runtime,
        schema_target_version: int,
        schema_steps: Iterable[MigrationStep] = (),
        **kwargs,
    ):  # type: ignore[override]
        data_root = _canonical_data_root(runtime.data_root)
        manifest = kwargs.get("manifest")
        if not isinstance(manifest, ReleaseManifest):
            raise TypeError("prepare requires manifest=ReleaseManifest")
        try:
            program = normalize_migration_program(schema_steps)
        except Exception as exc:
            raise UpdateRejectedError("schema migration program is not ordered/contiguous") from exc
        if manifest.application_schema_version != schema_target_version:
            raise UpdateRejectedError(
                "requested semantic schema target differs from authenticated release manifest"
            )
        if program and program[-1].to_version != schema_target_version:
            raise UpdateRejectedError(
                "schema migration program does not terminate at authenticated target schema"
            )
        if manifest.application_schema_migrations_sha256 != migration_steps_fingerprint(program):
            raise UpdateRejectedError(
                "schema migration program differs from authenticated release manifest"
            )
        plan = super().prepare(runtime=runtime, **kwargs)
        try:
            migrator = UpdateBoundSchemaMigrator(
                data_root,
                self.state_root,
                rollback_snapshot_sha256=plan.snapshot_sha256,
                rollback_snapshot_size_bytes=plan.snapshot_size_bytes,
            )
            migration_plan = migrator.prepare_read_only(
                profile_id=plan.profile_id,
                target_version=schema_target_version,
                steps=program,
            )
            binding = UpdateMigrationBinding(
                update_id=plan.update_id,
                update_plan_fingerprint=plan.fingerprint,
                manifest_fingerprint=manifest.fingerprint,
                target_release_id=plan.target_release_id,
                rollback_snapshot_sha256=plan.snapshot_sha256,
                rollback_snapshot_size_bytes=plan.snapshot_size_bytes,
                schema_program=program,
                migration_plan=migration_plan,
            )
            self._migration_bindings.create(binding)
        except Exception as exc:
            try:
                self._transition(
                    plan.update_id,
                    expected={"PREPARED"},
                    new_state="RECOVERY_REQUIRED",
                    evidence="schema:prepare-binding-failed",
                    reason=f"update schema migration could not be durably bound before package mutation: {exc}",
                )
            except Exception:
                pass
            raise UpdateRejectedError(
                "update schema migration could not be safely prepared; package mutation did not start"
            ) from exc
        return plan

    def _load_schema_binding(
        self,
        *,
        plan: UpdatePlan,
        manifest: ReleaseManifest,
    ) -> UpdateMigrationBinding:
        try:
            binding = self._migration_bindings.read(plan.update_id)
        except Exception as exc:
            raise UpdateRejectedError(
                "schema migration binding is missing, corrupt or no longer trustworthy"
            ) from exc
        if binding.update_plan_fingerprint != plan.fingerprint:
            raise UpdateRejectedError("schema migration binding belongs to a different update plan")
        if binding.manifest_fingerprint != manifest.fingerprint:
            raise UpdateRejectedError("schema migration binding belongs to a different release manifest")
        if binding.target_release_id != plan.target_release_id:
            raise UpdateRejectedError("schema migration binding target release differs from update plan")
        if binding.rollback_snapshot_sha256 != plan.snapshot_sha256:
            raise UpdateRejectedError("schema migration binding rollback snapshot differs from update plan")
        if binding.rollback_snapshot_size_bytes != plan.snapshot_size_bytes:
            raise UpdateRejectedError("schema migration binding rollback size differs from update plan")
        if binding.migration_plan.profile_id != plan.profile_id:
            raise UpdateRejectedError("schema migration binding belongs to a different profile")
        if binding.migration_plan.target_version != manifest.application_schema_version:
            raise UpdateRejectedError("bound schema target differs from authenticated release manifest")
        if (
            migration_steps_fingerprint(binding.schema_program)
            != manifest.application_schema_migrations_sha256
        ):
            raise UpdateRejectedError("bound schema program differs from authenticated release manifest")
        try:
            expected = select_migration_chain(
                source_version=binding.migration_plan.source_version,
                target_version=binding.migration_plan.target_version,
                program=binding.schema_program,
            )
        except Exception as exc:
            raise UpdateRejectedError("bound schema program cannot reproduce migration plan") from exc
        if expected != binding.migration_plan.steps:
            raise UpdateRejectedError("bound profile migration chain differs from authenticated program")
        return binding

    def _inspect_exact_hold(self, profile_id: str, process: ProcessIdentity):
        try:
            hold = self._leases.inspect(profile_id)
        except Exception as exc:
            raise _SchemaMigrationRecoveryRequired(
                f"update maintenance ownership state is unreadable: {exc}"
            ) from exc
        if hold is None or hold.process.fingerprint != process.fingerprint:
            raise _SchemaMigrationRecoveryRequired(
                "exact update maintenance hold is missing or owned by a different process"
            )
        return hold

    def _release_recovery_coordination(
        self,
        plan: UpdatePlan,
        process: ProcessIdentity,
    ) -> str | None:
        coordination_id = self._coordination_lease_id(plan.profile_id)
        try:
            coordination = self._leases.inspect(coordination_id)
        except Exception as exc:
            return f"update coordination ownership is unreadable during recovery cleanup: {exc}"
        if coordination is None:
            return None
        if coordination.process.fingerprint != process.fingerprint:
            return "update coordination ownership changed during recovery cleanup"
        try:
            self._release_coordination(plan.profile_id, process, coordination)
        except Exception as exc:
            return f"update coordination release failed during recovery cleanup: {exc}"
        return None

    def _run_bound_schema_migration(self, data_root: Path, profile_id: str) -> None:
        context = self._schema_apply_context
        if context is None:
            raise _SchemaMigrationRecoveryRequired(
                "post-install validation has no exact schema migration apply context"
            )
        if context.plan.profile_id != profile_id:
            raise _SchemaMigrationRecoveryRequired(
                "post-install schema migration context belongs to a different profile"
            )
        hold = self._inspect_exact_hold(profile_id, context.process)
        migrator = UpdateBoundSchemaMigrator(
            data_root,
            self.state_root,
            rollback_snapshot_sha256=context.binding.rollback_snapshot_sha256,
            rollback_snapshot_size_bytes=context.binding.rollback_snapshot_size_bytes,
        )
        try:
            result = migrator.migrate_under_maintenance(
                context.binding.migration_plan,
                maintenance_lease=hold,
            )
        except Exception as exc:
            try:
                current_hold = self._leases.inspect(profile_id)
            except Exception as hold_exc:
                raise _SchemaMigrationRecoveryRequired(
                    f"schema migration failed and maintenance ownership became unreadable: {hold_exc}"
                ) from exc
            if current_hold != hold:
                raise _SchemaMigrationRecoveryRequired(
                    f"schema migration lost exact maintenance ownership: {exc}"
                ) from exc
            raise
        if result.state == "RECOVERY_REQUIRED":
            raise _SchemaMigrationRecoveryRequired(result.evidence)
        if result.state == "ROLLED_BACK":
            raise UpdateRejectedError(
                f"target package schema migration rolled back before Headquarters validation: {result.evidence}"
            )
        if result.state not in {"NO_CHANGE", "SUCCEEDED"}:
            raise _SchemaMigrationRecoveryRequired(
                f"unsupported schema migration terminal state: {result.state}"
            )
        self._inspect_exact_hold(profile_id, context.process)

    def _validate_installed_headquarters(
        self,
        data_root: Path,
        profile_id: str,
    ) -> None:
        self._run_bound_schema_migration(data_root, profile_id)
        headquarters: HeadquartersMemory | None = None
        validation_error: Exception | None = None
        try:
            headquarters = self._memory_opener(data_root, profile_id)
            if not isinstance(headquarters, HeadquartersMemory):
                raise UpdateRejectedError("memory_opener did not return HeadquartersMemory")
            if headquarters.store.profile_id != profile_id:
                raise UpdateRejectedError("updated Headquarters opened a different profile")
            quick = headquarters.store._conn.execute("PRAGMA quick_check").fetchone()
            foreign = headquarters.store._conn.execute("PRAGMA foreign_key_check").fetchall()
            if quick is None or str(quick[0]) != "ok" or foreign:
                raise UpdateRejectedError(
                    "post-update canonical database integrity check failed"
                )
        except Exception as exc:
            validation_error = exc
        finally:
            if headquarters is not None:
                try:
                    headquarters.close()
                except Exception as close_exc:
                    raise _ValidationDatabaseOwnershipUncertain(
                        "post-update Headquarters could not prove canonical database release"
                    ) from close_exc
        if validation_error is not None:
            raise validation_error

    def _finish_guarded_recovery(
        self,
        *,
        plan: UpdatePlan,
        process: ProcessIdentity,
        evidence: str,
        reason: str,
    ) -> UpdateResult:
        coordination_problem = self._release_recovery_coordination(plan, process)
        if coordination_problem is not None:
            reason = f"{reason}; {coordination_problem}"
        journal = self._read_journal(plan.update_id)
        if journal.state == "VALIDATING":
            journal = self._mark_recovery(
                plan,
                expected={"VALIDATING"},
                evidence=evidence,
                reason=reason,
                install_receipt=journal.install_receipt,
            )
        return self._to_result(journal)

    def apply(self, **kwargs) -> UpdateResult:  # type: ignore[override]
        plan = kwargs.get("plan")
        manifest = kwargs.get("manifest")
        process = kwargs.get("process")
        if not isinstance(plan, UpdatePlan):
            raise TypeError("apply requires plan=UpdatePlan")
        if not isinstance(manifest, ReleaseManifest):
            raise TypeError("apply requires manifest=ReleaseManifest")
        if not isinstance(process, ProcessIdentity):
            raise TypeError("apply requires process=ProcessIdentity")
        if "data_root" not in kwargs:
            raise TypeError("apply requires data_root")
        if self._schema_apply_context is not None:
            raise UpdateRejectedError("one coordinator cannot execute two schema-bound updates concurrently")
        guarded_kwargs = dict(kwargs)
        guarded_kwargs["data_root"] = _canonical_data_root(kwargs["data_root"])
        binding = self._load_schema_binding(plan=plan, manifest=manifest)
        self._schema_apply_context = _SchemaApplyContext(plan, manifest, process, binding)
        try:
            try:
                return super().apply(**guarded_kwargs)
            except _ValidationDatabaseOwnershipUncertain as exc:
                return self._finish_guarded_recovery(
                    plan=plan,
                    process=process,
                    evidence="validation:database-release-uncertain",
                    reason=str(exc),
                )
            except _SchemaMigrationRecoveryRequired as exc:
                return self._finish_guarded_recovery(
                    plan=plan,
                    process=process,
                    evidence="schema:migration-recovery-required",
                    reason=str(exc),
                )
        finally:
            self._schema_apply_context = None
