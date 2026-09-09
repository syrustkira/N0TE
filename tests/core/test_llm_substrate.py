from __future__ import annotations

from dataclasses import replace

import pytest

from n0te.egress import OutboundInspector
from n0te.llm import (
    AdapterResponse,
    CredentialLease,
    ModelContextItem,
    ModelJob,
    ModelProfile,
    ModelRuntime,
    ModelRuntimeError,
    ToolCallProposal,
)


class Resolver:
    def __init__(self, leases=None):
        self.leases = dict(leases or {})
        self.calls = []

    def resolve(self, credential_ref):
        self.calls.append(credential_ref)
        return self.leases.get(credential_ref)


class RecordingAdapter:
    def __init__(self, adapter_id="adapter:test", *, response=None, fail=False):
        self.adapter_id = adapter_id
        self.response = response or AdapterResponse(
            "Prepared a bounded proposal.",
            structured={"status": "PROPOSED"},
            tool_calls=(
                ToolCallProposal(
                    "call:1",
                    "song.inspect",
                    {"scope": "current-song"},
                ),
            ),
            evidence_ref="adapter:test:response",
        )
        self.fail = fail
        self.calls = []

    def invoke(self, job, profile, credential):
        self.calls.append((job, profile, credential))
        if self.fail:
            raise RuntimeError("provider unavailable")
        return self.response


def job(*, instruction="Assess the active Song and propose the next bounded move."):
    return ModelJob(
        job_id="artist-song:next-move",
        purpose="Help the artist decide what to do next on the active Song.",
        instruction=instruction,
        context=(
            ModelContextItem(
                "active-song",
                "ARTIST_SONG_CONTEXT",
                "song:song_123",
                "song-revision:7",
                "Song is in chorus-production work; preserve the approved vocal direction.",
                True,
            ),
        ),
        allowed_tools=("song.inspect",),
        require_structured_output=True,
        instruction_private=True,
    )


def remote_profile(
    profile_id="profile:a",
    *,
    adapter_id="adapter:test",
    credential_ref="os-keychain:model-a",
    model="model-a",
    billing_mode="NO_INCREMENTAL_COST",
    enabled=True,
):
    return ModelProfile(
        profile_id=profile_id,
        provider="provider-test",
        model=model,
        adapter_id=adapter_id,
        destination="https://provider.invalid/model",
        capabilities=("TEXT", "STRUCTURED_OUTPUT", "TOOL_CALLS"),
        credential_ref=credential_ref,
        billing_mode=billing_mode,
        retention_statement="No retention for this acceptance fixture.",
        remote=True,
        enabled=enabled,
    )


def approval_for(runtime, current_job, profile):
    envelope = runtime.preview_egress(current_job, profile)
    return OutboundInspector.bind_confirmation(
        envelope,
        "artist-confirmation:model-egress",
    )


def test_ai_off_is_coherent_and_touches_no_profile_credential_or_adapter():
    resolver = Resolver(
        {"os-keychain:model-a": CredentialLease("secret-value", "keychain:test")}
    )
    adapter = RecordingAdapter()
    runtime = ModelRuntime(
        profiles=(remote_profile(),),
        adapters=(adapter,),
        credential_resolver=resolver,
    )

    result = runtime.run(job(), mode="OFF", profile_id="profile:a")

    assert result.status == "AI_OFF"
    assert result.output is None
    assert result.receipt.profile_id is None
    assert result.receipt.reason.startswith("AI is explicitly off")
    assert resolver.calls == []
    assert adapter.calls == []


def test_remote_job_requires_exact_outbound_confirmation_before_credential_or_model():
    resolver = Resolver(
        {"os-keychain:model-a": CredentialLease("secret-value", "keychain:test")}
    )
    adapter = RecordingAdapter()
    profile = remote_profile()
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(adapter,),
        credential_resolver=resolver,
    )
    current_job = job()

    missing = runtime.run(current_job, mode="ON", profile_id=profile.profile_id)
    assert missing.status == "EGRESS_CONFIRMATION_REQUIRED"
    assert resolver.calls == []
    assert adapter.calls == []

    approval = approval_for(runtime, current_job, profile)
    changed_job = job(instruction="Assess a different exact instruction.")
    stale = runtime.run(
        changed_job,
        mode="ON",
        profile_id=profile.profile_id,
        egress_approval=approval,
    )
    assert stale.status == "STALE_EGRESS_CONFIRMATION"
    assert resolver.calls == []
    assert adapter.calls == []


