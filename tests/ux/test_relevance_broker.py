from dataclasses import FrozenInstanceError, replace

import pytest

from n0te.relevance_broker import (
    REQUIRED_ALERT_KINDS,
    RelevanceBroker,
    RelevanceCandidate,
    RelevanceContextBinding,
    RelevanceScopeLeakError,
    StaleRelevanceContextError,
)


def binding(**changes):
    values = {
        "profile_id": "profile-1",
        "artist_id": "artist-1",
        "song_id": "song-1",
        "version_id": "version-1",
        "session_id": "session-1",
        "focus_id": "focus-1",
        "workspace_id": "workspace-1",
        "job_id": "job-1",
        "purpose_key": "MAKE",
        "operating_context": "NORMAL",
    }
    values.update(changes)
    return RelevanceContextBinding(**values)


def candidate(ctx, semantic_key="subject", surface="NOW", **changes):
    values = {
        "semantic_key": semantic_key,
        "surface": surface,
        "binding_fingerprint": ctx.fingerprint,
        "scope_profile_id": ctx.profile_id,
        "scope_artist_id": ctx.artist_id,
        "scope_song_id": ctx.song_id,
    }
    values.update(changes)
    return RelevanceCandidate(**values)


def decision(arbitration, key):
    return next(
        item
        for item in arbitration.surface_now + arbitration.held_decisions
        if item.semantic_key == key
    )


def test_binding_fingerprint_is_deterministic_and_context_sensitive():
    current = binding()
    assert current.fingerprint == binding().fingerprint
    for field_name, value in (
        ("profile_id", "profile-2"),
        ("artist_id", "artist-2"),
        ("song_id", "song-2"),
        ("version_id", "version-2"),
        ("session_id", "session-2"),
        ("focus_id", "focus-2"),
        ("workspace_id", "workspace-2"),
        ("job_id", "job-2"),
        ("purpose_key", "FINISH"),
        ("operating_context", "LIVE"),
    ):
        assert replace(current, **{field_name: value}).fingerprint != current.fingerprint


def test_broker_rejects_materially_moved_current_context():
    original = binding()
    broker = RelevanceBroker(original)
    for moved in (
        replace(original, song_id="song-2", version_id="version-2"),
        replace(original, version_id="version-2"),
        replace(original, session_id="session-2"),
        replace(original, focus_id="focus-2"),
        replace(original, workspace_id="workspace-2"),
        replace(original, job_id="job-2"),
        replace(original, purpose_key="FINISH"),
        replace(original, operating_context="RECORDING"),
    ):
        with pytest.raises(StaleRelevanceContextError):
            broker.arbitrate(moved, ())


def test_stale_ordinary_candidate_fails_closed():
    current = binding()
    old = replace(current, session_id="session-old")
    item = candidate(
        current,
        binding_fingerprint=old.fingerprint,
        blocks_next_step=True,
    )
    result = RelevanceBroker(current).arbitrate(current, (item,))
    assert result.surface_now == ()
    held = result.held_decisions[0]
    assert held.band == "STALE_CANDIDATE_CONTEXT"
    assert held.disposition == "HOLD"
    assert "STALE_CANDIDATE_CONTEXT" in held.reason_codes


def test_stale_explicit_request_cannot_masquerade_as_current_request():
    current = binding()
    old = replace(current, focus_id="focus-old")
    item = candidate(
        current,
        binding_fingerprint=old.fingerprint,
        explicit_artist_request=True,
    )
    result = RelevanceBroker(current).arbitrate(current, (item,))
    assert result.surface_now == ()
    assert result.held_decisions[0].band == "STALE_CANDIDATE_CONTEXT"


@pytest.mark.parametrize("alert_kind", REQUIRED_ALERT_KINDS)
def test_required_alert_survives_not_now_unsafe_and_stale_projection(alert_kind):
    current = binding()
    old = replace(current, session_id="session-old")
    item = candidate(
        current,
        binding_fingerprint=old.fingerprint,
        required_alert_kind=alert_kind,
        artist_not_now=True,
        discussion_state="UNSAFE",
        evidence_freshness="EXPIRED",
    )
    result = RelevanceBroker(current).arbitrate(current, (item,))
    surfaced = result.surface_now[0]
    assert surfaced.band == "REQUIRED_ALERT"
    assert surfaced.disposition == "SURFACE_NOW"
    assert "STALE_CANDIDATE_CONTEXT" in surfaced.reason_codes
    assert "EVIDENCE_EXPIRED" in surfaced.reason_codes
    assert "NOT_NOW_OVERRIDDEN_FOR_REQUIRED_ALERT" in surfaced.reason_codes
    assert "GUARDED_ALERT_SURFACING" in surfaced.reason_codes
    assert surfaced.action_authority_granted is False
    assert surfaced.mutation_authorized is False
    assert surfaced.external_action_authorized is False


