from __future__ import annotations

import pytest

from n0te.migration import (
    MigrationPlan,
    MigrationPlanError,
    MigrationStep,
    MigrationValidationError,
)
from n0te.schema_program import (
    migration_steps_fingerprint,
    select_migration_chain,
)
from n0te.update_migration import UpdateMigrationBinding


def edge(source: int, target: int) -> MigrationStep:
    return MigrationStep(
        source,
        target,
        f"schema {source} to {target}",
        (f"CREATE TABLE marker_{target}(value TEXT)",),
    )


def plan(source: int, target: int, steps) -> MigrationPlan:
    return MigrationPlan(
        migration_id="mig_" + "1" * 32,
        profile_id="prf_" + "2" * 32,
        source_version=source,
        target_version=target,
        source_identity_fingerprint="a" * 64,
        source_history_fingerprint="b" * 64,
        steps=tuple(steps),
    )


def binding(program, selected_plan: MigrationPlan) -> UpdateMigrationBinding:
    return UpdateMigrationBinding(
        update_id="upd_" + "3" * 32,
        update_plan_fingerprint="c" * 64,
        manifest_fingerprint="d" * 64,
        target_release_id="release-v3",
        rollback_snapshot_sha256="e" * 64,
        rollback_snapshot_size_bytes=4096,
        schema_program=tuple(program),
        migration_plan=selected_plan,
    )


def test_release_program_can_serve_multiple_source_schema_versions() -> None:
    one_two = edge(1, 2)
    two_three = edge(2, 3)
    program = (one_two, two_three)

    assert select_migration_chain(
        source_version=1, target_version=3, program=program
    ) == program
    assert select_migration_chain(
        source_version=2, target_version=3, program=program
    ) == (two_three,)
    assert select_migration_chain(
        source_version=3, target_version=3, program=program
    ) == ()


def test_profile_subset_does_not_change_authenticated_release_program_fingerprint() -> None:
    program = (edge(1, 2), edge(2, 3))
    authenticated = migration_steps_fingerprint(program)
    selected = select_migration_chain(
        source_version=2, target_version=3, program=program
    )

    assert migration_steps_fingerprint(program) == authenticated
    assert migration_steps_fingerprint(selected) != authenticated


def test_durable_binding_keeps_full_program_and_exact_selected_subset() -> None:
    one_two = edge(1, 2)
    two_three = edge(2, 3)
    program = (one_two, two_three)

    from_two = binding(program, plan(2, 3, (two_three,)))
    already_current = binding(program, plan(3, 3, ()))

    assert from_two.schema_program == program
    assert from_two.migration_plan.steps == (two_three,)
    assert already_current.schema_program == program
    assert already_current.migration_plan.steps == ()


def test_binding_rejects_selected_steps_not_derived_from_authenticated_program() -> None:
    program = (edge(1, 2), edge(2, 3))
    wrong = MigrationStep(
        2,
        3,
        "different schema 2 to 3",
        ("CREATE TABLE wrong_marker(value TEXT)",),
    )
    with pytest.raises(MigrationValidationError):
        binding(program, plan(2, 3, (wrong,)))


def test_program_must_be_ordered_contiguous_and_terminate_at_release_target() -> None:
    one_two = edge(1, 2)
    two_three = edge(2, 3)
    three_four = edge(3, 4)

    with pytest.raises(MigrationPlanError, match="ordered and contiguous"):
        migration_steps_fingerprint((two_three, one_two))
    with pytest.raises(MigrationPlanError, match="terminate at target_version"):
        select_migration_chain(
            source_version=1,
            target_version=3,
            program=(one_two, two_three, three_four),
        )
    with pytest.raises(MigrationPlanError, match="no contiguous migration path"):
        select_migration_chain(
            source_version=1,
            target_version=3,
            program=(two_three,),
        )
