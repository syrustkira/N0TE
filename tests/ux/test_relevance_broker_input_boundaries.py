import inspect

import pytest

from n0te.evidence_freshness import FRESHNESS_STATES
from n0te.relevance_broker import (
    EVIDENCE_FRESHNESS_STATES,
    RelevanceArbitration,
    RelevanceBroker,
    RelevanceBrokerError,
    RelevanceCandidate,
    RelevanceContextBinding,
    RelevanceDecision,
    UnsupportedRelevancePolicyError,
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


def candidate(ctx, **changes):
    values = {
        "semantic_key": "subject",
        "surface": "NOW",
        "binding_fingerprint": ctx.fingerprint,
        "scope_profile_id": ctx.profile_id,
        "scope_artist_id": ctx.artist_id,
        "scope_song_id": ctx.song_id,
    }
    values.update(changes)
    return RelevanceCandidate(**values)


def test_relevance_consumes_exact_canonical_freshness_vocabulary():
    assert set(EVIDENCE_FRESHNESS_STATES) == FRESHNESS_STATES
    assert set(EVIDENCE_FRESHNESS_STATES) == {
        "CURRENT",
        "REVALIDATION_REQUIRED",
        "EXPIRED",
        "UNKNOWN",
    }
    ctx = binding()
    for state in EVIDENCE_FRESHNESS_STATES:
        assert candidate(ctx, evidence_freshness=state).evidence_freshness == state
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, evidence_freshness="STALE")


@pytest.mark.parametrize("field_name", ("profile_id", "artist_id"))
def test_required_binding_ids_reject_non_text(field_name):
    with pytest.raises(RelevanceBrokerError):
        binding(**{field_name: None})
    with pytest.raises(RelevanceBrokerError):
        binding(**{field_name: 7})


@pytest.mark.parametrize(
    "field_name",
    (
        "song_id",
        "version_id",
        "session_id",
        "focus_id",
        "workspace_id",
        "job_id",
        "purpose_key",
    ),
)
def test_optional_binding_ids_reject_non_text(field_name):
    with pytest.raises(RelevanceBrokerError):
        binding(**{field_name: 7})


def test_nested_song_context_requires_song_binding():
    for field_name in ("version_id", "session_id", "focus_id", "workspace_id"):
        values = {
            "song_id": None,
            "version_id": None,
            "session_id": None,
            "focus_id": None,
            "workspace_id": None,
        }
        values[field_name] = f"{field_name}-1"
        with pytest.raises(RelevanceBrokerError):
            binding(**values)


def test_schema_version_rejects_bool_and_unknown_future_version():
    with pytest.raises(RelevanceBrokerError):
        binding(schema_version=True)
    with pytest.raises(UnsupportedRelevancePolicyError):
        binding(schema_version=2)


@pytest.mark.parametrize("value", (None, 7, True, "concert"))
def test_operating_context_requires_supported_text(value):
    with pytest.raises(RelevanceBrokerError):
        binding(operating_context=value)


@pytest.mark.parametrize(
    "field_name",
    (
        "semantic_key",
        "surface",
        "binding_fingerprint",
        "scope_profile_id",
        "scope_artist_id",
    ),
)
def test_candidate_required_text_fields_are_strict(field_name):
    ctx = binding()
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, **{field_name: None})
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, **{field_name: 3})


@pytest.mark.parametrize("field_name", ("scope_song_id", "scope_job_id"))
def test_candidate_optional_scope_fields_are_strict(field_name):
    ctx = binding()
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, **{field_name: 3})


@pytest.mark.parametrize(
    "field_name",
    (
        "blocks_next_step",
        "protects_future_option",
        "changes_next_decision",
        "explicit_artist_request",
        "artist_not_now",
        "requires_current_evidence",
    ),
)
def test_candidate_semantic_flags_require_exact_bool(field_name):
    ctx = binding()
    for bad in (0, 1, "true", None):
        with pytest.raises(RelevanceBrokerError):
            candidate(ctx, **{field_name: bad})


def test_context_selector_collections_require_unique_text_tuples():
    ctx = binding()
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, purpose_keys=["MAKE"])
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, purpose_keys=("MAKE", "MAKE"))
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, purpose_keys=(1,))
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, operating_contexts=["NORMAL"])
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, operating_contexts=("NORMAL", "NORMAL"))


