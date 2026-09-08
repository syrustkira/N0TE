from __future__ import annotations

from pathlib import Path

from n0te.instance import InstanceLeaseManager, ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.migration import MigrationStep
from n0te.platforms import PlatformEnvironment
from n0te.recovery import RecoveryManager
from n0te.update_migration import UpdateBoundSchemaMigrator


class DeadProbe:
    def status(self, process: ProcessIdentity) -> str:
        return "DEAD"


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=8101,
        start_token="stale-update-schema-owner",
    )


def test_read_only_update_schema_plan_does_not_treat_persisted_lease_as_liveness(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    memory = HeadquartersMemory.create(data_root, "Stale Lease Artist")
    profile_id = memory.store.profile_id
    memory.store.create_song("Still Here")
    snapshot = RecoveryManager(memory.store).create_snapshot()
    memory.close()

    stale_owner = process()
    leases = InstanceLeaseManager(state_root)
    assert leases.acquire(profile_id, stale_owner, DeadProbe()).status == "ACQUIRED"

    migrator = UpdateBoundSchemaMigrator(
        data_root,
        state_root,
        rollback_snapshot_sha256=snapshot.sha256,
        rollback_snapshot_size_bytes=snapshot.size_bytes,
    )
    plan = migrator.prepare_read_only(
        profile_id=profile_id,
        target_version=2,
        steps=(
            MigrationStep(
                1,
                2,
                "v2 marker",
                ("CREATE TABLE v2_marker(value TEXT)",),
            ),
        ),
    )

    assert plan.source_version == 1
    assert plan.target_version == 2
    assert leases.inspect(profile_id) is not None
