from __future__ import annotations

from dataclasses import dataclass, field

from n0te.cloudflare_workers_ai import (
    CloudflareWorkersAIAdapter,
    CloudflareWorkersAIError,
)
from n0te.egress import OutboundInspector
from n0te.llm import (
    CredentialLease,
    ModelContextItem,
    ModelJob,
    ModelProfile,
    ModelRuntime,
)


@dataclass
class RecordingTransport:
    response: object
    calls: list[dict[str, object]] = field(default_factory=list)
    error: Exception | None = None

    def post_json(self, *, url, headers, body, timeout_seconds):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": dict(body),
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


class Resolver:
    def __init__(self, lease):
        self.lease = lease
        self.calls = []

    def resolve(self, credential_ref):
        self.calls.append(credential_ref)
        return self.lease


def cloudflare_profile(*, destination=None, billing_mode="NO_INCREMENTAL_COST"):
    return ModelProfile(
        profile_id="profile:cloudflare-free",
        provider="cloudflare-workers-ai",
        model="@cf/example/free-plan-model",
        adapter_id=CloudflareWorkersAIAdapter.adapter_id,
        destination=destination
        or "https://api.cloudflare.com/client/v4/accounts/account-123/ai/v1/chat/completions",
        capabilities=("TEXT", "STRUCTURED_OUTPUT", "TOOL_CALLS"),
        credential_ref="os-keychain:cloudflare-workers-ai",
        billing_mode=billing_mode,
        retention_statement="Use only after the artist approves this provider egress policy.",
        remote=True,
    )


def reasoning_job(*, allowed_tools=()):
    return ModelJob(
        job_id="artist-song:cloud-proof",
        purpose="Assess the active Song without mutating it.",
        instruction="Return a JSON object with one proposed next move and its evidence basis.",
        context=(
            ModelContextItem(
                "active-song",
                "ARTIST_SONG_CONTEXT",
                "song:song_123",
                "song-revision:11",
                "The chorus is the current focus; approved vocal direction must be preserved.",
                True,
            ),
        ),
        allowed_tools=tuple(allowed_tools),
        require_structured_output=True,
        instruction_private=True,
    )


def approved_run(runtime, current_job, profile):
    envelope = runtime.preview_egress(current_job, profile)
    approval = OutboundInspector.bind_confirmation(
        envelope,
        "artist-confirmation:cloudflare-model-egress",
    )
    return runtime.run(
        current_job,
        mode="ON",
        profile_id=profile.profile_id,
        egress_approval=approval,
    )


def test_cloudflare_adapter_runs_bounded_structured_job_through_model_runtime():
    raw_secret = "cloudflare-token-must-stay-ephemeral"
    transport = RecordingTransport(
        {
            "id": "chatcmpl-n0te-1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"next_move":"Inspect chorus contrast","basis":"active-song"}',
                    }
                }
            ],
        }
    )
    lease = CredentialLease(raw_secret, "os-keychain:lease:cloudflare")
    resolver = Resolver(lease)
    adapter = CloudflareWorkersAIAdapter(transport=transport, timeout_seconds=12)
    profile = cloudflare_profile()
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(adapter,),
        credential_resolver=resolver,
    )
    current_job = reasoning_job()

    result = approved_run(runtime, current_job, profile)

    assert result.status == "COMPLETE"
    assert result.output is not None
    assert result.output.structured == {
        "next_move": "Inspect chorus contrast",
        "basis": "active-song",
    }
    assert result.receipt.evidence_ref == "cloudflare-workers-ai:chatcmpl-n0te-1"
    assert result.receipt.credential_ref == "os-keychain:cloudflare-workers-ai"
    assert result.receipt.credential_source_ref == "os-keychain:lease:cloudflare"
    assert raw_secret not in repr(result)
    assert raw_secret not in repr(result.receipt)

    assert resolver.calls == ["os-keychain:cloudflare-workers-ai"]
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == profile.destination
    assert call["headers"]["Authorization"] == f"Bearer {raw_secret}"
    assert call["timeout_seconds"] == 12.0
    body = call["body"]
    assert body["model"] == profile.model
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert raw_secret not in repr(body)
    user_message = body["messages"][1]["content"]
    assert "song:song_123" in user_message
    assert "song-revision:11" in user_message
    assert "approved vocal direction" in user_message


