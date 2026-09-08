from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.platforms import PlatformEnvironment
from n0te.profiles import ApplicationProfiles


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=99120,
        start_token="consumer-cold-start-regression",
    )


class ConsumerColdStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.data_root = (root / "data").resolve()
        self.state_root = (root / "state").resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_empty_profile_catalog_resolves_to_needs_creation_without_fake_selection(self) -> None:
        resolution = ApplicationProfiles(
            data_root=self.data_root,
            state_root=self.state_root,
        ).resolve()
        self.assertEqual(resolution.state, "NEEDS_CREATION")
        self.assertEqual(resolution.profiles, ())
        self.assertIsNone(resolution.selected_profile_id)
        self.assertEqual(resolution.issues, ())

    def test_fresh_consumer_shell_renders_welcome_before_any_profile_exists(self) -> None:
        shell = ConsumerShell(
            data_root=self.data_root,
            state_root=self.state_root,
            process=process(),
            probe=Probe(),
        )
        shell.start()
        try:
            try:
                with urlopen(Request(shell.address.origin + "/"), timeout=2.0) as response:
                    status = response.status
                    page = response.read().decode("utf-8")
            except HTTPError as exc:
                status = exc.code
                page = exc.read().decode("utf-8")
            self.assertEqual(status, 200, page)
            self.assertIn("Welcome to your Headquarters", page)
            self.assertIn('action="/profile/create"', page)
            self.assertFalse((self.data_root / "profiles").exists())
        finally:
            shell.stop()


if __name__ == "__main__":
    unittest.main()
