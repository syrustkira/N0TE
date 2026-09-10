from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Generic, Iterable, Mapping, Protocol, TypeVar
from urllib.parse import urlparse

from mcp import Client

from governance.execution_permit import ExecutionPermitAuthority
from governance.trusted_context import TrustedContextSnapshot

from .audio_engineering import EngineeringSnapshot
from .authority import ActionIntent
from .host_observation import HostObservationBinding
from .network import NetworkPolicy, NetworkRoute, TransportDecision
from .reference_calibration import (
    RankedReferenceCandidate,
    ReferenceCalibrationProfile,
    ReferenceDiscoveryProvider,
    discover_ranked_references,
)
from .session_reference_calibration import build_session_reference_evidence_bundle
from .shadow import HostShadowState

T = TypeVar("T")
DEFAULT_COORDINATOR_MCP_ENDPOINT = "http://127.0.0.1:8000/mcp"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ContextProvider(Protocol):
    def get(self, snapshot_id: str) -> TrustedContextSnapshot: ...


class CoordinatorGatewayError(RuntimeError):
    """A coordinator attempted to execute outside an owned gateway boundary."""


class ReferenceDiscoveryGatewayError(CoordinatorGatewayError):
    """A read-only reference provider could not be used under current policy."""


class CoordinatorReferenceClientError(CoordinatorGatewayError):
    """A host-side read-only reference request failed its coordinator boundary."""


class MutationVerificationError(CoordinatorGatewayError):
    """The mutation may have happened, but fresh acceptance evidence did not verify it."""


class MutationReconciliationError(CoordinatorGatewayError):
    """The mutation verified externally, but canonical owning state was not reconciled."""


@dataclass(frozen=True)
class ReferenceDiscoveryExecution:
    provider_id: str
    route_id: str
    route_kind: str
    registration_source_ref: str
    transport_reason_codes: tuple[str, ...]
    ranked: tuple[RankedReferenceCandidate, ...]
    read_only: bool = True
    action_authority_granted: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "provider_id",
            "route_id",
            "route_kind",
            "registration_source_ref",
        ):
            value = str(getattr(self, field_name)).strip()
            if not value:
                raise ReferenceDiscoveryGatewayError(
                    f"reference discovery {field_name} must not be empty"
                )
            object.__setattr__(self, field_name, value)
        reasons = tuple(str(item).strip() for item in self.transport_reason_codes)
        if any(not item for item in reasons):
            raise ReferenceDiscoveryGatewayError(
                "transport_reason_codes must contain non-empty values"
            )
        if self.read_only is not True or self.action_authority_granted is not False:
            raise ReferenceDiscoveryGatewayError(
                "reference discovery gateway may expose only read-only execution without action authority"
            )
        object.__setattr__(self, "transport_reason_codes", reasons)
        object.__setattr__(self, "ranked", tuple(self.ranked))


@dataclass(frozen=True)
class _ReferenceProviderRegistration:
    provider: ReferenceDiscoveryProvider
    route: NetworkRoute
    registration_source_ref: str


