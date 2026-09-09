from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping


class CompiledStateError(ValueError):
    pass


def _text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CompiledStateError(f"{label} must be non-empty")
    return text


def _unique_text(values: object, label: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise CompiledStateError(f"{label} must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _text(value, label)
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _iso(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, label).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompiledStateError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise CompiledStateError(f"{label} must include timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class StateFact:
    fact_id: str
    value: object
    state: str
    source_ref: str
    observed_at: datetime
    source_revision: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, object]) -> "StateFact":
        return cls(
            fact_id=_text(raw.get("fact_id"), "fact.fact_id"),
            value=raw.get("value"),
            state=_text(raw.get("state"), "fact.state"),
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
            state=_text(raw.get("state"), "cursor.state"),
            blockers=_unique_text(raw.get("blockers") or (), "cursor.blockers"),
        )


_ALLOWED_EPISTEMIC_TRANSITIONS = {
    "UNKNOWN": {"UNKNOWN", "OBSERVED", "DERIVED", "CLAIMED", "ASSUMED", "STALE"},
    "OBSERVED": {"OBSERVED", "DERIVED", "STALE", "SUPPORTED", "WEAK", "FAILED_TEST"},
    "DERIVED": {"DERIVED", "STALE", "SUPPORTED", "WEAK", "FAILED_TEST"},
    "CLAIMED": {"CLAIMED", "OBSERVED", "DERIVED", "STALE"},
    "ASSUMED": {"ASSUMED", "OBSERVED", "DERIVED", "STALE"},
    "STALE": {"STALE", "OBSERVED", "DERIVED", "UNKNOWN"},
    "UNTESTED": {"UNTESTED", "TESTING"},
    "TESTING": {"TESTING", "SUPPORTED", "WEAK", "FAILED_TEST"},
    "SUPPORTED": {"SUPPORTED", "TESTING", "STALE"},
    "WEAK": {"WEAK", "TESTING", "SUPPORTED", "FAILED_TEST", "STALE"},
    "FAILED_TEST": {"FAILED_TEST", "TESTING", "STALE"},
    "RISKY": {"RISKY", "REJECTED"},
    "REJECTED": {"REJECTED"},
    "PREPARED": {"PREPARED", "STAGED", "SUBMITTED"},
    "STAGED": {"STAGED", "SUBMITTED"},
    "SUBMITTED": {"SUBMITTED", "ACCEPTED"},
    "ACCEPTED": {"ACCEPTED", "LIVE"},
    "LIVE": {"LIVE", "VERIFIED"},
    "VERIFIED": {"VERIFIED", "STALE"},
}


def assert_epistemic_transition(current: str, nxt: str) -> None:
    current = _text(current, "current epistemic state")
    nxt = _text(nxt, "next epistemic state")
    allowed = _ALLOWED_EPISTEMIC_TRANSITIONS.get(current)
    if allowed is None:
        raise CompiledStateError(f"unknown epistemic state: {current}")
    if nxt not in allowed:
        raise CompiledStateError(f"illegal epistemic transition: {current} -> {nxt}")


@dataclass(frozen=True)
class CompiledExecutionState:
    retained_scope_refs: tuple[str, ...]
    truth_owners: Mapping[str, str]
    dependency_graph: Mapping[str, tuple[str, ...]]
    lens_dispatch: Mapping[str, tuple[str, ...]]
    cursor: JobCursor
    facts: Mapping[str, StateFact]
    incidents: tuple[str, ...]
    source_fingerprint: str
    compiled_at: datetime
    fingerprint: str

    def required_functions(self) -> tuple[str, ...]:
        return self.lens_dispatch[self.cursor.active_object]

    def next_dependencies(self) -> tuple[str, ...]:
        return self.dependency_graph.get(self.cursor.active_object, ())


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

    facts_value = raw.get("facts")
    facts_raw = [] if facts_value is None else facts_value
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
    return observed if observed.observed_at >= projected.observed_at else projected
