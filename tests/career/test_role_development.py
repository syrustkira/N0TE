from __future__ import annotations

import pytest

from n0te.career_roles import (
    RoleEvidence,
    assess_role,
    assess_transition,
    get_role_definition,
)
from n0te.lineage import ValidationError
from n0te.skills import SkillState


def skill(skill_id: str, level: str) -> SkillState:
    return SkillState(skill_id=skill_id, level=level, latest_assessment=None)


def test_artist_declaration_remains_visible_but_does_not_certify_role_stage() -> None:
    skills = {
        "artistic_voice": skill("artistic_voice", "PRACTICED"),
        "song_or_performance_finishing": skill(
            "song_or_performance_finishing", "PRACTICED"
        ),
    }
    declaration = RoleEvidence(
        id="roleev_declared_work",
        role_ids=("ARTIST",),
        kind="WORK_SAMPLE",
        source_kind="ARTIST_DECLARED",
        source_ref="artist:statement",
    )

    declared = assess_role("ARTIST", skill_states=skills, evidence=(declaration,))

    assert declared.current_stage_id is None
    assert declared.next_stage_id == "ARTIST-DEVELOPING-WORK"
    assert declared.missing_skills == ()
    assert tuple(item.kind for item in declared.missing_evidence) == ("WORK_SAMPLE",)
    assert declared.verified_evidence_ids == ()
    assert declared.declared_or_inferred_evidence_ids == (declaration.id,)

    observed = RoleEvidence(
        id="roleev_observed_work",
        role_ids=("ARTIST",),
        kind="WORK_SAMPLE",
        source_kind="OBSERVED",
        source_ref="song:version:artifact",
    )
    verified = assess_role("ARTIST", skill_states=skills, evidence=(declaration, observed))

    assert verified.current_stage_id == "ARTIST-DEVELOPING-WORK"
    assert verified.next_stage_id == "ARTIST-DELIVERING-WORK"
    assert verified.verified_evidence_ids == (observed.id,)
    assert verified.declared_or_inferred_evidence_ids == (declaration.id,)


def test_role_stage_progression_is_contiguous_and_role_specific() -> None:
    skills = {
        "arrangement": "APPLIED",
        "sound_selection": "PRACTICED",
        "daw_operation": "PRACTICED",
        "production_decision_making": "APPLIED",
        "collaborator_communication": "APPLIED",
    }
    later_credit = RoleEvidence(
        id="roleev_credit",
        role_ids=("PRODUCER",),
        kind="CREDIT",
        source_kind="VERIFIED_EXTERNAL",
        source_ref="credit:record-1",
    )
    collaborator = RoleEvidence(
        id="roleev_collab",
        role_ids=("PRODUCER",),
        kind="COLLABORATOR_OUTCOME",
        source_kind="OBSERVED",
        source_ref="session:delivery-1",
    )

    blocked = assess_role(
        "PRODUCER",
        skill_states=skills,
        evidence=(later_credit, collaborator),
    )
    assert blocked.current_stage_id is None
    assert blocked.next_stage_id == "PRODUCER-BUILDING-RECORDS"
    assert tuple(item.kind for item in blocked.missing_evidence) == ("WORK_SAMPLE",)

    work_sample = RoleEvidence(
        id="roleev_work",
        role_ids=("PRODUCER",),
        kind="WORK_SAMPLE",
        source_kind="OBSERVED",
        source_ref="song:production-pass",
    )
    delivered = assess_role(
        "PRODUCER",
        skill_states=skills,
        evidence=(work_sample, later_credit, collaborator),
    )

    assert delivered.attained_stage_ids == (
        "PRODUCER-BUILDING-RECORDS",
        "PRODUCER-DELIVERING-WITH-OTHERS",
    )
    assert delivered.current_stage_id == "PRODUCER-DELIVERING-WITH-OTHERS"
    assert delivered.next_stage_id == "PRODUCER-OWNING-OUTCOMES"


def test_transition_preserves_reusable_evidence_and_names_smallest_gap() -> None:
    skills = {
        "artistic_voice": "APPLIED",
        "song_or_performance_finishing": "PRACTICED",
        "arrangement": "APPLIED",
        "sound_selection": "PRACTICED",
        "daw_operation": "PRACTICED",
        "collaborator_communication": "APPLIED",
    }
    shared_work = RoleEvidence(
        id="roleev_shared_song",
        role_ids=("ARTIST", "PRODUCER"),
        kind="WORK_SAMPLE",
        source_kind="OBSERVED",
        source_ref="song:shared-artifact",
    )

    transition = assess_transition(
        "ARTIST",
        "PRODUCER",
        skill_states=skills,
        evidence=(shared_work,),
    )

    assert transition.preserved_evidence_ids == (shared_work.id,)
    assert "arrangement" in transition.preserved_skill_ids
    assert "sound_selection" in transition.preserved_skill_ids
    assert "daw_operation" in transition.preserved_skill_ids
    assert transition.target_stage_id == "PRODUCER-BUILDING-RECORDS"
    assert transition.target_missing_skills[0].skill_id == "production_decision_making"
    assert "production_decision_making" in (transition.smallest_next_evidence_step or "")
    assert "does not downgrade or abandon" in transition.tradeoffs[1]
    assert "popularity" in transition.tradeoffs[2]


def test_manager_responsibility_count_requires_distinct_verified_evidence() -> None:
    skills = {
        "career_planning": "PRACTICED",
        "stakeholder_communication": "INDEPENDENT",
        "release_coordination": "APPLIED",
        "rights_business_basics": "APPLIED",
        "opportunity_tracking": "APPLIED",
        "business_coordination": "INDEPENDENT",
    }
    evidence = (
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

    one_responsibility = assess_role("MANAGER", skill_states=skills, evidence=evidence)
    assert one_responsibility.current_stage_id == "MANAGER-COORDINATING-OUTCOMES"
    assert one_responsibility.next_stage_id == "MANAGER-OWNING-REPEATABLE-OPERATIONS"
    assert tuple(item.kind for item in one_responsibility.missing_evidence) == (
        "RESPONSIBILITY",
    )

    second_responsibility = RoleEvidence(
        id="resp-2",
        role_ids=("MANAGER",),
        kind="RESPONSIBILITY",
        source_kind="VERIFIED_EXTERNAL",
        source_ref="delivery:2",
    )
    complete = assess_role(
        "MANAGER",
        skill_states=skills,
        evidence=(*evidence, second_responsibility),
    )
    assert complete.current_stage_id == "MANAGER-OWNING-REPEATABLE-OPERATIONS"
    assert complete.next_stage_id is None
    assert complete.missing_skills == ()
    assert complete.missing_evidence == ()


def test_popularity_tenure_and_unknown_roles_cannot_be_smuggled_in_as_evidence() -> None:
    with pytest.raises(ValidationError, match="unsupported role evidence kind"):
        RoleEvidence(
            id="followers",
            role_ids=("ARTIST",),
            kind="FOLLOWER_COUNT",
            source_kind="VERIFIED_EXTERNAL",
            source_ref="platform:followers",
        )

    with pytest.raises(ValidationError, match="unsupported role evidence kind"):
        RoleEvidence(
            id="tenure",
            role_ids=("PRODUCER",),
            kind="YEARS_ACTIVE",
            source_kind="ARTIST_DECLARED",
            source_ref="artist:bio",
        )

    with pytest.raises(ValidationError, match="unknown career role"):
        get_role_definition("INFLUENCER")

    with pytest.raises(ValidationError, match="two different roles"):
        assess_transition("ARTIST", "ARTIST", skill_states={}, evidence=())
