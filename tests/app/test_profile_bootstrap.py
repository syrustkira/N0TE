from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from n0te.instance import InstanceLeaseManager, ProcessIdentity
from n0te.lineage import LineageStore
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment
from n0te.profiles import ApplicationProfiles, ApplicationProfilesError


BOOTSTRAP_LEASE_ID = "__profile_bootstrap__"


class Probe:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, process: ProcessIdentity, status: str) -> None:
        self.values[process.fingerprint] = status

    def status(self, process: ProcessIdentity) -> str:
        return self.values.get(process.fingerprint, "UNKNOWN")


def process(pid: int, token: str) -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=token,
    )


def create_profile(data_root: Path, artist_name: str) -> str:
    headquarters = HeadquartersMemory.create(data_root, artist_name)
    try:
        return headquarters.store.profile_id
    finally:
        headquarters.close()


def table_names(database_path: Path) -> tuple[str, ...]:
    conn = sqlite3.connect(database_path)
    try:
        return tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
    finally:
        conn.close()


def test_empty_install_requires_creation_input(tmp_path: Path) -> None:
    profiles = ApplicationProfiles(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
    )
    result = profiles.resolve()
    assert result.state == "NEEDS_CREATION"
    assert result.profiles == ()
    assert result.issues == ()


def test_first_profile_creation_then_next_resolve_selects_same_identity(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    profiles = ApplicationProfiles(data_root=data_root, state_root=state_root)
    proc = process(201, "first-create")
    probe = Probe()

    created = profiles.resolve(
        artist_name="  First   Artist  ",
        process=proc,
        probe=probe,
    )
    assert created.state == "CREATED"
    assert created.selected_profile_id is not None
    assert len(created.profiles) == 1
    assert created.profiles[0].artist_name == "First Artist"
    assert InstanceLeaseManager(state_root).inspect(BOOTSTRAP_LEASE_ID) is None

    again = profiles.resolve()
    assert again.state == "SELECTED_EXISTING"
    assert again.selected_profile_id == created.selected_profile_id
    assert again.profiles == created.profiles


def test_multiple_profiles_require_explicit_selection(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    first = create_profile(data_root, "One")
    second = create_profile(data_root, "Two")
    profiles = ApplicationProfiles(data_root=data_root, state_root=state_root)

    unresolved = profiles.resolve()
    assert unresolved.state == "NEEDS_SELECTION"
    assert {item.profile_id for item in unresolved.profiles} == {first, second}

    selected = profiles.resolve(selected_profile_id=second)
    assert selected.state == "SELECTED_EXISTING"
    assert selected.selected_profile_id == second


def test_invalid_or_unknown_explicit_selection_is_refused(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    create_profile(data_root, "One")
    create_profile(data_root, "Two")
    profiles = ApplicationProfiles(data_root=data_root, state_root=state_root)

    with pytest.raises(ApplicationProfilesError):
        profiles.resolve(selected_profile_id="not-a-profile")
    with pytest.raises(ApplicationProfilesError):
        profiles.resolve(selected_profile_id="prf_" + "f" * 32)


def test_corrupt_valid_looking_profile_blocks_silent_new_identity(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    corrupt_id = "prf_" + "a" * 32
    corrupt_dir = data_root / "profiles" / corrupt_id
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / LineageStore.DB_NAME).write_bytes(b"not sqlite")
    profiles = ApplicationProfiles(data_root=data_root, state_root=state_root)

    discovered = profiles.discover()
    assert discovered.profiles == ()
    assert len(discovered.issues) == 1
    assert discovered.issues[0].profile_ref == corrupt_id

    result = profiles.resolve(
        artist_name="Do Not Create Around Corruption",
        process=process(202, "corrupt"),
        probe=Probe(),
    )
    assert result.state == "RECOVERY_REQUIRED"
    assert result.profiles == ()
    assert tuple((data_root / "profiles").iterdir()) == (corrupt_dir,)


def test_valid_looking_symlink_profile_candidate_is_recovery_issue(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    profiles_root = data_root / "profiles"
    profiles_root.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    link = profiles_root / ("prf_" + "b" * 32)
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    snapshot = ApplicationProfiles(data_root=data_root, state_root=state_root).discover()
    assert snapshot.profiles == ()
    assert len(snapshot.issues) == 1
    assert "not a real directory" in snapshot.issues[0].reason


def test_dangling_profiles_root_symlink_is_not_treated_as_empty_install(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    data_root.mkdir()
    profiles_root = data_root / "profiles"
    try:
        profiles_root.symlink_to(tmp_path / "missing", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    result = ApplicationProfiles(data_root=data_root, state_root=state_root).resolve(
        artist_name="Must Not Create",
        process=process(203, "dangling-root"),
        probe=Probe(),
    )
    assert result.state == "RECOVERY_REQUIRED"
    assert result.profiles == ()
    assert "symlink" in result.issues[0].reason


def test_discovery_does_not_initialize_unrelated_headquarters_service_tables(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    store = LineageStore.create(data_root, "Bare Lineage")
    profile_id = store.profile_id
    database_path = store.database_path
    store.close()
    before = table_names(database_path)

    snapshot = ApplicationProfiles(data_root=data_root, state_root=state_root).discover()
    after = table_names(database_path)

    assert len(snapshot.profiles) == 1
    assert snapshot.profiles[0].profile_id == profile_id
    assert before == after


def test_non_profile_entries_are_ignored_without_disk_crawl(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    profiles_root = data_root / "profiles"
    profiles_root.mkdir(parents=True)
    (profiles_root / ".DS_Store").write_text("noise")
    (profiles_root / "cache").mkdir()

    snapshot = ApplicationProfiles(data_root=data_root, state_root=state_root).discover()
    assert snapshot.profiles == ()
    assert snapshot.issues == ()


def test_same_process_existing_bootstrap_ownership_blocks_second_creation(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    proc = process(204, "same-process-bootstrap")
    probe = Probe()
    manager = InstanceLeaseManager(state_root)
    held = manager.acquire(BOOTSTRAP_LEASE_ID, proc, probe)
    assert held.status == "ACQUIRED"

    profiles = ApplicationProfiles(data_root=data_root, state_root=state_root)
    result = profiles.resolve(artist_name="No Duplicate", process=proc, probe=probe)
    assert result.state == "BOOTSTRAP_BUSY"
    assert result.blocking_lease == held.lease
    assert not (data_root / "profiles").exists()

    manager.release(
        BOOTSTRAP_LEASE_ID,
        process=proc,
        lease_nonce=held.lease.lease_nonce,
    )


@pytest.mark.parametrize(
    ("owner_status", "expected_state"),
    [("ALIVE", "BOOTSTRAP_BUSY"), ("UNKNOWN", "RECOVERY_REQUIRED")],
)
def test_foreign_bootstrap_ownership_fails_closed(
    tmp_path: Path,
    owner_status: str,
    expected_state: str,
) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    owner = process(205, "bootstrap-owner")
    challenger = process(206, "bootstrap-challenger")
    probe = Probe()
    manager = InstanceLeaseManager(state_root)
    held = manager.acquire(BOOTSTRAP_LEASE_ID, owner, probe)
    assert held.status == "ACQUIRED"
    probe.set(owner, owner_status)

    result = ApplicationProfiles(data_root=data_root, state_root=state_root).resolve(
        artist_name="No Steal",
        process=challenger,
        probe=probe,
    )
    assert result.state == expected_state
    assert not (data_root / "profiles").exists()

    probe.set(owner, "ALIVE")
    manager.release(
        BOOTSTRAP_LEASE_ID,
        process=owner,
        lease_nonce=held.lease.lease_nonce,
    )


def test_verified_dead_bootstrap_owner_can_be_replaced_and_receipted(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    dead = process(207, "dead-bootstrap")
    replacement = process(208, "replacement-bootstrap")
    probe = Probe()
    manager = InstanceLeaseManager(state_root)
    stale = manager.acquire(BOOTSTRAP_LEASE_ID, dead, probe)
    assert stale.status == "ACQUIRED"
    probe.set(dead, "DEAD")

    result = ApplicationProfiles(data_root=data_root, state_root=state_root).resolve(
        artist_name="Recovered First Artist",
        process=replacement,
        probe=probe,
    )
    assert result.state == "CREATED"
    assert result.previous_bootstrap_lease == stale.lease
    assert InstanceLeaseManager(state_root).inspect(BOOTSTRAP_LEASE_ID) is None


def test_profile_that_appears_after_bootstrap_lock_prevents_duplicate_creation(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    proc = process(209, "race-recheck")
    probe = Probe()

    class RaceProfiles(ApplicationProfiles):
        calls = 0

        def discover(self):  # type: ignore[override]
            self.calls += 1
            if self.calls == 2:
                create_profile(data_root, "Won Elsewhere")
            return super().discover()

    profiles = RaceProfiles(data_root=data_root, state_root=state_root)
    result = profiles.resolve(
        artist_name="Would Be Duplicate",
        process=proc,
        probe=probe,
    )
    assert result.state == "SELECTED_EXISTING"
    snapshot = profiles.discover()
    assert len(snapshot.profiles) == 1
    assert snapshot.profiles[0].artist_name == "Won Elsewhere"


def test_profile_service_public_surface_has_no_destructive_or_cloud_actions() -> None:
    public = {
        name
        for name in dir(ApplicationProfiles)
        if not name.startswith("_") and callable(getattr(ApplicationProfiles, name))
    }
    assert public == {"discover", "resolve"}
    assert not (
        {
            "delete",
            "merge",
            "rename",
            "upload",
            "sync",
            "login",
            "create_account",
        }
        & public
    )
