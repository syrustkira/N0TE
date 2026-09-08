from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from n0te.instance import InstanceLeaseManager, ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.migration import (
    APP_SCHEMA_KEY,
    ApplicationSchemaMigrator,
    MigrationPlanError,
    MigrationStep,
    MigrationValidationError,
    SchemaMigrationError,
)
from n0te.platforms import PlatformEnvironment
from n0te.recovery import RecoveryManager, SnapshotValidationError


class Probe:
    def __init__(self, default: str = "DEAD"):
        self.default = default
        self.statuses: dict[str, str] = {}

    def set(self, process: ProcessIdentity, status: str) -> None:
        self.statuses[process.fingerprint] = status

    def status(self, process: ProcessIdentity) -> str:
        return self.statuses.get(process.fingerprint, self.default)


def process(pid: int, token: str) -> ProcessIdentity:
    platform = PlatformEnvironment.from_runtime_labels("Linux", "x86_64")
    return ProcessIdentity.from_start_token(platform, pid=pid, start_token=token)


def seeded_profile(tmp_path: Path) -> tuple[Path, Path, str, str]:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    memory = HeadquartersMemory.create(data_root, "TellMeN0TE")
    profile_id = memory.store.profile_id
    song = memory.store.create_song("Migration Song")
    asset = memory.store.attach_asset(
        song.id,
        name="mix.wav",
        sha256="a" * 64,
        source_uri="file:///mix.wav",
    )
    memory.store.create_version(song.id, label="v1", asset_ids=(asset.id,))
    memory.close()
    return data_root, state_root, profile_id, song.id


def table_exists(db_path: Path, table_name: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone() is not None
    finally:
        conn.close()


def app_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (APP_SCHEMA_KEY,)).fetchone()
        return 1 if row is None else int(row[0])
    finally:
        conn.close()


def db_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def step_v2(*statements: str) -> MigrationStep:
    return MigrationStep(
        1,
        2,
        "add bounded v2 application schema",
        tuple(statements) or ("CREATE TABLE app_v2_marker(value TEXT NOT NULL)",),
    )


def test_current_version_plan_is_explicit_no_change_and_writes_no_schema_state(tmp_path: Path) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    plan = migrator.prepare(profile_id=profile_id, target_version=1, steps=())
    db_path = data_root / "profiles" / profile_id / "lineage.sqlite3"

    assert app_version(db_path) == 1
    assert not table_exists(db_path, "application_schema_migrations")
    result = migrator.migrate(plan, maintenance_process=process(9001, "migration"), probe=Probe())
    assert result.state == "NO_CHANGE"
    assert result.installed_version == 1
    assert result.rollback_snapshot_sha256 is None
    assert migrator.history(profile_id) == ()
    assert not table_exists(db_path, "application_schema_migrations")


def test_missing_duplicate_out_of_order_and_downgrade_chains_fail_during_prepare(tmp_path: Path) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    s12 = step_v2()
    s23 = MigrationStep(2, 3, "v3", ("CREATE TABLE app_v3_marker(value TEXT)",))

    with pytest.raises(MigrationPlanError):
        migrator.prepare(profile_id=profile_id, target_version=3, steps=(s12,))
    with pytest.raises(MigrationPlanError):
        migrator.prepare(profile_id=profile_id, target_version=3, steps=(s12, s12, s23))
    with pytest.raises(MigrationPlanError):
        migrator.prepare(profile_id=profile_id, target_version=3, steps=(s23, s12))
    with pytest.raises(MigrationPlanError):
        migrator.prepare(profile_id=profile_id, target_version=0, steps=())


def test_destructive_or_transaction_control_sql_is_not_an_app01e_step() -> None:
    for statement in (
        "DROP TABLE songs",
        "DELETE FROM artists",
        "ATTACH DATABASE '/tmp/x' AS other",
        "PRAGMA writable_schema=ON",
        "COMMIT",
        "SAVEPOINT escape",
    ):
        with pytest.raises(MigrationPlanError):
            step_v2(statement)


def test_prepare_refuses_any_existing_runtime_lease_instead_of_trusting_stopped_string(tmp_path: Path) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    owner = process(1001, "runtime")
    probe = Probe("ALIVE")
    probe.set(owner, "ALIVE")
    assert InstanceLeaseManager(state_root).acquire(profile_id, owner, probe).status == "ACQUIRED"

    with pytest.raises(MigrationPlanError, match="runtime ownership"):
        ApplicationSchemaMigrator(data_root, state_root).prepare(
            profile_id=profile_id,
            target_version=2,
            steps=(step_v2(),),
        )


