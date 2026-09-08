from governance.action_authority import evaluate_standing_authority


def _manifest():
    return {
        "standing_authority": {
            "auto": ["READ_ONLY", "REVERSIBLE"],
            "human_required_when": ["spend_money", "enter_contract", "transfer_rights"],
        }
    }


def test_reversible_action_runs_without_human_bottleneck():
    decision = evaluate_standing_authority(action_class="REVERSIBLE", consequences=[], manifest=_manifest())
    assert decision.requires_human is False


def test_spend_requires_human_even_if_action_is_otherwise_reversible():
    decision = evaluate_standing_authority(
        action_class="REVERSIBLE",
        consequences=["spend_money"],
        manifest=_manifest(),
    )
    assert decision.requires_human is True
