from governance.check_governance import evaluate_current_construction_program


def test_current_program_exposes_two_real_disjoint_sibling_packages():
    current = evaluate_current_construction_program()
    assert current is not None
    program, result = current

    assert program["program_id"] == "PROGRAM-PERSONAL-PRODUCTION-001"
    assert program["state"] == "ACTIVE"
    assert [package["state"] for package in program["work_packages"]] == [
        "ACTIVE",
        "ACTIVE",
    ]
    assert result["eligible_work_package_ids"] == [
        "WP-003-HQ-REACHABILITY",
        "WP-010-COMPARE-DECIDE-REACHABILITY",
    ]
    assert result["blocked_work_packages"] == {}

    paths = [set(package["paths"]) for package in program["work_packages"]]
    assert paths[0].isdisjoint(paths[1])
    assert {requirement for package in program["work_packages"] for requirement in package["requirement_ids"]} == {
        "REQ-SCOPE-003",
        "REQ-SCOPE-010",
    }


def test_current_program_does_not_preclaim_acceptance_or_value():
    current = evaluate_current_construction_program()
    assert current is not None
    program, result = current
    for package in program["work_packages"]:
        states = result["package_states"][package["work_id"]]
        assert states["implementation"] == "UNPROVEN"
        assert states["acceptance"] == "UNPROVEN"
        assert package["evidence"]["VALUE_EVIDENCED"]["state"] == "UNPROVEN"