class ReferenceDiscoveryGateway:
    """Read-only provider execution under explicit connectivity and registration.

    The gateway is deliberately separate from ModelRuntime and the mutation gateway.
    A trusted runtime/bootstrap layer registers provider implementations. Callers may
    request discovery through those providers, but cannot register arbitrary code,
    gain DAW/provider mutation authority, or persist a reference merely by searching.

    NetworkPolicy decides only whether the provider route is transport-eligible.
    Provider registration is separate provenance showing which runtime capability was
    admitted. Returned candidates are still calibrated deterministically by N0TE.
    """

    def __init__(self, *, network_policy: NetworkPolicy):
        if not isinstance(network_policy, NetworkPolicy):
            raise TypeError("network_policy must be NetworkPolicy")
        self._network_policy = network_policy
        self._providers: dict[str, _ReferenceProviderRegistration] = {}

    @property
    def network_mode(self) -> str:
        return self._network_policy.mode

    @property
    def registered_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def register(
        self,
        provider_id: str,
        *,
        provider: ReferenceDiscoveryProvider,
        route: NetworkRoute,
        registration_source_ref: str,
    ) -> None:
        provider_id = str(provider_id).strip()
        source_ref = str(registration_source_ref).strip()
        if not provider_id:
            raise ReferenceDiscoveryGatewayError("provider_id must not be empty")
        if provider_id in self._providers:
            raise ReferenceDiscoveryGatewayError(
                f"reference provider already registered: {provider_id}"
            )
        if not callable(getattr(provider, "discover_references", None)):
            raise TypeError("provider must implement discover_references")
        if not isinstance(route, NetworkRoute):
            raise TypeError("route must be NetworkRoute")
        if not source_ref:
            raise ReferenceDiscoveryGatewayError(
                "registration_source_ref must not be empty"
            )
        self._providers[provider_id] = _ReferenceProviderRegistration(
            provider=provider,
            route=route,
            registration_source_ref=source_ref,
        )

    def transport_decision(self, provider_id: str) -> TransportDecision:
        provider_id = str(provider_id).strip()
        registration = self._providers.get(provider_id)
        if registration is None:
            raise ReferenceDiscoveryGatewayError(
                f"unregistered reference provider: {provider_id}"
            )
        return self._network_policy.evaluate(registration.route)

    def discover_ranked(
        self,
        provider_id: str,
        *,
        target: ReferenceCalibrationProfile,
        comparison_dimensions: Iterable[str],
        required_features: Iterable[str] = (),
        desired_tags: Iterable[str] = (),
        feature_weights: Mapping[str, float] | None = None,
        discovery_limit: int = 12,
        result_limit: int = 3,
    ) -> ReferenceDiscoveryExecution:
        provider_id = str(provider_id).strip()
        registration = self._providers.get(provider_id)
        if registration is None:
            raise ReferenceDiscoveryGatewayError(
                f"unregistered reference provider: {provider_id}"
            )
        decision = self._network_policy.evaluate(registration.route)
        if decision.status != "ALLOW":
            raise ReferenceDiscoveryGatewayError(
                "reference provider transport denied: "
                + ",".join(decision.reason_codes)
            )

        try:
            ranked = discover_ranked_references(
                registration.provider,
                target,
                comparison_dimensions=comparison_dimensions,
                required_features=required_features,
                desired_tags=desired_tags,
                feature_weights=feature_weights,
                discovery_limit=discovery_limit,
                result_limit=result_limit,
            )
        except CoordinatorGatewayError:
            raise
        except Exception as exc:
            raise ReferenceDiscoveryGatewayError(
                f"reference provider discovery failed: {provider_id}"
            ) from exc

        return ReferenceDiscoveryExecution(
            provider_id=provider_id,
            route_id=registration.route.route_id,
            route_kind=registration.route.kind,
            registration_source_ref=registration.registration_source_ref,
            transport_reason_codes=decision.reason_codes,
            ranked=ranked,
        )


