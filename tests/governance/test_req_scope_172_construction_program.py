import copy

import pytest

from governance.model import (
    EVIDENCE_DIMENSIONS,
    dogfood_tuple_is_eligible,
    evaluate_construction_program,
    personal_production_accepted,
    validate_construction_program,
)


def evidence(*proven):
    selected = set(proven)
    return {
        dim: {
            "state": "PROVEN" if dim in selected else "UNPROVEN",
            "evidence_refs": [f"proof:{dim.lower()}"] if dim in selected else [],
        }
        for dim in EVIDENCE_DIMENSIONS
    }


def package(
    work_id,
    path,
    requirement,
    *,
    state="ACTIVE",
    dependencies=(),
    authority_class="REVERSIBLE_WRITE",
    proven=(),
):
    return {
        "work_id": work_id,
        "parent_program_id": "PROGRAM-172",
        "state": state,
        "paths": [path],
        "requirement_ids": [requirement],
        "authority_class": authority_class,
        "dependencies": list(dependencies),
        "evidence": evidence(*proven),
    }


def program(packages):
    return {
        "program_id": "PROGRAM-172",
        "state": "ACTIVE",
        "authority_source": "artist-authorized REQ-SCOPE-172 constitutional migration",
        "allowed_paths": ["n0te/song.py", "n0te/daw.py", "n0te/evidence.py"],
        "allowed_requirement_ids": ["REQ-SCOPE-003", "REQ-SCOPE-010", "REQ-SCOPE-012"],
        "allowed_authority_classes": ["READ", "ADVISE", "REVERSIBLE_WRITE"],
        "work_packages": packages,
    }


def test_two_disjoint_sibling_packages_may_be_active_together():
    p = program([
        package("WP-SONG", "n0te/song.py", "REQ-SCOPE-003"),
        package("WP-DAW", "n0te/daw.py", "REQ-SCOPE-012"),
    ])
    result = evaluate_construction_program(p)
    assert result["eligible_work_package_ids"] == ["WP-SONG", "WP-DAW"]
    assert result["blocked_work_packages"] == {}


def test_path_requirement_and_authority_bounds_prevent_leakage():
    leaked_path = program([package("WP-X", "n0te/song.py", "REQ-SCOPE-003")])
    leaked_path["work_packages"][0]["paths"] = ["outside/secret.py"]
    with pytest.raises(ValueError, match="path authority leak"):
        validate_construction_program(leaked_path)

    leaked_req = program([package("WP-X", "n0te/song.py", "REQ-SCOPE-003")])
    leaked_req["work_packages"][0]["requirement_ids"] = ["REQ-SCOPE-999"]
    with pytest.raises(ValueError, match="requirement authority leak"):
        validate_construction_program(leaked_req)

    leaked_auth = program([package("WP-X", "n0te/song.py", "REQ-SCOPE-003")])
    leaked_auth["work_packages"][0]["authority_class"] = "CONSEQUENTIAL_EXTERNAL"
    with pytest.raises(ValueError, match="authority class leak"):
        validate_construction_program(leaked_auth)


def test_real_dependency_and_path_conflicts_block_only_affected_packages():
    p = program([
        package("WP-BASE", "n0te/evidence.py", "REQ-SCOPE-003", state="WAITING"),
        package(
            "WP-NEEDS-BASE",
            "n0te/song.py",
            "REQ-SCOPE-010",
            dependencies=("WP-BASE",),
        ),
        package("WP-A", "n0te/daw.py", "REQ-SCOPE-012"),
        package("WP-B", "n0te/daw.py", "REQ-SCOPE-010"),
    ])
    result = evaluate_construction_program(p)
    assert "dependency:WP-BASE" in result["blocked_work_packages"]["WP-NEEDS-BASE"]
    assert "path-conflict:WP-B" in result["blocked_work_packages"]["WP-A"]
    assert "path-conflict:WP-A" in result["blocked_work_packages"]["WP-B"]


def test_unrelated_incident_does_not_freeze_disjoint_package():
    p = program([
        package("WP-SONG", "n0te/song.py", "REQ-SCOPE-003"),
        package("WP-DAW", "n0te/daw.py", "REQ-SCOPE-012"),
    ])
    incidents = [
        {
            "incident_id": "INC-DAW",
            "state": "OPEN",
            "blast_radius": {"paths": ["n0te/daw.py"]},
        }
    ]
    result = evaluate_construction_program(p, incidents)
    assert result["eligible_work_package_ids"] == ["WP-SONG"]
    assert result["blocked_work_packages"]["WP-DAW"] == ["incident:INC-DAW"]
    assert "WP-SONG" not in result["blocked_work_packages"]


def test_implementation_and_acceptance_states_never_collapse():
    p = program([
        package(
            "WP-SONG",
            "n0te/song.py",
            "REQ-SCOPE-003",
            proven=("IMPLEMENTED", "INTEGRATED", "VERIFIED"),
        )
    ])
    states = evaluate_construction_program(p)["package_states"]["WP-SONG"]
    assert states == {"implementation": "PROVEN", "acceptance": "UNPROVEN"}


def test_proven_tuple_can_dogfood_while_other_tuple_remains_unverified():
    proven_tuple = evidence(
        "IMPLEMENTED",
        "INTEGRATED",
        "REACHABLE",
        "VERIFIED",
        "RECOVERABLE",
        "AUTHORITY_SAFE",
    )
    other_tuple = evidence("IMPLEMENTED")
    assert dogfood_tuple_is_eligible(proven_tuple)
    assert not dogfood_tuple_is_eligible(other_tuple)


def test_personal_production_acceptance_requires_all_declared_daw_os_and_reliability_obligations():
    accepted = evidence("CONSUMER_ACCEPTED")
    unaccepted = evidence()
    required_daws = {
        "Ableton Live",
        "FL Studio",
        "Logic Pro",
        "Pro Tools",
        "Studio One",
        "REAPER",
        "Generic Other",
    }
    required_os = {"macOS", "Windows", "Linux"}
    daw = {name: copy.deepcopy(accepted) for name in required_daws}
    os_family = {name: copy.deepcopy(accepted) for name in required_os}
    reliability = [copy.deepcopy(accepted), copy.deepcopy(accepted)]

    assert personal_production_accepted(
        daw_path_evidence=daw,
        os_family_evidence=os_family,
        reliability_evidence=reliability,
        required_daw_paths=required_daws,
        required_os_families=required_os,
    )

    daw["Logic Pro"] = unaccepted
    assert not personal_production_accepted(
        daw_path_evidence=daw,
        os_family_evidence=os_family,
        reliability_evidence=reliability,
        required_daw_paths=required_daws,
        required_os_families=required_os,
    )
