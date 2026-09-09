from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from .authority import ApprovalBinding
from .egress import OutboundEnvelope, OutboundInspector, OutboundMaterial

MODEL_MODES = {"ON", "OFF"}
BILLING_MODES = {"NO_INCREMENTAL_COST", "INCLUDED", "METERED"}
MODEL_CAPABILITIES = {
    "TEXT",
    "STRUCTURED_OUTPUT",
    "TOOL_CALLS",
    "VISION",
    "AUDIO_INPUT",
}
CREDENTIAL_STATES = {"AVAILABLE", "INVALID", "REVOKED"}
MODEL_RUN_STATUSES = {
    "COMPLETE",
    "AI_OFF",
    "NO_PROFILE",
    "PROFILE_DISABLED",
    "COST_BLOCKED",
    "CAPABILITY_BLOCKED",
    "EGRESS_CONFIRMATION_REQUIRED",
    "STALE_EGRESS_CONFIRMATION",
    "CREDENTIAL_UNAVAILABLE",
    "ADAPTER_UNAVAILABLE",
    "PROVIDER_ERROR",
    "INVALID_OUTPUT",
}

_MAX_CONTEXT_ITEMS = 64
_MAX_CONTEXT_CHARS = 240_000
_MAX_INSTRUCTION_CHARS = 32_000


class ModelRuntimeError(ValueError):
    """Invalid model-substrate input or invariant."""


def _text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ModelRuntimeError(f"{field_name} must be text")
    text = value.strip()
    if not text:
        raise ModelRuntimeError(f"{field_name} must not be empty")
    return text


