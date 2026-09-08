from __future__ import annotations

import pytest

from governance.compiled_execution_state import CompiledStateError, assert_epistemic_transition


def test_spark_rejected_is_not_earnings_failure():
    with pytest.raises(CompiledStateError):
        assert_epistemic_transition("REJECTED", "FAILED_TEST")


def test_unadvertised_services_are_not_failed_demand():
    with pytest.raises(CompiledStateError):
        assert_epistemic_transition("UNTESTED", "FAILED_TEST")


def test_prepared_or_published_artifact_cannot_jump_to_verified_outcome():
    for state in ("PREPARED", "SUBMITTED", "ACCEPTED"):
        with pytest.raises(CompiledStateError):
            assert_epistemic_transition(state, "VERIFIED")


def test_unknown_stays_unknown_until_evidence_changes_it():
    assert_epistemic_transition("UNKNOWN", "OBSERVED")
    with pytest.raises(CompiledStateError):
        assert_epistemic_transition("UNKNOWN", "VERIFIED")
