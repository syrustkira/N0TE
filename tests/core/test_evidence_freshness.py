from __future__ import annotations

import pytest

from n0te.capability_evidence import CapabilityObservation
from n0te.evidence_freshness import (
    EvidenceFreshnessError,
    FreshnessDependency,
    assess_capability_observation_freshness,
    assess_evidence_freshness,
)


def _dependency(
    kind: str = "BINARY",
    *,
    key: str = "engine",
    observed: str = "v1",
    current: str | None = "v1",
) -> FreshnessDependency:
    return FreshnessDependency(
        kind=kind,
        key=key,
        observed_fingerprint=observed,
        current_fingerprint=current,
    )


def _capability_observation() -> CapabilityObservation:
    return CapabilityObservation(
        sequence=1,
        id="capev_1",
        workspace_id="workspace_1",
        workspace_observation_id="workspace_observation_1",
        host_runtime_fingerprint="runtime_1",
        route_id="route_1",
        route_kind="HOST_NATIVE",
        capability="stem.separate",
        display_name="Host stem separation",
        brand="Host",
        availability="AVAILABLE",
        evidence_kind="ADAPTER_TEST",
        evidence_ref="adapter-test-1",
        observed_at_epoch_seconds=100,
        task_fit=0.8,
        editability=0.7,
        locality=1.0,
        privacy=1.0,
        latency=0.7,
        reversibility=0.8,
        cost_efficiency=1.0,
        portability=0.4,
        paid=False,
    )


def test_current_evidence_never_gains_authority_or_correctness_claim() -> None:
    assessment = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=10_000,
        dependencies=(_dependency(),),
    )

    assert assessment.state == "CURRENT"
    assert assessment.age_seconds == 9_900
    assert assessment.reason_codes == ()
    assert assessment.usable_as_current is True
    assert assessment.reverification_required is False
    assert assessment.freshness_proves_correctness is False
    assert assessment.grants_execution_authority is False
    assert assessment.grants_external_action_authority is False
    assert assessment.grants_mutation_authority is False
    assert assessment.grants_purchase_authority is False
    assert assessment.grants_activation_authority is False


def test_age_does_not_expire_evidence_without_an_explicit_policy() -> None:
    assessment = assess_evidence_freshness(
        observed_at_epoch_seconds=1,
        as_of_epoch_seconds=10_000_000,
    )
    assert assessment.state == "CURRENT"
    assert assessment.max_age_seconds is None


def test_max_age_is_opt_in_and_exceeded_only_after_boundary() -> None:
    at_boundary = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=160,
        max_age_seconds=60,
    )
    assert at_boundary.state == "CURRENT"

    stale = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=161,
        max_age_seconds=60,
    )
    assert stale.state == "REVALIDATION_REQUIRED"
    assert stale.reason_codes == ("MAX_AGE_EXCEEDED",)


def test_explicit_expiry_is_expired_at_exact_boundary() -> None:
    assessment = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=200,
        expires_at_epoch_seconds=200,
    )
    assert assessment.state == "EXPIRED"
    assert assessment.reason_codes == ("EXPLICIT_EXPIRY_REACHED",)
    assert assessment.reverification_required is True


@pytest.mark.parametrize(
    "kind",
    [
        "HOST_RUNTIME",
        "HOST_EDITION",
        "OS",
        "BINARY",
        "PLUGIN",
        "PROVIDER_VERSION",
        "AUTH_SCOPE",
        "PERMISSION",
        "ENTITLEMENT",
        "OTHER",
    ],
)
def test_relevant_changed_dependency_requires_revalidation(kind: str) -> None:
    assessment = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=101,
        dependencies=(
            _dependency(kind, key="relevant", observed="old", current="new"),
        ),
    )
    assert assessment.state == "REVALIDATION_REQUIRED"
    assert assessment.reason_codes == (f"DEPENDENCY_CHANGED:{kind}:relevant",)
    assert assessment.dependency_states[0].state == "CHANGED"


