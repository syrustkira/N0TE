from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from n0te.app_runtime import ApplicationRuntime
from n0te.artifacts import ArtifactRecord, ManifestAuthenticityEvidence, ReleaseManifest
from n0te.instance import InstanceLeaseManager, ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.migration import MigrationResult, MigrationStep
from n0te.platforms import PlatformEnvironment
from n0te.recovery import RecoveryManager
from n0te.safe_update import ApplicationUpdateCoordinator
from n0te.schema_program import migration_steps_fingerprint
from n0te.support import SupportTarget
from n0te.update import (
    PackageActionReceipt,
    UpdateExecuteOnceError,
    UpdateJournalCorruptionError,
    UpdateRejectedError,
)
from n0te.update_migration import UpdateBoundSchemaMigrator


class Probe:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, process: ProcessIdentity, state: str) -> None:
        self.values[process.fingerprint] = state

    def status(self, process: ProcessIdentity) -> str:
        return self.values.get(process.fingerprint, "UNKNOWN")


def process(pid: int = 901, token: str = "update-owner") -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=token,
    )


def schema_step_v2(*statements: str) -> MigrationStep:
    return MigrationStep(
        1,
        2,
        "release-v2 semantic schema",
        tuple(statements)
        or ("CREATE TABLE release_v2_schema_marker(value TEXT NOT NULL)",),
    )


def schema_step_v3(*statements: str) -> MigrationStep:
    return MigrationStep(
        2,
        3,
        "release-v3 semantic schema",
        tuple(statements)
        or ("CREATE TABLE release_v3_schema_marker(value TEXT NOT NULL)",),
    )


def release_fixture(
    payload: bytes = b"n0te-release-v2",
    *,
    schema_target_version: int = 1,
    schema_steps=(),
):
    program = tuple(schema_steps)
    target = SupportTarget.from_runtime_labels(os_name="Linux", machine="x86_64")
    record = ArtifactRecord(
        artifact_id="linux-x86_64-package",
        target_fingerprint=target.fingerprint,
        package_kind="TEST_PACKAGE",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    manifest = ReleaseManifest(
        release_id="release-v2",
        version="2.0.0",
        source_commit_sha="a" * 40,
        build_inputs_sha256="b" * 64,
        dependency_inventory_sha256="c" * 64,
        license_inventory_sha256="d" * 64,
        artifacts=(record,),
        application_schema_version=schema_target_version,
        application_schema_migrations_sha256=migration_steps_fingerprint(program),
    )
    authenticity = ManifestAuthenticityEvidence(
        manifest_fingerprint=manifest.fingerprint,
        status="VERIFIED",
        verifier_id="test-platform-verifier",
        scheme="TEST-ONLY-VERIFIER-RECEIPT",
        evidence_ref="evidence:manifest-authenticity",
    )
    return target, record, manifest, authenticity, payload


class Driver:
    def __init__(
        self,
        *,
        install_state: str = "SUCCEEDED",
        rollback_state: str = "SUCCEEDED",
        install_release: str | None = "release-v2",
        rollback_release: str | None = "release-v1",
    ) -> None:
        self.install_state = install_state
        self.rollback_state = rollback_state
        self.install_release = install_release
        self.rollback_release = rollback_release
        self.calls: list[str] = []

    def perform(self, request, artifact_bytes):
        self.calls.append(request.action)
        if request.action == "INSTALL":
            return PackageActionReceipt(
                request_fingerprint=request.fingerprint,
                action="INSTALL",
                state=self.install_state,
                evidence_ref=f"driver:install:{self.install_state.lower()}",
                resulting_release_id=self.install_release,
            )
        return PackageActionReceipt(
            request_fingerprint=request.fingerprint,
            action="ROLLBACK",
            state=self.rollback_state,
            evidence_ref=f"driver:rollback:{self.rollback_state.lower()}",
            resulting_release_id=self.rollback_release,
        )


def application_schema_version(data_root: Path, profile_id: str) -> int:
    db = data_root / "profiles" / profile_id / "lineage.sqlite3"
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key='application_semantic_schema_version'"
        ).fetchone()
        return 1 if row is None else int(row[0])
    finally:
        conn.close()