def _optional_text(value: object | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ModelRuntimeError(f"{field_name} must be boolean")
    return value


def _enum(value: object, field_name: str, allowed: set[str]) -> str:
    text = _text(value, field_name).upper().replace("-", "_").replace(" ", "_")
    if text not in allowed:
        raise ModelRuntimeError(f"unsupported {field_name}: {text}")
    return text


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ModelRuntimeError(f"{field_name} must be a tuple")
    result = tuple(_text(item, field_name) for item in value)
    if len(result) != len(set(result)):
        raise ModelRuntimeError(f"{field_name} must not contain duplicates")
    return result


def _json_copy(value: Mapping[str, object], field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ModelRuntimeError(f"{field_name} must be a mapping")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ModelRuntimeError(f"{field_name} must be JSON-compatible") from exc
    if not isinstance(decoded, dict):
        raise ModelRuntimeError(f"{field_name} must encode an object")
    return decoded


def _fingerprint(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ModelContextItem:
    """One bounded canonical context projection sent to a model.

    source_ref and revision_fingerprint stay owned by the source subsystem. The
    model layer receives a projection and never becomes another Artist/Song truth
    store.
    """

    item_id: str
    category: str
    source_ref: str
    revision_fingerprint: str
    content: str
    private: bool

    def __post_init__(self) -> None:
        for name in ("item_id", "category", "source_ref", "revision_fingerprint", "content"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "private", _bool(self.private, "private"))

    @property
    def content_fingerprint(self) -> str:
        return _fingerprint(
            {
                "source_ref": self.source_ref,
                "revision_fingerprint": self.revision_fingerprint,
                "content": self.content,
            }
        )


@dataclass(frozen=True)
class ModelJob:
    """Provider-neutral reasoning job over bounded canonical context."""

    job_id: str
    purpose: str
    instruction: str
    context: tuple[ModelContextItem, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    require_structured_output: bool = False
    instruction_private: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _text(self.job_id, "job_id"))
        object.__setattr__(self, "purpose", _text(self.purpose, "purpose"))
        instruction = _text(self.instruction, "instruction")
        if len(instruction) > _MAX_INSTRUCTION_CHARS:
            raise ModelRuntimeError("instruction exceeds bounded model input size")
        object.__setattr__(self, "instruction", instruction)
        context = tuple(self.context)
        if len(context) > _MAX_CONTEXT_ITEMS:
            raise ModelRuntimeError("context item count exceeds bounded model input size")
        if not all(isinstance(item, ModelContextItem) for item in context):
            raise TypeError("context must contain ModelContextItem values")
        ids = [item.item_id for item in context]
        if len(ids) != len(set(ids)):
            raise ModelRuntimeError("context item IDs must be unique")
        if sum(len(item.content) for item in context) > _MAX_CONTEXT_CHARS:
            raise ModelRuntimeError("context exceeds bounded model input size")
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "allowed_tools", _text_tuple(self.allowed_tools, "allowed_tools"))
        object.__setattr__(
            self,
            "require_structured_output",
            _bool(self.require_structured_output, "require_structured_output"),
        )
        object.__setattr__(
            self,
            "instruction_private",
            _bool(self.instruction_private, "instruction_private"),
        )

    @property
    def fingerprint(self) -> str:
        """Canonical job identity deliberately excludes provider/model selection."""
        return _fingerprint(
            {
                "job_id": self.job_id,
                "purpose": self.purpose,
                "instruction": self.instruction,
                "instruction_private": self.instruction_private,
                "context": [
                    {
                        "item_id": item.item_id,
                        "category": item.category,
                        "source_ref": item.source_ref,
                        "revision_fingerprint": item.revision_fingerprint,
                        "content_fingerprint": item.content_fingerprint,
                        "private": item.private,
                    }
                    for item in self.context
                ],
                "allowed_tools": list(self.allowed_tools),
                "require_structured_output": self.require_structured_output,
            }
        )


@dataclass(frozen=True)
class ModelProfile:
    """Non-secret provider/model preference.

    credential_ref is an opaque reference into a credential owner such as an OS
    keychain. Raw credentials never belong in this profile.
    """

    profile_id: str
    provider: str
    model: str
    adapter_id: str
    destination: str
    capabilities: tuple[str, ...] = ("TEXT",)
    credential_ref: str | None = None
    billing_mode: str = "NO_INCREMENTAL_COST"
    retention_statement: str = "Provider retention policy must be known before use."
    remote: bool = True
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "profile_id",
            "provider",
            "model",
            "adapter_id",
            "destination",
            "retention_statement",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "credential_ref",
            _optional_text(self.credential_ref, "credential_ref"),
        )
        object.__setattr__(
            self,
            "billing_mode",
            _enum(self.billing_mode, "billing_mode", BILLING_MODES),
        )
        capabilities = tuple(
            _enum(value, "model capability", MODEL_CAPABILITIES)
            for value in self.capabilities
        )
        if not capabilities or len(capabilities) != len(set(capabilities)):
            raise ModelRuntimeError("capabilities must be unique and non-empty")
        object.__setattr__(self, "capabilities", tuple(sorted(capabilities)))
        object.__setattr__(self, "remote", _bool(self.remote, "remote"))
        object.__setattr__(self, "enabled", _bool(self.enabled, "enabled"))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "profile_id": self.profile_id,
                "provider": self.provider,
                "model": self.model,
                "adapter_id": self.adapter_id,
                "destination": self.destination,
                "capabilities": list(self.capabilities),
                "credential_ref": self.credential_ref,
                "billing_mode": self.billing_mode,
                "retention_statement": self.retention_statement,
                "remote": self.remote,
                "enabled": self.enabled,
            }
        )


@dataclass(frozen=True)
class CredentialLease:
    """Ephemeral secret resolved only at call time."""

    secret: str = field(repr=False, compare=False)
    source_ref: str
    state: str = "AVAILABLE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "secret", _text(self.secret, "credential secret"))
        object.__setattr__(self, "source_ref", _text(self.source_ref, "credential source_ref"))
        object.__setattr__(
            self,
            "state",
            _enum(self.state, "credential state", CREDENTIAL_STATES),
        )


class CredentialResolver(Protocol):
    def resolve(self, credential_ref: str) -> CredentialLease | None: ...


@dataclass(frozen=True)
class ToolCallProposal:
    call_id: str
    tool_name: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "call_id", _text(self.call_id, "call_id"))
        object.__setattr__(self, "tool_name", _text(self.tool_name, "tool_name"))
        object.__setattr__(
            self,
            "arguments",
            _json_copy(self.arguments, "tool arguments"),
        )