def test_unknown_current_dependency_fails_closed_without_calling_it_changed() -> None:
    assessment = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=101,
        dependencies=(_dependency("PERMISSION", current=None),),
    )
    assert assessment.state == "UNKNOWN"
    assert assessment.reason_codes == ("DEPENDENCY_UNKNOWN:PERMISSION:engine",)
    assert assessment.dependency_states[0].state == "UNKNOWN"
    assert assessment.usable_as_current is False


def test_superseded_source_requires_revalidation_and_unknown_source_stays_unknown() -> None:
    superseded = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=101,
        source_current=False,
    )
    assert superseded.state == "REVALIDATION_REQUIRED"
    assert superseded.reason_codes == ("SOURCE_SUPERSEDED",)

    unknown = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=101,
        source_current=None,
    )
    assert unknown.state == "UNKNOWN"
    assert unknown.reason_codes == ("SOURCE_CURRENT_UNKNOWN",)


def test_multiple_revalidation_reasons_are_preserved_deterministically() -> None:
    assessment = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=500,
        source_current=False,
        max_age_seconds=100,
        dependencies=(
            _dependency("PLUGIN", key="z_plugin", observed="1", current=None),
            _dependency("BINARY", key="engine", observed="a", current="b"),
            _dependency("AUTH_SCOPE", key="provider", observed="read", current="write"),
        ),
    )
    assert assessment.state == "REVALIDATION_REQUIRED"
    assert assessment.reason_codes == (
        "SOURCE_SUPERSEDED",
        "DEPENDENCY_CHANGED:AUTH_SCOPE:provider",
        "DEPENDENCY_CHANGED:BINARY:engine",
        "MAX_AGE_EXCEEDED",
        "DEPENDENCY_UNKNOWN:PLUGIN:z_plugin",
    )
    assert tuple(
        (item.kind, item.key, item.state) for item in assessment.dependency_states
    ) == (
        ("AUTH_SCOPE", "provider", "CHANGED"),
        ("BINARY", "engine", "CHANGED"),
        ("PLUGIN", "z_plugin", "UNKNOWN"),
    )


def test_explicit_expiry_has_state_precedence_but_preserves_other_reasons() -> None:
    assessment = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=200,
        expires_at_epoch_seconds=200,
        source_current=False,
        dependencies=(_dependency(observed="old", current="new"),),
    )
    assert assessment.state == "EXPIRED"
    assert assessment.reason_codes == (
        "EXPLICIT_EXPIRY_REACHED",
        "SOURCE_SUPERSEDED",
        "DEPENDENCY_CHANGED:BINARY:engine",
    )


def test_changed_dependency_outweighs_other_unknown_dependency() -> None:
    assessment = assess_evidence_freshness(
        observed_at_epoch_seconds=100,
        as_of_epoch_seconds=101,
        dependencies=(
            _dependency("ENTITLEMENT", key="plan", observed="grant1", current="grant2"),
            _dependency("PERMISSION", key="scope", current=None),
        ),
    )
    assert assessment.state == "REVALIDATION_REQUIRED"
    assert assessment.reverification_required is True
    assert "DEPENDENCY_UNKNOWN:PERMISSION:scope" in assessment.reason_codes


def test_capability_adapter_keeps_exact_current_environment_current() -> None:
    observation = _capability_observation()
    assessment = assess_capability_observation_freshness(
        observation,
        as_of_epoch_seconds=160,
        current_workspace_observation_id="workspace_observation_1",
        current_host_runtime_fingerprint="runtime_1",
        max_age_seconds=60,
    )
    assert assessment.state == "CURRENT"
    assert tuple(item.state for item in assessment.dependency_states) == (
        "CURRENT",
        "CURRENT",
    )


