from __future__ import annotations

from dataclasses import dataclass

from .capabilities import CapabilityCandidate

TOOL_FORMAT_KINDS = {
    "VST3",
    "AU",
    "AAX",
    "CLAP",
    "LV2",
    "LADSPA",
    "OTHER",
}


class ToolIdentityError(ValueError):
    """Invalid semantic Tool identity/evidence input."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ToolIdentityError(f"{field} must not be empty")
    return text


def _real_bool(value: bool, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be bool")
    return value


@dataclass(frozen=True)
class ToolEndpoint:
    """One observed format/native endpoint belonging to a semantic Tool.

    Endpoint presence is not capability, entitlement, hostability or control.
    `evidence_ref` records why this endpoint identity is represented at all.
    """

    endpoint_id: str
    format_kind: str
    native_identity: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "endpoint_id", _text(self.endpoint_id, "endpoint.endpoint_id")
        )
        format_kind = str(self.format_kind).strip().upper()
        if format_kind not in TOOL_FORMAT_KINDS:
            raise ToolIdentityError(f"unsupported tool format: {format_kind}")
        object.__setattr__(self, "format_kind", format_kind)
        object.__setattr__(
            self,
            "native_identity",
            _text(self.native_identity, "endpoint.native_identity"),
        )
        object.__setattr__(
            self, "evidence_ref", _text(self.evidence_ref, "endpoint.evidence_ref")
        )


@dataclass(frozen=True)
class ToolCapabilityBinding:
    """Bind one existing CORE-03A OWNED_TOOL candidate to one endpoint."""

    endpoint_id: str
    candidate: CapabilityCandidate

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "endpoint_id", _text(self.endpoint_id, "capability.endpoint_id")
        )
        if not isinstance(self.candidate, CapabilityCandidate):
            raise TypeError("capability candidate must be CapabilityCandidate")
        if self.candidate.route_kind != "OWNED_TOOL":
            raise ToolIdentityError(
                "ToolCapabilityBinding requires an OWNED_TOOL CapabilityCandidate"
            )


@dataclass(frozen=True)
class ToolParameterBinding:
    """Explicit semantic parameter mapping for one endpoint."""

    endpoint_id: str
    semantic_key: str
    native_parameter_ref: str
    readable: bool
    writable: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "endpoint_id", _text(self.endpoint_id, "parameter.endpoint_id")
        )
        object.__setattr__(
            self, "semantic_key", _text(self.semantic_key, "parameter.semantic_key")
        )
        object.__setattr__(
            self,
            "native_parameter_ref",
            _text(self.native_parameter_ref, "parameter.native_parameter_ref"),
        )
        object.__setattr__(self, "readable", _real_bool(self.readable, "parameter.readable"))
        object.__setattr__(self, "writable", _real_bool(self.writable, "parameter.writable"))
        if not self.readable and not self.writable:
            raise ToolIdentityError(
                "parameter binding must be readable, writable, or both"
            )
        object.__setattr__(
            self, "evidence_ref", _text(self.evidence_ref, "parameter.evidence_ref")
        )


@dataclass(frozen=True)
class ToolStateBinding:
    """Explicit endpoint state read/write support evidence."""

    endpoint_id: str
    readable: bool
    writable: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "endpoint_id", _text(self.endpoint_id, "state.endpoint_id")
        )
        object.__setattr__(self, "readable", _real_bool(self.readable, "state.readable"))
        object.__setattr__(self, "writable", _real_bool(self.writable, "state.writable"))
        if not self.readable and not self.writable:
            raise ToolIdentityError("state binding must be readable, writable, or both")
        object.__setattr__(
            self, "evidence_ref", _text(self.evidence_ref, "state.evidence_ref")
        )


@dataclass(frozen=True)
class SemanticToolProfile:
    """One stable owned-tool identity spanning explicit format endpoints.

    This object performs no discovery, loading, licensing inference, parameter
    control or state I/O. It only normalizes explicit identity/evidence facts.
    """

    tool_id: str
    display_name: str
    endpoints: tuple[ToolEndpoint, ...]
    capabilities: tuple[ToolCapabilityBinding, ...] = ()
    parameters: tuple[ToolParameterBinding, ...] = ()
    state_bindings: tuple[ToolStateBinding, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_id", _text(self.tool_id, "tool.tool_id"))
        object.__setattr__(
            self, "display_name", _text(self.display_name, "tool.display_name")
        )

        endpoints = tuple(self.endpoints)
        if not endpoints:
            raise ToolIdentityError("tool.endpoints must contain at least one endpoint")
        if not all(isinstance(item, ToolEndpoint) for item in endpoints):
            raise TypeError("all tool endpoints must be ToolEndpoint")
        endpoint_ids = [item.endpoint_id for item in endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ToolIdentityError("tool endpoint_id values must be unique")
        native_keys = [(item.format_kind, item.native_identity) for item in endpoints]
        if len(native_keys) != len(set(native_keys)):
            raise ToolIdentityError(
                "tool format/native_identity endpoint pairs must be unique"
            )
        endpoints = tuple(sorted(endpoints, key=lambda item: item.endpoint_id))
        known_endpoints = {item.endpoint_id for item in endpoints}

        capabilities = tuple(self.capabilities)
        if not all(isinstance(item, ToolCapabilityBinding) for item in capabilities):
            raise TypeError("all tool capabilities must be ToolCapabilityBinding")
        candidate_ids = [item.candidate.candidate_id for item in capabilities]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ToolIdentityError("tool capability candidate IDs must be unique")
        for binding in capabilities:
            if binding.endpoint_id not in known_endpoints:
                raise ToolIdentityError(
                    f"capability binding references unknown endpoint: {binding.endpoint_id}"
                )
        capabilities = tuple(
            sorted(capabilities, key=lambda item: item.candidate.candidate_id)
        )

        parameters = tuple(self.parameters)
        if not all(isinstance(item, ToolParameterBinding) for item in parameters):
            raise TypeError("all tool parameters must be ToolParameterBinding")
        parameter_keys = [(item.endpoint_id, item.semantic_key) for item in parameters]
        if len(parameter_keys) != len(set(parameter_keys)):
            raise ToolIdentityError(
                "semantic parameter key may bind only once per endpoint"
            )
        for binding in parameters:
            if binding.endpoint_id not in known_endpoints:
                raise ToolIdentityError(
                    f"parameter binding references unknown endpoint: {binding.endpoint_id}"
                )
        parameters = tuple(
            sorted(parameters, key=lambda item: (item.semantic_key, item.endpoint_id))
        )

        state_bindings = tuple(self.state_bindings)
        if not all(isinstance(item, ToolStateBinding) for item in state_bindings):
            raise TypeError("all tool state bindings must be ToolStateBinding")
        state_endpoint_ids = [item.endpoint_id for item in state_bindings]
        if len(state_endpoint_ids) != len(set(state_endpoint_ids)):
            raise ToolIdentityError("tool state may bind only once per endpoint")
        for binding in state_bindings:
            if binding.endpoint_id not in known_endpoints:
                raise ToolIdentityError(
                    f"state binding references unknown endpoint: {binding.endpoint_id}"
                )
        state_bindings = tuple(
            sorted(state_bindings, key=lambda item: item.endpoint_id)
        )

        object.__setattr__(self, "endpoints", endpoints)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "state_bindings", state_bindings)

    def endpoint(self, endpoint_id: str) -> ToolEndpoint | None:
        endpoint_id = _text(endpoint_id, "endpoint_id")
        return next(
            (item for item in self.endpoints if item.endpoint_id == endpoint_id),
            None,
        )

    def candidates(self) -> tuple[CapabilityCandidate, ...]:
        return tuple(item.candidate for item in self.capabilities)

    def candidates_for(self, capability: str) -> tuple[CapabilityCandidate, ...]:
        capability = _text(capability, "capability")
        return tuple(
            item.candidate
            for item in self.capabilities
            if item.candidate.capability == capability
        )

    def parameter_bindings_for(
        self, semantic_key: str
    ) -> tuple[ToolParameterBinding, ...]:
        semantic_key = _text(semantic_key, "semantic_key")
        return tuple(
            item for item in self.parameters if item.semantic_key == semantic_key
        )

    def state_for_endpoint(self, endpoint_id: str) -> ToolStateBinding | None:
        endpoint_id = _text(endpoint_id, "endpoint_id")
        return next(
            (item for item in self.state_bindings if item.endpoint_id == endpoint_id),
            None,
        )
