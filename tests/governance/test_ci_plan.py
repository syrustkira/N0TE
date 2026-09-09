import pytest

from governance.ci_plan import (
    FULL,
    GOVERNANCE,
    TARGETED,
    ChangeScopeError,
    classify,
    validate_change_scope,
)


def test_product_code_requires_full_suite():
    plan = classify(
        ["n0te/consumer_shell.py", "tests/acceptance/test_produce.py"],
        event_name="pull_request",
    )
    assert plan.tier == FULL
    assert plan.pytest_targets == ("tests",)


def test_mechanical_closeout_uses_governance_suite_only():
    plan = classify(
        [
            "governance/current_state.json",
            "governance/programs/PROGRAM-PERSONAL-PRODUCTION-002.json",
            "governance/evidence/REQ-SCOPE-090.json",
            "tests/governance/test_first_real_construction_program.py",
        ],
        event_name="pull_request",
    )
    assert plan.tier == GOVERNANCE
    assert plan.pytest_targets == ("tests/governance",)


def test_tests_only_use_bounded_target_roots_plus_governance():
    plan = classify(
        [
            "tests/ops/test_musical_transactions.py",
            "tests/acceptance/test_req_scope_079_090_produce_reachability.py",
        ],
        event_name="pull_request",
    )
    assert plan.tier == TARGETED
    assert plan.pytest_targets == (
        "tests/acceptance",
        "tests/governance",
        "tests/ops",
    )


def test_governance_core_and_workflow_changes_force_full_suite():
    for path in (
        "governance/model.py",
        "governance/check_governance.py",
        "governance/ci_plan.py",
        "governance/closeout.py",
        ".github/workflows/governance.yml",
    ):
        plan = classify([path], event_name="pull_request")
        assert plan.tier == FULL
        assert plan.pytest_targets == ("tests",)


def test_unknown_mixed_change_fails_conservative_to_full():
    plan = classify(["scripts/new_runtime_tool.py"], event_name="pull_request")
    assert plan.tier == FULL


def _state(program_ref, evidence_ref="governance/evidence/REQ-SCOPE-172.json"):
    return {
        "active_construction_program": program_ref,
        "current_requirement_evidence": {
            "REQ-SCOPE-172": evidence_ref,
        },
    }


def _terminal_state(
    program_ref,
    *,
    program_id="PROGRAM-A",
    evidence_ref="governance/evidence/REQ-SCOPE-172-terminal.json",
    completion_ref="governance/evidence/PROGRAM-A-completion.json",
):
    return {
        "current_requirement_evidence": {
            "REQ-SCOPE-172": evidence_ref,
        },
        "last_completed_construction_program": {
            "program_id": program_id,
            "program_path": program_ref,
            "completion_receipt": completion_ref,
        },
    }


def _program(program_id, active_paths=(), complete_paths=()):
    packages = []
    if active_paths:
        packages.append(
            {
                "work_id": f"{program_id}-ACTIVE",
                "state": "ACTIVE",
                "paths": list(active_paths),
            }
        )
    if complete_paths:
        packages.append(
            {
                "work_id": f"{program_id}-COMPLETE",
                "state": "COMPLETE",
                "paths": list(complete_paths),
            }
        )
    return {
        "program_id": program_id,
        "state": "ACTIVE" if active_paths else "COMPLETE",
        "work_packages": packages,
    }


def _completed_program(
    program_id,
    paths,
    *,
    completion_ref="governance/evidence/PROGRAM-A-completion.json",
):
    program = _program(program_id, complete_paths=paths)
    program["completion_receipt"] = completion_ref
    return program


def test_change_scope_rejects_substantive_path_outside_active_package():
    ref = "governance/programs/PROGRAM-A.json"
    state = _state(ref)
    program = _program("PROGRAM-A", active_paths=("n0te/allowed.py",))
    with pytest.raises(ChangeScopeError, match="exceed selected construction authority"):
        validate_change_scope(
            ["n0te/allowed.py", "governance/trusted_context.py"],
            base_state=state,
            base_program=program,
            head_state=state,
            head_program=program,
        )


def test_change_scope_allows_current_state_selected_program_and_current_evidence_records():
    ref = "governance/programs/PROGRAM-A.json"
    state = _state(ref)
    program = _program("PROGRAM-A", active_paths=("n0te/allowed.py",))
    authorized = validate_change_scope(
        [
            "n0te/allowed.py",
            "governance/current_state.json",
            ref,
            "governance/evidence/REQ-SCOPE-172.json",
        ],
        base_state=state,
        base_program=program,
        head_state=state,
        head_program=program,
    )
    assert authorized == ("n0te/allowed.py",)


def test_same_program_closeout_accepts_path_active_in_base_even_if_complete_in_head():
    ref = "governance/programs/PROGRAM-A.json"
    state = _state(ref)
    base_program = _program("PROGRAM-A", active_paths=("n0te/fix.py",))
    head_program = _program("PROGRAM-A", complete_paths=("n0te/fix.py",))
    authorized = validate_change_scope(
        ["n0te/fix.py", ref],
        base_state=state,
        base_program=base_program,
        head_state=state,
        head_program=head_program,
    )
    assert authorized == ("n0te/fix.py",)


