import hashlib
import unittest

from n0te.artifacts import (
    ArtifactRecord,
    ArtifactTrustError,
    ManifestAuthenticityEvidence,
    ReleaseArtifactVerifier,
    ReleaseManifest,
)
from n0te.support import SupportTarget


class Platform00DArtifactTrustTests(unittest.TestCase):
    def setUp(self):
        self.mac = SupportTarget.from_runtime_labels(os_name="Darwin", machine="arm64")
        self.windows = SupportTarget.from_runtime_labels(os_name="Windows", machine="amd64")
        self.mac_bytes = b"n0te-macos-arm64-package"
        self.windows_bytes = b"n0te-windows-x64-package"

    @staticmethod
    def digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def artifact(self, artifact_id, target, payload, package_kind="PACKAGE"):
        return ArtifactRecord(
            artifact_id=artifact_id,
            target_fingerprint=target.fingerprint,
            package_kind=package_kind,
            size_bytes=len(payload),
            sha256=self.digest(payload),
        )

    def manifest(self, artifacts, version="1.0.0", release_id="release-1"):
        return ReleaseManifest(
            release_id=release_id,
            version=version,
            source_commit_sha="a" * 40,
            build_inputs_sha256="b" * 64,
            dependency_inventory_sha256="c" * 64,
            license_inventory_sha256="d" * 64,
            artifacts=tuple(artifacts),
        )

    @staticmethod
    def authenticity(manifest, status="VERIFIED"):
        return ManifestAuthenticityEvidence(
            manifest_fingerprint=manifest.fingerprint,
            status=status,
            verifier_id="platform-release-verifier",
            scheme="platform-authenticity",
            evidence_ref="verify:manifest:1",
        )

    def test_manifest_artifact_order_is_canonical_and_fingerprint_deterministic(self):
        mac = self.artifact("mac", self.mac, self.mac_bytes)
        win = self.artifact("win", self.windows, self.windows_bytes)
        first = self.manifest((win, mac))
        second = self.manifest((mac, win))
        self.assertEqual(first.artifacts, second.artifacts)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_source_commit_must_be_exact_hex_sha(self):
        artifact = self.artifact("mac", self.mac, self.mac_bytes)
        with self.assertRaises(ArtifactTrustError):
            ReleaseManifest(
                "release",
                "1.0",
                "not-a-sha",
                "b" * 64,
                "c" * 64,
                "d" * 64,
                (artifact,),
            )

    def test_build_dependency_and_license_digests_are_mandatory_sha256(self):
        artifact = self.artifact("mac", self.mac, self.mac_bytes)
        for field in (
            "build_inputs_sha256",
            "dependency_inventory_sha256",
            "license_inventory_sha256",
        ):
            values = dict(
                release_id="release",
                version="1.0",
                source_commit_sha="a" * 40,
                build_inputs_sha256="b" * 64,
                dependency_inventory_sha256="c" * 64,
                license_inventory_sha256="d" * 64,
                artifacts=(artifact,),
            )
            values[field] = "bad"
            with self.assertRaises(ArtifactTrustError):
                ReleaseManifest(**values)

    def test_artifact_requires_exact_target_fingerprint(self):
        with self.assertRaises(ArtifactTrustError):
            ArtifactRecord("mac", "bad", "pkg", 1, "a" * 64)

    def test_artifact_ids_and_target_package_routes_are_unique(self):
        mac = self.artifact("mac", self.mac, self.mac_bytes, "pkg")
        same_id = self.artifact("mac", self.windows, self.windows_bytes, "pkg")
        with self.assertRaises(ArtifactTrustError):
            self.manifest((mac, same_id))
        same_route = self.artifact("mac-2", self.mac, self.mac_bytes, "PKG")
        with self.assertRaises(ArtifactTrustError):
            self.manifest((mac, same_route))

    def test_package_kind_is_canonical_uppercase(self):
        record = self.artifact("mac", self.mac, self.mac_bytes, "MsIx")
        self.assertEqual(record.package_kind, "MSIX")

    def test_authenticity_evidence_requires_verifier_scheme_and_reference(self):
        manifest = self.manifest((self.artifact("mac", self.mac, self.mac_bytes),))
        for field in ("verifier_id", "scheme", "evidence_ref"):
            values = dict(
                manifest_fingerprint=manifest.fingerprint,
                status="VERIFIED",
                verifier_id="verifier",
                scheme="scheme",
                evidence_ref="ref",
            )
            values[field] = " "
            with self.assertRaises(ArtifactTrustError):
                ManifestAuthenticityEvidence(**values)

    def test_missing_or_untrusted_authenticity_fails_closed(self):
        manifest = self.manifest((self.artifact("mac", self.mac, self.mac_bytes),))
        for authenticity in (
            None,
            self.authenticity(manifest, "UNTRUSTED"),
            self.authenticity(manifest, "UNKNOWN"),
        ):
            result = ReleaseArtifactVerifier.verify(
                manifest=manifest,
                artifact_id="mac",
                artifact_bytes=self.mac_bytes,
                expected_target=self.mac,
                authenticity=authenticity,
            )
            self.assertEqual(result.status, "MANIFEST_UNAUTHENTICATED")

    def test_authenticity_for_another_manifest_is_rejected(self):
        artifact = self.artifact("mac", self.mac, self.mac_bytes)
        first = self.manifest((artifact,), version="1.0.0", release_id="first")
        second = self.manifest((artifact,), version="1.0.1", release_id="second")
        result = ReleaseArtifactVerifier.verify(
            manifest=second,
            artifact_id="mac",
            artifact_bytes=self.mac_bytes,
            expected_target=self.mac,
            authenticity=self.authenticity(first),
        )
        self.assertEqual(result.status, "MANIFEST_MISMATCH")

    def test_unknown_artifact_is_rejected(self):
        manifest = self.manifest((self.artifact("mac", self.mac, self.mac_bytes),))
        result = ReleaseArtifactVerifier.verify(
            manifest=manifest,
            artifact_id="missing",
            artifact_bytes=self.mac_bytes,
            expected_target=self.mac,
            authenticity=self.authenticity(manifest),
        )
        self.assertEqual(result.status, "ARTIFACT_NOT_FOUND")

    def test_wrong_expected_target_is_rejected(self):
        manifest = self.manifest((self.artifact("mac", self.mac, self.mac_bytes),))
        result = ReleaseArtifactVerifier.verify(
            manifest=manifest,
            artifact_id="mac",
            artifact_bytes=self.mac_bytes,
            expected_target=self.windows,
            authenticity=self.authenticity(manifest),
        )
        self.assertEqual(result.status, "TARGET_MISMATCH")

    def test_size_mismatch_is_rejected_before_hash_claim(self):
        manifest = self.manifest((self.artifact("mac", self.mac, self.mac_bytes),))
        result = ReleaseArtifactVerifier.verify(
            manifest=manifest,
            artifact_id="mac",
            artifact_bytes=self.mac_bytes + b"x",
            expected_target=self.mac,
            authenticity=self.authenticity(manifest),
        )
        self.assertEqual(result.status, "SIZE_MISMATCH")
        self.assertIsNone(result.actual_sha256)

    def test_same_size_tampering_is_hash_mismatch(self):
        manifest = self.manifest((self.artifact("mac", self.mac, self.mac_bytes),))
        tampered = bytearray(self.mac_bytes)
        tampered[-1] ^= 1
        result = ReleaseArtifactVerifier.verify(
            manifest=manifest,
            artifact_id="mac",
            artifact_bytes=tampered,
            expected_target=self.mac,
            authenticity=self.authenticity(manifest),
        )
        self.assertEqual(result.status, "HASH_MISMATCH")
        self.assertNotEqual(result.actual_sha256, manifest.artifact("mac").sha256)

    def test_exact_bytes_target_and_authenticity_are_ready(self):
        manifest = self.manifest((self.artifact("mac", self.mac, self.mac_bytes),))
        result = ReleaseArtifactVerifier.verify(
            manifest=manifest,
            artifact_id="mac",
            artifact_bytes=self.mac_bytes,
            expected_target=self.mac,
            authenticity=self.authenticity(manifest),
        )
        self.assertEqual(result.status, "READY")
        self.assertEqual(result.actual_sha256, self.digest(self.mac_bytes))

    def test_memoryview_is_supported_without_io(self):
        manifest = self.manifest((self.artifact("mac", self.mac, self.mac_bytes),))
        result = ReleaseArtifactVerifier.verify(
            manifest=manifest,
            artifact_id="mac",
            artifact_bytes=memoryview(self.mac_bytes),
            expected_target=self.mac,
            authenticity=self.authenticity(manifest),
        )
        self.assertEqual(result.status, "READY")

    def test_two_artifacts_verify_independently(self):
        manifest = self.manifest(
            (
                self.artifact("mac", self.mac, self.mac_bytes, "pkg"),
                self.artifact("win", self.windows, self.windows_bytes, "msix"),
            )
        )
        auth = self.authenticity(manifest)
        mac = ReleaseArtifactVerifier.verify(
            manifest=manifest,
            artifact_id="mac",
            artifact_bytes=self.mac_bytes,
            expected_target=self.mac,
            authenticity=auth,
        )
        win = ReleaseArtifactVerifier.verify(
            manifest=manifest,
            artifact_id="win",
            artifact_bytes=self.windows_bytes,
            expected_target=self.windows,
            authenticity=auth,
        )
        self.assertEqual((mac.status, win.status), ("READY", "READY"))

    def test_changing_any_provenance_digest_changes_manifest_fingerprint(self):
        artifact = self.artifact("mac", self.mac, self.mac_bytes)
        base = self.manifest((artifact,))
        changed = ReleaseManifest(
            release_id=base.release_id,
            version=base.version,
            source_commit_sha=base.source_commit_sha,
            build_inputs_sha256="e" * 64,
            dependency_inventory_sha256=base.dependency_inventory_sha256,
            license_inventory_sha256=base.license_inventory_sha256,
            artifacts=base.artifacts,
        )
        self.assertNotEqual(base.fingerprint, changed.fingerprint)

    def test_older_release_can_still_verify_for_explicit_rollback_policy(self):
        artifact = self.artifact("mac", self.mac, self.mac_bytes)
        old = self.manifest((artifact,), version="0.9.0", release_id="old")
        result = ReleaseArtifactVerifier.verify(
            manifest=old,
            artifact_id="mac",
            artifact_bytes=self.mac_bytes,
            expected_target=self.mac,
            authenticity=self.authenticity(old),
        )
        self.assertEqual(result.status, "READY")

    def test_artifact_size_must_be_positive_integer_not_bool(self):
        for size in (0, -1, True, 1.2):
            with self.assertRaises(ArtifactTrustError):
                ArtifactRecord("mac", self.mac.fingerprint, "pkg", size, "a" * 64)

    def test_verifier_exposes_no_install_sign_download_or_execute_surface(self):
        public = {
            name
            for name in dir(ReleaseArtifactVerifier)
            if not name.startswith("_")
            and callable(getattr(ReleaseArtifactVerifier, name))
        }
        self.assertEqual(public, {"verify"})
        forbidden = {"install", "update", "rollback", "sign", "download", "execute"}
        self.assertFalse(public & forbidden)


if __name__ == "__main__":
    unittest.main()