@pytest.mark.parametrize(
    "field_name,bad_value",
    (
        ("urgency", "SOONISH"),
        ("evidence_freshness", "MAYBE"),
        ("discussion_state", "KINDA_SAFE"),
        ("transaction_state", "DONE"),
        ("required_alert_kind", "FYI"),
    ),
)
def test_candidate_enums_reject_unknown_values(field_name, bad_value):
    ctx = binding()
    with pytest.raises(RelevanceBrokerError):
        candidate(ctx, **{field_name: bad_value})


def test_candidate_enum_fields_do_not_coerce_non_text():
    ctx = binding()
    for field_name in (
        "urgency",
        "evidence_freshness",
        "discussion_state",
        "transaction_state",
        "required_alert_kind",
    ):
        with pytest.raises(RelevanceBrokerError):
            candidate(ctx, **{field_name: 1})


def test_arbitration_rejects_duplicate_semantic_keys():
    ctx = binding()
    values = (candidate(ctx), candidate(ctx, surface="SONG"))
    with pytest.raises(RelevanceBrokerError):
        RelevanceBroker(ctx).arbitrate(ctx, values)


def test_arbitration_rejects_non_candidate_values():
    ctx = binding()
    with pytest.raises(TypeError):
        RelevanceBroker(ctx).arbitrate(ctx, ("not-a-candidate",))


def test_arbitration_requires_exact_binding_type():
    ctx = binding()
    with pytest.raises(TypeError):
        RelevanceBroker(ctx).arbitrate("not-a-binding", ())


def test_broker_constructor_requires_binding_type():
    with pytest.raises(TypeError):
        RelevanceBroker("not-a-binding")


def test_decision_authority_fields_cannot_be_forged_via_constructor():
    kwargs = {
        "semantic_key": "subject",
        "surface": "NOW",
        "disposition": "HOLD",
        "band": "BACKGROUND",
        "reason_codes": ("BACKGROUND_ONLY",),
        "binding_fingerprint": "fingerprint",
    }
    for field_name, value in (
        ("authority_effect", "GRANTED"),
        ("action_authority_granted", True),
        ("mutation_authorized", True),
        ("external_action_authorized", True),
        ("policy_version", 99),
    ):
        with pytest.raises(TypeError):
            RelevanceDecision(**kwargs, **{field_name: value})


def test_arbitration_authority_fields_cannot_be_forged_via_constructor():
    kwargs = {
        "binding_fingerprint": "fingerprint",
        "surface_groups": (),
        "held_decisions": (),
    }
    for field_name, value in (
        ("authority_effect", "GRANTED"),
        ("action_authority_granted", True),
        ("mutation_authorized", True),
        ("external_action_authorized", True),
        ("policy_version", 99),
    ):
        with pytest.raises(TypeError):
            RelevanceArbitration(**kwargs, **{field_name: value})


def test_result_validation_rejects_unexplained_or_unknown_decisions():
    with pytest.raises(RelevanceBrokerError):
        RelevanceDecision(
            semantic_key="subject",
            surface="NOW",
            disposition="HOLD",
            band="BACKGROUND",
            reason_codes=(),
            binding_fingerprint="fingerprint",
        )
    with pytest.raises(RelevanceBrokerError):
        RelevanceDecision(
            semantic_key="subject",
            surface="NOW",
            disposition="MAYBE",
            band="BACKGROUND",
            reason_codes=("WHY",),
            binding_fingerprint="fingerprint",
        )


def test_public_candidate_contract_has_no_numeric_relevance_or_confidence_knob():
    signature = inspect.signature(RelevanceCandidate)
    assert "score" not in signature.parameters
    assert "relevance_score" not in signature.parameters
    assert "confidence" not in signature.parameters
    ctx = binding()
    with pytest.raises(TypeError):
        RelevanceCandidate(
            semantic_key="subject",
            surface="NOW",
            binding_fingerprint=ctx.fingerprint,
            scope_profile_id=ctx.profile_id,
            scope_artist_id=ctx.artist_id,
            scope_song_id=ctx.song_id,
            score=0.99,
        )
