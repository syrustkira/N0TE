from __future__ import annotations

from dataclasses import dataclass

from .capabilities import (
    CapabilityResolution,
    N0TEableJob,
    ResolutionConstraints,
)
from .studio import StudioCapabilityProfile

TEMPLATE_FAMILIES = {
    "SONG",
    "ARRANGEMENT",
    "PRODUCTION",
    "MIX",
    "VOCAL",
    "DRUM",
    "SOUND_DESIGN",
    "RELEASE",
    "CONTENT",
    "CAMPAIGN",
    "SOCIAL",
    "LIVE",
}
TEMPLATE_PLAN_STATUSES = {"FULL", "PARTIAL", "UNAVAILABLE"}


class TemplateValidationError(ValueError):
    """Invalid provider-neutral Template input or plan request."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise TemplateValidationError(f"{field} must not be empty")
    return text


def _tags(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (tuple, list)):
        raise TypeError("role.tags must be a tuple or list of strings")
    result: list[str] = []
    for value in values:
        text = _text(value, "role.tags")
        if text not in result:
            result.append(text)
    return tuple(sorted(result))


@dataclass(frozen=True)
class TemplateRole:
    """One semantic role inside a provider-neutral Template.

    A role names the artist job/capability that must or may be available. It does
    not name a host, provider, candidate, plug-in instance, track index or route.
    Those belong to Studio capability facts and later adapters.
    """

    role_id: str
    capability: str
    description: str
    required: bool = True
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role_id", _text(self.role_id, "role.role_id"))
        object.__setattr__(
            self, "capability", _text(self.capability, "role.capability")
        )
        object.__setattr__(
            self, "description", _text(self.description, "role.description")
        )
        if not isinstance(self.required, bool):
            raise TypeError("role.required must be bool")
        object.__setattr__(self, "tags", _tags(self.tags))


@dataclass(frozen=True)
class TemplateDefinition:
    """Immutable N0TE Template meaning that exists above providers/hosts."""

    template_id: str
    family: str
    name: str
    intent: str
    roles: tuple[TemplateRole, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "template_id", _text(self.template_id, "template.template_id")
        )
        family = str(self.family).strip().upper()
        if family not in TEMPLATE_FAMILIES:
            raise TemplateValidationError(f"unsupported template family: {family}")
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "name", _text(self.name, "template.name"))
        object.__setattr__(self, "intent", _text(self.intent, "template.intent"))

        roles = tuple(self.roles)
        if not roles:
            raise TemplateValidationError("template.roles must contain at least one role")
        if not all(isinstance(role, TemplateRole) for role in roles):
            raise TypeError("all template roles must be TemplateRole")
        role_ids = [role.role_id for role in roles]
        if len(role_ids) != len(set(role_ids)):
            raise TemplateValidationError("template role_id values must be unique")
        object.__setattr__(
            self,
            "roles",
            tuple(sorted(roles, key=lambda role: role.role_id)),
        )


@dataclass(frozen=True)
class TemplateRolePlan:
    role: TemplateRole
    job: N0TEableJob
    resolution: CapabilityResolution

    @property
    def available(self) -> bool:
        return self.resolution.status == "RESOLVED"


@dataclass(frozen=True)
class TemplatePlan:
    template_id: str
    environment_id: str
    status: str
    role_plans: tuple[TemplateRolePlan, ...]

    @property
    def unavailable_required_role_ids(self) -> tuple[str, ...]:
        return tuple(
            item.role.role_id
            for item in self.role_plans
            if item.role.required and not item.available
        )

    @property
    def unavailable_optional_role_ids(self) -> tuple[str, ...]:
        return tuple(
            item.role.role_id
            for item in self.role_plans
            if not item.role.required and not item.available
        )


class TemplatePlanner:
    """Pure Studio support planner for provider-neutral Templates.

    Planning never mutates a host or template. It derives stable N0TEable jobs
    from semantic roles and delegates every legitimacy/score decision to the
    StudioCapabilityProfile / CORE-03A resolver path.
    """

    @staticmethod
    def _job(template: TemplateDefinition, role: TemplateRole) -> N0TEableJob:
        return N0TEableJob(
            id=f"template:{template.template_id}:role:{role.role_id}",
            capability=role.capability,
            description=role.description,
        )

    def plan(
        self,
        template: TemplateDefinition,
        studio: StudioCapabilityProfile,
        constraints: ResolutionConstraints = ResolutionConstraints(),
    ) -> TemplatePlan:
        if not isinstance(template, TemplateDefinition):
            raise TypeError("template must be TemplateDefinition")
        if not isinstance(studio, StudioCapabilityProfile):
            raise TypeError("studio must be StudioCapabilityProfile")
        if not isinstance(constraints, ResolutionConstraints):
            raise TypeError("constraints must be ResolutionConstraints")

        plans: list[TemplateRolePlan] = []
        for role in template.roles:
            job = self._job(template, role)
            plans.append(
                TemplateRolePlan(
                    role=role,
                    job=job,
                    resolution=studio.resolve(job, constraints),
                )
            )
        role_plans = tuple(plans)

        required_missing = any(
            item.role.required and not item.available for item in role_plans
        )
        optional_missing = any(
            not item.role.required and not item.available for item in role_plans
        )
        if required_missing:
            status = "UNAVAILABLE"
        elif optional_missing:
            status = "PARTIAL"
        else:
            status = "FULL"

        return TemplatePlan(
            template_id=template.template_id,
            environment_id=studio.environment_id,
            status=status,
            role_plans=role_plans,
        )
