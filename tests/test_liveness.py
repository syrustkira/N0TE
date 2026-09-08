from governance.liveness import LivenessObservation, reconcile_liveness


def test_runtime_truth_overrides_stale_projected_liveness():
    result = reconcile_liveness(LivenessObservation(
        object_id="master-coordinator",
        projected="ENABLED",
        runtime="DISABLED",
        runtime_ref="automation-runtime",
    ))
    assert result.effective_state == "DISABLED"
    assert result.contradiction is True
    assert result.incident_class == "STALE_PROJECTION_LIVENESS_CONTRADICTION"
