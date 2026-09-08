from __future__ import annotations

import json
from pathlib import Path

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


def test_current_manifest_encodes_observed_context_and_routing_failures():
    manifest = json.loads(Path("governance/execution_manifest.json").read_text(encoding="utf-8"))
    constraints = manifest["decision_constraints"]

    assert constraints["production_entry"]["requires_rough_demo"] is False
    assert constraints["service_demand"]["meaningful_exposure_required_before_failure_conclusion"] is True
    assert constraints["economic_selection"]["forbidden_shortcut"] == "LOW_CASH=>LOWEST_VALUE_JOB"
    assert "retrieval" in constraints["anti_loop"]["rule"].lower()
    assert "runtime" in constraints["runtime_precedence"]["rule"].lower()

    acquisition_functions = set(manifest["lens_dispatch"]["service_acquisition"])
    assert {
        "epistemology",
        "anti_flattening",
        "discovery",
        "distribution",
        "sales",
        "experiments",
        "finance",
        "friction_memory",
    }.issubset(acquisition_functions)

    cursor = json.loads(Path("governance/current_execution_cursor.json").read_text(encoding="utf-8"))
    assert cursor["job_id"] == "SERVICE-ACQUISITION-001"
    assert cursor["active_object"] == "service_acquisition"
