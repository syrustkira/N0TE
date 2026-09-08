from copy import deepcopy

import pytest

from governance.execution_envelope import (
    ExecutionEnvelopeError,
    evaluate_execution_envelope,
    require_execution_permit,
    validate_execution_envelope,
)


def valid_envelope():
    return {
        "intent": {
            "description": "Move a real professional-music buyer from discovery toward a paid start without flattening the retained artist-business system.",
            "outcome_class": "EXTERNAL_STATE",
        },
        "active_object": "professional-music-acquisition",
        "retained_scope_refs": [
            "artist",
            "producer",
            "songwriter",
            "engineer",
            "catalog",
            "services",
            "licensing",
            "relationships",
            "audience",
            "delegated-system",
        ],
        "dependencies": {
            "upstream": ["offer", "proof", "typed-inquiry-path"],
            "downstream": ["qualified-inquiry", "brief", "quote", "paid-start"],
        },
        "truth_owners": {
            "WHY": "TELLMEN0TE MASTER CONTEXT",
            "WHAT": "TELLMEN0TE_PUBLIC_SCOPE_CANON",
            "HOW": "current business/label + public operations procedures",
            "NOW": "TELLMEN0TE_OS",
            "PROOF": "fresh provider/runtime/public evidence",
        },
        "claims": [
            {
                "statement": "The professional-music services funnel is live but meaningful acquisition is not yet evidenced.",
                "truth_type": "CURRENT_FACT",
                "evidence_posture": "OBSERVED",
                "source_refs": ["TELLMEN0TE_OS:REVENUE_PIPELINE"],
                "blocking": False,
            },
            {
                "statement": "A buyer may enter before a rough demo exists.",
                "truth_type": "ACCEPTED_SCOPE",
                "evidence_posture": "DERIVED",
                "source_refs": ["public-services-scope", "production-journey"],
                "blocking": False,
            },
        ],
        "required_functions": [
            "epistemic_typing",
            "anti_flattening",
            "business_operator_lens",
            "offer",
            "discovery",
            "distribution",
            "sales",
            "conversion",
            "friction_memory",
        ],
        "invoked_functions": [
            "epistemic_typing",
            "anti_flattening",
            "business_operator_lens",
            "offer",
            "discovery",
            "distribution",
            "sales",
            "conversion",
            "friction_memory",
        ],
        "authority": {
            "action_class": "REVERSIBLE",
            "authorized": True,
            "requires_human": False,
            "source_ref": "current coordinator authority",
        },
        "acceptance": {
            "outcome_class": "EXTERNAL_STATE",
            "observable_condition": "The selected acquisition increment has real exposure evidence and the resulting funnel state is reconciled without calling publication itself conversion.",
            "evidence_required": ["exposure receipt", "destination receipt", "funnel-state readback"],
            "artifact_is_not_completion": True,
        },
        "next_causal_dependency": {
            "state": "KNOWN",
            "description": "Route any qualified inquiry into brief, quote and paid-start handling; otherwise analyze the observed funnel transition before changing the offer.",
            "source_ref": "TELLMEN0TE_OS:REVENUE_PIPELINE",
        },
    }


def test_valid_execution_envelope_can_receive_permit():
    envelope = valid_envelope()
    assert validate_execution_envelope(envelope) is True
    result = require_execution_permit(envelope)
    assert result.valid is True
    assert result.execution_allowed is True
    assert len(result.envelope_fingerprint) == 64


def test_missing_truth_plane_fails_closed_before_action():
    envelope = valid_envelope()
    del envelope["truth_owners"]["PROOF"]
    result = evaluate_execution_envelope(envelope)
    assert result.valid is False
    assert result.execution_allowed is False
    assert "truth_owners.PROOF" in result.blockers[0]


def test_missing_function_invocation_is_not_equivalent_to_storing_the_function():
    envelope = valid_envelope()
    envelope["invoked_functions"].remove("friction_memory")
    with pytest.raises(ExecutionEnvelopeError, match="required functions were not invoked"):
        validate_execution_envelope(envelope)


def test_blocking_unknown_prevents_stateful_execution():
    envelope = valid_envelope()
    envelope["claims"].append(
        {
            "statement": "Whether the destination still matches canonical scope is unresolved.",
            "truth_type": "UNKNOWN",
            "evidence_posture": "UNKNOWN",
            "source_refs": [],
            "blocking": True,
        }
    )
    result = evaluate_execution_envelope(envelope)
    assert result.valid is True
    assert result.execution_allowed is False
    assert result.blockers == ("blocking epistemic claim claims[2]=UNKNOWN",)


def test_stale_blocking_current_fact_cannot_masquerade_as_current_truth():
    envelope = valid_envelope()
    envelope["claims"][0]["evidence_posture"] = "STALE"
    result = evaluate_execution_envelope(envelope)
    assert result.valid is True
    assert result.execution_allowed is False
    assert "STALE" in result.blockers[0]


def test_unapproved_stateful_action_fails_closed():
    envelope = valid_envelope()
    envelope["authority"]["authorized"] = False
    result = evaluate_execution_envelope(envelope)
    assert result.execution_allowed is False
    assert "not authorized" in result.blockers[0]


def test_irreversible_action_cannot_drop_human_authority():
    envelope = valid_envelope()
    envelope["authority"]["action_class"] = "IRREVERSIBLE"
    envelope["authority"]["requires_human"] = False
    result = evaluate_execution_envelope(envelope)
    assert result.execution_allowed is False
    assert "must require human authority" in result.blockers[0]


def test_external_job_cannot_be_closed_by_artifact_only_acceptance():
    envelope = valid_envelope()
    envelope["acceptance"]["artifact_is_not_completion"] = False
    with pytest.raises(ExecutionEnvelopeError, match="artifact-only completion"):
        validate_execution_envelope(envelope)


def test_acceptance_cannot_silently_change_the_human_job():
    envelope = valid_envelope()
    envelope["acceptance"]["outcome_class"] = "ARTIFACT"
    with pytest.raises(ExecutionEnvelopeError, match="must match intent outcome_class"):
        validate_execution_envelope(envelope)


def test_retained_scope_cannot_disappear_when_one_room_is_active():
    envelope = valid_envelope()
    original = set(envelope["retained_scope_refs"])
    envelope["active_object"] = "mixing"
    assert validate_execution_envelope(envelope) is True
    assert set(envelope["retained_scope_refs"]) == original


def test_read_only_reconstruction_can_remain_allowed_without_stateful_authority():
    envelope = deepcopy(valid_envelope())
    envelope["authority"] = {
        "action_class": "READ_ONLY",
        "authorized": False,
        "requires_human": False,
        "source_ref": "read-only reconstruction",
    }
    result = evaluate_execution_envelope(envelope)
    assert result.valid is True
    assert result.execution_allowed is True