def test_cloudflare_adapter_rejects_non_cloudflare_or_credential_bearing_destination():
    adapter = CloudflareWorkersAIAdapter(
        transport=RecordingTransport({"choices": []})
    )
    lease = CredentialLease("secret", "os-keychain:lease")
    current_job = reasoning_job()

    for destination in (
        "https://example.com/client/v4/accounts/a/ai/v1/chat/completions",
        "http://api.cloudflare.com/client/v4/accounts/a/ai/v1/chat/completions",
        "https://user:pass@api.cloudflare.com/client/v4/accounts/a/ai/v1/chat/completions",
        "https://api.cloudflare.com/client/v4/accounts/a/ai/v1/chat/completions?token=bad",
        "https://api.cloudflare.com/client/v4/accounts/a/ai/run/model",
    ):
        profile = cloudflare_profile(destination=destination)
        try:
            adapter.invoke(current_job, profile, lease)
        except CloudflareWorkersAIError:
            pass
        else:
            raise AssertionError(f"unsafe Workers AI destination accepted: {destination}")


def test_cloudflare_adapter_keeps_tool_use_fail_closed_until_exact_schema_binding_exists():
    raw_secret = "tool-test-secret"
    transport = RecordingTransport(
        {
            "id": "should-not-run",
            "choices": [{"message": {"content": "{}"}}],
        }
    )
    profile = cloudflare_profile()
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(CloudflareWorkersAIAdapter(transport=transport),),
        credential_resolver=Resolver(
            CredentialLease(raw_secret, "os-keychain:tool-test")
        ),
    )
    current_job = reasoning_job(allowed_tools=("song.inspect",))

    result = approved_run(runtime, current_job, profile)

    assert result.status == "PROVIDER_ERROR"
    assert result.output is None
    assert result.receipt.reason == "Model adapter failed truthfully: CloudflareWorkersAIError"
    assert transport.calls == []
    assert raw_secret not in repr(result)


def test_cloudflare_provider_and_malformed_output_fail_truthfully_without_secret_leakage():
    raw_secret = "provider-failure-secret"
    profile = cloudflare_profile()
    current_job = reasoning_job()

    failing_transport = RecordingTransport(
        {},
        error=CloudflareWorkersAIError("Workers AI returned HTTP 503."),
    )
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(CloudflareWorkersAIAdapter(transport=failing_transport),),
        credential_resolver=Resolver(
            CredentialLease(raw_secret, "os-keychain:provider-failure")
        ),
    )
    failed = approved_run(runtime, current_job, profile)
    assert failed.status == "PROVIDER_ERROR"
    assert failed.receipt.reason == "Model adapter failed truthfully: CloudflareWorkersAIError"
    assert raw_secret not in repr(failed)

    malformed_transport = RecordingTransport(
        {
            "id": "chatcmpl-malformed",
            "choices": [{"message": {"content": "not-json"}}],
        }
    )
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(CloudflareWorkersAIAdapter(transport=malformed_transport),),
        credential_resolver=Resolver(
            CredentialLease(raw_secret, "os-keychain:malformed")
        ),
    )
    malformed = approved_run(runtime, current_job, profile)
    assert malformed.status == "PROVIDER_ERROR"
    assert malformed.output is None
    assert raw_secret not in repr(malformed)


def test_cloudflare_adapter_never_turns_provider_tool_calls_into_execution():
    raw_secret = "unexpected-tool-secret"
    transport = RecordingTransport(
        {
            "id": "chatcmpl-unexpected-tool",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "song.write",
                                    "arguments": '{"value":"overwrite"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
    )
    profile = cloudflare_profile()
    runtime = ModelRuntime(
        profiles=(profile,),
        adapters=(CloudflareWorkersAIAdapter(transport=transport),),
        credential_resolver=Resolver(
            CredentialLease(raw_secret, "os-keychain:unexpected-tool")
        ),
    )

    result = approved_run(runtime, reasoning_job(), profile)

    assert result.status == "PROVIDER_ERROR"
    assert result.output is None
    assert raw_secret not in repr(result)
    assert len(transport.calls) == 1