def _loopback_mcp_endpoint(value: str) -> str:
    endpoint = str(value).strip()
    if not endpoint:
        raise CoordinatorReferenceClientError("coordinator endpoint must not be empty")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise CoordinatorReferenceClientError(
            "coordinator endpoint must use http or https"
        )
    host = (parsed.hostname or "").casefold()
    if host not in _LOOPBACK_HOSTS:
        raise CoordinatorReferenceClientError(
            "host observation evidence may be sent only to a loopback coordinator"
        )
    if parsed.username is not None or parsed.password is not None:
        raise CoordinatorReferenceClientError(
            "coordinator endpoint must not embed credentials"
        )
    if parsed.query or parsed.fragment or parsed.params:
        raise CoordinatorReferenceClientError(
            "coordinator endpoint must not include params, query or fragment"
        )
    if parsed.path.rstrip("/") != "/mcp":
        raise CoordinatorReferenceClientError(
            "coordinator endpoint must target the /mcp Streamable HTTP path"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise CoordinatorReferenceClientError("coordinator endpoint has invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise CoordinatorReferenceClientError("coordinator endpoint has invalid port")
    return endpoint


def _request_strings(values: Iterable[str], field: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise CoordinatorReferenceClientError(f"{field} must be a sequence")
    out: list[str] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            raise CoordinatorReferenceClientError(
                f"{field} must contain non-empty values"
            )
        out.append(text)
    return out


def _mcp_reference_payload(result: object) -> object:
    """Extract one unambiguous JSON value from an MCP CallToolResult.

    MCP structured_content is preferred when present. MCP tools without a declared
    structured output schema may instead return their JSON serialization as a single
    TextContent block. Only strict JSON is accepted from that compatibility path.
    """
    if getattr(result, "is_error", False) is True:
        raise CoordinatorReferenceClientError(
            "coordinator reference tool returned an MCP error"
        )

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured

    content = getattr(result, "content", None)
    if not isinstance(content, (list, tuple)) or len(content) != 1:
        raise CoordinatorReferenceClientError(
            "coordinator returned no unambiguous reference-discovery payload"
        )
    block = content[0]
    if getattr(block, "type", None) != "text":
        raise CoordinatorReferenceClientError(
            "coordinator reference fallback must be exactly one text content block"
        )
    text = getattr(block, "text", None)
    if not isinstance(text, str):
        raise CoordinatorReferenceClientError(
            "coordinator reference text payload is invalid"
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CoordinatorReferenceClientError(
            "coordinator reference text payload is not strict JSON"
        ) from exc


def _validated_reference_response(
    payload: object,
    *,
    provider_id: str,
    session: dict[str, object],
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise CoordinatorReferenceClientError(
            "coordinator returned no structured reference-discovery payload"
        )
    if payload.get("provider_id") != provider_id:
        raise CoordinatorReferenceClientError(
            "coordinator response provider does not match request"
        )
    if payload.get("read_only") is not True:
        raise CoordinatorReferenceClientError(
            "coordinator reference response is not explicitly read-only"
        )
    if payload.get("action_authority_granted") is not False:
        raise CoordinatorReferenceClientError(
            "coordinator reference response attempted to grant action authority"
        )

    binding = session.get("binding")
    calibration = payload.get("session_calibration")
    if not isinstance(binding, dict) or not isinstance(calibration, dict):
        raise CoordinatorReferenceClientError(
            "coordinator response is missing session identity evidence"
        )
    for field in ("workspace_id", "song_id", "workspace_observation_id"):
        expected = binding.get(field)
        if not isinstance(expected, str) or calibration.get(field) != expected:
            raise CoordinatorReferenceClientError(
                f"coordinator response crossed session identity: {field}"
            )

    ranked = payload.get("ranked")
    primary = payload.get("primary")
    if not isinstance(ranked, list):
        raise CoordinatorReferenceClientError(
            "coordinator response ranked references must be a list"
        )
    if primary is not None and not isinstance(primary, dict):
        raise CoordinatorReferenceClientError(
            "coordinator response primary reference must be an object or null"
        )
    if ranked:
        if primary is None or primary != ranked[0]:
            raise CoordinatorReferenceClientError(
                "coordinator primary reference must equal the first ranked candidate"
            )
    elif primary is not None:
        raise CoordinatorReferenceClientError(
            "coordinator returned a primary reference without ranked candidates"
        )

    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CoordinatorReferenceClientError(
            "coordinator reference response is not JSON-transportable"
        ) from exc
    return json.loads(encoded)


class CoordinatorReferenceClient:
    """Host-side loopback client for the coordinator's read-only reference tool.

    The client owns only evidence transport. It serializes typed current host state,
    calls the coordinator over MCP Streamable HTTP, and verifies that the response
    remains bound to the same workspace/Song/observation with no action authority.
    It never persists a reference, mutates a DAW, or accepts a remote coordinator URL.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_COORDINATOR_MCP_ENDPOINT,
        *,
        client_factory: Callable[..., object] = Client,
    ) -> None:
        if not callable(client_factory):
            raise TypeError("client_factory must be callable")
        self.endpoint = _loopback_mcp_endpoint(endpoint)
        self._client_factory = client_factory

    async def discover_session(
        self,
        provider_id: str,
        binding: HostObservationBinding,
        shadow: HostShadowState,
        *,
        comparison_dimensions: Iterable[str],
        engineering_snapshot: EngineeringSnapshot | None = None,
        semantic_tags: Iterable[str] = (),
        required_features: Iterable[str] = (),
        desired_tags: Iterable[str] = (),
        feature_weights: Mapping[str, float] | None = None,
        discovery_limit: int = 12,
        result_limit: int = 3,
    ) -> dict[str, object]:
        provider = str(provider_id).strip()
        if not provider:
            raise CoordinatorReferenceClientError("provider_id must not be empty")
        session = build_session_reference_evidence_bundle(
            binding,
            shadow,
            engineering_snapshot=engineering_snapshot,
        )
        arguments = {
            "provider_id": provider,
            "session": session,
            "comparison_dimensions": _request_strings(
                comparison_dimensions, "comparison_dimensions"
            ),
            "semantic_tags": _request_strings(semantic_tags, "semantic_tags"),
            "required_features": _request_strings(
                required_features, "required_features"
            ),
            "desired_tags": _request_strings(desired_tags, "desired_tags"),
            "feature_weights": (
                None if feature_weights is None else dict(feature_weights)
            ),
            "discovery_limit": discovery_limit,
            "result_limit": result_limit,
        }
        try:
            async with self._client_factory(
                self.endpoint,
                raise_exceptions=True,
            ) as client:
                result = await client.call_tool(
                    "discover_session_reference_candidates",
                    arguments,
                )
        except CoordinatorReferenceClientError:
            raise
        except Exception as exc:
            raise CoordinatorReferenceClientError(
                "loopback coordinator reference discovery failed"
            ) from exc

        return _validated_reference_response(
            _mcp_reference_payload(result),
            provider_id=provider,
            session=session,
        )


@dataclass(frozen=True)
class MutationVerification:
    verified: bool
    evidence_refs: tuple[str, ...]
    observation: str

    def __post_init__(self) -> None:
        if type(self.verified) is not bool:
            raise TypeError("verification.verified must be bool")
        refs = tuple(str(item).strip() for item in self.evidence_refs)
        if any(not item for item in refs) or len(refs) != len(set(refs)):
            raise CoordinatorGatewayError("verification evidence_refs must be unique non-empty refs")
        observation = str(self.observation).strip()
        if not observation:
            raise CoordinatorGatewayError("verification observation must not be empty")
        if self.verified and not refs:
            raise CoordinatorGatewayError("verified mutation requires fresh evidence refs")
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "observation", observation)


@dataclass(frozen=True)
class GatedExecutionResult(Generic[T]):
    permit_id: str
    action_id: str
    action_intent_fingerprint: str
    result: T
    verification: MutationVerification
    reconciliation_ref: str


@dataclass(frozen=True)
class _MutationRegistration(Generic[T]):
    action_class: str
    executor: Callable[[ActionIntent], T]
    verifier: Callable[[ActionIntent, T], MutationVerification]
    reconciler: Callable[[ActionIntent, T, MutationVerification], str]


class CoordinatorMutationGateway:
    """Permit -> mutation -> fresh verification -> canonical reconciliation.

    The gateway consumes a one-time permit before calling the executor. A failed
    executor, ambiguous verification, or failed reconciliation never restores the
    permit. Callers must reconstruct current state and obtain a fresh permit before
    retrying, preventing stale replay after a partial or uncertain side effect.
    """

    def __init__(
        self,
        *,
        permits: ExecutionPermitAuthority,
        contexts: ContextProvider,
    ):
        if not isinstance(permits, ExecutionPermitAuthority):
            raise TypeError("permits must be ExecutionPermitAuthority")
        if not callable(getattr(contexts, "get", None)):
            raise TypeError("contexts must provide get(snapshot_id)")
        self._permits = permits
        self._contexts = contexts
        self._mutations: dict[str, _MutationRegistration] = {}

    def register(
        self,
        name: str,
        *,
        action_class: str,
        executor: Callable[[ActionIntent], T],
        verifier: Callable[[ActionIntent, T], MutationVerification],
        reconciler: Callable[[ActionIntent, T, MutationVerification], str],
    ) -> None:
        name = str(name).strip()
        action_class = str(action_class).strip().upper()
        if not name:
            raise CoordinatorGatewayError("mutation name must not be empty")
        if name in self._mutations:
            raise CoordinatorGatewayError(f"mutation already registered: {name}")
        if action_class == "READ_ONLY":
            raise CoordinatorGatewayError("read-only operations do not belong in mutation gateway")
        if action_class not in {"REVERSIBLE", "COMPENSATABLE", "IRREVERSIBLE"}:
            raise CoordinatorGatewayError(f"unsupported mutation action class: {action_class}")
        for field, fn in (("executor", executor), ("verifier", verifier), ("reconciler", reconciler)):
            if not callable(fn):
                raise TypeError(f"{field} must be callable")
        self._mutations[name] = _MutationRegistration(
            action_class=action_class,
            executor=executor,
            verifier=verifier,
            reconciler=reconciler,
        )

    @property
    def registered_mutations(self) -> tuple[str, ...]:
        return tuple(sorted(self._mutations))

    def execute(
        self,
        name: str,
        *,
        permit_token: str,
        context_snapshot_id: str,
        envelope,
        action: ActionIntent,
    ) -> GatedExecutionResult[T]:
        name = str(name).strip()
        registration = self._mutations.get(name)
        if registration is None:
            raise CoordinatorGatewayError(f"unregistered mutation: {name}")
        if not isinstance(action, ActionIntent):
            raise TypeError("action must be ActionIntent")
        if action.action_class != registration.action_class:
            raise CoordinatorGatewayError(
                f"registered mutation class mismatch expected={registration.action_class} action={action.action_class}"
            )

        snapshot = self._contexts.get(context_snapshot_id)
        consumed = self._permits.consume(
            token=permit_token,
            envelope=envelope,
            action=action,
            snapshot=snapshot,
        )

        result = registration.executor(action)
        verification = registration.verifier(action, result)
        if not isinstance(verification, MutationVerification):
            raise MutationVerificationError(
                "mutation verifier returned no typed verification; completion remains unproven"
            )
        if not verification.verified:
            raise MutationVerificationError(
                "mutation completion is unverified after execution; reconstruct current state before any retry"
            )

        reconciliation_ref = registration.reconciler(action, result, verification)
        reconciliation_ref = str(reconciliation_ref).strip()
        if not reconciliation_ref:
            raise MutationReconciliationError(
                "verified mutation has no canonical reconciliation receipt"
            )

        return GatedExecutionResult(
            permit_id=consumed.permit_id,
            action_id=action.action_id,
            action_intent_fingerprint=action.intent_fingerprint,
            result=result,
            verification=verification,
            reconciliation_ref=reconciliation_ref,
        )