def test_cross_profile_candidate_is_rejected_before_arbitration():
    current = binding()
    item = candidate(current, scope_profile_id="profile-2")
    with pytest.raises(RelevanceScopeLeakError):
        RelevanceBroker(current).arbitrate(current, (item,))


def test_cross_artist_candidate_is_rejected_before_arbitration():
    current = binding()
    item = candidate(current, scope_artist_id="artist-2")
    with pytest.raises(RelevanceScopeLeakError):
        RelevanceBroker(current).arbitrate(current, (item,))


def test_song_bound_context_rejects_other_song_even_when_job_matches():
    current = binding()
    item = candidate(
        current,
        scope_song_id="song-2",
        scope_job_id=current.job_id,
    )
    with pytest.raises(RelevanceScopeLeakError):
        RelevanceBroker(current).arbitrate(current, (item,))


def test_current_context_requires_all_declared_context_facets_to_match():
    current = binding()
    matching = candidate(
        current,
        "matching",
        scope_job_id="job-1",
        purpose_keys=("MAKE",),
        operating_contexts=("NORMAL",),
    )
    mismatched = candidate(
        current,
        "mismatched",
        scope_job_id="job-2",
        purpose_keys=("MAKE",),
        operating_contexts=("NORMAL",),
    )
    result = RelevanceBroker(current).arbitrate(current, (matching, mismatched))
    assert [item.semantic_key for item in result.surface_now] == ["matching"]
    assert result.surface_groups[0].band == "CURRENT_CONTEXT"
    other = decision(result, "mismatched")
    assert other.band == "BACKGROUND"
    assert "DIFFERENT_JOB" in other.reason_codes


def test_explicit_current_request_overrides_not_now_without_authority():
    current = binding()
    item = candidate(
        current,
        explicit_artist_request=True,
        artist_not_now=True,
    )
    result = RelevanceBroker(current).arbitrate(current, (item,))
    surfaced = result.surface_now[0]
    assert surfaced.band == "EXPLICIT_REQUEST"
    assert "NOT_NOW_OVERRIDDEN_FOR_EXPLICIT_REQUEST" in surfaced.reason_codes
    assert result.action_authority_granted is False
    assert result.mutation_authorized is False
    assert result.external_action_authorized is False
    assert result.authority_effect == "UNCHANGED"


def test_not_now_defers_ordinary_blocking_work():
    current = binding()
    item = candidate(current, blocks_next_step=True, artist_not_now=True)
    result = RelevanceBroker(current).arbitrate(current, (item,))
    held = result.held_decisions[0]
    assert held.band == "ARTIST_NOT_NOW"
    assert held.disposition == "DEFER"


def test_discussion_safety_fails_closed_for_unsolicited_work():
    current = binding()
    unsafe = candidate(current, "unsafe", discussion_state="UNSAFE", blocks_next_step=True)
    unknown = candidate(current, "unknown", discussion_state="UNKNOWN", blocks_next_step=True)
    result = RelevanceBroker(current).arbitrate(current, (unsafe, unknown))
    assert result.surface_now == ()
    assert decision(result, "unsafe").band == "UNSAFE_TO_DISCUSS"
    assert decision(result, "unsafe").disposition == "SUPPRESS"
    assert decision(result, "unknown").band == "DISCUSSION_UNKNOWN"


def test_required_current_evidence_holds_noncurrent_canonical_freshness():
    current = binding()
    values = tuple(
        candidate(
            current,
            state.lower(),
            requires_current_evidence=True,
            evidence_freshness=state,
            blocks_next_step=True,
        )
        for state in ("REVALIDATION_REQUIRED", "EXPIRED", "UNKNOWN")
    )
    result = RelevanceBroker(current).arbitrate(current, values)
    assert result.surface_now == ()
    assert {item.band for item in result.held_decisions} == {"NEEDS_FRESH_EVIDENCE"}


