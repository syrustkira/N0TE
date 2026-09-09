from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping


EPISTEMIC_STATES = frozenset({
    "OBSERVED",
    "DERIVED",
    "CLAIMED",
    "ASSUMED",
    "STALE",
    "UNKNOWN",
    "UNTESTED",
    "TESTING",
    "SUPPORTED",
    "WEAK",
    "FAILED_TEST",
    "BLOCKED",
    "REJECTED",
    "RISKY",
    "PREPARED",
    "SUBMITTED",
    "ACCEPTED",
    "LIVE",
    "VERIFIED",
})

# Explicitly legal semantic transitions. Anything not listed is rejected.
_ALLOWED_TRANSITIONS = {
    "UNKNOWN": {"OBSERVED", "DERIVED", "CLAIMED", "ASSUMED", "STALE", "UNTESTED", "BLOCKED", "REJECTED", "RISKY"},
    "UNTESTED": {"TESTING", "BLOCKED", "REJECTED", "RISKY"},
    "TESTING": {"SUPPORTED", "WEAK", "FAILED_TEST", "BLOCKED"},
    "PREPARED": {"SUBMITTED", "BLOCKED", "REJECTED"},
    "SUBMITTED": {"ACCEPTED", "REJECTED", "BLOCKED"},
    "ACCEPTED": {"LIVE", "BLOCKED"},
    "LIVE": {"VERIFIED", "STALE", "BLOCKED"},
    "OBSERVED": {"STALE", "DERIVED"},
    "DERIVED": {"STALE", "OBSERVED"},
    "CLAIMED": {"OBSERVED", "STALE", "REJECTED"},
    "ASSUMED": {"OBSERVED", "DERIVED", "STALE", "REJECTED"},
    "STALE": {"OBSERVED", "DERIVED", "UNKNOWN", "BLOCKED"},
    "SUPPORTED": {"STALE", "TESTING"},
    "WEAK": {"STALE", "TESTING"},
    "FAILED_TEST": {"STALE", "TESTING"},
    "BLOCKED": {"UNTESTED", "TESTING", "PREPARED", "OBSERVED", "UNKNOWN"},
    "RISKY": {"REJECTED", "UNTESTED", "BLOCKED"},
    "REJECTED": {"STALE"},
    "VERIFIED": {"STALE"},
}

# These collapses caused real regressions and are never accepted as shorthand.
_FORBIDDEN_COLLAPSES = {
    ("REJECTED", "FAILED_TEST"),
    ("RISKY", "FAILED_TEST"),
    ("UNTESTED", "FAILED_TEST"),
    ("PREPARED", "VERIFIED"),
    ("SUBMITTED", "VERIFIED"),
    ("ACCEPTED", "VERIFIED"),
}


class CompiledStateError(ValueError):
    pass


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompiledStateError(f"{field} must be non-empty text")
    return value.strip()


def _unique_text(values: Iterable[object], field: str) -> tuple[str, ...]:
    out = tuple(_text(item, field) for item in values)
    if len(out) != len(set(out)):
        raise CompiledStateError(f"{field} contains duplicates")
    return out


def _iso(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompiledStateError(f"{field} must be ISO-8601") from exc
    if dt.tzinfo is None:
        raise CompiledStateError(f"{field} must include timezone")
    return dt.astimezone(timezone.utc)


def assert_epistemic_transition(previous: str, current: str) -> None:
    previous = _text(previous, "previous_state").upper()
    current = _text(current, "current_state").upper()
    if previous not in EPISTEMIC_STATES or current not in EPISTEMIC_STATES:
        raise CompiledStateError("unsupported epistemic state")
    if previous == current:
        return
    if (previous, current) in _FORBIDDEN_COLLAPSES:
        raise CompiledStateError(f"forbidden epistemic collapse {previous}->{current}")
    if current not in _ALLOWED_TRANSITIONS.get(previous, set()):
        raise CompiledStateError(f"illegal epistemic transition {previous}->{current}")


@dataclass(frozen=True)
class StateFact:
    fact_id: str
    value: str
    state: str
    source_ref: str
    observed_at: datetime
    source_revision: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, object]) -> "StateFact":
        state = _text(raw.get("state"), "fact.state").upper()
        if state not in EPISTEMIC_STATES:
            raise CompiledStateError(f"unsupported fact state: {state}")
        return cls(
            fact_id=_text(raw.get("fact_id"), "fact.fact_id"),
            value=_text(raw.get("value"), "fact.value"),
            state=state,
            source_ref=_text(raw.get("source_ref"), "fact.source_ref"),
            observed_at=_iso(raw.get("observed_at"), "fact.observed_at"),
            source_revision=_text(raw.get("source_revision"), "fact.source_revision"),
        )


@dataclass(frozen=True)
class JobCursor:
    job_id: str
    outcome: str
    active_object: str
    current_step: str
    acceptance: str
    state: str
    blockers: tuple[str, ...]

    @classmethod
    def from_raw(cls, raw: Mapping[str, object]) -> "JobCursor":
        return cls(
            job_id=_text(raw.get("job_id"), "cursor.job_id"),
            outcome=_text(raw.get("outcome"), "cursor.outcome"),
            active_object=_text(raw.get("active_object"), "cursor.active_object"),
            current_step=_text(raw.get("current_step"), "cursor.current_step"),
            acceptance=_text(raw.get("acceptance"), "cursor.acceptance"),
            state=_text(raw.get("state"), "cursor.state").upper(),
            blockers=_unique_text(raw.get("blockers") or (), "cursor.blockers"),
        )


