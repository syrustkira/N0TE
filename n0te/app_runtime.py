from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .instance import (
    InstanceLease,
    InstanceLeaseManager,
    ProcessIdentity,
    ProcessProbe,
)
from .memory import HeadquartersMemory

RUNTIME_STATES = {"STOPPED", "RUNNING", "RECOVERY_REQUIRED"}
LAUNCH_STATUSES = {
    "STARTED",
    "ALREADY_RUNNING",
    "REOPEN_EXISTING",
    "HELD_BY_OTHER",
    "UNCERTAIN",
    "START_FAILED",
    "RECOVERY_REQUIRED",
}
QUIT_STATUSES = {"STOPPED", "ALREADY_STOPPED", "RECOVERY_REQUIRED"}


class ApplicationRuntimeError(RuntimeError):
    """Invalid application-runtime lifecycle transition."""


@dataclass(frozen=True)
class LaunchResult:
    status: str
    profile_id: str
    lease: InstanceLease | None
    previous_lease: InstanceLease | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in LAUNCH_STATUSES:
            raise ApplicationRuntimeError(f"invalid launch status: {self.status}")


@dataclass(frozen=True)
class QuitResult:
    status: str
    profile_id: str | None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in QUIT_STATUSES:
            raise ApplicationRuntimeError(f"invalid quit status: {self.status}")


