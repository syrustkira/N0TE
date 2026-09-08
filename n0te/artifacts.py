from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from .support import SupportTarget

AUTHENTICITY_STATES = {"VERIFIED", "UNTRUSTED", "UNKNOWN"}
VERIFICATION_STATES = {
    "READY",
    "MANIFEST_UNAUTHENTICATED",
    "MANIFEST_MISMATCH",
    "ARTIFACT_NOT_FOUND",
    "TARGET_MISMATCH",
    "SIZE_MISMATCH",
    "HASH_MISMATCH",
}
EMPTY_SCHEMA_MIGRATION_SHA256 = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


class ArtifactTrustError(ValueError):
    """Malformed release-manifest or artifact-trust input."""


def _text(value: str, field: str) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise ArtifactTrustError(f"{field} must not be empty")
    return text


def _hex(value: str, length: int, field: str) -> str:
    text = str(value).strip().lower()
    if len(text) != length or any(ch not in "0123456789abcdef" for ch in text):
        raise ArtifactTrustError(
            f"{field} must be a {length}-character hexadecimal digest"
        )
    return text


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    target_fingerprint: str
    package_kind: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _text(self.artifact_id, "artifact_id"))
        object.__setattr__(
            self,
            "target_fingerprint",
            _hex(self.target_fingerprint, 64, "target_fingerprint"),
        )
        object.__setattr__(
            self,
            "package_kind",
            _text(self.package_kind, "package_kind").upper(),
        )
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ArtifactTrustError("size_bytes must be an integer")
        if self.size_bytes <= 0:
            raise ArtifactTrustError("size_bytes must be positive")
        object.__setattr__(self, "sha256", _hex(self.sha256, 64, "sha256"))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "package_kind": self.package_kind,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "target_fingerprint": self.target_fingerprint,
        }


@dataclass(frozen=True)
class ReleaseManifest:
    release_id: str
    version: str
    source_commit_sha: str
    build_inputs_sha256: str
    dependency_inventory_sha256: str
    license_inventory_sha256: str
    artifacts: tuple[ArtifactRecord, ...]
    application_schema_version: int = 1
    application_schema_migrations_sha256: str = EMPTY_SCHEMA_MIGRATION_SHA256

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_id", _text(self.release_id, "release_id"))
        object.__setattr__(self, "version", _text(self.version, "version"))
        object.__setattr__(
            self,
            "source_commit_sha",
            _hex(self.source_commit_sha, 40, "source_commit_sha"),
        )
        for field in (
            "build_inputs_sha256",
            "dependency_inventory_sha256",
            "license_inventory_sha256",
        ):
            object.__setattr__(self, field, _hex(getattr(self, field), 64, field))
        if (
            isinstance(self.application_schema_version, bool)
            or not isinstance(self.application_schema_version, int)
            or self.application_schema_version < 1
        ):
            raise ArtifactTrustError("application_schema_version must be a positive integer")
        object.__setattr__(
            self,
            "application_schema_migrations_sha256",
            _hex(
                self.application_schema_migrations_sha256,
                64,
                "application_schema_migrations_sha256",
            ),
        )

        records = tuple(self.artifacts)
        if not records:
            raise ArtifactTrustError("release manifest requires at least one artifact")
        for record in records:
            if not isinstance(record, ArtifactRecord):
                raise TypeError("artifacts must contain ArtifactRecord values")
        records = tuple(sorted(records, key=lambda record: record.artifact_id))
        artifact_ids: set[str] = set()
        target_packages: set[tuple[str, str]] = set()
        for record in records:
            if record.artifact_id in artifact_ids:
                raise ArtifactTrustError(f"duplicate artifact_id: {record.artifact_id}")
            artifact_ids.add(record.artifact_id)
            route = (record.target_fingerprint, record.package_kind)
            if route in target_packages:
                raise ArtifactTrustError(
                    "manifest cannot contain duplicate target/package artifact routes"
                )
            target_packages.add(route)
        object.__setattr__(self, "artifacts", records)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "application_schema_migrations_sha256": self.application_schema_migrations_sha256,
            "application_schema_version": self.application_schema_version,
            "artifacts": [record.canonical_payload() for record in self.artifacts],
            "build_inputs_sha256": self.build_inputs_sha256,
            "dependency_inventory_sha256": self.dependency_inventory_sha256,
            "license_inventory_sha256": self.license_inventory_sha256,
            "release_id": self.release_id,
            "source_commit_sha": self.source_commit_sha,
            "version": self.version,
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json(self.canonical_payload())).hexdigest()

    def artifact(self, artifact_id: str) -> ArtifactRecord | None:
        wanted = _text(artifact_id, "artifact_id")
        for record in self.artifacts:
            if record.artifact_id == wanted:
                return record
        return None


