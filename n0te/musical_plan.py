from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Protocol

from .capabilities import ROUTE_KINDS
from .focus import FOCUS_DIMENSIONS, FocusContext, FocusContextService
from .hosts import HostRuntimeIdentity, normalize_host_family


class MusicalPlanError(ValueError):
    """Invalid or mismatched durable musical-plan meaning."""


class MusicalPlanStaleError(MusicalPlanError):
    """A durable plan no longer matches the exact observed focus it was bound to."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise MusicalPlanError(f"{field} must be text")
    text = " ".join(value.split())
    if not text:
        raise MusicalPlanError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _text_tuple(values, field: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise MusicalPlanError(f"{field} must be a sequence of text values")
    try:
        normalized = tuple(_text(value, field) for value in values)
    except TypeError as exc:
        raise MusicalPlanError(f"{field} must be a sequence of text values") from exc
    if not allow_empty and not normalized:
        raise MusicalPlanError(f"{field} must not be empty")
    if len(set(normalized)) != len(normalized):
        raise MusicalPlanError(f"{field} must not contain duplicates")
    return normalized


def _focus_dimension(value: str) -> str:
    dimension = _text(value, "dimension").upper().replace("-", "_").replace(" ", "_")
    if dimension not in FOCUS_DIMENSIONS:
        raise MusicalPlanError(f"unsupported focus dimension: {dimension}")
    return dimension


@dataclass(frozen=True)
class MusicalTarget:
    """Exact host-neutral target identity captured from FocusContext."""

    dimension: str
    refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimension", _focus_dimension(self.dimension))
        object.__setattr__(
            self,
            "refs",
            _text_tuple(self.refs, "target.refs", allow_empty=False),
        )


@dataclass(frozen=True)
class MusicalPlan:
    """Durable musical meaning before any host command is chosen.

    Host-specific commands, provider calls and transport details deliberately do
    not live here. The runtime fingerprint is only a stale-target binding witness.
    """

    plan_id: str
    song_id: str
    workspace_id: str
    workspace_observation_id: str
    host_runtime_fingerprint: str
    artist_intent: str
    targets: tuple[MusicalTarget, ...]
    section_ref: str | None
    constraints: tuple[str, ...]
    locked_element_refs: tuple[str, ...]
    editable_element_refs: tuple[str, ...]
    desired_change: str
    verification_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "plan_id",
            "song_id",
            "workspace_id",
            "workspace_observation_id",
            "host_runtime_fingerprint",
            "artist_intent",
            "desired_change",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        targets = tuple(self.targets)
        if not targets or not all(isinstance(target, MusicalTarget) for target in targets):
            raise MusicalPlanError("targets must contain at least one MusicalTarget")
        dimensions = [target.dimension for target in targets]
        if len(set(dimensions)) != len(dimensions):
            raise MusicalPlanError("targets must not repeat a focus dimension")
        object.__setattr__(self, "targets", targets)
        section_ref = _optional_text(self.section_ref, "section_ref")
        section_targets = [target for target in targets if target.dimension == "SONG_SECTION"]
        if section_ref is None and section_targets:
            raise MusicalPlanError("SONG_SECTION target requires section_ref")
        if section_ref is not None:
            if len(section_targets) != 1 or section_targets[0].refs != (section_ref,):
                raise MusicalPlanError("section_ref must equal the exact SONG_SECTION target")
        object.__setattr__(self, "section_ref", section_ref)
        object.__setattr__(self, "constraints", _text_tuple(self.constraints, "constraints"))
        object.__setattr__(
            self,
            "locked_element_refs",
            _text_tuple(self.locked_element_refs, "locked_element_refs"),
        )
        object.__setattr__(
            self,
            "editable_element_refs",
            _text_tuple(self.editable_element_refs, "editable_element_refs"),
        )
        if set(self.locked_element_refs) & set(self.editable_element_refs):
            raise MusicalPlanError("an element cannot be both locked and editable")
        object.__setattr__(
            self,
            "verification_refs",
            _text_tuple(self.verification_refs, "verification_refs", allow_empty=False),
        )
        object.__setattr__(
            self,
            "provenance_refs",
            _text_tuple(self.provenance_refs, "provenance_refs", allow_empty=False),
        )

    @property
    def fingerprint(self) -> str:
        payload = {
            "schema": "n0te.musical-plan/v1",
            "plan_id": self.plan_id,
            "song_id": self.song_id,
            "workspace_id": self.workspace_id,
            "workspace_observation_id": self.workspace_observation_id,
            "host_runtime_fingerprint": self.host_runtime_fingerprint,
            "artist_intent": self.artist_intent,
            "targets": [
                {"dimension": target.dimension, "refs": list(target.refs)}
                for target in self.targets
            ],
            "section_ref": self.section_ref,
            "constraints": list(self.constraints),
            "locked_element_refs": list(self.locked_element_refs),
            "editable_element_refs": list(self.editable_element_refs),
            "desired_change": self.desired_change,
            "verification_refs": list(self.verification_refs),
            "provenance_refs": list(self.provenance_refs),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def target(self, dimension: str) -> MusicalTarget | None:
        canonical = _focus_dimension(dimension)
        for target in self.targets:
            if target.dimension == canonical:
                return target
        return None


@dataclass(frozen=True)
class CompiledHostAction:
    """Opaque host implementation detail produced from durable MusicalPlan meaning."""

    action_id: str
    route_kind: str
    capability: str
    payload_ref: str
    postcondition_ref: str
    compensatable: bool
    depends_on_action_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _text(self.action_id, "action_id"))
        route = _text(self.route_kind, "route_kind").upper()
        if route not in ROUTE_KINDS:
            raise MusicalPlanError(f"unsupported route kind: {route}")
        object.__setattr__(self, "route_kind", route)
        for field in ("capability", "payload_ref", "postcondition_ref"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if not isinstance(self.compensatable, bool):
            raise MusicalPlanError("compensatable must be boolean")
        dependencies = _text_tuple(self.depends_on_action_ids, "depends_on_action_ids")
        if self.action_id in dependencies:
            raise MusicalPlanError("action cannot depend on itself")
        object.__setattr__(self, "depends_on_action_ids", dependencies)


@dataclass(frozen=True)
class HostCompilation:
    """Validated host-specific compilation envelope. It does not execute actions."""

    plan_id: str
    plan_fingerprint: str
    compiler_id: str
    compiler_version: str
    host_family: str
    host_runtime_fingerprint: str
    actions: tuple[CompiledHostAction, ...]
    evidence_ref: str

    def __post_init__(self) -> None:
        for field in (
            "plan_id",
            "plan_fingerprint",
            "compiler_id",
            "compiler_version",
            "host_runtime_fingerprint",
            "evidence_ref",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(self, "host_family", normalize_host_family(self.host_family))
        actions = tuple(self.actions)
        if not actions or not all(isinstance(action, CompiledHostAction) for action in actions):
            raise MusicalPlanError("actions must contain at least one CompiledHostAction")
        ids = [action.action_id for action in actions]
        if len(set(ids)) != len(ids):
            raise MusicalPlanError("compiled action ids must be unique")
        seen: set[str] = set()
        for action in actions:
            missing = set(action.depends_on_action_ids) - seen
            if missing:
                raise MusicalPlanError(
                    f"action {action.action_id} depends on unavailable prior actions: {sorted(missing)}"
                )
            seen.add(action.action_id)
        object.__setattr__(self, "actions", actions)


class HostPlanCompiler(Protocol):
    compiler_id: str
    compiler_version: str
    host_family: str

    def compile(self, plan: MusicalPlan, runtime: HostRuntimeIdentity) -> HostCompilation: ...


class MusicalPlanService:
    """Prepare and validate portable musical meaning, then call a host compiler.

    This service deliberately owns no execution, transaction, provider, DAW or
    persistence method. Host mutation starts downstream after a compilation has
    been accepted by the transaction/authority path.
    """

    def __init__(self, focus: FocusContextService):
        if not isinstance(focus, FocusContextService):
            raise TypeError("focus must be FocusContextService")
        self.focus = focus

    @staticmethod
    def _dimensions(values) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)):
            raise MusicalPlanError("required_dimensions must be a sequence")
        try:
            dimensions = tuple(_focus_dimension(value) for value in values)
        except TypeError as exc:
            raise MusicalPlanError("required_dimensions must be a sequence") from exc
        if not dimensions:
            raise MusicalPlanError("required_dimensions must not be empty")
        if len(set(dimensions)) != len(dimensions):
            raise MusicalPlanError("required_dimensions must not contain duplicates")
        return dimensions

    def prepare(
        self,
        context: FocusContext,
        *,
        artist_intent: str,
        required_dimensions,
        desired_change: str,
        constraints=(),
        locked_element_refs=(),
        editable_element_refs=(),
        verification_refs=(),
        provenance_refs=(),
        plan_id: str | None = None,
    ) -> MusicalPlan:
        if not isinstance(context, FocusContext):
            raise TypeError("context must be FocusContext")
        dimensions = self._dimensions(required_dimensions)
        self.focus.validate_current(context)
        exact = self.focus.require_exact(context, *dimensions)
        targets = tuple(MusicalTarget(item.dimension, item.refs) for item in exact)
        section = next((target for target in targets if target.dimension == "SONG_SECTION"), None)
        section_ref = None if section is None else section.refs[0]
        normalized_constraints = _text_tuple(constraints, "constraints")
        normalized_locked = _text_tuple(locked_element_refs, "locked_element_refs")
        normalized_editable = _text_tuple(editable_element_refs, "editable_element_refs")
        normalized_verification = _text_tuple(
            verification_refs,
            "verification_refs",
            allow_empty=False,
        )
        automatic_provenance = [context.observation_evidence_ref]
        automatic_provenance.extend(item.evidence_ref for item in exact)
        supplied_provenance = _text_tuple(provenance_refs, "provenance_refs")
        combined_provenance = tuple(dict.fromkeys((*automatic_provenance, *supplied_provenance)))
        return MusicalPlan(
            plan_id=plan_id or f"plan_{uuid.uuid4().hex}",
            song_id=context.song_id,
            workspace_id=context.workspace_id,
            workspace_observation_id=context.workspace_observation_id,
            host_runtime_fingerprint=context.host_runtime_fingerprint,
            artist_intent=artist_intent,
            targets=targets,
            section_ref=section_ref,
            constraints=normalized_constraints,
            locked_element_refs=normalized_locked,
            editable_element_refs=normalized_editable,
            desired_change=desired_change,
            verification_refs=normalized_verification,
            provenance_refs=combined_provenance,
        )

    def validate_current(self, plan: MusicalPlan, context: FocusContext) -> MusicalPlan:
        if not isinstance(plan, MusicalPlan):
            raise TypeError("plan must be MusicalPlan")
        if not isinstance(context, FocusContext):
            raise TypeError("context must be FocusContext")
        self.focus.validate_current(context)
        bindings = {
            "song_id": context.song_id,
            "workspace_id": context.workspace_id,
            "workspace_observation_id": context.workspace_observation_id,
            "host_runtime_fingerprint": context.host_runtime_fingerprint,
        }
        for field, observed in bindings.items():
            if getattr(plan, field) != observed:
                raise MusicalPlanStaleError(f"STALE_{field.upper()}")
        for target in plan.targets:
            observed = context.get(target.dimension)
            if observed is None or observed.state != "OBSERVED_EXACT" or observed.refs != target.refs:
                raise MusicalPlanStaleError(f"STALE_TARGET_{target.dimension}")
        return plan

    def compile(
        self,
        plan: MusicalPlan,
        context: FocusContext,
        runtime: HostRuntimeIdentity,
        compiler: HostPlanCompiler,
    ) -> HostCompilation:
        self.validate_current(plan, context)
        if not isinstance(runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        if runtime.fingerprint != plan.host_runtime_fingerprint:
            raise MusicalPlanStaleError("STALE_HOST_RUNTIME")
        try:
            compiler_id = _text(compiler.compiler_id, "compiler.compiler_id")
            compiler_version = _text(compiler.compiler_version, "compiler.compiler_version")
            compiler_family = normalize_host_family(compiler.host_family)
            compile_plan = compiler.compile
        except AttributeError as exc:
            raise TypeError("compiler must implement HostPlanCompiler") from exc
        if not callable(compile_plan):
            raise TypeError("compiler.compile must be callable")
        if compiler_family != runtime.family:
            raise MusicalPlanError("compiler host family does not match active runtime")
        compilation = compile_plan(plan, runtime)
        if not isinstance(compilation, HostCompilation):
            raise MusicalPlanError("compiler must return HostCompilation")
        expected = {
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "compiler_id": compiler_id,
            "compiler_version": compiler_version,
            "host_family": runtime.family,
            "host_runtime_fingerprint": runtime.fingerprint,
        }
        for field, value in expected.items():
            if getattr(compilation, field) != value:
                raise MusicalPlanError(f"compiler returned mismatched {field}")
        return compilation
