import json
from pathlib import Path

import pytest

from governance.model import (
    EVIDENCE_DIMENSIONS,
    evaluate_construction_program,
    validate_constitutional_migration,
    validate_construction_program,
)

ROOT = Path(__file__).resolve().parents[2]


def unproven_evidence():
    return {
        dim: {"state": "UNPROVEN", "evidence_refs": []}
        for dim in EVIDENCE_DIMENSIONS
    }


def base_package(work_id, path, dependency=()):
    return {
        "work_id": work_id,
        "parent_program_id": "PROGRAM-172",
        "state": "ACTIVE",
        "paths": [path],
        "requirement_ids": ["REQ-SCOPE-172"],
        "authority_class": "GOVERNANCE",
        "dependencies": list(dependency),
        "evidence": unproven_evidence(),
    }


def base_program(packages):
    return {
        "program_id": "PROGRAM-172",
        "state": "ACTIVE",
        "authority_source": "artist-authorized constitutional migration",
        "allowed_paths": ["governance/a.py", "governance/b.py"],
        "allowed_requirement_ids": ["REQ-SCOPE-172"],
        "allowed_authority_classes": ["GOVERNANCE"],
        "work_packages": packages,
    }


def test_dependency_cycles_are_rejected_before_work_selection():
    program = base_program([
        base_package("A", "governance/a.py", dependency=("B",)),
        base_package("B", "governance/b.py", dependency=("A",)),
    ])
    with pytest.raises(ValueError, match="dependency cycle"):
        validate_construction_program(program)


def test_global_incident_needs_evidence_before_it_can_freeze_disjoint_work():
    program = base_program([
        base_package("A", "governance/a.py"),
        base_package("B", "governance/b.py"),
    ])
    incident = {"incident_id": "INC-GLOBAL", "state": "OPEN", "global": True}
    with pytest.raises(ValueError, match="global incident requires blast-radius evidence"):
        evaluate_construction_program(program, [incident])

    incident["global_evidence_refs"] = ["runtime:shared-state-corruption"]
    result = evaluate_construction_program(program, [incident])
    assert set(result["blocked_work_packages"]) == {"A", "B"}


def test_candidate_satisfies_existing_constitutional_migration_guardrails():
    candidate = json.loads(
        (
            ROOT
            / "migration"
            / "constitutional"
            / "req-scope-172-construction-program-candidate.json"
        ).read_text()
    )
    assert validate_constitutional_migration(candidate)
    assert candidate["temporary_migration_authority"]["permanent_bypass"] is False
    assert candidate["temporary_migration_authority"]["expires_on_activation_or_rejection"] is True