def test_capability_adapter_revalidates_after_workspace_or_runtime_change() -> None:
    observation = _capability_observation()

    workspace_changed = assess_capability_observation_freshness(
        observation,
        as_of_epoch_seconds=101,
        current_workspace_observation_id="workspace_observation_2",
        current_host_runtime_fingerprint="runtime_1",
    )
    assert workspace_changed.state == "REVALIDATION_REQUIRED"
    assert (
        "DEPENDENCY_CHANGED:WORKSPACE_OBSERVATION:workspace_observation"
        in workspace_changed.reason_codes
    )

    runtime_changed = assess_capability_observation_freshness(
        observation,
        as_of_epoch_seconds=101,
        current_workspace_observation_id="workspace_observation_1",
        current_host_runtime_fingerprint="runtime_2",
    )
    assert runtime_changed.state == "REVALIDATION_REQUIRED"
    assert "DEPENDENCY_CHANGED:HOST_RUNTIME:host_runtime" in runtime_changed.reason_codes


def test_capability_adapter_can_bind_entitlement_and_permission_dependencies() -> None:
    observation = _capability_observation()
    assessment = assess_capability_observation_freshness(
        observation,
        as_of_epoch_seconds=101,
        current_workspace_observation_id="workspace_observation_1",
        current_host_runtime_fingerprint="runtime_1",
        dependencies=(
            _dependency(
                "ENTITLEMENT",
                key="license",
                observed="license-v1",
                current="license-v2",
            ),
            _dependency(
                "PERMISSION",
                key="automation-scope",
                observed="scope-v1",
                current="scope-v1",
            ),
        ),
    )
    assert assessment.state == "REVALIDATION_REQUIRED"
    assert "DEPENDENCY_CHANGED:ENTITLEMENT:license" in assessment.reason_codes


def test_capability_adapter_unknown_environment_is_not_silently_green() -> None:
    observation = _capability_observation()
    assessment = assess_capability_observation_freshness(
        observation,
        as_of_epoch_seconds=101,
        current_workspace_observation_id=None,
        current_host_runtime_fingerprint=None,
    )
    assert assessment.state == "UNKNOWN"
    assert assessment.usable_as_current is False


def test_future_observation_and_impossible_expiry_are_rejected() -> None:
    with pytest.raises(EvidenceFreshnessError, match="predates the evidence observation"):
        assess_evidence_freshness(
            observed_at_epoch_seconds=101,
            as_of_epoch_seconds=100,
        )

    with pytest.raises(EvidenceFreshnessError, match="expires_at_epoch_seconds predates"):
        assess_evidence_freshness(
            observed_at_epoch_seconds=100,
            as_of_epoch_seconds=100,
            expires_at_epoch_seconds=99,
        )


def test_semantic_inputs_fail_closed_instead_of_coercing_types() -> None:
    with pytest.raises(EvidenceFreshnessError, match="non-negative integer"):
        assess_evidence_freshness(
            observed_at_epoch_seconds=True,
            as_of_epoch_seconds=100,
        )
    with pytest.raises(EvidenceFreshnessError, match="source_current"):
        assess_evidence_freshness(
            observed_at_epoch_seconds=100,
            as_of_epoch_seconds=100,
            source_current=1,  # type: ignore[arg-type]
        )
    with pytest.raises(EvidenceFreshnessError, match="current_fingerprint must be text"):
        _dependency(current=123)  # type: ignore[arg-type]
    with pytest.raises(EvidenceFreshnessError, match="unsupported dependency kind"):
        _dependency("MAGIC")


def test_duplicate_dependency_identity_is_rejected() -> None:
    with pytest.raises(EvidenceFreshnessError, match="duplicate freshness dependency"):
        assess_evidence_freshness(
            observed_at_epoch_seconds=100,
            as_of_epoch_seconds=100,
            dependencies=(
                _dependency("PLUGIN", key="same", observed="a", current="a"),
                _dependency("PLUGIN", key="same", observed="b", current="b"),
            ),
        )


def test_capability_adapter_rejects_wrong_object_type() -> None:
    with pytest.raises(EvidenceFreshnessError, match="CapabilityObservation"):
        assess_capability_observation_freshness(  # type: ignore[arg-type]
            object(),
            as_of_epoch_seconds=100,
            current_workspace_observation_id="workspace_observation_1",
            current_host_runtime_fingerprint="runtime_1",
        )
