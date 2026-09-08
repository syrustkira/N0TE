from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .instance import InstanceLease, InstanceLeaseManager, ProcessIdentity, ProcessProbe
from .lineage import LineageStore

_PROFILE_ID = re.compile(r"^prf_[0-9a-f]{32}$")
_BOOTSTRAP_LEASE_ID = "__profile_bootstrap__"
RESOLUTION_STATES = {
    "CREATED",
    "SELECTED_EXISTING",
    "NEEDS_SELECTION",
    "NEEDS_CREATION",
    "BOOTSTRAP_BUSY",
    "RECOVERY_REQUIRED",
}


class ApplicationProfilesError(RuntimeError):
    """Invalid or unsafe application-profile bootstrap state."""


def _text(value: str, field: str) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise ApplicationProfilesError(f"{field} must not be empty")
    return text


@dataclass(frozen=True)
class ApplicationProfile:
    profile_id: str
    artist_name: str

    def __post_init__(self) -> None:
        if not _PROFILE_ID.fullmatch(str(self.profile_id)):
            raise ApplicationProfilesError("invalid profile_id")
        object.__setattr__(self, "artist_name", _text(self.artist_name, "artist_name"))


@dataclass(frozen=True)
class ProfileIssue:
    profile_ref: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_ref", _text(self.profile_ref, "profile_ref"))
        object.__setattr__(self, "reason", _text(self.reason, "reason"))


@dataclass(frozen=True)
class ProfileCatalogSnapshot:
    profiles: tuple[ApplicationProfile, ...]
    issues: tuple[ProfileIssue, ...]

    def __post_init__(self) -> None:
        profiles = tuple(sorted(tuple(self.profiles), key=lambda item: item.profile_id))
        issues = tuple(sorted(tuple(self.issues), key=lambda item: (item.profile_ref, item.reason)))
        if len({item.profile_id for item in profiles}) != len(profiles):
            raise ApplicationProfilesError("duplicate profile identity in catalog")
        object.__setattr__(self, "profiles", profiles)
        object.__setattr__(self, "issues", issues)


@dataclass(frozen=True)
class ProfileResolution:
    state: str
    profiles: tuple[ApplicationProfile, ...]
    selected_profile_id: str | None = None
    issues: tuple[ProfileIssue, ...] = ()
    blocking_lease: InstanceLease | None = None
    previous_bootstrap_lease: InstanceLease | None = None

    def __post_init__(self) -> None:
        if self.state not in RESOLUTION_STATES:
            raise ApplicationProfilesError(f"invalid resolution state: {self.state}")
        profiles = tuple(self.profiles)
        issues = tuple(self.issues)
        object.__setattr__(self, "profiles", profiles)
        object.__setattr__(self, "issues", issues)
        profile_ids = {profile.profile_id for profile in profiles}
        if self.selected_profile_id is not None and self.selected_profile_id not in profile_ids:
            raise ApplicationProfilesError("selected profile is not present in resolution catalog")
        if self.state in {"CREATED", "SELECTED_EXISTING"} and self.selected_profile_id is None:
            raise ApplicationProfilesError(f"{self.state} requires selected_profile_id")
        if self.state == "NEEDS_SELECTION" and len(profiles) < 2:
            raise ApplicationProfilesError("NEEDS_SELECTION requires multiple profiles")
        if self.state == "NEEDS_CREATION" and (profiles or issues):
            raise ApplicationProfilesError("NEEDS_CREATION requires an empty healthy catalog")
        if self.state == "BOOTSTRAP_BUSY" and self.blocking_lease is None:
            raise ApplicationProfilesError("BOOTSTRAP_BUSY requires blocking_lease")
        if self.state == "RECOVERY_REQUIRED" and not issues:
            raise ApplicationProfilesError("RECOVERY_REQUIRED requires explicit issues")