def test_program_transition_requires_repository_owner_identity():
    base_ref = "governance/programs/PROGRAM-OLD.json"
    head_ref = "governance/programs/PROGRAM-NEW.json"
    base_state = _state(base_ref)
    head_state = _state(head_ref)
    base_program = _program("PROGRAM-OLD", active_paths=("n0te/old.py",))
    head_program = _program("PROGRAM-NEW", active_paths=("governance/ci_plan.py",))

    with pytest.raises(
        ChangeScopeError,
        match="repository-owner identity evidence",
    ):
        validate_change_scope(
            [
                "governance/current_state.json",
                head_ref,
                "governance/ci_plan.py",
            ],
            base_state=base_state,
            base_program=base_program,
            head_state=head_state,
            head_program=head_program,
        )

    with pytest.raises(
        ChangeScopeError,
        match="repository-owner authority",
    ):
        validate_change_scope(
            [
                "governance/current_state.json",
                head_ref,
                "governance/ci_plan.py",
            ],
            base_state=base_state,
            base_program=base_program,
            head_state=head_state,
            head_program=head_program,
            actor="automation-bot",
            repository_owner="syrustkira",
        )


def test_program_transition_authorizes_only_new_active_program_substantive_paths_for_owner():
    base_ref = "governance/programs/PROGRAM-OLD.json"
    head_ref = "governance/programs/PROGRAM-NEW.json"
    base_state = _state(base_ref)
    head_state = _state(head_ref)
    base_program = _program("PROGRAM-OLD", active_paths=("n0te/old.py",))
    head_program = _program("PROGRAM-NEW", active_paths=("governance/ci_plan.py",))

    validate_change_scope(
        [
            "governance/current_state.json",
            head_ref,
            "governance/ci_plan.py",
        ],
        base_state=base_state,
        base_program=base_program,
        head_state=head_state,
        head_program=head_program,
        actor="SyRusTKira",
        repository_owner="syrustkira",
    )

    with pytest.raises(ChangeScopeError, match="n0te/old.py"):
        validate_change_scope(
            [
                "governance/current_state.json",
                head_ref,
                "n0te/old.py",
            ],
            base_state=base_state,
            base_program=base_program,
            head_state=head_state,
            head_program=head_program,
            actor="syrustkira",
            repository_owner="syrustkira",
        )


def test_terminal_completion_accepts_exact_closeout_control_plane_for_owner():
    ref = "governance/programs/PROGRAM-A.json"
    base_state = _state(ref)
    head_state = _terminal_state(ref)
    base_program = _program("PROGRAM-A", active_paths=("n0te/final.py",))
    head_program = _completed_program("PROGRAM-A", ("n0te/final.py",))

    authorized = validate_change_scope(
        [
            "governance/current_state.json",
            ref,
            "governance/evidence/REQ-SCOPE-172-terminal.json",
            "governance/evidence/PROGRAM-A-completion.json",
        ],
        base_state=base_state,
        base_program=base_program,
        head_state=head_state,
        head_program=head_program,
        actor="syrustkira",
        repository_owner="syrustkira",
        event_name="pull_request",
    )
    assert authorized == ("n0te/final.py",)


def test_terminal_completion_accepts_mechanical_closeout_bot_only_on_push():
    ref = "governance/programs/PROGRAM-A.json"
    base_state = _state(ref)
    head_state = _terminal_state(ref)
    base_program = _program("PROGRAM-A", active_paths=("n0te/final.py",))
    head_program = _completed_program("PROGRAM-A", ("n0te/final.py",))
    paths = [
        "governance/current_state.json",
        ref,
        "governance/evidence/REQ-SCOPE-172-terminal.json",
        "governance/evidence/PROGRAM-A-completion.json",
    ]

    validate_change_scope(
        paths,
        base_state=base_state,
        base_program=base_program,
        head_state=head_state,
        head_program=head_program,
        actor="github-actions[bot]",
        repository_owner="syrustkira",
        event_name="push",
    )

    with pytest.raises(
        ChangeScopeError,
        match="mechanical-closeout authority",
    ):
        validate_change_scope(
            paths,
            base_state=base_state,
            base_program=base_program,
            head_state=head_state,
            head_program=head_program,
            actor="github-actions[bot]",
            repository_owner="syrustkira",
            event_name="pull_request",
        )


def test_terminal_completion_rejects_incomplete_or_mismatched_program_state():
    ref = "governance/programs/PROGRAM-A.json"
    base_state = _state(ref)
    head_state = _terminal_state(ref)
    base_program = _program("PROGRAM-A", active_paths=("n0te/final.py",))

    incomplete = _program("PROGRAM-A", active_paths=("n0te/final.py",))
    incomplete["completion_receipt"] = "governance/evidence/PROGRAM-A-completion.json"
    with pytest.raises(ChangeScopeError, match="COMPLETE program state"):
        validate_change_scope(
            ["governance/current_state.json", ref],
            base_state=base_state,
            base_program=base_program,
            head_state=head_state,
            head_program=incomplete,
            actor="syrustkira",
            repository_owner="syrustkira",
            event_name="pull_request",
        )

    mismatch = _completed_program("PROGRAM-B", ("n0te/final.py",))
    with pytest.raises(ChangeScopeError, match="program id mismatch"):
        validate_change_scope(
            ["governance/current_state.json", ref],
            base_state=base_state,
            base_program=base_program,
            head_state=head_state,
            head_program=mismatch,
            actor="syrustkira",
            repository_owner="syrustkira",
            event_name="pull_request",
        )
