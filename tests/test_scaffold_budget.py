import pytest

from governance.scaffold_budget import ScaffoldBudgetError, require_scaffold_justification


def test_new_scaffold_without_observed_failure_or_blocked_outcome_is_rejected():
    with pytest.raises(ScaffoldBudgetError):
        require_scaffold_justification({
            "expected_supervision_reduction": "seems cleaner",
        })


def test_observed_failure_with_expected_attention_reduction_is_allowed():
    item = require_scaffold_justification({
        "observed_failure_ref": "ACTIVE_SUBSYSTEM_ERASED_RETAINED_SCOPE",
        "expected_supervision_reduction": "prevent the user from reconstructing retained scope",
    })
    assert item.observed_failure_ref == "ACTIVE_SUBSYSTEM_ERASED_RETAINED_SCOPE"
