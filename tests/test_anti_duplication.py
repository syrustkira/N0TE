import pytest

from governance.anti_duplication import DuplicateGovernanceError, GovernanceAddition, validate_governance_addition


def test_existing_equivalent_owner_blocks_new_governance_layer():
    with pytest.raises(DuplicateGovernanceError, match="extend that owner"):
        validate_governance_addition(GovernanceAddition(
            proposed_name="CONTINUE Whole Building Law 2",
            proposed_semantic_owner="continuity",
            searched_existing_owners=("START HERE", "CONTROL_PLANE"),
            equivalent_owner="START HERE",
        ))


def test_extension_is_permitted_when_difference_is_explicit():
    item = GovernanceAddition(
        proposed_name="Executable transition validator",
        proposed_semantic_owner="epistemic state machine",
        searched_existing_owners=("PUBLIC_SCOPE_CANON", "execution_envelope"),
        equivalent_owner="PUBLIC_SCOPE_CANON",
        reason_existing_owner_cannot_be_extended="canonical prose cannot itself reject illegal runtime transitions",
    )
    assert validate_governance_addition(item) == item
