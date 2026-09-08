from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from n0te.app_runtime import ApplicationRuntime
from n0te.artifacts import ArtifactRecord, ManifestAuthenticityEvidence, ReleaseManifest
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment
from n0te.safe_update import ApplicationUpdateCoordinator
from n0te.support import SupportTarget
from n0te.update import UpdateRejectedError


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


class MustNotRunDriver:
    def __init__(self) -> None:
        self.calls = 0

    def perform(self, request, artifact_bytes):
        self.calls += 1
        raise AssertionError("package driver must not run for rejected data-root identity")


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=1701,
        start_token="safe-update-root-binding",
    )


def release(payload: bytes = b"root-bound-release"):
    target = SupportTarget.from_runtime_labels(os_name="Linux", machine="x86_64")
    record = ArtifactRecord(
        artifact_id="linux-root-bound-package",
        target_fingerprint=target.fingerprint,
        package_kind="TEST_PACKAGE",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    manifest = ReleaseManifest(
        release_id="release-root-v2",
        version="2.0.0",
        source_commit_sha="1" * 40,
        build_inputs_sha256="2" * 64,
        dependency_inventory_sha256="3" * 64,
        license_inventory_sha256="4" * 64,
        artifacts=(record,),
    )
    auth = ManifestAuthenticityEvidence(
        manifest_fingerprint=manifest.fingerprint,
        status="VERIFIED",
        verifier_id="root-test-verifier",
        scheme="TEST-ONLY",
        evidence_ref="evidence:root-manifest",
    )
    return target, record, manifest, auth, payload


def make_profile(data_root: Path) -> str:
    headquarters = HeadquartersMemory.create(data_root, "Root Bound Artist")
    try:
        return headquarters.store.profile_id
    finally:
        headquarters.close()


def symlink_dir(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink unavailable in this test environment: {exc}")


def test_prepare_rejects_symlinked_data_root_before_snapshot_or_quit(tmp_path: Path) -> None:
    real_data = tmp_path / "real-data"
    state_root = tmp_path / "state"
    profile_id = make_profile(real_data)
    alias = tmp_path / "data-alias"
    symlink_dir(alias, real_data)

    proc = process()
    probe = Probe()
    runtime = ApplicationRuntime(data_root=alias, state_root=state_root)
    assert runtime.launch(profile_id=profile_id, process=proc, probe=probe).status == "STARTED"
    coordinator = ApplicationUpdateCoordinator(state_root=state_root)
    target, record, manifest, auth, payload = release()

    with pytest.raises(UpdateRejectedError):
        coordinator.prepare(
            runtime=runtime,
            current_release_id="release-root-v1",
            manifest=manifest,
            artifact_id=record.artifact_id,
            artifact_bytes=payload,
            target=target,
            authenticity=auth,
            process=proc,
            probe=probe,
            schema_target_version=1,
        )

    assert runtime.state == "RUNNING"
    assert not coordinator.updates_root.exists()
    assert runtime.quit().status == "STOPPED"


def test_apply_rejects_symlink_alias_before_package_driver(tmp_path: Path) -> None:
    real_data = tmp_path / "real-data"
    state_root = tmp_path / "state"
    profile_id = make_profile(real_data)
    proc = process()
    probe = Probe()
    runtime = ApplicationRuntime(data_root=real_data, state_root=state_root)
    assert runtime.launch(profile_id=profile_id, process=proc, probe=probe).status == "STARTED"

    coordinator = ApplicationUpdateCoordinator(state_root=state_root)
    target, record, manifest, auth, payload = release()
    plan = coordinator.prepare(
        runtime=runtime,
        current_release_id="release-root-v1",
        manifest=manifest,
        artifact_id=record.artifact_id,
        artifact_bytes=payload,
        target=target,
        authenticity=auth,
        process=proc,
        probe=probe,
        schema_target_version=1,
    )
    assert runtime.state == "STOPPED"

    alias = tmp_path / "apply-alias"
    symlink_dir(alias, real_data)
    driver = MustNotRunDriver()
    with pytest.raises(UpdateRejectedError):
        coordinator.apply(
            plan=plan,
            data_root=alias,
            manifest=manifest,
            artifact_bytes=payload,
            target=target,
            authenticity=auth,
            driver=driver,
            process=proc,
            probe=probe,
        )

    assert driver.calls == 0
    assert coordinator.status(plan.update_id).state == "PREPARED"