@dataclass(frozen=True)
class AdapterResponse:
    text: str
    structured: Mapping[str, object] | None = None
    tool_calls: tuple[ToolCallProposal, ...] = ()
    evidence_ref: str = "model-response:adapter"

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _text(self.text, "response text"))
        if self.structured is not None:
            object.__setattr__(
                self,
                "structured",
                _json_copy(self.structured, "structured response"),
            )
        calls = tuple(self.tool_calls)
        if not all(isinstance(call, ToolCallProposal) for call in calls):
            raise TypeError("tool_calls must contain ToolCallProposal values")
        call_ids = [call.call_id for call in calls]
        if len(call_ids) != len(set(call_ids)):
            raise ModelRuntimeError("tool call IDs must be unique")
        object.__setattr__(self, "tool_calls", calls)
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "evidence_ref"))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "text": self.text,
                "structured": self.structured,
                "tool_calls": [
                    {
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                        "arguments": call.arguments,
                    }
                    for call in self.tool_calls
                ],
                "evidence_ref": self.evidence_ref,
            }
        )


class ModelAdapter(Protocol):
    adapter_id: str

    def invoke(
        self,
        job: ModelJob,
        profile: ModelProfile,
        credential: CredentialLease | None,
    ) -> AdapterResponse: ...


@dataclass(frozen=True)
class ModelRunReceipt:
    status: str
    job_id: str
    job_fingerprint: str
    profile_id: str | None
    profile_fingerprint: str | None
    provider: str | None
    model: str | None
    adapter_id: str | None
    credential_ref: str | None
    credential_source_ref: str | None
    egress_intent_fingerprint: str | None
    output_fingerprint: str | None
    evidence_ref: str | None
    reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _enum(self.status, "model run status", MODEL_RUN_STATUSES),
        )
        object.__setattr__(self, "job_id", _text(self.job_id, "job_id"))
        object.__setattr__(
            self,
            "job_fingerprint",
            _text(self.job_fingerprint, "job_fingerprint"),
        )
        for name in (
            "profile_id",
            "profile_fingerprint",
            "provider",
            "model",
            "adapter_id",
            "credential_ref",
            "credential_source_ref",
            "egress_intent_fingerprint",
            "output_fingerprint",
            "evidence_ref",
            "reason",
        ):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))


@dataclass(frozen=True)
class ModelRunResult:
    status: str
    output: AdapterResponse | None
    receipt: ModelRunReceipt

    def __post_init__(self) -> None:
        status = _enum(self.status, "model run status", MODEL_RUN_STATUSES)
        object.__setattr__(self, "status", status)
        if self.receipt.status != status:
            raise ModelRuntimeError("result and receipt status must match")
        if status == "COMPLETE" and self.output is None:
            raise ModelRuntimeError("COMPLETE model result requires output")
        if status != "COMPLETE" and self.output is not None:
            raise ModelRuntimeError("non-COMPLETE model result must not expose output")


