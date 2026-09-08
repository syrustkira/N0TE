from __future__ import annotations

import pytest

from n0te.career_roles import RoleEvidence, assess_role
from n0te.lineage import ValidationError


def _manager_skills() -> dict[str, str]:
    return {
        "career_planning": "PRACTICED",
        "stakeholder_communication": "INDEPENDENT",
        "release_coordination": "APPLIED",
        "rights_business_basics": "APPLIED",
        "opportunity_tracking": "APPLIED",
        "business_coordination": "INDEPENDENT",
    }


def _base_manager_evidence() -> tuple[RoleEvidence, ...]:
    return (
        RoleEvidence(
            id="plan-1",
            role_ids=("MANAGER",),
            kind="PLANNING_ARTIFACT",
            source_kind="OBSERVED",
            source_ref="plan:1",
        ),
        RoleEvidence(
            id="resp-1",
            role_ids=("MANAGER",),
            kind="RESPONSIBILITY",
            source_kind="OBSERVED",
            source_ref="delivery:1",
        ),
        RoleEvidence(
            id="collab-1",
            role_ids=("MANAGER",),
            kind="COLLABORATOR_OUTCOME",
            source_kind="VERIFIED_EXTERNAL",
            source_ref="artist:outcome:1",
        ),
        RoleEvidence(
            id="repeat-1",
            role_ids=("MANAGER",),
            kind="REPEAT_DELIVERY",
            source_kind="OBSERVED",
            source_ref="delivery:repeat",
        ),
    )


def test_duplicate_evidence_id_is_rejected_instead_of_double_counted() -> None:
    evidence = _base_manager_evidence()

    with pytest.raises(ValidationError, match="role evidence IDs must be unique"):
        assess_role(
            "MANAGER",
            skill_states=_manager_skills(),
            evidence=(*evidence, evidence[1]),
        )


def test_two_ids_for_same_underlying_source_do_not_manufacture_two_responsibilities() -> None:
    evidence = _base_manager_evidence()
    duplicate_source = RoleEvidence(
        id="resp-alias",
        role_ids=("MANAGER",),
        kind="RESPONSIBILITY",
        source_kind="VERIFIED_EXTERNAL",
        source_ref="delivery:1",
    )

    assessment = assess_role(
        "MANAGER",
        skill_states=_manager_skills(),
        evidence=(*evidence, duplicate_source),
    )

    assert assessment.current_stage_id == "MANAGER-COORDINATING-OUTCOMES"
    assert assessment.next_stage_id == "MANAGER-OWNING-REPEATABLE-OPERATIONS"
    assert tuple(item.kind for item in assessment.missing_evidence) == (
        "RESPONSIBILITY",
    )
