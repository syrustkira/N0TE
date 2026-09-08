from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from n0te.career_state import (
    CAREER_STATES,
    CareerStateMemory,
    StaleCareerStateError,
    career_state_definition,
)
from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory


def test_career_state_is_explicit_append_only_context_with_real_weighting(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Career State Artist")
    try:
        career = CareerStateMemory(hq.store)
        assert career.current_state() is None
        assert career.recommendation_weight("creation") == "NORMAL"

        creating = career.record_state(
            "creating",
            rationale="Protect a writing run before release planning expands.",
            expected_current_id=None,
        )
        assert creating.state == "CREATING"
        assert creating.truth_type == "USER_DECLARED"
        assert creating.rationale == "Protect a writing run before release planning expands."
        assert career.recommendation_weight("creation") == "FAVOR"
        assert career.recommendation_weight("recovery") == "PROTECT"
        assert career.recommendation_weight("optional expansion") == "DEFER_OPTIONAL"
        assert career.recommendation_weight("release") == "NORMAL"

        releasing = career.record_state(
            "releasing",
            rationale="The single is finished and the release cycle is active.",
            expected_current_id=creating.id,
        )
        assert releasing.state == "RELEASING"
        assert [entry.state for entry in career.history()] == ["CREATING", "RELEASING"]
        assert career.recommendation_weight("release") == "FAVOR"
        assert career.recommendation_weight("creation") == "PROTECT"

        activity = hq.store._conn.execute(
            "SELECT event_type,payload_json FROM activity_events "
            "WHERE object_type='CAREER_STATE' ORDER BY rowid"
        ).fetchall()
        assert [str(row["event_type"]) for row in activity] == [
            "CAREER_STATE_RECORDED",
            "CAREER_STATE_RECORDED",
        ]
        assert '"state":"CREATING"' in str(activity[0]["payload_json"])
        assert '"state":"RELEASING"' in str(activity[1]["payload_json"])

        with pytest.raises(sqlite3.IntegrityError, match="history is immutable"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "UPDATE career_state_entries SET state='GROWING' WHERE id=?",
                    (creating.id,),
                )
        with pytest.raises(sqlite3.IntegrityError, match="history is immutable"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "DELETE FROM career_state_entries WHERE id=?",
                    (creating.id,),
                )
    finally:
        hq.close()


def test_career_state_stale_and_invalid_inputs_fail_without_rewriting_history(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Career State Boundary Artist")
    try:
        career = CareerStateMemory(hq.store)
        first = career.record_state(
            "building",
            rationale=None,
            expected_current_id=None,
        )
        with pytest.raises(StaleCareerStateError, match="changed after it was reviewed"):
            career.record_state(
                "growing",
                expected_current_id=None,
            )
        with pytest.raises(ValidationError, match="unsupported Career State"):
            career.record_state(
                "famous",
                expected_current_id=first.id,
            )
        with pytest.raises(ValidationError, match="unsupported Career State recommendation topic"):
            career.recommendation_weight("followers")

        same = career.record_state(
            "BUILDING",
            rationale=None,
            expected_current_id=first.id,
        )
        assert same.id == first.id
        assert len(career.history()) == 1
        assert career.current_state() == first
    finally:
        hq.close()


def test_career_state_relaunch_and_profile_isolation_preserve_exact_artist_context(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    first_hq = HeadquartersMemory.create(root, "First Career Artist")
    first_profile = first_hq.store.profile_id
    try:
        first = CareerStateMemory(first_hq.store).record_state(
            "client-heavy",
            rationale="Paid production commitments dominate this month.",
            expected_current_id=None,
        )
        assert first.state == "CLIENT_HEAVY"
    finally:
        first_hq.close()

    second_hq = HeadquartersMemory.create(root, "Second Career Artist")
    second_profile = second_hq.store.profile_id
    try:
        second_career = CareerStateMemory(second_hq.store)
        assert second_career.current_state() is None
        second_career.record_state(
            "experimenting",
            expected_current_id=None,
        )
    finally:
        second_hq.close()

    reopened_first = HeadquartersMemory.open(root, first_profile)
    try:
        current = CareerStateMemory(reopened_first.store).current_state()
        assert current is not None
        assert current.state == "CLIENT_HEAVY"
        assert current.rationale == "Paid production commitments dominate this month."
    finally:
        reopened_first.close()

    reopened_second = HeadquartersMemory.open(root, second_profile)
    try:
        current = CareerStateMemory(reopened_second.store).current_state()
        assert current is not None
        assert current.state == "EXPERIMENTING"
    finally:
        reopened_second.close()


def test_canonical_career_states_are_working_seasons_not_ranked_levels() -> None:
    assert CAREER_STATES == (
        "SURVIVAL",
        "BUILDING",
        "CREATING",
        "RELEASING",
        "GROWING",
        "TOURING",
        "CLIENT_HEAVY",
        "RECOVERY",
        "EXPERIMENTING",
    )
    labels = [career_state_definition(state).label for state in CAREER_STATES]
    assert labels == [
        "Survival",
        "Building",
        "Creating",
        "Releasing",
        "Growing",
        "Touring",
        "Client-heavy",
        "Recovery",
        "Experimenting",
    ]
    assert "senior" not in " ".join(labels).lower()
