from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from n0te.app_runtime import ApplicationRuntime
from n0te.artifacts import ArtifactRecord, ManifestAuthenticityEvidence, ReleaseManifest
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.migration import ApplicationSchemaMigrator, MigrationStep
from n0te.platforms import PlatformEnvironment
from n0te.safe_update import ApplicationUpdateCoordinator
from n0te.schema_program import migration_steps_fingerprint
from n0te.support import SupportTarget
from n0te.update import PackageActionReceipt


class DeadProbe:
    def status(self, process: ProcessIdentity) -> str:
        return "DEAD"


class Driver:
    def perform(self, request, artifact_bytes):
        if request.action == "INSTALL":
            return PackageActionReceipt(
                request_fingerprint=request.fingerprint,
                action="INSTALL",
                state="SUCCEEDED",
                evidence_ref="subset:installed",
                resulting_release_id=request.target_release_id,
            )
        raise AssertionError("successful no-op schema subset must not roll back package")


def proc(pid: int, token: str) -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=token,
    )


def history_count(data_root: Path, profile_id: str) -> int:
    db = data_root / "profiles" / profile_id / "lineage.sqlite3"
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM application_schema_migrations").fetchone()[0])
    finally:
        conn.close()


def test_profile_already_at_target_uses_empty_subset_of_authenticated_full_program(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    memory = HeadquartersMemory.create(data_root, "Already Current Artist")
    profile_id = memory.store.profile_id
    song = memory.store.create_song("Already Current Song")
    memory.close()

    step = MigrationStep(
        1,
        2,
        "schema 1 to 2",
        ("CREATE TABLE schema_v2_marker(value TEXT)",),
    )
    standalone = ApplicationSchemaMigrator(data_root, state_root)
    migration_plan = standalone.prepare(
        profile_id=profile_id,
        target_version=2,
        steps=(step,),
    )
    first = standalone.migrate(
        migration_plan,
        maintenance_process=proc(8201, "initial-schema-migration"),
        probe=DeadProbe(),
    )
    assert first.state == "SUCCEEDED"
    assert history_count(data_root, profile_id) == 1

    process = proc(8202, "safe-update")
    runtime = ApplicationRuntime(data_root=data_root, state_root=state_root)
    assert runtime.launch(profile_id=profile_id, process=process, probe=DeadProbe()).status == "STARTED"
    assert runtime.headquarters.store.active_song().id == song.id

    package = b"schema-two-package-update"
    target = SupportTarget.from_runtime_labels(os_name="Linux", machine="x86_64")
    artifact = ArtifactRecord(
        artifact_id="linux-package",
        target_fingerprint=target.fingerprint,
        package_kind="TEST_PACKAGE",
        size_bytes=len(package),
        sha256=hashlib.sha256(package).hexdigest(),
    )
    manifest = ReleaseManifest(
        release_id="schema-two-release",
        version="2.1.0",
        source_commit_sha="a" * 40,
        build_inputs_sha256="b" * 64,
        dependency_inventory_sha256="c" * 64,
        license_inventory_sha256="d" * 64,
        artifacts=(artifact,),
        application_schema_version=2,
        application_schema_migrations_sha256=migration_steps_fingerprint((step,)),
    )
    auth = ManifestAuthenticityEvidence(
        manifest_fingerprint=manifest.fingerprint,
        status="VERIFIED",
        verifier_id="subset-verifier",
        scheme="TEST",
        evidence_ref="subset:manifest",
    )
    coordinator = ApplicationUpdateCoordinator(state_root=state_root)
    update_plan = coordinator.prepare(
        runtime=runtime,
        current_release_id="schema-two-release-old-package",
        manifest=manifest,
        artifact_id=artifact.artifact_id,
        artifact_bytes=package,
        target=target,
        authenticity=auth,
        process=process,
        probe=DeadProbe(),
        schema_target_version=2,
        schema_steps=(step,),
    )
    binding = coordinator._migration_bindings.read(update_plan.update_id)
    assert binding.schema_program == (step,)
    assert binding.migration_plan.source_version == 2
    assert binding.migration_plan.target_version == 2
    assert binding.migration_plan.steps == ()

    result = coordinator.apply(
        plan=update_plan,
        data_root=data_root,
        manifest=manifest,
        artifact_bytes=package,
        target=target,
        authenticity=auth,
        driver=Driver(),
        process=process,
        probe=DeadProbe(),
    )
    assert result.state == "SUCCEEDED"
    assert history_count(data_root, profile_id) == 1

    reopened = HeadquartersMemory.open(data_root, profile_id)
    assert reopened.store.active_song().id == song.id
    reopened.close()
