import unittest

from n0te.platforms import PlatformEnvironment
from n0te.support import SupportTarget
from n0te.worker import (
    WorkerCapability,
    WorkerEnvelope,
    WorkerEnvelopeError,
    WorkerIdentity,
    WorkerRequest,
    WorkerResult,
)


class Platform00EWorkerEnvelopeTests(unittest.TestCase):
    def platform(self, os_name="Darwin", machine="arm64"):
        return PlatformEnvironment.from_runtime_labels(os_name, machine)

    def target(self, os_name="Darwin", machine="arm64"):
        return SupportTarget.from_runtime_labels(os_name=os_name, machine=machine)

    def worker(self, os_name="Darwin", machine="arm64", digest="a" * 64):
        return WorkerIdentity("plugin_worker", self.platform(os_name, machine), digest)

    def capability(
        self,
        *,
        job="tool.process",
        format_kind="VST3",
        mode="NATIVE_ONLY",
        architectures=("ARM64",),
    ):
        return WorkerCapability("cap:tool", job, format_kind, mode, tuple(architectures))

    def request(
        self,
        worker,
        target,
        *,
        request_id="req:1",
        job="tool.process",
        format_kind="VST3",
        workload_architecture="ARM64",
        timeout_ms=1000,
    ):
        return WorkerRequest(
            request_id=request_id,
            worker_fingerprint=worker.fingerprint,
            target_fingerprint=target.fingerprint,
            job_id=job,
            format_kind=format_kind,
            workload_architecture=workload_architecture,
            payload_fingerprint="b" * 64,
            timeout_ms=timeout_ms,
        )

    def assertWorkerError(self, fn):
        with self.assertRaises(WorkerEnvelopeError):
            fn()

    def test_worker_fingerprint_changes_with_build_or_architecture(self):
        self.assertNotEqual(self.worker().fingerprint, self.worker(digest="c" * 64).fingerprint)
        self.assertNotEqual(self.worker().fingerprint, self.worker(machine="amd64").fingerprint)

    def test_native_route_is_accepted(self):
        worker = self.worker()
        target = self.target()
        request = self.request(worker, target)
        route = WorkerEnvelope.plan(
            worker=worker,
            capability=self.capability(),
            request=request,
            target=target,
        )
        self.assertFalse(route.foreign_architecture)
        self.assertEqual(route.execution_mode, "NATIVE_ONLY")

    def test_request_bound_to_another_worker_is_rejected(self):
        worker = self.worker()
        request = self.request(worker, self.target())
        other = self.worker(digest="c" * 64)
        self.assertWorkerError(
            lambda: WorkerEnvelope.plan(
                worker=other,
                capability=self.capability(),
                request=request,
                target=self.target(),
            )
        )

    def test_request_bound_to_another_target_is_rejected(self):
        worker = self.worker()
        request = self.request(worker, self.target())
        self.assertWorkerError(
            lambda: WorkerEnvelope.plan(
                worker=worker,
                capability=self.capability(),
                request=request,
                target=self.target("Windows", "amd64"),
            )
        )

    def test_wrong_job_and_format_are_rejected(self):
        worker = self.worker()
        target = self.target()
        for request in (
            self.request(worker, target, job="different.job"),
            self.request(worker, target, format_kind="AU"),
        ):
            self.assertWorkerError(
                lambda request=request: WorkerEnvelope.plan(
                    worker=worker,
                    capability=self.capability(),
                    request=request,
                    target=target,
                )
            )

    def test_undeclared_workload_architecture_is_rejected(self):
        worker = self.worker()
        target = self.target()
        request = self.request(worker, target, workload_architecture="X86_64")
        self.assertWorkerError(
            lambda: WorkerEnvelope.plan(
                worker=worker,
                capability=self.capability(),
                request=request,
                target=target,
            )
        )

    def test_native_only_foreign_worker_is_rejected(self):
        worker = self.worker(machine="amd64")
        target = self.target(machine="arm64")
        request = self.request(worker, target, workload_architecture="X86_64")
        self.assertWorkerError(
            lambda: WorkerEnvelope.plan(
                worker=worker,
                capability=self.capability(
                    mode="NATIVE_ONLY", architectures=("X86_64",)
                ),
                request=request,
                target=target,
            )
        )

    def test_explicit_isolated_foreign_architecture_route_is_accepted(self):
        worker = self.worker(machine="amd64")
        target = self.target(machine="arm64")
        request = self.request(worker, target, workload_architecture="X86_64")
        route = WorkerEnvelope.plan(
            worker=worker,
            capability=self.capability(
                mode="ISOLATED_FOREIGN_ARCH", architectures=("X86_64",)
            ),
            request=request,
            target=target,
        )
        self.assertTrue(route.foreign_architecture)
        self.assertEqual(route.execution_mode, "ISOLATED_FOREIGN_ARCH")

    def test_foreign_architecture_request_must_match_worker_architecture(self):
        worker = self.worker(machine="amd64")
        target = self.target(machine="arm64")
        request = self.request(worker, target, workload_architecture="ARM64")
        self.assertWorkerError(
            lambda: WorkerEnvelope.plan(
                worker=worker,
                capability=self.capability(
                    mode="ISOLATED_FOREIGN_ARCH",
                    architectures=("ARM64", "X86_64"),
                ),
                request=request,
                target=target,
            )
        )

    def test_foreign_worker_must_remain_on_same_os_family(self):
        worker = self.worker(os_name="Windows", machine="amd64")
        target = self.target(os_name="Darwin", machine="arm64")
        request = self.request(worker, target, workload_architecture="X86_64")
        self.assertWorkerError(
            lambda: WorkerEnvelope.plan(
                worker=worker,
                capability=self.capability(
                    mode="ISOLATED_FOREIGN_ARCH", architectures=("X86_64",)
                ),
                request=request,
                target=target,
            )
        )

    def test_success_requires_result_fingerprint_receipt_and_evidence(self):
        worker = self.worker()
        target = self.target()
        request = self.request(worker, target)
        with self.assertRaises(WorkerEnvelopeError):
            WorkerResult(request.fingerprint, worker.fingerprint, "SUCCEEDED", "evidence")
        result = WorkerResult(
            request.fingerprint,
            worker.fingerprint,
            "SUCCEEDED",
            "evidence:worker",
            "c" * 64,
            "receipt:worker",
        )
        self.assertEqual(
            WorkerEnvelope.validate_result(worker=worker, request=request, result=result),
            result,
        )

    def test_non_success_result_cannot_carry_success_receipt(self):
        worker = self.worker()
        request = self.request(worker, self.target())
        with self.assertRaises(WorkerEnvelopeError):
            WorkerResult(
                request.fingerprint,
                worker.fingerprint,
                "FAILED",
                "evidence",
                "c" * 64,
                "receipt",
            )

    def test_failed_crashed_timed_out_and_unknown_remain_distinct(self):
        worker = self.worker()
        request = self.request(worker, self.target())
        states = tuple(
            WorkerResult(
                request.fingerprint,
                worker.fingerprint,
                state,
                f"evidence:{state.lower()}",
            ).state
            for state in ("FAILED", "CRASHED", "TIMED_OUT", "UNKNOWN")
        )
        self.assertEqual(states, ("FAILED", "CRASHED", "TIMED_OUT", "UNKNOWN"))

    def test_result_for_another_request_is_rejected(self):
        worker = self.worker()
        target = self.target()
        first = self.request(worker, target, request_id="req:1")
        second = self.request(worker, target, request_id="req:2")
        result = WorkerResult(first.fingerprint, worker.fingerprint, "FAILED", "evidence")
        self.assertWorkerError(
            lambda: WorkerEnvelope.validate_result(
                worker=worker, request=second, result=result
            )
        )

    def test_result_for_another_worker_is_rejected(self):
        worker = self.worker()
        other = self.worker(digest="c" * 64)
        request = self.request(worker, self.target())
        result = WorkerResult(request.fingerprint, other.fingerprint, "FAILED", "evidence")
        self.assertWorkerError(
            lambda: WorkerEnvelope.validate_result(
                worker=worker, request=request, result=result
            )
        )

    def test_timeout_must_be_positive_integer_not_bool(self):
        worker = self.worker()
        target = self.target()
        for timeout in (0, -1, True, 1.2):
            self.assertWorkerError(
                lambda timeout=timeout: self.request(
                    worker, target, timeout_ms=timeout
                )
            )

    def test_request_identity_is_deterministic(self):
        worker = self.worker()
        target = self.target()
        self.assertEqual(
            self.request(worker, target).fingerprint,
            self.request(worker, target).fingerprint,
        )

    def test_workload_architecture_list_is_canonical(self):
        first = self.capability(architectures=("X86_64", "ARM64", "X86_64"))
        second = self.capability(architectures=("ARM64", "X86_64"))
        self.assertEqual(first.workload_architectures, second.workload_architectures)

    def test_unknown_workload_architecture_is_rejected(self):
        with self.assertRaises(WorkerEnvelopeError):
            self.capability(architectures=("UNKNOWN",))

    def test_worker_envelope_exposes_no_process_or_plugin_execution_surface(self):
        public = {
            name
            for name in dir(WorkerEnvelope)
            if not name.startswith("_") and callable(getattr(WorkerEnvelope, name))
        }
        self.assertEqual(public, {"plan", "validate_result"})
        self.assertFalse(
            public
            & {
                "spawn",
                "subprocess",
                "load",
                "process_audio",
                "kill",
                "install",
                "execute",
                "send",
                "connect",
            }
        )


if __name__ == "__main__":
    unittest.main()