def test_runtime_race_after_prepare_blocks_migration_before_staged_install(tmp_path: Path) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    plan = migrator.prepare(profile_id=profile_id, target_version=2, steps=(step_v2(),))
    runtime = process(1002, "runtime-race")
    probe = Probe("ALIVE")
    probe.set(runtime, "ALIVE")
    assert InstanceLeaseManager(state_root).acquire(profile_id, runtime, probe).status == "ACQUIRED"

    with pytest.raises(MigrationPlanError, match="not safely stopped"):
        migrator.migrate(plan, maintenance_process=process(9002, "migration"), probe=probe)
    assert app_version(data_root / "profiles" / profile_id / "lineage.sqlite3") == 1


def test_verified_stale_runtime_lease_can_be_replaced_by_bounded_maintenance_owner(tmp_path: Path) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    plan = migrator.prepare(profile_id=profile_id, target_version=2, steps=(step_v2(),))
    stale = process(1003, "stale-runtime")
    dead_probe = Probe("DEAD")
    assert InstanceLeaseManager(state_root).acquire(profile_id, stale, dead_probe).status == "ACQUIRED"

    result = migrator.migrate(
        plan,
        maintenance_process=process(9003, "migration"),
        probe=dead_probe,
    )
    assert result.state == "SUCCEEDED"
    assert result.rollback_snapshot_sha256 is not None
    assert InstanceLeaseManager(state_root).inspect(profile_id) is None


def test_failed_staged_sql_leaves_live_database_byte_for_byte_untouched(tmp_path: Path) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    db_path = data_root / "profiles" / profile_id / "lineage.sqlite3"
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    plan = migrator.prepare(
        profile_id=profile_id,
        target_version=2,
        steps=(
            step_v2(
                "CREATE TABLE app_v2_marker(value TEXT NOT NULL)",
                "INSERT INTO table_that_does_not_exist(value) VALUES('boom')",
            ),
        ),
    )
    before = db_sha(db_path)

    with pytest.raises(SchemaMigrationError, match="before install"):
        migrator.migrate(plan, maintenance_process=process(9004, "migration"), probe=Probe())
    assert app_version(db_path) == 1
    assert not table_exists(db_path, "app_v2_marker")
    assert db_sha(db_path) == before


def test_identity_repointing_in_stage_is_detected_before_live_install(tmp_path: Path) -> None:
    data_root, state_root, profile_id, song_id = seeded_profile(tmp_path)
    db_path = data_root / "profiles" / profile_id / "lineage.sqlite3"
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    plan = migrator.prepare(
        profile_id=profile_id,
        target_version=2,
        steps=(step_v2("UPDATE metadata SET value='' WHERE key='active_song_id'"),),
    )
    before = db_sha(db_path)
    assert song_id

    with pytest.raises(MigrationValidationError, match="changed canonical Artist/Song identity"):
        migrator.migrate(plan, maintenance_process=process(9005, "migration"), probe=Probe())
    assert app_version(db_path) == 1
    assert db_sha(db_path) == before


def test_successful_migration_installs_only_validated_candidate_and_exposes_history(tmp_path: Path) -> None:
    data_root, state_root, profile_id, song_id = seeded_profile(tmp_path)
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    step = step_v2("CREATE TABLE app_v2_marker(value TEXT NOT NULL DEFAULT 'ready')")
    plan = migrator.prepare(profile_id=profile_id, target_version=2, steps=(step,))

    result = migrator.migrate(plan, maintenance_process=process(9006, "migration"), probe=Probe())
    db_path = data_root / "profiles" / profile_id / "lineage.sqlite3"
    assert result.state == "SUCCEEDED"
    assert result.installed_version == 2
    assert result.rollback_snapshot_sha256 is not None
    assert app_version(db_path) == 2
    assert table_exists(db_path, "app_v2_marker")
    with HeadquartersMemory.open(data_root, profile_id) as reopened:
        assert reopened.store.active_song() is not None
        assert reopened.store.active_song().id == song_id

    history = migrator.history(profile_id)
    assert len(history) == 1
    assert history[0].migration_id == plan.migration_id
    assert history[0].from_version == 1
    assert history[0].to_version == 2
    assert history[0].step_fingerprint == step.fingerprint
    assert history[0].description == step.description


def test_identity_change_after_prepare_invalidates_plan_before_maintenance_snapshot(tmp_path: Path) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    plan = migrator.prepare(profile_id=profile_id, target_version=2, steps=(step_v2(),))
    with HeadquartersMemory.open(data_root, profile_id) as memory:
        memory.store.create_song("changed after plan")

    with pytest.raises(MigrationValidationError, match="identity changed"):
        migrator.migrate(plan, maintenance_process=process(9007, "migration"), probe=Probe())