def test_zero_new_cash_default_blocks_metered_profile_before_credential_or_adapter():
    resolver = Resolver(
        {"os-keychain:model-a": CredentialLease("secret-value", "keychain:test")}
    )
    adapter = RecordingAdapter()
    profile = remote_profile(billing_mode="METERED")
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(adapter,),
        credential_resolver=resolver,
    )

    result = runtime.run(job(), mode="ON", profile_id=profile.profile_id)

    assert result.status == "COST_BLOCKED"
    assert "Metered model spend is not authorized" in result.receipt.reason
    assert resolver.calls == []
    assert adapter.calls == []


def test_raw_credential_is_ephemeral_and_never_enters_profile_or_receipt_repr():
    raw_secret = "sk-test-do-not-persist"
    lease = CredentialLease(raw_secret, "os-keychain:lease:1")
    assert raw_secret not in repr(lease)

    resolver = Resolver({"os-keychain:model-a": lease})
    adapter = RecordingAdapter()
    profile = remote_profile()
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(adapter,),
        credential_resolver=resolver,
    )
    current_job = job()
    result = runtime.run(
        current_job,
        mode="ON",
        profile_id=profile.profile_id,
        egress_approval=approval_for(runtime, current_job, profile),
    )

    assert result.status == "COMPLETE"
    assert adapter.calls[0][2] is lease
    assert result.receipt.credential_ref == "os-keychain:model-a"
    assert result.receipt.credential_source_ref == "os-keychain:lease:1"
    assert raw_secret not in repr(profile)
    assert raw_secret not in repr(result.receipt)
    assert raw_secret not in repr(result)


def test_invalid_or_revoked_credentials_fail_truthfully_without_model_call():
    for state in ("INVALID", "REVOKED"):
        profile = remote_profile()
        resolver = Resolver(
            {
                "os-keychain:model-a": CredentialLease(
                    "not-usable",
                    f"os-keychain:{state.lower()}",
                    state,
                )
            }
        )
        adapter = RecordingAdapter()
        runtime = ModelRuntime(
            profiles=(profile,),
            adapters=(adapter,),
            credential_resolver=resolver,
        )
        current_job = job()

        result = runtime.run(
            current_job,
            mode="ON",
            profile_id=profile.profile_id,
            egress_approval=approval_for(runtime, current_job, profile),
        )

        assert result.status == "CREDENTIAL_UNAVAILABLE"
        assert state.lower() in result.receipt.reason
        assert adapter.calls == []


def test_same_canonical_job_swaps_profiles_without_migrating_semantics():
    first_adapter = RecordingAdapter("adapter:first")
    second_adapter = RecordingAdapter(
        "adapter:second",
        response=AdapterResponse(
            "Second model reached the same bounded job.",
            structured={"status": "PROPOSED", "variant": 2},
            tool_calls=(
                ToolCallProposal("call:2", "song.inspect", {"scope": "current-song"}),
            ),
            evidence_ref="adapter:second:response",
        ),
    )
    first = remote_profile(
        "profile:first",
        adapter_id="adapter:first",
        credential_ref="key:first",
        model="model-first",
    )
    second = remote_profile(
        "profile:second",
        adapter_id="adapter:second",
        credential_ref="key:second",
        model="model-second",
    )
    resolver = Resolver(
        {
            "key:first": CredentialLease("first-secret", "keychain:first"),
            "key:second": CredentialLease("second-secret", "keychain:second"),
        }
    )
    runtime = ModelRuntime(
        profiles=(first, second),
        adapters=(first_adapter, second_adapter),
        credential_resolver=resolver,
    )
    current_job = job()
    job_fp = current_job.fingerprint

    first_result = runtime.run(
        current_job,
        mode="ON",
        profile_id=first.profile_id,
        egress_approval=approval_for(runtime, current_job, first),
    )
    second_result = runtime.run(
        current_job,
        mode="ON",
        profile_id=second.profile_id,
        egress_approval=approval_for(runtime, current_job, second),
    )

    assert first_result.status == second_result.status == "COMPLETE"
    assert first_result.receipt.job_fingerprint == second_result.receipt.job_fingerprint == job_fp
    assert first_result.receipt.profile_id != second_result.receipt.profile_id
    assert first_result.receipt.model != second_result.receipt.model
    assert current_job.fingerprint == job_fp


