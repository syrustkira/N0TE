import json
import os
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

from n0te.instance import (
    InstanceLeaseCorruptionError,
    InstanceLeaseError,
    InstanceLeaseManager,
    InstanceLeaseOwnershipError,
    ProcessIdentity,
    _TakeoverMarker,
    semantic_lease_ref,
)
from n0te.platforms import PlatformEnvironment, PlatformRoots, target_tier


class Probe:
    def __init__(self, default="UNKNOWN"):
        self.default = default
        self.values = {}

    def set(self, process, status):
        self.values[process.fingerprint] = status

    def status(self, process):
        return self.values.get(process.fingerprint, self.default)


class SequenceProbe:
    def __init__(self, sequences, default="UNKNOWN"):
        self.sequences = {key: list(values) for key, values in sequences.items()}
        self.default = default

    def status(self, process):
        values = self.sequences.get(process.fingerprint, [])
        return values.pop(0) if values else self.default


class Platform00BInstanceLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.manager = InstanceLeaseManager(self.root)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def platform(os_family="LINUX", architecture="X86_64"):
        return PlatformEnvironment(
            os_family=os_family,
            architecture=architecture,
            raw_os_name=os_family,
            raw_machine=architecture,
            target_tier=target_tier(os_family, architecture),
        )

    @classmethod
    def process(cls, pid, token, os_family="LINUX", architecture="X86_64"):
        return ProcessIdentity.from_start_token(
            cls.platform(os_family, architecture),
            pid=pid,
            start_token=token,
        )

    def test_pid_reuse_is_not_process_identity(self):
        old = self.process(100, "start:a")
        reused = self.process(100, "start:b")
        self.assertEqual(old.pid, reused.pid)
        self.assertNotEqual(old.fingerprint, reused.fingerprint)

    def test_semantic_lease_ref_is_peer_platform_shape(self):
        roots = (
            PlatformRoots(
                "MACOS",
                PurePosixPath("/data"), PurePosixPath("/config"),
                PurePosixPath("/state"), PurePosixPath("/cache"), PurePosixPath("/logs"),
            ),
            PlatformRoots(
                "LINUX",
                PurePosixPath("/data"), PurePosixPath("/config"),
                PurePosixPath("/state"), PurePosixPath("/cache"), PurePosixPath("/logs"),
            ),
            PlatformRoots(
                "WINDOWS",
                PureWindowsPath("C:/data"), PureWindowsPath("C:/config"),
                PureWindowsPath("C:/state"), PureWindowsPath("C:/cache"), PureWindowsPath("C:/logs"),
            ),
        )
        refs = tuple(semantic_lease_ref(item, "profile_x") for item in roots)
        self.assertTrue(str(refs[0]).endswith("profiles/profile_x/instance/lease.json"))
        self.assertTrue(str(refs[1]).endswith("profiles/profile_x/instance/lease.json"))
        self.assertTrue(str(refs[2]).lower().endswith(r"profiles\profile_x\instance\lease.json"))

    def test_same_process_acquire_is_idempotent_and_exact_owner_can_release(self):
        process = self.process(1, "same")
        first = self.manager.acquire("profile", process, Probe("ALIVE"))
        second = self.manager.acquire("profile", process, Probe("ALIVE"))
        self.assertEqual(first.status, "ACQUIRED")
        self.assertEqual(second.status, "ALREADY_OWNED")
        self.assertEqual(first.lease, second.lease)
        self.manager.release(
            "profile", process=process, lease_nonce=first.lease.lease_nonce
        )
        self.assertIsNone(self.manager.inspect("profile"))

    def test_alive_foreign_owner_blocks_takeover(self):
        owner = self.process(1, "owner")
        challenger = self.process(2, "challenger")
        existing = self.manager.acquire("profile", owner, Probe())
        probe = Probe()
        probe.set(owner, "ALIVE")
        result = self.manager.acquire("profile", challenger, probe)
        self.assertEqual(result.status, "HELD_BY_OTHER")
        self.assertEqual(self.manager.inspect("profile"), existing.lease)

    def test_unknown_foreign_owner_fails_closed_without_stealing(self):
        owner = self.process(1, "owner")
        challenger = self.process(2, "challenger")
        existing = self.manager.acquire("profile", owner, Probe())
        probe = Probe()
        probe.set(owner, "UNKNOWN")
        result = self.manager.acquire("profile", challenger, probe)
        self.assertEqual(result.status, "UNCERTAIN")
        self.assertEqual(self.manager.inspect("profile"), existing.lease)

    def test_verified_dead_owner_is_archived_and_replaced(self):
        owner = self.process(1, "owner")
        challenger = self.process(2, "challenger")
        existing = self.manager.acquire("profile", owner, Probe())
        probe = Probe()
        probe.set(owner, "DEAD")
        result = self.manager.acquire("profile", challenger, probe)
        self.assertEqual(result.status, "REPLACED_STALE")
        self.assertEqual(result.previous_lease, existing.lease)
        self.assertEqual(self.manager.inspect("profile"), result.lease)
        archive = (
            self.root / "profiles" / "profile" / "instance" / "stale"
            / f"{existing.lease.lease_nonce}.json"
        )
        self.assertTrue(archive.exists())

    def test_takeover_reprobes_and_cancels_if_owner_is_alive_or_unknown(self):
        for final, expected in (("ALIVE", "HELD_BY_OTHER"), ("UNKNOWN", "UNCERTAIN")):
            with self.subTest(final=final):
                root = Path(tempfile.mkdtemp()).resolve()
                manager = InstanceLeaseManager(root)
                owner = self.process(10, f"owner:{final}")
                challenger = self.process(11, f"challenger:{final}")
                manager.acquire("profile", owner, Probe())
                result = manager.acquire(
                    "profile",
                    challenger,
                    SequenceProbe({owner.fingerprint: ["DEAD", final]}),
                )
                self.assertEqual(result.status, expected)
                self.assertEqual(manager.inspect("profile").process, owner)
                self.assertIsNone(manager._read_marker("profile"))

    def test_interrupted_takeover_resumes_before_and_after_archive(self):
        for after_archive in (False, True):
            with self.subTest(after_archive=after_archive):
                root = Path(tempfile.mkdtemp()).resolve()
                manager = InstanceLeaseManager(root)
                owner = self.process(20, f"owner:{after_archive}")
                taker = self.process(21, f"taker:{after_archive}")
                existing = manager.acquire("profile", owner, Probe())
                manager._prepare_dirs("profile")
                marker = _TakeoverMarker.new(taker, existing.lease)
                self.assertTrue(
                    manager._write_exclusive(manager._marker_path("profile"), marker.to_data())
                )
                if after_archive:
                    os.replace(
                        manager._lease_path("profile"),
                        manager._stale_dir("profile") / f"{existing.lease.lease_nonce}.json",
                    )
                probe = Probe()
                probe.set(owner, "DEAD")
                resumed = manager.acquire("profile", taker, probe)
                self.assertEqual(resumed.status, "REPLACED_STALE")
                self.assertEqual(resumed.previous_lease, existing.lease)
                self.assertIsNone(manager._read_marker("profile"))

    def test_live_or_unknown_foreign_takeover_marker_is_not_cleared(self):
        for status in ("ALIVE", "UNKNOWN"):
            with self.subTest(status=status):
                root = Path(tempfile.mkdtemp()).resolve()
                manager = InstanceLeaseManager(root)
                owner = self.process(30, f"owner:{status}")
                taker = self.process(31, f"taker:{status}")
                third = self.process(32, f"third:{status}")
                existing = manager.acquire("profile", owner, Probe())
                manager._prepare_dirs("profile")
                marker = _TakeoverMarker.new(taker, existing.lease)
                manager._write_exclusive(manager._marker_path("profile"), marker.to_data())
                probe = Probe()
                probe.set(taker, status)
                result = manager.acquire("profile", third, probe)
                self.assertEqual(result.status, "UNCERTAIN")
                self.assertEqual(manager._read_marker("profile"), marker)

    def test_verified_dead_takeover_marker_does_not_block_profile_forever(self):
        owner = self.process(40, "owner")
        dead_taker = self.process(41, "dead-taker")
        third = self.process(42, "third")
        existing = self.manager.acquire("profile", owner, Probe())
        self.manager._prepare_dirs("profile")
        marker = _TakeoverMarker.new(dead_taker, existing.lease)
        self.manager._write_exclusive(self.manager._marker_path("profile"), marker.to_data())
        probe = Probe()
        probe.set(dead_taker, "DEAD")
        probe.set(owner, "ALIVE")
        result = self.manager.acquire("profile", third, probe)
        self.assertEqual(result.status, "HELD_BY_OTHER")
        self.assertIsNone(self.manager._read_marker("profile"))

    def test_wrong_process_or_nonce_cannot_release(self):
        owner = self.process(1, "owner")
        other = self.process(2, "other")
        result = self.manager.acquire("profile", owner, Probe())
        with self.assertRaises(InstanceLeaseOwnershipError):
            self.manager.release(
                "profile", process=other, lease_nonce=result.lease.lease_nonce
            )
        with self.assertRaises(InstanceLeaseOwnershipError):
            self.manager.release(
                "profile", process=owner, lease_nonce="0" * 32
            )
        self.assertEqual(self.manager.inspect("profile"), result.lease)

    def test_malformed_tampered_and_symlink_lease_fail_visibly(self):
        directory = self.root / "profiles" / "profile" / "instance"
        directory.mkdir(parents=True)
        lease_path = directory / "lease.json"
        lease_path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(InstanceLeaseCorruptionError):
            self.manager.inspect("profile")
        lease_path.unlink()

        process = self.process(1, "owner")
        self.manager.acquire("profile", process, Probe())
        data = json.loads(lease_path.read_text(encoding="utf-8"))
        data["process"]["fingerprint"] = "0" * 64
        lease_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(InstanceLeaseCorruptionError):
            self.manager.inspect("profile")
        lease_path.unlink()

        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        os.symlink(target, lease_path)
        with self.assertRaises(InstanceLeaseCorruptionError):
            self.manager.inspect("profile")

    def test_profile_isolation_and_path_validation(self):
        first = self.process(1, "first")
        second = self.process(2, "second")
        self.assertEqual(self.manager.acquire("one", first, Probe()).status, "ACQUIRED")
        self.assertEqual(self.manager.acquire("two", second, Probe()).status, "ACQUIRED")
        self.assertEqual(self.manager.inspect("one").process, first)
        self.assertEqual(self.manager.inspect("two").process, second)
        for bad in ("", ".", "..", "../x", "a/b", r"a\b"):
            with self.subTest(profile=bad):
                with self.assertRaises(InstanceLeaseError):
                    self.manager.acquire(bad, first, Probe())
        with self.assertRaises(InstanceLeaseError):
            InstanceLeaseManager("relative/state")

    def test_atomic_acquire_race_has_exactly_one_new_owner(self):
        first = self.process(101, "first")
        second = self.process(102, "second")
        barrier = threading.Barrier(2)
        results = []
        errors = []
        probe = Probe("ALIVE")

        def worker(process):
            try:
                barrier.wait()
                results.append(self.manager.acquire("profile", process, probe))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(process,))
            for process in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(sum(item.status == "ACQUIRED" for item in results), 1)
        self.assertTrue(
            all(item.status in {"ACQUIRED", "HELD_BY_OTHER", "UNCERTAIN"} for item in results)
        )
        self.assertIsNotNone(self.manager.inspect("profile"))

    def test_public_manager_has_no_process_execution_or_kill_verb(self):
        public = {
            name
            for name in dir(InstanceLeaseManager)
            if not name.startswith("_") and callable(getattr(InstanceLeaseManager, name))
        }
        self.assertTrue({"acquire", "inspect", "release"}.issubset(public))
        self.assertFalse({"kill", "launch", "terminate", "signal", "connect"} & public)


if __name__ == "__main__":
    unittest.main()