class ModelRuntime:
    """Provider-neutral, authority-bounded model invocation substrate.

    Model output is proposal data only. This runtime exposes no canonical write,
    tool execution, DAW mutation, provider send beyond the model invocation, or
    truth-promotion method. Tool calls must be handed to their owning capability
    and authority path separately.
    """

    def __init__(
        self,
        *,
        profiles: tuple[ModelProfile, ...] = (),
        adapters: tuple[ModelAdapter, ...] = (),
        credential_resolver: CredentialResolver | None = None,
        allow_metered: bool = False,
    ) -> None:
        profile_items = tuple(profiles)
        if not all(isinstance(item, ModelProfile) for item in profile_items):
            raise TypeError("profiles must contain ModelProfile values")
        self._profiles = {item.profile_id: item for item in profile_items}
        if len(self._profiles) != len(profile_items):
            raise ModelRuntimeError("profile IDs must be unique")

        adapter_items = tuple(adapters)
        adapter_map: dict[str, ModelAdapter] = {}
        for adapter in adapter_items:
            adapter_id = _text(getattr(adapter, "adapter_id", None), "adapter.adapter_id")
            if not callable(getattr(adapter, "invoke", None)):
                raise TypeError("adapter must implement invoke")
            if adapter_id in adapter_map:
                raise ModelRuntimeError("adapter IDs must be unique")
            adapter_map[adapter_id] = adapter
        self._adapters = adapter_map
        self.credential_resolver = credential_resolver
        self.allow_metered = _bool(allow_metered, "allow_metered")

    def profile(self, profile_id: str) -> ModelProfile | None:
        return self._profiles.get(str(profile_id))

    @staticmethod
    def preview_egress(job: ModelJob, profile: ModelProfile) -> OutboundEnvelope:
        if not isinstance(job, ModelJob):
            raise TypeError("job must be ModelJob")
        if not isinstance(profile, ModelProfile):
            raise TypeError("profile must be ModelProfile")
        if not profile.remote:
            raise ModelRuntimeError("local model profiles do not cross an egress boundary")

        instruction_fp = _fingerprint({"instruction": job.instruction})
        materials = [
            OutboundMaterial(
                item_id="model-instruction",
                category="MODEL_INSTRUCTION",
                source_ref=f"model-job:{job.job_id}",
                revision_fingerprint=f"sha256:{instruction_fp}",
                private=job.instruction_private,
            )
        ]
        materials.extend(
            OutboundMaterial(
                item_id=f"context:{item.item_id}",
                category=item.category,
                source_ref=item.source_ref,
                revision_fingerprint=f"sha256:{item.content_fingerprint}",
                private=item.private,
            )
            for item in job.context
        )
        return OutboundEnvelope(
            request_id=f"model:{job.job_id}:{profile.profile_id}",
            job_id=job.job_id,
            description=f"Run bounded N0TE reasoning job with {profile.provider}/{profile.model}",
            destination=profile.destination,
            purpose=job.purpose,
            materials=tuple(materials),
            retention_statement=profile.retention_statement,
            cost_statement=f"billing_mode={profile.billing_mode}",
        )

    def _receipt(
        self,
        *,
        status: str,
        job: ModelJob,
        profile: ModelProfile | None = None,
        credential: CredentialLease | None = None,
        egress_intent_fingerprint: str | None = None,
        output: AdapterResponse | None = None,
        reason: str | None = None,
    ) -> ModelRunReceipt:
        return ModelRunReceipt(
            status=status,
            job_id=job.job_id,
            job_fingerprint=job.fingerprint,
            profile_id=None if profile is None else profile.profile_id,
            profile_fingerprint=None if profile is None else profile.fingerprint,
            provider=None if profile is None else profile.provider,
            model=None if profile is None else profile.model,
            adapter_id=None if profile is None else profile.adapter_id,
            credential_ref=None if profile is None else profile.credential_ref,
            credential_source_ref=None if credential is None else credential.source_ref,
            egress_intent_fingerprint=egress_intent_fingerprint,
            output_fingerprint=None if output is None else output.fingerprint,
            evidence_ref=None if output is None else output.evidence_ref,
            reason=reason,
        )

    def _blocked(
        self,
        status: str,
        job: ModelJob,
        *,
        profile: ModelProfile | None = None,
        credential: CredentialLease | None = None,
        egress_intent_fingerprint: str | None = None,
        reason: str,
    ) -> ModelRunResult:
        receipt = self._receipt(
            status=status,
            job=job,
            profile=profile,
            credential=credential,
            egress_intent_fingerprint=egress_intent_fingerprint,
            reason=reason,
        )
        return ModelRunResult(status, None, receipt)

    def run(
        self,
        job: ModelJob,
        *,
        mode: str,
        profile_id: str | None = None,
        egress_approval: ApprovalBinding | None = None,
    ) -> ModelRunResult:
        if not isinstance(job, ModelJob):
            raise TypeError("job must be ModelJob")
        mode = _enum(mode, "model mode", MODEL_MODES)
        if mode == "OFF":
            return self._blocked(
                "AI_OFF",
                job,
                reason="AI is explicitly off; no profile, credential, adapter or model was touched.",
            )

        if profile_id is None:
            return self._blocked(
                "NO_PROFILE",
                job,
                reason="No model profile was selected.",
            )
        profile = self.profile(_text(profile_id, "profile_id"))
        if profile is None:
            return self._blocked(
                "NO_PROFILE",
                job,
                reason="The selected model profile does not exist.",
            )
        if not profile.enabled:
            return self._blocked(
                "PROFILE_DISABLED",
                job,
                profile=profile,
                reason="The selected model profile is disabled.",
            )
        if profile.billing_mode == "METERED" and not self.allow_metered:
            return self._blocked(
                "COST_BLOCKED",
                job,
                profile=profile,
                reason="Metered model spend is not authorized by this runtime.",
            )

        required_capabilities = {"TEXT"}
        if job.allowed_tools:
            required_capabilities.add("TOOL_CALLS")
        if job.require_structured_output:
            required_capabilities.add("STRUCTURED_OUTPUT")
        missing_capabilities = required_capabilities - set(profile.capabilities)
        if missing_capabilities:
            return self._blocked(
                "CAPABILITY_BLOCKED",
                job,
                profile=profile,
                reason=f"Model profile lacks required capabilities: {sorted(missing_capabilities)}",
            )

        egress_intent_fingerprint: str | None = None
        if profile.remote:
            envelope = self.preview_egress(job, profile)
            egress_intent_fingerprint = envelope.to_action_intent().intent_fingerprint
            if egress_approval is None:
                return self._blocked(
                    "EGRESS_CONFIRMATION_REQUIRED",
                    job,
                    profile=profile,
                    egress_intent_fingerprint=egress_intent_fingerprint,
                    reason="Remote model use requires exact outbound confirmation.",
                )
            validation = OutboundInspector.validate_confirmation(envelope, egress_approval)
            if validation.status != "VALID":
                return self._blocked(
                    "STALE_EGRESS_CONFIRMATION",
                    job,
                    profile=profile,
                    egress_intent_fingerprint=egress_intent_fingerprint,
                    reason="Outbound confirmation no longer matches this exact model payload.",
                )

        credential: CredentialLease | None = None
        if profile.credential_ref is not None:
            if self.credential_resolver is None:
                return self._blocked(
                    "CREDENTIAL_UNAVAILABLE",
                    job,
                    profile=profile,
                    egress_intent_fingerprint=egress_intent_fingerprint,
                    reason="No credential resolver is available for the selected profile.",
                )
            try:
                credential = self.credential_resolver.resolve(profile.credential_ref)
            except Exception:
                credential = None
            if credential is None or not isinstance(credential, CredentialLease):
                return self._blocked(
                    "CREDENTIAL_UNAVAILABLE",
                    job,
                    profile=profile,
                    egress_intent_fingerprint=egress_intent_fingerprint,
                    reason="The selected credential reference could not be resolved.",
                )
            if credential.state != "AVAILABLE":
                return self._blocked(
                    "CREDENTIAL_UNAVAILABLE",
                    job,
                    profile=profile,
                    credential=credential,
                    egress_intent_fingerprint=egress_intent_fingerprint,
                    reason=f"The selected credential is {credential.state.lower()}.",
                )

        adapter = self._adapters.get(profile.adapter_id)
        if adapter is None:
            return self._blocked(
                "ADAPTER_UNAVAILABLE",
                job,
                profile=profile,
                credential=credential,
                egress_intent_fingerprint=egress_intent_fingerprint,
                reason="No adapter is installed for the selected model profile.",
            )

        try:
            output = adapter.invoke(job, profile, credential)
        except Exception as exc:
            return self._blocked(
                "PROVIDER_ERROR",
                job,
                profile=profile,
                credential=credential,
                egress_intent_fingerprint=egress_intent_fingerprint,
                reason=f"Model adapter failed truthfully: {type(exc).__name__}",
            )
        if not isinstance(output, AdapterResponse):
            return self._blocked(
                "INVALID_OUTPUT",
                job,
                profile=profile,
                credential=credential,
                egress_intent_fingerprint=egress_intent_fingerprint,
                reason="Model adapter returned an unsupported response type.",
            )
        if job.require_structured_output and output.structured is None:
            return self._blocked(
                "INVALID_OUTPUT",
                job,
                profile=profile,
                credential=credential,
                egress_intent_fingerprint=egress_intent_fingerprint,
                reason="The job required structured output but the adapter returned none.",
            )
        disallowed_tools = [
            call.tool_name for call in output.tool_calls if call.tool_name not in job.allowed_tools
        ]
        if disallowed_tools:
            return self._blocked(
                "INVALID_OUTPUT",
                job,
                profile=profile,
                credential=credential,
                egress_intent_fingerprint=egress_intent_fingerprint,
                reason=f"Model proposed tools outside the bounded job: {sorted(set(disallowed_tools))}",
            )
        if output.tool_calls and "TOOL_CALLS" not in profile.capabilities:
            return self._blocked(
                "INVALID_OUTPUT",
                job,
                profile=profile,
                credential=credential,
                egress_intent_fingerprint=egress_intent_fingerprint,
                reason="Model returned tool calls without a tool-capable profile contract.",
            )

        receipt = self._receipt(
            status="COMPLETE",
            job=job,
            profile=profile,
            credential=credential,
            egress_intent_fingerprint=egress_intent_fingerprint,
            output=output,
        )
        return ModelRunResult("COMPLETE", output, receipt)
