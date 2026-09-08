from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LivenessObservation:
    object_id: str
    projected: str
    runtime: str
    runtime_ref: str


@dataclass(frozen=True)
class LivenessReconciliation:
    object_id: str
    effective_state: str
    contradiction: bool
    incident_class: str | None
    evidence_ref: str


def reconcile_liveness(observation: LivenessObservation) -> LivenessReconciliation:
    for field in (observation.object_id, observation.projected, observation.runtime, observation.runtime_ref):
        if not isinstance(field, str) or not field.strip():
            raise ValueError("liveness fields must be non-empty")
    projected = observation.projected.strip().upper()
    runtime = observation.runtime.strip().upper()
    contradiction = projected != runtime
    return LivenessReconciliation(
        object_id=observation.object_id.strip(),
        effective_state=runtime,
        contradiction=contradiction,
        incident_class="STALE_PROJECTION_LIVENESS_CONTRADICTION" if contradiction else None,
        evidence_ref=observation.runtime_ref.strip(),
    )