class ApplicationProfiles:
    """Local application profile discovery and first-profile bootstrap.

    Only direct canonical profile candidates under data_root/profiles are read.
    Discovery uses SQLite read-only mode plus the canonical LineageStore integrity
    validator, so catalog reads cannot enable WAL, initialize service schemas or
    otherwise turn profile selection into a write path. This service never
    deletes, merges, renames, uploads, syncs or crawls for profiles outside the
    application data root.
    """

    def __init__(self, *, data_root: str | Path, state_root: str | Path):
        data = Path(data_root)
        state = Path(state_root)
        if not data.is_absolute():
            raise ApplicationProfilesError("data_root must be absolute")
        if not state.is_absolute():
            raise ApplicationProfilesError("state_root must be absolute")
        self.data_root = data
        self.state_root = state
        self._leases = InstanceLeaseManager(state)

    @property
    def profiles_root(self) -> Path:
        return self.data_root / "profiles"

    @staticmethod
    def _issue(profile_ref: str, exc: BaseException | str) -> ProfileIssue:
        if isinstance(exc, BaseException):
            reason = f"{type(exc).__name__}: {exc}"
        else:
            reason = str(exc)
        return ProfileIssue(profile_ref, reason)

    def _inspect_profile_read_only(self, profile_id: str) -> ApplicationProfile:
        LineageStore._validate_profile_id(profile_id)
        db_path = self.data_root / "profiles" / profile_id / LineageStore.DB_NAME
        if db_path.is_symlink():
            raise ApplicationProfilesError("lineage database must not be a symlink")
        if not db_path.is_file():
            raise ApplicationProfilesError("lineage database is missing")
        try:
            uri = db_path.resolve(strict=True).as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            try:
                LineageStore._validate_existing(conn, profile_id)
                row = conn.execute(
                    "SELECT a.id, a.display_name "
                    "FROM artists a "
                    "JOIN metadata m ON m.key = 'primary_artist_id' AND m.value = a.id"
                ).fetchone()
                if row is None:
                    raise ApplicationProfilesError("primary Artist identity is missing")
                return ApplicationProfile(profile_id, str(row["display_name"]))
            finally:
                conn.close()
        except ApplicationProfilesError:
            raise
        except Exception as exc:
            raise ApplicationProfilesError(
                f"read-only lineage inspection failed: {exc}"
            ) from exc

    def discover(self) -> ProfileCatalogSnapshot:
        root = self.profiles_root
        if root.is_symlink():
            return ProfileCatalogSnapshot(
                (),
                (ProfileIssue("profiles", "canonical profiles root must not be a symlink"),),
            )
        if not root.exists():
            return ProfileCatalogSnapshot((), ())
        if not root.is_dir():
            return ProfileCatalogSnapshot(
                (),
                (ProfileIssue("profiles", "canonical profiles root is not a directory"),),
            )

        profiles: list[ApplicationProfile] = []
        issues: list[ProfileIssue] = []
        try:
            entries = sorted(os.scandir(root), key=lambda entry: entry.name)
        except OSError as exc:
            return ProfileCatalogSnapshot((), (self._issue("profiles", exc),))

        for entry in entries:
            name = entry.name
            if not name.startswith("prf_"):
                continue
            if not _PROFILE_ID.fullmatch(name):
                issues.append(ProfileIssue(name, "invalid profile identity directory name"))
                continue
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                issues.append(ProfileIssue(name, "profile candidate is not a real directory"))
                continue
            try:
                profiles.append(self._inspect_profile_read_only(name))
            except Exception as exc:
                issues.append(self._issue(name, exc))

        return ProfileCatalogSnapshot(tuple(profiles), tuple(issues))

    @staticmethod
    def _existing_resolution(
        snapshot: ProfileCatalogSnapshot,
        selected_profile_id: str | None,
        *,
        previous_bootstrap_lease: InstanceLease | None = None,
    ) -> ProfileResolution | None:
        if snapshot.issues:
            return ProfileResolution(
                "RECOVERY_REQUIRED",
                snapshot.profiles,
                issues=snapshot.issues,
                previous_bootstrap_lease=previous_bootstrap_lease,
            )

        if selected_profile_id is not None:
            selected = str(selected_profile_id).strip()
            if not _PROFILE_ID.fullmatch(selected):
                raise ApplicationProfilesError("selected_profile_id is invalid")
            if selected not in {profile.profile_id for profile in snapshot.profiles}:
                raise ApplicationProfilesError("selected profile does not exist in the healthy catalog")
            return ProfileResolution(
                "SELECTED_EXISTING",
                snapshot.profiles,
                selected_profile_id=selected,
                previous_bootstrap_lease=previous_bootstrap_lease,
            )

        if len(snapshot.profiles) == 1:
            return ProfileResolution(
                "SELECTED_EXISTING",
                snapshot.profiles,
                selected_profile_id=snapshot.profiles[0].profile_id,
                previous_bootstrap_lease=previous_bootstrap_lease,
            )
        if len(snapshot.profiles) > 1:
            return ProfileResolution(
                "NEEDS_SELECTION",
                snapshot.profiles,
                previous_bootstrap_lease=previous_bootstrap_lease,
            )
        return None

    def resolve(
        self,
        *,
        artist_name: str | None = None,
        selected_profile_id: str | None = None,
        process: ProcessIdentity | None = None,
        probe: ProcessProbe | None = None,
    ) -> ProfileResolution:
        snapshot = self.discover()
        existing = self._existing_resolution(snapshot, selected_profile_id)
        if existing is not None:
            return existing

        if artist_name is None or not str(artist_name).strip():
            return ProfileResolution("NEEDS_CREATION", ())
        artist = _text(artist_name, "artist_name")
        if process is None or not isinstance(process, ProcessIdentity):
            raise ApplicationProfilesError(
                "first-profile creation requires exact ProcessIdentity"
            )
        if probe is None:
            raise ApplicationProfilesError("first-profile creation requires ProcessProbe")

        acquired = self._leases.acquire(_BOOTSTRAP_LEASE_ID, process, probe)
        if acquired.status in {"HELD_BY_OTHER", "ALREADY_OWNED"}:
            return ProfileResolution(
                "BOOTSTRAP_BUSY",
                (),
                blocking_lease=acquired.lease,
            )
        if acquired.status == "UNCERTAIN":
            return ProfileResolution(
                "RECOVERY_REQUIRED",
                (),
                issues=(
                    ProfileIssue(
                        _BOOTSTRAP_LEASE_ID,
                        "first-profile creation ownership is uncertain and will not be stolen",
                    ),
                ),
                blocking_lease=acquired.lease,
            )
        if acquired.status not in {"ACQUIRED", "REPLACED_STALE"}:
            raise ApplicationProfilesError(
                f"unexpected bootstrap lease status: {acquired.status}"
            )
        if acquired.lease is None:
            raise ApplicationProfilesError("bootstrap lease result is missing ownership")

        result: ProfileResolution
        try:
            locked_snapshot = self.discover()
            existing_after_lock = self._existing_resolution(
                locked_snapshot,
                None,
                previous_bootstrap_lease=acquired.previous_lease,
            )
            if existing_after_lock is not None:
                result = existing_after_lock
            else:
                store: LineageStore | None = None
                created_profile: ApplicationProfile | None = None
                creation_issues: list[ProfileIssue] = []
                try:
                    store = LineageStore.create(self.data_root, artist)
                    created_profile = ApplicationProfile(
                        store.profile_id,
                        store.artist().display_name,
                    )
                except Exception as exc:
                    creation_issues.append(self._issue("profile-create", exc))
                finally:
                    if store is not None:
                        try:
                            store.close()
                        except Exception as exc:
                            creation_issues.append(
                                self._issue(
                                    store.profile_id,
                                    f"Lineage store close failed after profile creation: {exc}",
                                )
                            )
                if creation_issues:
                    result = ProfileResolution(
                        "RECOVERY_REQUIRED",
                        (() if created_profile is None else (created_profile,)),
                        selected_profile_id=(
                            None if created_profile is None else created_profile.profile_id
                        ),
                        issues=tuple(creation_issues),
                        previous_bootstrap_lease=acquired.previous_lease,
                    )
                elif created_profile is None:
                    raise ApplicationProfilesError(
                        "profile creation produced neither profile nor issue"
                    )
                else:
                    result = ProfileResolution(
                        "CREATED",
                        (created_profile,),
                        selected_profile_id=created_profile.profile_id,
                        previous_bootstrap_lease=acquired.previous_lease,
                    )
        finally:
            try:
                self._leases.release(
                    _BOOTSTRAP_LEASE_ID,
                    process=process,
                    lease_nonce=acquired.lease.lease_nonce,
                )
            except Exception as exc:
                issue = self._issue(
                    _BOOTSTRAP_LEASE_ID,
                    f"bootstrap lease release failed: {exc}",
                )
                if "result" in locals():
                    result = ProfileResolution(
                        "RECOVERY_REQUIRED",
                        result.profiles,
                        selected_profile_id=result.selected_profile_id,
                        issues=tuple(result.issues) + (issue,),
                        blocking_lease=acquired.lease,
                        previous_bootstrap_lease=acquired.previous_lease,
                    )
                else:
                    result = ProfileResolution(
                        "RECOVERY_REQUIRED",
                        (),
                        issues=(issue,),
                        blocking_lease=acquired.lease,
                        previous_bootstrap_lease=acquired.previous_lease,
                    )
        return result
