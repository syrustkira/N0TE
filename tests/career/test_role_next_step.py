from __future__ import annotations

from n0te.career_roles import RoleEvidence, assess_transition


def test_transition_recommends_one_next_evidence_item_when_stage_needs_two() -> None:
    skills = {
        "career_planning": "PRACTICED",
        "stakeholder_communication": "INDEPENDENT",
        "release_coordination": "APPLIED",
        "rights_business_basics": "APPLIED",
        "opportunity_tracking": "APPLIED",
        "business_coordination": "INDEPENDENT",
        "artistic_voice": "PRACTICED",
        "song_or_performance_finishing": "PRACTICED",
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

    transition = assess_transition(
        "ARTIST",
        "MANAGER",
        skill_states=skills,
        evidence=evidence,
    )

    assert transition.target_missing_evidence[0].kind == "RESPONSIBILITY"
    assert transition.target_missing_evidence[0].count == 2
    assert transition.smallest_next_evidence_step is not None
    assert transition.smallest_next_evidence_step.startswith(
        "Capture one verified RESPONSIBILITY evidence item"
    )
    assert "Capture 2" not in transition.smallest_next_evidence_step
