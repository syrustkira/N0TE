from __future__ import annotations

import hashlib
from pathlib import Path

from n0te.app_runtime import ApplicationRuntime
from n0te.artifacts import ArtifactRecord, ManifestAuthenticityEvidence, ReleaseManifest
from n0te.instance import InstanceLeaseManager, ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.migration import MigrationResult, MigrationStep
from n0te.platforms import PlatformEnvironment
from n0te.safe_update import ApplicationUpdateCoordinator
from n0te.schema_program import migration_steps_fingerprint
from n0te.support import SupportTarget
from n0te.update import PackageActionReceipt
from n0te.update_migration import UpdateBoundSchemaMigrator


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "DEAD"


class Driver:
    def perform(self, request, artifact_bytes):
        if request.action == "INSTALL":
            return PackageActionReceipt(
                request_fingerprint=request.fingerprint,
                action="INSTALL",
                state="SUCCEEDED",
                evidence_ref="recovery-test:installed",
                resulting_release_id=request.target_release_id,
            )
        raise AssertionError("guarded ambiguous recovery must not blindly roll back package")


def proc() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=9917,
        start_token="guarded-recovery",
    )


def prepared(tmp_path: Path):
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    memory = HeadquartersMemory.create(data_root, "Guarded Recovery Artist")
    profile_id = memory.store.profile_id
    memory.store.create_song("Guarded Recovery Song")
    memory.close()

    process = proc()
    probe = Probe()
    runtime = ApplicationRuntime(data_root=data_root, state_root=state_root)
    assert runtime.launch(profile_id=profile_id, process=process, probe=probe).status == "STARTED"

    step = MigrationStep(
        1,
        2,
        "v2 guarded recovery marker",
        ("CREATE TABLE guarded_v2(value TEXT)",),
    )
    package = b"guarded-recovery-release"
    target = SupportTarget.from_runtime_labels(os_name="Linux", machine="x86_64")
    artifact = ArtifactRecord(
        artifact_id="linux-package",
        target_fingerprint=target.fingerprint,
        package_kind="TEST_PACKAGE",
        size_bytes=len(package),
        sha256=hashlib.sha256(package).hexdigest(),
    )
    manifest = ReleaseManifest(
        release_id="release-v2",
        version="2.0.0",
        source_commit_sha="a" * 40,
        build_inputs_sha256="b" * 64,
        dependency_inventory_sha256="c" * 64,
        license_inventory_sha256="d" * 64,
        artifacts=(artifact,),
        application_schema_version=2,
        application_schema_migrations_sha256=migration_steps_fingerprint((step,)),
    )
    authenticity = ManifestAuthenticityEvidence(
        manifest_fingerprint=manifest.fingerprint,
        status="VERIFIED",
        verifier_id="guarded-recovery-verifier",
        scheme="TEST",
        evidence_ref="guarded-recovery:manifest",
    )
    coordinator = ApplicationUpdateCoordinator(state_root=state_root)
    plan = coordinator.prepare(
        runtime=runtime,
        current_release_id="release-v1",
        manifest=manifest,
        artifact_id=artifact.artifact_id,
        artifact_bytes=package,
        target=target,
        authenticity=authenticity,
        process=process,
        probe=probe,
        schema_target_version=2,
        schema_steps=(step,),
    )
    return (
        data_root,
        state_root,
        profile_id,
        process,
        probe,
        coordinator,
        plan,
        target,
        manifest,
        authenticity,
        package,
    )


def test_ambiguous_schema_recovery_releases_coordination_and_retains_profile_hold(
    tmp_path: Path, monkeypatch
) -> None:
    (
        data_root,
        state_root,
        profile_id,
        process,
        probe,
        coordinator,
        plan,
        target,
        manifest,
        authenticity,
        package,
    ) = prepared(tmp_path)

    def ambiguous(self, migration_plan, *, maintenance_lease):
        return MigrationResult(
            "RECOVERY_REQUIRED",
            migration_plan,
            None,
            plan.snapshot_sha256,
            "simulated ambiguous schema state",
        )

    monkeypatch.setattr(UpdateBoundSchemaMigrator, "migrate_under_maintenance", ambiguous)
    result = coordinator.apply(
        plan=plan,
        data_root=data_root,
        manifest=manifest,
        artifact_bytes=package,
        target=target,
        authenticity=authenticity,
        driver=Driver(),
        process=process,
        probe=probe,
    )
    assert result.state == "RECOVERY_REQUIRED"

    leases = InstanceLeaseManager(state_root)
    assert leases.inspect(profile_id) is not None
    coordination_id = coordinator._coordination_lease_id(profile_id)
    assert leases.inspect(coordination_id) is None