@dataclass(frozen=True)
class ManifestAuthenticityEvidence:
    manifest_fingerprint: str
    status: str
    verifier_id: str
    scheme: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "manifest_fingerprint",
            _hex(self.manifest_fingerprint, 64, "manifest_fingerprint"),
        )
        if self.status not in AUTHENTICITY_STATES:
            raise ArtifactTrustError(
                f"invalid manifest authenticity status: {self.status}"
            )
        object.__setattr__(self, "verifier_id", _text(self.verifier_id, "verifier_id"))
        object.__setattr__(self, "scheme", _text(self.scheme, "scheme"))
        object.__setattr__(
            self,
            "evidence_ref",
            _text(self.evidence_ref, "evidence_ref"),
        )


@dataclass(frozen=True)
class ArtifactVerification:
    status: str
    manifest_fingerprint: str
    artifact_id: str
    expected_target_fingerprint: str
    actual_sha256: str | None
    reason: str

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATES:
            raise ArtifactTrustError(f"invalid verification status: {self.status}")


class ReleaseArtifactVerifier:
    """Pure fail-closed verifier for already-produced package/update bytes.

    The authenticated manifest binds both package bytes and the application
    semantic-schema version/program expected by that release. This class does
    not execute migrations, install artifacts, download bytes, or choose releases.
    """

    @staticmethod
    def verify(
        *,
        manifest: ReleaseManifest,
        artifact_id: str,
        artifact_bytes: bytes | bytearray | memoryview,
        expected_target: SupportTarget,
        authenticity: ManifestAuthenticityEvidence | None,
    ) -> ArtifactVerification:
        if not isinstance(manifest, ReleaseManifest):
            raise TypeError("manifest must be ReleaseManifest")
        if not isinstance(expected_target, SupportTarget):
            raise TypeError("expected_target must be SupportTarget")
        if not isinstance(artifact_bytes, (bytes, bytearray, memoryview)):
            raise TypeError("artifact_bytes must be bytes-like")

        wanted_id = _text(artifact_id, "artifact_id")
        expected_fingerprint = expected_target.fingerprint

        if authenticity is None or authenticity.status != "VERIFIED":
            return ArtifactVerification(
                status="MANIFEST_UNAUTHENTICATED",
                manifest_fingerprint=manifest.fingerprint,
                artifact_id=wanted_id,
                expected_target_fingerprint=expected_fingerprint,
                actual_sha256=None,
                reason="exact manifest has no VERIFIED authenticity evidence",
            )
        if authenticity.manifest_fingerprint != manifest.fingerprint:
            return ArtifactVerification(
                status="MANIFEST_MISMATCH",
                manifest_fingerprint=manifest.fingerprint,
                artifact_id=wanted_id,
                expected_target_fingerprint=expected_fingerprint,
                actual_sha256=None,
                reason="authenticity evidence belongs to a different manifest fingerprint",
            )

        record = manifest.artifact(wanted_id)
        if record is None:
            return ArtifactVerification(
                status="ARTIFACT_NOT_FOUND",
                manifest_fingerprint=manifest.fingerprint,
                artifact_id=wanted_id,
                expected_target_fingerprint=expected_fingerprint,
                actual_sha256=None,
                reason="artifact_id is absent from the authenticated manifest",
            )
        if record.target_fingerprint != expected_fingerprint:
            return ArtifactVerification(
                status="TARGET_MISMATCH",
                manifest_fingerprint=manifest.fingerprint,
                artifact_id=wanted_id,
                expected_target_fingerprint=expected_fingerprint,
                actual_sha256=None,
                reason="artifact target does not match the exact expected support target",
            )

        payload = bytes(artifact_bytes)
        if len(payload) != record.size_bytes:
            return ArtifactVerification(
                status="SIZE_MISMATCH",
                manifest_fingerprint=manifest.fingerprint,
                artifact_id=wanted_id,
                expected_target_fingerprint=expected_fingerprint,
                actual_sha256=None,
                reason="artifact byte length does not match authenticated manifest",
            )
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if actual_sha256 != record.sha256:
            return ArtifactVerification(
                status="HASH_MISMATCH",
                manifest_fingerprint=manifest.fingerprint,
                artifact_id=wanted_id,
                expected_target_fingerprint=expected_fingerprint,
                actual_sha256=actual_sha256,
                reason="artifact SHA-256 does not match authenticated manifest",
            )

        return ArtifactVerification(
            status="READY",
            manifest_fingerprint=manifest.fingerprint,
            artifact_id=wanted_id,
            expected_target_fingerprint=expected_fingerprint,
            actual_sha256=actual_sha256,
            reason="artifact bytes, target, semantic-schema declaration and exact authenticated manifest agree",
        )
