import tempfile
import unittest
from pathlib import Path

from n0te.platforms import (
    PlatformEnvironment,
    PlatformError,
    normalize_architecture,
    normalize_os_family,
    resolve_application_roots,
)


class Platform00AEnvironmentRootTests(unittest.TestCase):
    def test_os_aliases_are_platform_neutral_and_unknown_is_explicit(self):
        self.assertEqual(
            [normalize_os_family(value) for value in ("Darwin", "macOS", "Windows", "win32", "Linux", "FreeBSD")],
            ["MACOS", "MACOS", "WINDOWS", "WINDOWS", "LINUX", "UNSUPPORTED"],
        )

    def test_architecture_aliases_normalize_without_claiming_unknown_support(self):
        self.assertEqual(
            [normalize_architecture(value) for value in ("arm64", "aarch64", "AMD64", "x86_64", "i686", "armv7l", "riscv64", "mips")],
            ["ARM64", "ARM64", "X86_64", "X86_64", "X86_32", "ARMV7", "RISCV64", "UNKNOWN"],
        )

    def test_target_tier_is_policy_target_not_false_acceptance(self):
        self.assertEqual(PlatformEnvironment.from_runtime_labels("Darwin", "arm64").target_tier, "CORE_TARGET")
        self.assertEqual(PlatformEnvironment.from_runtime_labels("Windows", "i686").target_tier, "EXTENDED_TARGET")
        self.assertEqual(PlatformEnvironment.from_runtime_labels("Linux", "mips").target_tier, "UNVERIFIED")
        self.assertEqual(PlatformEnvironment.from_runtime_labels("FreeBSD", "amd64").target_tier, "UNSUPPORTED_PLATFORM")

    def test_macos_uses_normal_library_roots(self):
        roots = resolve_application_roots(
            PlatformEnvironment.from_runtime_labels("Darwin", "arm64"),
            home="/Users/artist",
        )
        self.assertEqual(str(roots.data_root), "/Users/artist/Library/Application Support/N0TE")
        self.assertEqual(str(roots.config_root), "/Users/artist/Library/Application Support/N0TE/Config")
        self.assertEqual(str(roots.cache_root), "/Users/artist/Library/Caches/N0TE")
        self.assertEqual(str(roots.log_root), "/Users/artist/Library/Logs/N0TE")

    def test_windows_honors_appdata_and_localappdata(self):
        roots = resolve_application_roots(
            PlatformEnvironment.from_runtime_labels("Windows", "AMD64"),
            home=r"C:\Users\Artist",
            environment={"APPDATA": r"D:\Roaming", "LOCALAPPDATA": r"E:\Local"},
        )
        self.assertEqual(str(roots.config_root), r"D:\Roaming\N0TE")
        self.assertEqual(str(roots.data_root), r"E:\Local\N0TE\Data")
        self.assertEqual(str(roots.state_root), r"E:\Local\N0TE\State")

    def test_windows_has_deterministic_home_fallbacks(self):
        roots = resolve_application_roots(
            PlatformEnvironment.from_runtime_labels("Windows", "arm64"),
            home=r"C:\Users\Artist",
        )
        self.assertEqual(str(roots.config_root), r"C:\Users\Artist\AppData\Roaming\N0TE")
        self.assertEqual(str(roots.cache_root), r"C:\Users\Artist\AppData\Local\N0TE\Cache")

    def test_linux_honors_xdg_roots(self):
        roots = resolve_application_roots(
            PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
            home="/home/artist",
            environment={
                "XDG_DATA_HOME": "/mnt/datax",
                "XDG_CONFIG_HOME": "/mnt/configx",
                "XDG_STATE_HOME": "/mnt/statex",
                "XDG_CACHE_HOME": "/mnt/cachex",
            },
        )
        self.assertEqual(str(roots.data_root), "/mnt/datax/N0TE")
        self.assertEqual(str(roots.config_root), "/mnt/configx/N0TE")
        self.assertEqual(str(roots.log_root), "/mnt/statex/N0TE/logs")

    def test_linux_has_xdg_fallbacks(self):
        roots = resolve_application_roots(
            PlatformEnvironment.from_runtime_labels("Linux", "aarch64"),
            home="/home/artist",
        )
        self.assertEqual(str(roots.data_root), "/home/artist/.local/share/N0TE")
        self.assertEqual(str(roots.config_root), "/home/artist/.config/N0TE")
        self.assertEqual(str(roots.state_root), "/home/artist/.local/state/N0TE")

    def test_relative_home_or_override_is_rejected(self):
        platform = PlatformEnvironment.from_runtime_labels("Linux", "x86_64")
        with self.assertRaises(PlatformError):
            resolve_application_roots(platform, home="relative")
        with self.assertRaises(PlatformError):
            resolve_application_roots(
                platform,
                home="/home/artist",
                environment={"XDG_DATA_HOME": "relative"},
            )

    def test_unsupported_platform_does_not_guess_roots(self):
        platform = PlatformEnvironment.from_runtime_labels("FreeBSD", "amd64")
        with self.assertRaises(PlatformError):
            resolve_application_roots(platform, home="/home/artist")

    def test_profile_id_remains_opaque_across_root_changes(self):
        platform = PlatformEnvironment.from_runtime_labels("Linux", "x86_64")
        left = resolve_application_roots(platform, home="/home/artist")
        right = resolve_application_roots(platform, home="/srv/relocated-home")
        profile_id = "profile_abc"
        self.assertEqual(left.profile_data_root(profile_id).name, profile_id)
        self.assertEqual(right.profile_data_root(profile_id).name, profile_id)
        for invalid in ("../x", "a/b", r"a\b", "..", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(PlatformError):
                    left.profile_data_root(invalid)

    def test_resolution_is_pure_and_creates_no_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            # The function is deliberately resolving a Linux environment. Feed
            # it a Linux absolute path even when this test itself runs on Windows,
            # and use the host temp directory only as a mutation guard.
            host_guard = Path(temp)
            before = tuple(host_guard.iterdir())
            home = f"/__n0te_purity__/{host_guard.name}/not-created-home"
            roots = resolve_application_roots(
                PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
                home=home,
            )
            self.assertEqual(tuple(host_guard.iterdir()), before)
            self.assertEqual(str(roots.data_root), f"{home}/.local/share/N0TE")

    def test_no_platform_root_uses_legacy_ableton_branded_state(self):
        fixtures = (
            (PlatformEnvironment.from_runtime_labels("Darwin", "arm64"), "/Users/a"),
            (PlatformEnvironment.from_runtime_labels("Linux", "x86_64"), "/home/a"),
            (PlatformEnvironment.from_runtime_labels("Windows", "amd64"), r"C:\Users\a"),
        )
        for platform, home in fixtures:
            roots = resolve_application_roots(platform, home=home)
            rendered = "|".join(
                str(value)
                for value in (
                    roots.data_root,
                    roots.config_root,
                    roots.state_root,
                    roots.cache_root,
                    roots.log_root,
                )
            )
            self.assertNotIn(".n0te-ableton-ai", rendered.lower())
            self.assertIn("N0TE", rendered)


if __name__ == "__main__":
    unittest.main()
