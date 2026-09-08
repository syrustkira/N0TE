from governance.ci_plan import FULL, GOVERNANCE, TARGETED, classify


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
