import pytest

from governance.recovery import RecoveryError, resolve_recovery, uncertain_external_action


def test_uncertain_external_action_disables_blind_retry():
    state = uncertain_external_action("post-1", "provider response ambiguous")
    assert state.status == "UNKNOWN_RECOVERY_REQUIRED"
    assert state.retry_allowed is False
    assert state.requires_fresh_observation is True


def test_recovery_requires_fresh_observation_before_retry():
    state = uncertain_external_action("send-1", "timeout after send")
    assert resolve_recovery(state, observed=False) == state
    with pytest.raises(RecoveryError):
        resolve_recovery(state, observed=True)
    recovered = resolve_recovery(state, observed=True, observation_ref="provider:message-123")
    assert recovered.status == "RECOVERED"
    assert recovered.retry_allowed is True
