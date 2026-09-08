from __future__ import annotations

from datetime import datetime, timezone

import pytest

from governance.compiled_execution_state import (
    CompiledStateError,
    StateFact,
    assert_epistemic_transition,
    classify_recurrence,
    compile_execution_state,
    prefer_fresher_fact,
    recurrence_fingerprint,
)
from governance.execution_state_compiler import compile_from_manifest


def _manifest():
    return {
        "retained_scope_refs": ["artist", "producer", "songwriter", "engineer", "services", "service_acquisition"],
        "truth_owners": {
            "WHY": "master-context",
            "WHAT": "public-scope",
            "HOW": "product-db",
            "NOW": "os",
            "PROOF": "runtime",
        },
        "dependency_graph": {
            "services": ["service_acquisition"],
            "service_acquisition": ["qualified_inquiry"],
            "qualified_inquiry": [],
        },
        "lens_dispatch": {
            "artist": ["epistemology"],
            "producer": ["epistemology"],
            "songwriter": ["epistemology"],
            "engineer": ["epistemology"],
            "services": ["epistemology", "anti_flattening", "offer"],
            "service_acquisition": ["epistemology", "anti_flattening", "distribution", "sales", "experiments"],
        },
    }


def _cursor(active_object="service_acquisition"):
    return {
        "job_id": "job-1",
        "outcome": "obtain demand evidence",
        "active_object": active_object,
        "current_step": "run bounded acquisition increment",
        "acceptance": "external demand evidence observed",
        "state": "READY",
        "blockers": [],
    }


def _facts():
    return [
        {
            "fact_id": "services",
            "value": "live full-entry funnel",
            "state": "VERIFIED",
            "source_ref": "runtime",
            "observed_at": "2026-09-08T22:00:00-05:00",
            "source_revision": "r1",
        },
        {
            "fact_id": "acquisition",
            "value": "not meaningfully run",
            "state": "UNTESTED",
            "source_ref": "os",
            "observed_at": "2026-09-08T22:00:00-05:00",
            "source_revision": "r2",
        },
    ]


def test_compiler_preserves_entire_scope_while_activating_one_room():
    result = compile_from_manifest(
        manifest=_manifest(),
        cursor=_cursor(),
        facts=_facts(),
        compiled_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert result["retained_scope_refs"] == _manifest()["retained_scope_refs"]
    assert result["cursor"]["active_object"] == "service_acquisition"
    assert result["required_functions"] == _manifest()["lens_dispatch"]["service_acquisition"]
    assert result["next_dependencies"] == ["qualified_inquiry"]


def test_active_room_cannot_exist_outside_retained_building():
    with pytest.raises(CompiledStateError, match="retained scope"):
        compile_from_manifest(
            manifest=_manifest(),
            cursor=_cursor("forgotten_room"),
            facts=_facts(),
            compiled_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        )


def test_active_room_must_have_machine_dispatched_functions():
    manifest = _manifest()
    manifest["lens_dispatch"].pop("service_acquisition")
    with pytest.raises(CompiledStateError, match="lens dispatch"):
        compile_from_manifest(
            manifest=manifest,
            cursor=_cursor(),
            facts=_facts(),
            compiled_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        )

@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ("UNTESTED", "FAILED_TEST"),
        ("REJECTED", "FAILED_TEST"),
        ("RISKY", "FAILED_TEST"),
        ("PREPARED", "VERIFIED"),
        ("SUBMITTED", "VERIFIED"),
    ],
)
def test_known_semantic_collapses_are_illegal(previous, current):
    with pytest.raises(CompiledStateError):
        assert_epistemic_transition(previous, current)


def test_actual_test_can_reach_failed_test_only_through_testing():
    assert_epistemic_transition("UNTESTED", "TESTING")
    assert_epistemic_transition("TESTING", "FAILED_TEST")


def test_rejected_is_not_demand_failure():
    with pytest.raises(CompiledStateError):
        assert_epistemic_transition("REJECTED", "FAILED_TEST")


def test_unadvertised_service_is_not_failed_demand():
    with pytest.raises(CompiledStateError):
        assert_epistemic_transition("UNTESTED", "FAILED_TEST")


def test_provider_runtime_truth_beats_older_projection():
    projected = StateFact.from_raw({
        "fact_id": "services_runtime",
        "value": "LIVE 404",
        "state": "STALE",
        "source_ref": "os",
        "observed_at": "2026-09-08T20:00:00-05:00",
        "source_revision": "old",
    })
    observed = StateFact.from_raw({
        "fact_id": "services_runtime",
        "value": "HTTP 200",
        "state": "OBSERVED",
        "source_ref": "runtime",
        "observed_at": "2026-09-08T21:00:00-05:00",
        "source_revision": "new",
    })
    assert prefer_fresher_fact(projected, observed) == observed


def test_recurrence_has_stable_machine_signature():
    fingerprint = recurrence_fingerprint(
        objective="Represent full artist business",
        failure_class="ACTIVE_SUBSYSTEM_ERASED_RETAINED_SCOPE",
        active_object="services",
        missing_refs=["songwriter", "catalog", "licensing"],
    )
    assert classify_recurrence(fingerprint, [fingerprint]) is True


def test_compiled_state_fingerprint_changes_when_cursor_changes():
    first = compile_from_manifest(
        manifest=_manifest(),
        cursor=_cursor(),
        facts=_facts(),
        compiled_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    cursor = _cursor()
    cursor["current_step"] = "next step"
    second = compile_from_manifest(
        manifest=_manifest(),
        cursor=cursor,
        facts=_facts(),
        compiled_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert first["compiled_fingerprint"] != second["compiled_fingerprint"]


def test_compiled_state_is_disposable_and_reproducible():
    kwargs = dict(
        manifest=_manifest(),
        cursor=_cursor(),
        facts=_facts(),
        incidents=["KNOWN_FAILURE"],
        compiled_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert compile_from_manifest(**kwargs) == compile_from_manifest(**kwargs)