def test_post_install_reopen_failure_restores_exact_maintenance_owned_snapshot(tmp_path: Path, monkeypatch) -> None:
    data_root, state_root, profile_id, song_id = seeded_profile(tmp_path)
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    plan = migrator.prepare(profile_id=profile_id, target_version=2, steps=(step_v2(),))

    real_snapshot = RecoveryManager.create_snapshot
    captured: dict[str, str] = {}

    def capture_snapshot(self):
        snapshot = real_snapshot(self)
        captured["sha256"] = snapshot.sha256
        return snapshot

    def fail_open(cls, root, requested_profile):
        raise RuntimeError("simulated post-install reopen failure")

    monkeypatch.setattr(RecoveryManager, "create_snapshot", capture_snapshot)
    monkeypatch.setattr(HeadquartersMemory, "open", classmethod(fail_open))
    result = migrator.migrate(plan, maintenance_process=process(9008, "migration"), probe=Probe())
    db_path = data_root / "profiles" / profile_id / "lineage.sqlite3"

    assert result.state == "ROLLED_BACK"
    assert result.installed_version == 1
    assert result.rollback_snapshot_sha256 == captured["sha256"]
    assert db_sha(db_path) == captured["sha256"]
    assert app_version(db_path) == 1
    assert not table_exists(db_path, "app_v2_marker")
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT value FROM metadata WHERE key='active_song_id'").fetchone()[0] == song_id
    finally:
        conn.close()


def test_restore_failure_is_explicit_recovery_required_not_false_rollback(tmp_path: Path, monkeypatch) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    plan = migrator.prepare(profile_id=profile_id, target_version=2, steps=(step_v2(),))

    def fail_open(cls, root, requested_profile):
        raise RuntimeError("simulated installed-state failure")

    def fail_restore(cls, root, requested_profile, *, expected_sha256):
        raise SnapshotValidationError("simulated restore failure")

    monkeypatch.setattr(HeadquartersMemory, "open", classmethod(fail_open))
    monkeypatch.setattr(RecoveryManager, "restore_snapshot", classmethod(fail_restore))
    result = migrator.migrate(plan, maintenance_process=process(9009, "migration"), probe=Probe())

    assert result.state == "RECOVERY_REQUIRED"
    assert result.installed_version is None
    assert result.rollback_snapshot_sha256 is not None
    assert "restore also failed" in result.evidence


def test_step_cannot_rewrite_prior_migration_history(tmp_path: Path) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    first = migrator.prepare(profile_id=profile_id, target_version=2, steps=(step_v2(),))
    assert migrator.migrate(first, maintenance_process=process(9010, "migration-one"), probe=Probe()).state == "SUCCEEDED"

    rewrite = MigrationStep(
        2,
        3,
        "attempt to rewrite prior history",
        (
            "UPDATE application_schema_migrations SET description='rewritten' WHERE sequence=1",
            "CREATE TABLE app_v3_marker(value TEXT)",
        ),
    )
    second = migrator.prepare(profile_id=profile_id, target_version=3, steps=(rewrite,))
    db_path = data_root / "profiles" / profile_id / "lineage.sqlite3"
    before = db_sha(db_path)

    with pytest.raises(MigrationValidationError, match="rewrote prior migration history"):
        migrator.migrate(second, maintenance_process=process(9011, "migration-two"), probe=Probe())
    assert app_version(db_path) == 2
    assert db_sha(db_path) == before
    assert migrator.history(profile_id)[0].description == "add bounded v2 application schema"


def test_plan_sql_cannot_modify_live_database_before_candidate_validation(tmp_path: Path) -> None:
    data_root, state_root, profile_id, _ = seeded_profile(tmp_path)
    db_path = data_root / "profiles" / profile_id / "lineage.sqlite3"
    migrator = ApplicationSchemaMigrator(data_root, state_root)
    plan = migrator.prepare(
        profile_id=profile_id,
        target_version=2,
        steps=(step_v2("UPDATE artists SET display_name='silently rewritten'"),),
    )

    with pytest.raises(MigrationValidationError):
        migrator.migrate(plan, maintenance_process=process(9012, "migration"), probe=Probe())
    with HeadquartersMemory.open(data_root, profile_id) as memory:
        assert memory.store.artist().display_name == "TellMeN0TE"
    assert app_version(db_path) == 1