def migration_history_count(data_root: Path, profile_id: str) -> int:
    db = data_root / "profiles" / profile_id / "lineage.sqlite3"
    conn = sqlite3.connect(db)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='application_schema_migrations'"
        ).fetchone()
        if exists is None:
            return 0
        return int(
            conn.execute("SELECT COUNT(*) FROM application_schema_migrations").fetchone()[0]
        )
    finally:
        conn.close()


def running_profile(tmp_path: Path):
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    bootstrap = HeadquartersMemory.create(data_root, "Update Artist")
    profile_id = bootstrap.store.profile_id
    bootstrap.close()
    proc = process()
    probe = Probe()
    runtime = ApplicationRuntime(data_root=data_root, state_root=state_root)
    assert runtime.launch(profile_id=profile_id, process=proc, probe=probe).status == "STARTED"
    song = runtime.headquarters.store.create_song("Update Safety Song")
    version = runtime.headquarters.store.create_version(song.id, label="Before update")
    return data_root, state_root, profile_id, proc, probe, runtime, song, version


def prepared_update(
    tmp_path: Path,
    *,
    memory_opener=HeadquartersMemory.open,
    schema_target_version: int = 2,
    schema_steps=None,
):
    data_root, state_root, profile_id, proc, probe, runtime, song, version = running_profile(
        tmp_path
    )
    program = (schema_step_v2(),) if schema_steps is None else tuple(schema_steps)
    target, record, manifest, authenticity, payload = release_fixture(
        schema_target_version=schema_target_version,
        schema_steps=program,
    )
    coordinator = ApplicationUpdateCoordinator(
        state_root=state_root,
        memory_opener=memory_opener,
    )
    plan = coordinator.prepare(
        runtime=runtime,
        current_release_id="release-v1",
        manifest=manifest,
        artifact_id=record.artifact_id,
        artifact_bytes=payload,
        target=target,
        authenticity=authenticity,
        process=proc,
        probe=probe,
        schema_target_version=schema_target_version,
        schema_steps=program,
    )
    assert runtime.state == "STOPPED"
    return (
        data_root,
        state_root,
        profile_id,
        proc,
        probe,
        coordinator,
        plan,
        target,
        record,
        manifest,
        authenticity,
        payload,
        song,
        version,
    )


def apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe):
    return coordinator.apply(
        plan=plan,
        data_root=data_root,
        manifest=manifest,
        artifact_bytes=payload,
        target=target,
        authenticity=auth,
        driver=driver,
        process=proc,
        probe=probe,
    )


