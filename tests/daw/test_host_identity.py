import dataclasses
import unittest

from n0te.hosts import (
    CORE_HOST_FAMILIES,
    HOST_FAMILIES,
    HostIdentityError,
    HostRuntimeIdentity,
    assert_identity_contract_has_no_priority_fields,
    normalize_host_family,
    normalize_translation_mode,
)


class HostIdentityTests(unittest.TestCase):
    def make(self, family="ABLETON_LIVE", **kwargs):
        args = dict(
            host_family=family,
            version="12.1",
            edition="Suite",
            os_name="Darwin",
            machine="arm64",
        )
        args.update(kwargs)
        return HostRuntimeIdentity.from_runtime_labels(**args)

    def test_all_six_named_hosts_are_peers_plus_generic_other(self):
        self.assertEqual(
            HOST_FAMILIES,
            (
                "ABLETON_LIVE", "FL_STUDIO", "LOGIC_PRO", "PRO_TOOLS",
                "STUDIO_ONE", "REAPER", "GENERIC_OTHER",
            ),
        )
        self.assertEqual(len(CORE_HOST_FAMILIES), 6)
        for family in CORE_HOST_FAMILIES:
            with self.subTest(family=family):
                self.assertEqual(self.make(family).family, family)

    def test_alias_normalization_is_deterministic(self):
        pairs = {
            "Ableton Live": "ABLETON_LIVE",
            "FL-Studio": "FL_STUDIO",
            "Apple Logic Pro": "LOGIC_PRO",
            "Avid Pro Tools": "PRO_TOOLS",
            "PreSonus Studio One": "STUDIO_ONE",
            "Cockos REAPER": "REAPER",
            "generic_other": "GENERIC_OTHER",
        }
        for raw, expected in pairs.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_host_family(raw), expected)

    def test_unknown_host_does_not_silently_become_generic(self):
        with self.assertRaises(HostIdentityError):
            normalize_host_family("Bitwig Studio")

    def test_generic_other_requires_explicit_identity_label(self):
        with self.assertRaises(HostIdentityError):
            self.make("GENERIC_OTHER")
        a = self.make("GENERIC_OTHER", generic_host_label="Bitwig Studio")
        b = self.make("GENERIC_OTHER", generic_host_label="Tracktion Waveform")
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_generic_label_case_and_spacing_are_cosmetic(self):
        a = self.make("GENERIC_OTHER", generic_host_label="  Bitwig   Studio ")
        b = self.make("GENERIC_OTHER", generic_host_label="bitwig studio")
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_core_display_name_is_cosmetic(self):
        a = self.make("LOGIC_PRO", display_name="Logic Pro")
        b = self.make("LOGIC_PRO", display_name="My Logic Workspace")
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertNotEqual(a.canonical_display_name, b.canonical_display_name)

    def test_platform_aliases_converge(self):
        a = self.make("REAPER", os_name="Darwin", machine="arm64")
        b = self.make("REAPER", os_name="macOS", machine="aarch64")
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_raw_platform_labels_and_target_tier_are_not_fingerprint_identity(self):
        a = self.make("REAPER", os_name="Darwin", machine="arm64")
        b = self.make("REAPER", os_name="mac", machine="aarch64")
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_material_runtime_dimensions_change_fingerprint(self):
        base = self.make("STUDIO_ONE")
        variants = [
            self.make("STUDIO_ONE", version="12.2"),
            self.make("STUDIO_ONE", edition="Artist"),
            self.make("STUDIO_ONE", os_name="Windows", machine="arm64"),
            self.make("STUDIO_ONE", translation_mode="Rosetta 2"),
        ]
        for variant in variants:
            self.assertNotEqual(base.fingerprint, variant.fingerprint)

    def test_translation_aliases_are_canonical(self):
        self.assertEqual(normalize_translation_mode("none"), "NATIVE")
        self.assertEqual(normalize_translation_mode("Rosetta 2"), "ROSETTA_2")
        self.assertEqual(normalize_translation_mode("wow64"), "WINDOWS_X86_EMULATION")
        with self.assertRaises(HostIdentityError):
            normalize_translation_mode("magic bridge")

    def test_core_host_rejects_generic_label(self):
        with self.assertRaises(HostIdentityError):
            self.make("REAPER", generic_host_label="Other")

    def test_payload_contains_identity_not_support_or_ranking(self):
        payload = self.make("PRO_TOOLS").identity_payload()
        self.assertEqual(
            set(payload),
            {
                "family", "version", "edition", "os_family", "architecture",
                "translation_mode", "generic_host_label", "fingerprint",
            },
        )
        for forbidden in (
            "rank", "priority", "default", "preferred", "capability", "support",
            "path", "process", "adapter",
        ):
            self.assertFalse(any(forbidden in key.casefold() for key in payload))

    def test_dataclass_has_no_priority_capability_or_location_fields(self):
        assert_identity_contract_has_no_priority_fields()
        names = {field.name for field in dataclasses.fields(HostRuntimeIdentity)}
        self.assertEqual(
            names,
            {
                "family", "version", "edition", "platform", "translation_mode",
                "display_name", "generic_host_label",
            },
        )

    def test_fingerprint_is_deterministic_and_pure(self):
        item = self.make("FL_STUDIO")
        self.assertEqual(item.fingerprint, item.fingerprint)
        self.assertEqual(item.identity_payload(), item.identity_payload())

    def test_family_changes_identity_without_implying_priority(self):
        fingerprints = {self.make(family).fingerprint for family in CORE_HOST_FAMILIES}
        self.assertEqual(len(fingerprints), 6)


if __name__ == "__main__":
    unittest.main()