@dataclass(frozen=True)
class CompiledExecutionState:
    retained_scope_refs: tuple[str, ...]
    truth_owners: dict[str, str]
    dependency_graph: dict[str, tuple[str, ...]]
    lens_dispatch: dict[str, tuple[str, ...]]
    cursor: JobCursor
    facts: dict[str, StateFact]
    incidents: tuple[str, ...]
    source_fingerprint: str
    compiled_at: datetime
    fingerprint: str

    def next_dependencies(self) -> tuple[str, ...]:
        return self.dependency_graph.get(self.cursor.active_object, ())

    def required_functions(self) -> tuple[str, ...]:
        return self.lens_dispatch.get(self.cursor.active_object, ())


def compile_execution_state(raw: Mapping[str, object]) -> CompiledExecutionState:
    if not isinstance(raw, Mapping):
        raise CompiledStateError("compiled-state input must be an object")

    retained = _unique_text(raw.get("retained_scope_refs") or (), "retained_scope_refs")
    if not retained:
        raise CompiledStateError("retained_scope_refs must not be empty")

    owners_raw = raw.get("truth_owners")
    if not isinstance(owners_raw, Mapping):
        raise CompiledStateError("truth_owners must be an object")
    required_planes = ("WHY", "WHAT", "HOW", "NOW", "PROOF")
    owners = {plane: _text(owners_raw.get(plane), f"truth_owners.{plane}") for plane in required_planes}

    graph_raw = raw.get("dependency_graph")
    if not isinstance(graph_raw, Mapping):
        raise CompiledStateError("dependency_graph must be an object")
    graph = {str(key).strip(): _unique_text(value or (), f"dependency_graph.{key}") for key, value in graph_raw.items()}

    dispatch_raw = raw.get("lens_dispatch")
    if not isinstance(dispatch_raw, Mapping):
        raise CompiledStateError("lens_dispatch must be an object")
    dispatch = {str(key).strip(): _unique_text(value or (), f"lens_dispatch.{key}") for key, value in dispatch_raw.items()}

    cursor_raw = raw.get("cursor")
    if not isinstance(cursor_raw, Mapping):
        raise CompiledStateError("cursor must be an object")
    cursor = JobCursor.from_raw(cursor_raw)
    if cursor.active_object not in retained:
        raise CompiledStateError("active_object must remain inside retained scope")
    if cursor.active_object not in dispatch:
        raise CompiledStateError("active_object has no deterministic lens dispatch")

    facts_raw = raw.get("facts")
    if facts_raw is None:
        facts_raw = []
    if not isinstance(facts_raw, list):
        raise CompiledStateError("facts must be a list")
    facts: dict[str, StateFact] = {}
    for item in facts_raw:
        if not isinstance(item, Mapping):
            raise CompiledStateError("fact must be an object")
        fact = StateFact.from_raw(item)
        if fact.fact_id in facts:
            raise CompiledStateError(f"duplicate fact_id: {fact.fact_id}")
        facts[fact.fact_id] = fact

    incidents = _unique_text(raw.get("incidents") or (), "incidents")
    source_fingerprint = _text(raw.get("source_fingerprint"), "source_fingerprint")
    compiled_at = _iso(raw.get("compiled_at"), "compiled_at")

    canonical = {
        "retained_scope_refs": retained,
        "truth_owners": owners,
        "dependency_graph": graph,
        "lens_dispatch": dispatch,
        "cursor": {
            "job_id": cursor.job_id,
            "outcome": cursor.outcome,
            "active_object": cursor.active_object,
            "current_step": cursor.current_step,
            "acceptance": cursor.acceptance,
            "state": cursor.state,
            "blockers": cursor.blockers,
        },
        "facts": {
            key: {
                "value": fact.value,
                "state": fact.state,
                "source_ref": fact.source_ref,
                "observed_at": fact.observed_at.isoformat(),
                "source_revision": fact.source_revision,
            }
            for key, fact in sorted(facts.items())
        },
        "incidents": incidents,
        "source_fingerprint": source_fingerprint,
        "compiled_at": compiled_at.isoformat(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return CompiledExecutionState(
        retained_scope_refs=retained,
        truth_owners=owners,
        dependency_graph=graph,
        lens_dispatch=dispatch,
        cursor=cursor,
        facts=facts,
        incidents=incidents,
        source_fingerprint=source_fingerprint,
        compiled_at=compiled_at,
        fingerprint=fingerprint,
    )


def prefer_fresher_fact(projected: StateFact, observed: StateFact) -> StateFact:
    """Current observed provider/runtime evidence wins over an older projection."""
    if projected.fact_id != observed.fact_id:
        raise CompiledStateError("cannot reconcile different facts")
    if observed.observed_at < projected.observed_at:
        return projected
    return observed


def recurrence_fingerprint(*, objective: str, failure_class: str, active_object: str, missing_refs: Iterable[str] = ()) -> str:
    payload = {
        "objective": _text(objective, "objective").lower(),
        "failure_class": _text(failure_class, "failure_class").upper(),
        "active_object": _text(active_object, "active_object").lower(),
        "missing_refs": sorted(_unique_text(missing_refs, "missing_refs")),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def classify_recurrence(fingerprint: str, previous_fingerprints: Iterable[str]) -> bool:
    fingerprint = _text(fingerprint, "fingerprint")
    return fingerprint in set(_unique_text(previous_fingerprints, "previous_fingerprints"))