def test_success_runs_bound_schema_migration_then_reopens_exact_song(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    data_root, state_root, profile_id, proc, probe, coordinator, plan, target, _, manifest, auth, payload, song, version = values
    driver = Driver()

    result = apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert result.state == "SUCCEEDED"
    assert driver.calls == ["INSTALL"]
    assert application_schema_version(data_root, profile_id) == 2
    assert migration_history_count(data_root, profile_id) == 1
    assert coordinator.status(plan.update_id).retry_allowed is False
    assert InstanceLeaseManager(state_root).inspect(profile_id) is None

    runtime = ApplicationRuntime(data_root=data_root, state_root=state_root)
    assert runtime.launch(profile_id=profile_id, process=proc, probe=probe).status == "STARTED"
    resumed = runtime.headquarters.store.active_song()
    assert resumed is not None
    assert resumed.id == song.id
    assert resumed.current_version_id == version.id
    assert runtime.quit().status == "STOPPED"


def test_failed_changed_package_rolls_back_before_schema_migration(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    data_root, _, profile_id, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    driver = Driver(install_state="FAILED_CHANGED", install_release=None)

    result = apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert result.state == "ROLLED_BACK"
    assert result.restored_sha256 == plan.snapshot_sha256
    assert driver.calls == ["INSTALL", "ROLLBACK"]
    assert application_schema_version(data_root, profile_id) == 1
    assert migration_history_count(data_root, profile_id) == 0


def test_schema_failure_after_package_success_rolls_back_package_and_snapshot(tmp_path: Path) -> None:
    bad_step = schema_step_v2(
        "CREATE TABLE release_v2_schema_marker(value TEXT)",
        "INSERT INTO missing_schema_table(value) VALUES('boom')",
    )
    values = prepared_update(tmp_path, schema_steps=(bad_step,))
    data_root, _, profile_id, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    driver = Driver()

    result = apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert result.state == "ROLLED_BACK"
    assert driver.calls == ["INSTALL", "ROLLBACK"]
    assert result.restored_sha256 == plan.snapshot_sha256
    assert application_schema_version(data_root, profile_id) == 1
    assert migration_history_count(data_root, profile_id) == 0
    assert RecoveryManager.inspect_snapshot(data_root, profile_id).sha256 == plan.snapshot_sha256


def test_migration_success_then_headquarters_failure_rolls_back_both_layers(tmp_path: Path) -> None:
    def bad_open(root, profile_id):
        raise RuntimeError("simulated updated build cannot open Headquarters")

    values = prepared_update(tmp_path, memory_opener=bad_open)
    data_root, _, profile_id, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    driver = Driver()

    result = apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert result.state == "ROLLED_BACK"
    assert driver.calls == ["INSTALL", "ROLLBACK"]
    assert result.restored_sha256 == plan.snapshot_sha256
    assert application_schema_version(data_root, profile_id) == 1
    assert migration_history_count(data_root, profile_id) == 0
    assert RecoveryManager.inspect_snapshot(data_root, profile_id).sha256 == plan.snapshot_sha256


def test_schema_recovery_required_does_not_blindly_roll_back_package(tmp_path: Path, monkeypatch) -> None:
    values = prepared_update(tmp_path)
    data_root, state_root, profile_id, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    driver = Driver()

    def ambiguous(self, migration_plan, *, maintenance_lease):
        return MigrationResult(
            "RECOVERY_REQUIRED",
            migration_plan,
            None,
            plan.snapshot_sha256,
            "simulated ambiguous installed schema outcome",
        )

    monkeypatch.setattr(UpdateBoundSchemaMigrator, "migrate_under_maintenance", ambiguous)
    result = apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert result.state == "RECOVERY_REQUIRED"
    assert driver.calls == ["INSTALL"]
    leases = InstanceLeaseManager(state_root)
    assert leases.inspect(profile_id) is not None
    assert leases.inspect(coordinator._coordination_lease_id(profile_id)) is None
    assert coordinator.status(plan.update_id).retry_allowed is False


def test_unknown_install_is_recovery_required_and_never_retried(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    data_root, state_root, profile_id, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    driver = Driver(install_state="UNKNOWN", install_release=None)

    result = apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert result.state == "RECOVERY_REQUIRED"
    status = coordinator.status(plan.update_id)
    assert status.requires_recovery is True
    assert status.retry_allowed is False
    assert driver.calls == ["INSTALL"]
    assert InstanceLeaseManager(state_root).inspect(profile_id) is not None
    with pytest.raises(UpdateExecuteOnceError):
        apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)


def test_wrong_success_release_receipt_becomes_ambiguous_recovery(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    data_root, _, _, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    driver = Driver(install_release="some-other-release")

    result = apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert result.state == "RECOVERY_REQUIRED"
    assert driver.calls == ["INSTALL"]


def test_rollback_failure_does_not_attempt_snapshot_restore(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    data_root, _, _, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    driver = Driver(
        install_state="FAILED_CHANGED",
        install_release=None,
        rollback_state="FAILED",
        rollback_release=None,
    )

    result = apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert result.state == "RECOVERY_REQUIRED"
    assert result.restored_sha256 is None
    assert driver.calls == ["INSTALL", "ROLLBACK"]


def test_song_change_after_prepare_refuses_before_package_callback(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    data_root, _, profile_id, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    reopened = HeadquartersMemory.open(data_root, profile_id)
    reopened.store.create_song("Sneaky post-prepare edit")
    reopened.close()
    driver = Driver()

    with pytest.raises(UpdateRejectedError):
        apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert driver.calls == []
    assert coordinator.status(plan.update_id).state == "PREPARED"


def test_manifest_schema_target_mismatch_rejects_before_snapshot_or_quit(tmp_path: Path) -> None:
    _, state_root, _, proc, probe, runtime, _, _ = running_profile(tmp_path)
    step = schema_step_v2()
    target, record, manifest, auth, payload = release_fixture(
        schema_target_version=2,
        schema_steps=(step,),
    )
    coordinator = ApplicationUpdateCoordinator(state_root=state_root)

    with pytest.raises(UpdateRejectedError, match="authenticated release manifest"):
        coordinator.prepare(
            runtime=runtime,
            current_release_id="release-v1",
            manifest=manifest,
            artifact_id=record.artifact_id,
            artifact_bytes=payload,
            target=target,
            authenticity=auth,
            process=proc,
            probe=probe,
            schema_target_version=1,
            schema_steps=(),
        )
    assert runtime.state == "RUNNING"
    assert not coordinator.updates_root.exists()
    assert runtime.quit().status == "STOPPED"


def test_manifest_schema_program_mismatch_rejects_before_snapshot_or_quit(tmp_path: Path) -> None:
    _, state_root, _, proc, probe, runtime, _, _ = running_profile(tmp_path)
    authenticated = schema_step_v2()
    supplied = schema_step_v2("CREATE TABLE another_v2_marker(value TEXT)")
    target, record, manifest, auth, payload = release_fixture(
        schema_target_version=2,
        schema_steps=(authenticated,),
    )
    coordinator = ApplicationUpdateCoordinator(state_root=state_root)

    with pytest.raises(UpdateRejectedError, match="schema migration program"):
        coordinator.prepare(
            runtime=runtime,
            current_release_id="release-v1",
            manifest=manifest,
            artifact_id=record.artifact_id,
            artifact_bytes=payload,
            target=target,
            authenticity=auth,
            process=proc,
            probe=probe,
            schema_target_version=2,
            schema_steps=(supplied,),
        )
    assert runtime.state == "RUNNING"
    assert not coordinator.updates_root.exists()
    assert runtime.quit().status == "STOPPED"


def test_authenticated_program_not_ending_at_target_rejects_before_snapshot_or_quit(tmp_path: Path) -> None:
    _, state_root, _, proc, probe, runtime, _, _ = running_profile(tmp_path)
    incomplete = schema_step_v2()
    target, record, manifest, auth, payload = release_fixture(
        schema_target_version=3,
        schema_steps=(incomplete,),
    )
    coordinator = ApplicationUpdateCoordinator(state_root=state_root)

    with pytest.raises(UpdateRejectedError, match="does not terminate"):
        coordinator.prepare(
            runtime=runtime,
            current_release_id="release-v1",
            manifest=manifest,
            artifact_id=record.artifact_id,
            artifact_bytes=payload,
            target=target,
            authenticity=auth,
            process=proc,
            probe=probe,
            schema_target_version=3,
            schema_steps=(incomplete,),
        )
    assert runtime.state == "RUNNING"
    assert not coordinator.updates_root.exists()
    assert runtime.quit().status == "STOPPED"


def test_profile_without_authenticated_path_marks_recovery_before_package_mutation(tmp_path: Path) -> None:
    data_root, state_root, profile_id, proc, probe, runtime, _, _ = running_profile(tmp_path)
    only_two_three = schema_step_v3()
    target, record, manifest, auth, payload = release_fixture(
        schema_target_version=3,
        schema_steps=(only_two_three,),
    )
    coordinator = ApplicationUpdateCoordinator(state_root=state_root)

    with pytest.raises(UpdateRejectedError):
        coordinator.prepare(
            runtime=runtime,
            current_release_id="release-v1",
            manifest=manifest,
            artifact_id=record.artifact_id,
            artifact_bytes=payload,
            target=target,
            authenticity=auth,
            process=proc,
            probe=probe,
            schema_target_version=3,
            schema_steps=(only_two_three,),
        )
    assert runtime.state == "STOPPED"
    updates = list(coordinator.updates_root.glob("upd_*.json"))
    assert len(updates) == 1
    assert coordinator.status(updates[0].stem).state == "RECOVERY_REQUIRED"
    assert application_schema_version(data_root, profile_id) == 1


def test_schema_binding_tamper_is_rejected_before_package_callback(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    data_root, state_root, _, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    binding_path = state_root / "update-schema" / f"{plan.update_id}.json"
    envelope = json.loads(binding_path.read_text())
    envelope["payload"]["target_release_id"] = "tampered-release"
    binding_path.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")
    driver = Driver()

    with pytest.raises(UpdateRejectedError):
        apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert driver.calls == []
    assert coordinator.status(plan.update_id).state == "PREPARED"


def test_existing_prepared_update_blocks_second_update_for_same_profile(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    data_root, state_root, profile_id, proc, probe, coordinator, _, target, record, manifest, auth, payload, _, _ = values
    runtime = ApplicationRuntime(data_root=data_root, state_root=state_root)
    assert runtime.launch(profile_id=profile_id, process=proc, probe=probe).status == "STARTED"

    with pytest.raises(UpdateRejectedError):
        coordinator.prepare(
            runtime=runtime,
            current_release_id="release-v1",
            manifest=manifest,
            artifact_id=record.artifact_id,
            artifact_bytes=payload,
            target=target,
            authenticity=auth,
            process=proc,
            probe=probe,
            schema_target_version=2,
            schema_steps=(schema_step_v2(),),
        )
    assert runtime.state == "RUNNING"
    assert runtime.quit().status == "STOPPED"


def test_illegal_recomputed_history_is_rejected_as_corruption(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    _, _, _, _, _, coordinator, plan, _, _, _, _, _, _, _ = values
    path = coordinator.updates_root / f"{plan.update_id}.json"
    envelope = json.loads(path.read_text())
    envelope["state"] = "SUCCEEDED"
    envelope["history"].append({"state": "SUCCEEDED", "evidence": "tampered:shortcut"})
    payload = {key: value for key, value in envelope.items() if key != "integrity_sha256"}
    envelope["integrity_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    path.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(UpdateJournalCorruptionError):
        coordinator.status(plan.update_id)


def test_crash_breadcrumb_is_recovery_not_retry_permission(tmp_path: Path) -> None:
    values = prepared_update(tmp_path)
    _, _, _, _, _, coordinator, plan, _, _, _, _, _, _, _ = values
    coordinator._transition(
        plan.update_id,
        expected={"PREPARED"},
        new_state="INSTALLING",
        evidence="test:simulated-crash-after-start",
    )
    status = coordinator.status(plan.update_id)
    assert status.state == "INSTALLING"
    assert status.requires_recovery is True
    assert status.retry_allowed is False


def test_validation_close_failure_does_not_auto_rollback_under_open_database(tmp_path: Path) -> None:
    opened: list[HeadquartersMemory] = []

    def close_failing_open(root, profile_id):
        headquarters = HeadquartersMemory.open(root, profile_id)
        opened.append(headquarters)

        def fail_close():
            raise RuntimeError("simulated close failure")

        headquarters.close = fail_close  # type: ignore[method-assign]
        return headquarters

    values = prepared_update(tmp_path, memory_opener=close_failing_open)
    data_root, state_root, profile_id, proc, probe, coordinator, plan, target, _, manifest, auth, payload, _, _ = values
    driver = Driver()

    result = apply(coordinator, plan, data_root, manifest, payload, target, auth, driver, proc, probe)
    assert result.state == "RECOVERY_REQUIRED"
    assert driver.calls == ["INSTALL"]
    leases = InstanceLeaseManager(state_root)
    assert leases.inspect(profile_id) is not None
    assert leases.inspect(coordinator._coordination_lease_id(profile_id)) is None
    for headquarters in opened:
        headquarters.store.close()
