from __future__ import annotations

import hashlib

import pytest

from n0te.artifacts import (
    ArtifactRecord,
    ArtifactTrustError,
    EMPTY_SCHEMA_MIGRATION_SHA256,
    ReleaseManifest,
)
from n0te.migration import MigrationStep
from n0te.schema_program import migration_steps_fingerprint
from n0te.support import SupportTarget


def artifact() -> ArtifactRecord:
    payload = b"schema-bound-release"
    target = SupportTarget.from_runtime_labels(os_name="Linux", machine="x86_64")
    return ArtifactRecord(
        artifact_id="linux-package",
        target_fingerprint=target.fingerprint,
        package_kind="TEST_PACKAGE",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def manifest(*, schema_version: int = 1, schema_program=()) -> ReleaseManifest:
    program = tuple(schema_program)
    return ReleaseManifest(
        release_id="release-schema",
        version="2.0.0",
        source_commit_sha="a" * 40,
        build_inputs_sha256="b" * 64,
        dependency_inventory_sha256="c" * 64,
        license_inventory_sha256="d" * 64,
        artifacts=(artifact(),),
        application_schema_version=schema_version,
        application_schema_migrations_sha256=migration_steps_fingerprint(program),
    )


def test_default_manifest_explicitly_authenticates_current_schema_and_empty_program() -> None:
    default = ReleaseManifest(
        release_id="default",
        version="1.0.0",
        source_commit_sha="a" * 40,
        build_inputs_sha256="b" * 64,
        dependency_inventory_sha256="c" * 64,
        license_inventory_sha256="d" * 64,
        artifacts=(artifact(),),
    )
    assert default.application_schema_version == 1
    assert default.application_schema_migrations_sha256 == EMPTY_SCHEMA_MIGRATION_SHA256
    assert default.application_schema_migrations_sha256 == migration_steps_fingerprint(())


def test_schema_version_and_program_are_part_of_authenticated_manifest_fingerprint() -> None:
    step = MigrationStep(
        1,
        2,
        "v2 marker",
        ("CREATE TABLE v2_marker(value TEXT)",),
    )
    current = manifest(schema_version=1, schema_program=())
    target = manifest(schema_version=2, schema_program=(step,))
    different_program = manifest(
        schema_version=2,
        schema_program=(
            MigrationStep(
                1,
                2,
                "different v2 marker",
                ("CREATE TABLE v2_other(value TEXT)",),
            ),
        ),
    )
    assert current.fingerprint != target.fingerprint
    assert target.fingerprint != different_program.fingerprint
    assert target.canonical_payload()["application_schema_version"] == 2
    assert (
        target.canonical_payload()["application_schema_migrations_sha256"]
        == migration_steps_fingerprint((step,))
    )


def test_invalid_semantic_schema_declarations_fail_closed() -> None:
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ArtifactTrustError):
            ReleaseManifest(
                release_id="invalid",
                version="1.0.0",
                source_commit_sha="a" * 40,
                build_inputs_sha256="b" * 64,
                dependency_inventory_sha256="c" * 64,
                license_inventory_sha256="d" * 64,
                artifacts=(artifact(),),
                application_schema_version=invalid,  # type: ignore[arg-type]
            )
    with pytest.raises(ArtifactTrustError):
        ReleaseManifest(
            release_id="invalid-hash",
            version="1.0.0",
            source_commit_sha="a" * 40,
            build_inputs_sha256="b" * 64,
            dependency_inventory_sha256="c" * 64,
            license_inventory_sha256="d" * 64,
            artifacts=(artifact(),),
            application_schema_migrations_sha256="bad",
        )