def test_model_tool_calls_are_bounded_proposals_not_execution_authority():
    profile = remote_profile()
    output = AdapterResponse(
        "I propose an allowed read and an unowned mutation.",
        structured={"status": "PROPOSED"},
        tool_calls=(
            ToolCallProposal("call:ok", "song.inspect", {}),
            ToolCallProposal("call:bad", "song.canonical_write", {"value": "overwrite"}),
        ),
        evidence_ref="adapter:unsafe-proposal",
    )
    adapter = RecordingAdapter(response=output)
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(adapter,),
        credential_resolver=Resolver(
            {"os-keychain:model-a": CredentialLease("secret", "keychain:test")}
        ),
    )
    current_job = job()

    result = runtime.run(
        current_job,
        mode="ON",
        profile_id=profile.profile_id,
        egress_approval=approval_for(runtime, current_job, profile),
    )

    assert result.status == "INVALID_OUTPUT"
    assert result.output is None
    assert "song.canonical_write" in result.receipt.reason
    public_methods = {
        name
        for name in dir(ModelRuntime)
        if not name.startswith("_") and callable(getattr(ModelRuntime, name))
    }
    for forbidden in ("execute_tool", "write_truth", "mutate", "commit", "publish"):
        assert forbidden not in public_methods


def test_structured_output_contract_and_provider_failure_are_truthful():
    profile = remote_profile()
    no_structured_adapter = RecordingAdapter(
        response=AdapterResponse(
            "Plain text only.",
            structured=None,
            evidence_ref="adapter:plain",
        )
    )
    resolver = Resolver(
        {"os-keychain:model-a": CredentialLease("secret", "keychain:test")}
    )
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(no_structured_adapter,),
        credential_resolver=resolver,
    )
    current_job = job()
    result = runtime.run(
        current_job,
        mode="ON",
        profile_id=profile.profile_id,
        egress_approval=approval_for(runtime, current_job, profile),
    )
    assert result.status == "INVALID_OUTPUT"
    assert "required structured output" in result.receipt.reason

    failing = RecordingAdapter(fail=True)
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(failing,),
        credential_resolver=resolver,
    )
    result = runtime.run(
        current_job,
        mode="ON",
        profile_id=profile.profile_id,
        egress_approval=approval_for(runtime, current_job, profile),
    )
    assert result.status == "PROVIDER_ERROR"
    assert result.output is None
    assert result.receipt.reason == "Model adapter failed truthfully: RuntimeError"


def test_local_profile_needs_no_egress_or_credential_and_preserves_same_job_contract():
    adapter = RecordingAdapter("adapter:local")
    profile = ModelProfile(
        profile_id="profile:local",
        provider="local-compatible",
        model="local-model",
        adapter_id="adapter:local",
        destination="local://model",
        capabilities=("TEXT", "STRUCTURED_OUTPUT", "TOOL_CALLS"),
        credential_ref=None,
        billing_mode="NO_INCREMENTAL_COST",
        retention_statement="Local model receives no network egress.",
        remote=False,
    )
    runtime = ModelRuntime(profiles=(profile,), adapters=(adapter,))

    result = runtime.run(job(), mode="ON", profile_id=profile.profile_id)

    assert result.status == "COMPLETE"
    assert result.receipt.egress_intent_fingerprint is None
    assert result.receipt.credential_ref is None
    assert adapter.calls[0][2] is None


def test_context_is_bounded_and_profile_capability_mismatch_fails_before_adapter():
    oversized = "x" * 240_001
    with pytest.raises(ModelRuntimeError):
        ModelJob(
            "job:too-large",
            "boundedness test",
            "Inspect bounded context.",
            context=(
                ModelContextItem(
                    "huge",
                    "CONTEXT",
                    "source:huge",
                    "rev:1",
                    oversized,
                    True,
                ),
            ),
        )

    profile = replace(
        remote_profile(),
        capabilities=("TEXT",),
    )
    adapter = RecordingAdapter()
    runtime = ModelRuntime(profiles=(profile,), adapters=(adapter,))
    result = runtime.run(job(), mode="ON", profile_id=profile.profile_id)
    assert result.status == "CAPABILITY_BLOCKED"
    assert adapter.calls == []