class ApplicationRuntime:
    """Own one consumer runtime session for one existing N0TE profile.

    The runtime composes the canonical Headquarters database with the shared
    per-profile instance lease. It does not create a window, spawn a daemon,
    focus an existing UI, install/update software, or kill another process.
    """

    def __init__(
        self,
        *,
        data_root: str | Path,
        state_root: str | Path,
        memory_opener: Callable[[str | Path, str], HeadquartersMemory] = HeadquartersMemory.open,
    ):
        data = Path(data_root)
        state = Path(state_root)
        if not data.is_absolute():
            raise ApplicationRuntimeError("data_root must be absolute")
        if not state.is_absolute():
            raise ApplicationRuntimeError("state_root must be absolute")
        if not callable(memory_opener):
            raise TypeError("memory_opener must be callable")
        try:
            from .direct_fan_shell import install_direct_fan_headquarters

            install_direct_fan_headquarters()
        except Exception as exc:
            raise ApplicationRuntimeError(
                f"Direct Fan consumer installation failed before runtime launch: {exc}"
            ) from exc
        self.data_root = data
        self.state_root = state
        self._memory_opener = memory_opener
        self._leases = InstanceLeaseManager(state)
        self._state = "STOPPED"
        self._profile_id: str | None = None
        self._process: ProcessIdentity | None = None
        self._lease: InstanceLease | None = None
        self._headquarters: HeadquartersMemory | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def profile_id(self) -> str | None:
        return self._profile_id

    @property
    def headquarters(self) -> HeadquartersMemory:
        if self._state != "RUNNING" or self._headquarters is None:
            raise ApplicationRuntimeError("Headquarters is available only while RUNNING")
        return self._headquarters

    def _clear(self) -> None:
        self._state = "STOPPED"
        self._profile_id = None
        self._process = None
        self._lease = None
        self._headquarters = None

    def launch(
        self,
        *,
        profile_id: str,
        process: ProcessIdentity,
        probe: ProcessProbe,
    ) -> LaunchResult:
        profile = str(profile_id).strip()
        if not profile:
            raise ApplicationRuntimeError("profile_id must not be empty")
        if not isinstance(process, ProcessIdentity):
            raise TypeError("process must be ProcessIdentity")
        if self._state == "RECOVERY_REQUIRED":
            raise ApplicationRuntimeError(
                "runtime requires explicit quit/recovery before another launch"
            )
        if self._state == "RUNNING":
            if (
                self._profile_id == profile
                and self._process is not None
                and self._process.same_launch(process)
            ):
                return LaunchResult(
                    "ALREADY_RUNNING",
                    profile,
                    self._lease,
                    reason="this ApplicationRuntime already owns the requested exact launch/profile",
                )
            raise ApplicationRuntimeError(
                "one ApplicationRuntime cannot launch another profile/process while RUNNING"
            )

        acquired = self._leases.acquire(profile, process, probe)
        if acquired.status == "ALREADY_OWNED":
            return LaunchResult(
                "REOPEN_EXISTING",
                profile,
                acquired.lease,
                acquired.previous_lease,
                "the exact launch already owns this profile; do not open a second Headquarters",
            )
        if acquired.status == "HELD_BY_OTHER":
            return LaunchResult(
                "HELD_BY_OTHER",
                profile,
                acquired.lease,
                reason="another verified-live exact launch owns this profile",
            )
        if acquired.status == "UNCERTAIN":
            return LaunchResult(
                "UNCERTAIN",
                profile,
                acquired.lease,
                reason="profile ownership cannot be proven safe to replace",
            )
        if acquired.status not in {"ACQUIRED", "REPLACED_STALE"} or acquired.lease is None:
            raise ApplicationRuntimeError(
                f"unexpected lease acquire result: {acquired.status}"
            )

        lease = acquired.lease
        try:
            headquarters = self._memory_opener(self.data_root, profile)
            if not isinstance(headquarters, HeadquartersMemory):
                raise ApplicationRuntimeError(
                    "memory_opener did not return HeadquartersMemory"
                )
            if headquarters.store.profile_id != profile:
                try:
                    headquarters.close()
                except Exception as close_exc:
                    raise ApplicationRuntimeError(
                        "memory_opener returned a different profile and that Headquarters could not be closed"
                    ) from close_exc
                raise ApplicationRuntimeError(
                    "memory_opener returned Headquarters for a different profile"
                )
        except Exception as exc:
            try:
                self._leases.release(
                    profile,
                    process=process,
                    lease_nonce=lease.lease_nonce,
                )
            except Exception as cleanup_exc:
                self._state = "RECOVERY_REQUIRED"
                self._profile_id = profile
                self._process = process
                self._lease = lease
                self._headquarters = None
                return LaunchResult(
                    "RECOVERY_REQUIRED",
                    profile,
                    lease,
                    acquired.previous_lease,
                    f"Headquarters open failed and lease cleanup also failed: {cleanup_exc}",
                )
            return LaunchResult(
                "START_FAILED",
                profile,
                None,
                acquired.previous_lease,
                f"Headquarters open failed: {exc}",
            )

        try:
            from .career_state_shell import install_career_state_headquarters
            from .credits_shell import install_credits_headquarters

            install_credits_headquarters()
            install_career_state_headquarters()
        except Exception as exc:
            try:
                headquarters.close()
            except Exception as close_exc:
                self._state = "RECOVERY_REQUIRED"
                self._profile_id = profile
                self._process = process
                self._lease = lease
                self._headquarters = headquarters
                return LaunchResult(
                    "RECOVERY_REQUIRED",
                    profile,
                    lease,
                    acquired.previous_lease,
                    f"Headquarters consumer installation failed and Headquarters could not close safely: {close_exc}",
                )
            try:
                self._leases.release(
                    profile,
                    process=process,
                    lease_nonce=lease.lease_nonce,
                )
            except Exception as cleanup_exc:
                self._state = "RECOVERY_REQUIRED"
                self._profile_id = profile
                self._process = process
                self._lease = lease
                self._headquarters = None
                return LaunchResult(
                    "RECOVERY_REQUIRED",
                    profile,
                    lease,
                    acquired.previous_lease,
                    f"Headquarters consumer installation failed and lease cleanup also failed: {cleanup_exc}",
                )
            return LaunchResult(
                "START_FAILED",
                profile,
                None,
                acquired.previous_lease,
                f"Headquarters consumer installation failed: {exc}",
            )

        self._state = "RUNNING"
        self._profile_id = profile
        self._process = process
        self._lease = lease
        self._headquarters = headquarters
        return LaunchResult(
            "STARTED",
            profile,
            lease,
            acquired.previous_lease,
            (
                "replaced verified-stale owner and opened Headquarters"
                if acquired.status == "REPLACED_STALE"
                else "acquired profile runtime and opened Headquarters"
            ),
        )

    def quit(self) -> QuitResult:
        if self._state == "STOPPED":
            return QuitResult("ALREADY_STOPPED", None, "runtime is already stopped")

        profile = self._profile_id
        process = self._process
        lease = self._lease
        if profile is None or process is None or lease is None:
            raise ApplicationRuntimeError(
                "runtime recovery state is missing exact lease ownership"
            )

        if self._headquarters is not None:
            try:
                self._headquarters.close()
            except Exception as exc:
                self._state = "RECOVERY_REQUIRED"
                return QuitResult(
                    "RECOVERY_REQUIRED",
                    profile,
                    f"Headquarters close failed; lease remains owned: {exc}",
                )
            self._headquarters = None

        try:
            self._leases.release(
                profile,
                process=process,
                lease_nonce=lease.lease_nonce,
            )
        except Exception as exc:
            self._state = "RECOVERY_REQUIRED"
            return QuitResult(
                "RECOVERY_REQUIRED",
                profile,
                f"Headquarters is closed but exact lease release failed: {exc}",
            )

        self._clear()
        return QuitResult("STOPPED", profile, "Headquarters closed and lease released")