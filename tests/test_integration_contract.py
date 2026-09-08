import pytest

from governance.integration_contract import REQUIRED_EXECUTION_SEQUENCE, assert_single_loop_sequence


def test_expected_single_loop_sequence_passes():
    assert_single_loop_sequence(REQUIRED_EXECUTION_SEQUENCE)


def test_extra_controller_loop_is_rejected():
    sequence = list(REQUIRED_EXECUTION_SEQUENCE)
    sequence.insert(1, "SECOND_REASONING_CONTROLLER")
    with pytest.raises(ValueError, match="single compiled coordinator loop"):
        assert_single_loop_sequence(sequence)