def test_revalidation_required_evidence_may_remain_descriptive_when_not_required():
    current = binding()
    item = candidate(
        current,
        evidence_freshness="REVALIDATION_REQUIRED",
        scope_job_id=current.job_id,
    )
    result = RelevanceBroker(current).arbitrate(current, (item,))
    surfaced = result.surface_now[0]
    assert surfaced.band == "CURRENT_CONTEXT"
    assert "EVIDENCE_REVALIDATION_REQUIRED" in surfaced.reason_codes


def test_normal_precedence_is_qualitative_not_scored():
    current = binding()
    values = (
        candidate(current, "future", scope_song_id=None, protects_future_option=True),
        candidate(current, "context", scope_job_id=current.job_id),
        candidate(current, "time", urgency="TIME_WINDOW"),
        candidate(current, "decision", changes_next_decision=True),
        candidate(current, "block", blocks_next_step=True),
    )
    result = RelevanceBroker(current).arbitrate(current, values)
    assert [item.semantic_key for item in result.surface_now] == ["block"]
    assert result.surface_groups[0].band == "BLOCKING"
    assert {
        item.semantic_key: item.band for item in result.held_decisions
    } == {
        "context": "CURRENT_CONTEXT",
        "decision": "DECISION_CHANGING",
        "future": "FUTURE_OPTION",
        "time": "TIME_SENSITIVE",
    }
    assert all(
        "LOWER_RELEVANCE_THAN_SURFACED_GROUP" in item.reason_codes
        for item in result.held_decisions
    )


def test_awaiting_artist_transaction_is_blocking_not_approval():
    current = binding()
    item = candidate(current, transaction_state="AWAITING_ARTIST")
    result = RelevanceBroker(current).arbitrate(current, (item,))
    surfaced = result.surface_now[0]
    assert surfaced.band == "BLOCKING"
    assert "TRANSACTION_AWAITING_ARTIST" in surfaced.reason_codes
    assert surfaced.action_authority_granted is False


def test_equal_band_remains_explicit_tie_with_canonical_only_order():
    current = binding()
    values = (
        candidate(current, "zeta", surface="SONG", blocks_next_step=True),
        candidate(current, "alpha", surface="NOW", blocks_next_step=True),
    )
    result = RelevanceBroker(current).arbitrate(current, values)
    group = result.surface_groups[0]
    assert group.band == "BLOCKING"
    assert group.tied is True
    assert group.ordering_semantics == "CANONICAL_ONLY_NOT_RELATIVE_RELEVANCE"
    assert [item.semantic_key for item in group.decisions] == ["alpha", "zeta"]
    assert result.has_tie is True


def test_mandatory_subjects_hold_all_normal_work():
    current = binding()
    explicit = candidate(current, "ask", explicit_artist_request=True)
    blocker = candidate(current, "block", blocks_next_step=True)
    result = RelevanceBroker(current).arbitrate(current, (blocker, explicit))
    assert [item.semantic_key for item in result.surface_now] == ["ask"]
    held = decision(result, "block")
    assert held.band == "BLOCKING"
    assert "MANDATORY_CONTEXT_ALREADY_SURFACED" in held.reason_codes


def test_background_is_a_truthful_no_surface_result():
    current = binding()
    item = candidate(current, scope_song_id=None)
    result = RelevanceBroker(current).arbitrate(current, (item,))
    assert result.surface_now == ()
    assert result.held_decisions[0].band == "BACKGROUND"
    assert "BACKGROUND_ONLY" in result.held_decisions[0].reason_codes


def test_empty_arbitration_is_valid_and_authority_free():
    current = binding()
    result = RelevanceBroker(current).arbitrate(current, ())
    assert result.surface_groups == ()
    assert result.held_decisions == ()
    assert result.surface_now == ()
    assert result.action_authority_granted is False


def test_candidate_and_binding_are_frozen_values():
    current = binding()
    item = candidate(current)
    with pytest.raises(FrozenInstanceError):
        current.song_id = "song-2"
    with pytest.raises(FrozenInstanceError):
        item.blocks_next_step = True
