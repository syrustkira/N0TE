from __future__ import annotations

from dataclasses import dataclass

from .capabilities import CapabilityResolution, N0TEableJob, ResolutionConstraints
from .studio import StudioCapabilityProfile

RECIPE_AUTHORITY_CLASSES = {
    "READ_ONLY",
    "REVERSIBLE_MUTATION",
    "CONSEQUENTIAL_ACTION",
}
RECIPE_RECOVERY_POLICIES = {
    "NONE",
    "RETRY_SAFE",
    "ROLLBACK_REQUIRED",
    "MANUAL_RECOVERY",
}
RECIPE_PLAN_STATUSES = {"READY", "UNAVAILABLE"}


class RecipeValidationError(ValueError):
    """Invalid provider-neutral Recipe input or plan request."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise RecipeValidationError(f"{field} must not be empty")
    return text


def _text_tuple(
    values: tuple[str, ...] | list[str] | None,
    field: str,
    *,
    required: bool = False,
    sort_values: bool = False,
) -> tuple[str, ...]:
    if values is None:
        values = ()
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{field} must be a tuple or list of strings")
    result: list[str] = []
    for value in values:
        text = _text(value, field)
        if text not in result:
            result.append(text)
    if required and not result:
        raise RecipeValidationError(f"{field} must contain at least one value")
    return tuple(sorted(result)) if sort_values else tuple(result)


@dataclass(frozen=True)
class RecipeStep:
    """One semantic step in a host-neutral Recipe.

    `authority_class` declares what a future executor would need. It is never an
    approval token and grants no authority. Postconditions and recovery policy
    are explicit planning metadata, not proof that execution happened.
    """

    step_id: str
    capability: str
    description: str
    authority_class: str
    postconditions: tuple[str, ...]
    recovery_policy: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _text(self.step_id, "step.step_id"))
        object.__setattr__(
            self, "capability", _text(self.capability, "step.capability")
        )
        object.__setattr__(
            self, "description", _text(self.description, "step.description")
        )
        authority = str(self.authority_class).strip().upper()
        if authority not in RECIPE_AUTHORITY_CLASSES:
            raise RecipeValidationError(f"unsupported authority class: {authority}")
        object.__setattr__(self, "authority_class", authority)
        recovery = str(self.recovery_policy).strip().upper()
        if recovery not in RECIPE_RECOVERY_POLICIES:
            raise RecipeValidationError(f"unsupported recovery policy: {recovery}")
        object.__setattr__(self, "recovery_policy", recovery)
        object.__setattr__(
            self,
            "postconditions",
            _text_tuple(
                self.postconditions,
                "step.postconditions",
                required=True,
                sort_values=True,
            ),
        )
        object.__setattr__(
            self,
            "depends_on",
            _text_tuple(self.depends_on, "step.depends_on", sort_values=True),
        )


@dataclass(frozen=True)
class RecipeDefinition:
    """Immutable ordered producer/orchestration intent above hosts/providers."""

    recipe_id: str
    name: str
    intent: str
    steps: tuple[RecipeStep, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "recipe_id", _text(self.recipe_id, "recipe.recipe_id"))
        object.__setattr__(self, "name", _text(self.name, "recipe.name"))
        object.__setattr__(self, "intent", _text(self.intent, "recipe.intent"))
        steps = tuple(self.steps)
        if not steps:
            raise RecipeValidationError("recipe.steps must contain at least one step")
        if not all(isinstance(step, RecipeStep) for step in steps):
            raise TypeError("all recipe steps must be RecipeStep")
        step_ids = [step.step_id for step in steps]
        if len(step_ids) != len(set(step_ids)):
            raise RecipeValidationError("recipe step_id values must be unique")

        seen: set[str] = set()
        for step in steps:
            for dependency in step.depends_on:
                if dependency not in seen:
                    raise RecipeValidationError(
                        f"step {step.step_id} dependency must reference an earlier step: {dependency}"
                    )
            seen.add(step.step_id)
        object.__setattr__(self, "steps", steps)


@dataclass(frozen=True)
class RecipeStepPlan:
    step: RecipeStep
    job: N0TEableJob
    resolution: CapabilityResolution

    @property
    def available(self) -> bool:
        return self.resolution.status == "RESOLVED"


@dataclass(frozen=True)
class RecipePlan:
    recipe_id: str
    environment_id: str
    status: str
    step_plans: tuple[RecipeStepPlan, ...]

    @property
    def unavailable_step_ids(self) -> tuple[str, ...]:
        return tuple(
            item.step.step_id for item in self.step_plans if not item.available
        )

    @property
    def declared_authority_classes(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(item.step.authority_class for item in self.step_plans)
        )


class RecipePlanner:
    """Pure Studio support planner for host-neutral Recipes.

    Planning derives stable jobs and delegates all route legitimacy/scoring to
    StudioCapabilityProfile. It does not execute steps, grant authority, create
    transactions, assert postconditions, or perform recovery.
    """

    @staticmethod
    def _job(recipe: RecipeDefinition, step: RecipeStep) -> N0TEableJob:
        return N0TEableJob(
            id=f"recipe:{recipe.recipe_id}:step:{step.step_id}",
            capability=step.capability,
            description=step.description,
        )

    def plan(
        self,
        recipe: RecipeDefinition,
        studio: StudioCapabilityProfile,
        constraints: ResolutionConstraints = ResolutionConstraints(),
    ) -> RecipePlan:
        if not isinstance(recipe, RecipeDefinition):
            raise TypeError("recipe must be RecipeDefinition")
        if not isinstance(studio, StudioCapabilityProfile):
            raise TypeError("studio must be StudioCapabilityProfile")
        if not isinstance(constraints, ResolutionConstraints):
            raise TypeError("constraints must be ResolutionConstraints")

        plans: list[RecipeStepPlan] = []
        for step in recipe.steps:
            job = self._job(recipe, step)
            plans.append(
                RecipeStepPlan(
                    step=step,
                    job=job,
                    resolution=studio.resolve(job, constraints),
                )
            )
        step_plans = tuple(plans)
        status = "READY" if all(item.available for item in step_plans) else "UNAVAILABLE"
        return RecipePlan(
            recipe_id=recipe.recipe_id,
            environment_id=studio.environment_id,
            status=status,
            step_plans=step_plans,
        )
