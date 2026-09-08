from __future__ import annotations

from dataclasses import dataclass

from .learning import LearningEpisode, LearningMemory
from .lineage import ValidationError

INTERACTION_DEPTH_MODES = (
    "DO_IT",
    "WITH_ME",
    "SHOW_ME",
    "EXPLAIN_WHY",
    "LET_ME_TRY",
)

_MODE_LABELS = {
    "DO_IT": "DO IT",
    "WITH_ME": "WITH ME",
    "SHOW_ME": "SHOW ME",
    "EXPLAIN_WHY": "EXPLAIN WHY",
    "LET_ME_TRY": "LET ME TRY",
}
_SOURCE_LABELS = {
    "USER_DECLARED": "artist-reported",
    "OBSERVED": "observed",
    "MEASURED": "measured",
    "PROVIDER_VERIFIED": "provider-verified",
    "REMEMBERED": "remembered",
    "INFERRED": "inferred",
}


class InteractionDepthError(RuntimeError):
    """A requested collaboration-depth plan cannot be prepared truthfully."""


class StaleInteractionDepthError(InteractionDepthError):
    """The Song or Learning evidence moved after the interaction choice was prepared."""


@dataclass(frozen=True)
class InteractionDepthBinding:
    song_id: str
    episode_id: str
    expected_consequence_ids: tuple[str, ...]
    expected_decision_id: str | None


@dataclass(frozen=True)
class InteractionPlanStep:
    actor: str
    instruction: str


@dataclass(frozen=True)
class InteractionDepthPlan:
    mode: str
    label: str
    song_id: str
    episode_id: str
    subject: str
    change: str
    n0te_role: str
    artist_role: str
    next_step: str
    steps: tuple[InteractionPlanStep, ...]
    evidence_summary: tuple[str, ...]
    execution_requested: bool
    action_authority_granted: bool = False
    mutation_permitted_by_mode: bool = False


class InteractionDepthService:
    """Separate collaboration depth from mutation authority for one real job.

    A mode expresses how much agency the artist wants N0TE to take. It never grants
    action authority, execution eligibility, provider access, entitlement, approval,
    or mutation permission. The bounded consumer seam is one current canonical
    Learning episode. Plans use the exact change and represented consequence evidence
    so the five modes alter useful behavior rather than merely relabeling one answer.
    """

    def __init__(self, learning: LearningMemory):
        if not isinstance(learning, LearningMemory):
            raise TypeError("InteractionDepthService requires canonical LearningMemory")
        self.learning = learning
        self.store = learning.store

    @staticmethod
    def normalize_mode(value: str) -> str:
        mode = str(value).strip().upper().replace(" ", "_")
        if mode not in INTERACTION_DEPTH_MODES:
            raise ValidationError(f"unsupported interaction depth: {mode}")
        return mode

    @staticmethod
    def _binding_for(episode: LearningEpisode) -> InteractionDepthBinding:
        return InteractionDepthBinding(
            song_id=episode.song_id,
            episode_id=episode.id,
            expected_consequence_ids=tuple(item.id for item in episode.consequences),
            expected_decision_id=None if episode.decision is None else episode.decision.id,
        )

    def _current_open_episode(self) -> LearningEpisode | None:
        song = self.store.active_song()
        if song is None:
            return None
        for episode in reversed(self.learning.episodes_for_song(song.id)):
            if episode.decision is None:
                return episode
        return None

    def binding_for(self, episode_id: str) -> InteractionDepthBinding:
        current = self._current_open_episode()
        if current is None or current.id != str(episode_id):
            raise StaleInteractionDepthError(
                "That Learning job is no longer the current open job for the active Song."
            )
        return self._binding_for(current)

    def current_binding(self) -> InteractionDepthBinding | None:
        episode = self._current_open_episode()
        return None if episode is None else self._binding_for(episode)

    def _episode_for_binding(self, binding: InteractionDepthBinding) -> LearningEpisode:
        if not isinstance(binding, InteractionDepthBinding):
            raise TypeError("binding must be InteractionDepthBinding")
        active = self.store.active_song()
        episode = self.learning.get_episode(binding.episode_id)
        if active is None or active.id != binding.song_id:
            raise StaleInteractionDepthError(
                "The active Song changed after this interaction choice was prepared."
            )
        if episode is None or episode.song_id != binding.song_id:
            raise StaleInteractionDepthError(
                "That Learning job no longer belongs to the active Song."
            )
        current = self._current_open_episode()
        if current is None or current.id != binding.episode_id:
            raise StaleInteractionDepthError(
                "The current Learning job changed after this interaction choice was prepared."
            )
        current_consequence_ids = tuple(item.id for item in episode.consequences)
        current_decision_id = None if episode.decision is None else episode.decision.id
        if (
            current_consequence_ids != binding.expected_consequence_ids
            or current_decision_id != binding.expected_decision_id
        ):
            raise StaleInteractionDepthError(
                "That Learning job changed after this interaction choice was prepared."
            )
        if episode.decision is not None:
            raise StaleInteractionDepthError(
                "That Learning job already has a final decision. Start or choose an open job."
            )
        return episode

    @staticmethod
    def _evidence_summary(episode: LearningEpisode) -> tuple[str, ...]:
        if not episode.consequences:
            return (
                "No consequence has been recorded yet. Treat the proposed change as a test, not a proven fix.",
            )
        out: list[str] = []
        for item in episode.consequences:
            try:
                source = _SOURCE_LABELS[item.source_kind]
            except KeyError as exc:
                raise InteractionDepthError(
                    "Learning evidence source semantics changed; interaction guidance stopped safely."
                ) from exc
            details = [
                f"{item.observation} ({source}, {round(item.confidence * 100)}% confidence)"
            ]
            if item.conditions:
                details.append("Conditions: " + "; ".join(item.conditions))
            if item.confounders:
                details.append("Possible confounders: " + "; ".join(item.confounders))
            out.append(". ".join(details))
        return tuple(out)

    @classmethod
    def _plan_for(cls, episode: LearningEpisode, mode: str) -> InteractionDepthPlan:
        evidence = cls._evidence_summary(episode)
        common = dict(
            mode=mode,
            label=_MODE_LABELS[mode],
            song_id=episode.song_id,
            episode_id=episode.id,
            subject=episode.subject_ref,
            change=episode.change_description,
            evidence_summary=evidence,
            action_authority_granted=False,
            mutation_permitted_by_mode=False,
        )
        if mode == "DO_IT":
            steps = (
                InteractionPlanStep(
                    "N0TE",
                    f"Keep the requested scope exact: {episode.change_description}. Do not silently broaden the job.",
                ),
                InteractionPlanStep(
                    "N0TE",
                    "Resolve whether a verified executor exists for this exact job and current environment. If none exists, stop at a truthful blocked state.",
                ),
                InteractionPlanStep(
                    "AUTHORITY",
                    "If the route would mutate the project or an outside system, obtain the separate exact authority that route requires before execution.",
                ),
                InteractionPlanStep(
                    "YOU",
                    "Judge the actual result, then record what was observed so N0TE can compare evidence rather than assume success.",
                ),
            )
            return InteractionDepthPlan(
                **common,
                n0te_role=(
                    "Lead the job as far as verified capability and separate action authority permit; "
                    "never use the collaboration mode itself as permission."
                ),
                artist_role=(
                    "Provide any separately required approval and judge the result rather than being told a mutation succeeded."
                ),
                next_step=steps[0].instruction,
                steps=steps,
                execution_requested=True,
            )
        if mode == "WITH_ME":
            steps = (
                InteractionPlanStep(
                    "N0TE",
                    f"Frame one variable only: the current Learning job is '{episode.subject_ref}', and the bounded change is '{episode.change_description}'.",
                ),
                InteractionPlanStep(
                    "YOU",
                    "Make or approve only that bounded step while leaving unrelated parts alone as much as practical.",
                ),
                InteractionPlanStep(
                    "N0TE",
                    "Compare what you report afterward with the evidence already recorded here; call out uncertainty instead of declaring cause.",
                ),
                InteractionPlanStep(
                    "YOU",
                    "Record the consequence you actually noticed, then choose KEEP, REVERT, REVISE, or INCONCLUSIVE when the evidence is sufficient.",
                ),
            )
            return InteractionDepthPlan(
                **common,
                n0te_role=(
                    "Hold context, pace the work one bounded step at a time, and help interpret evidence without taking taste authority away."
                ),
                artist_role=(
                    "Perform or approve each meaningful step, listen or inspect the result, and make the judgment calls."
                ),
                next_step=steps[0].instruction,
                steps=steps,
                execution_requested=False,
            )
        if mode == "SHOW_ME":
            steps = (
                InteractionPlanStep(
                    "N0TE",
                    f"Walk through the baseline first: focus on '{episode.subject_ref}' before changing anything and name what would count as a noticeable difference.",
                ),
                InteractionPlanStep(
                    "N0TE",
                    f"Demonstrate the proposed path conceptually: BEFORE → '{episode.change_description}' → AFTER. This walkthrough does not claim the project was modified.",
                ),
                InteractionPlanStep(
                    "N0TE",
                    "Point out what to listen for or inspect in the before/after and which observations would still be ambiguous.",
                ),
                InteractionPlanStep(
                    "YOU",
                    "Decide whether the walkthrough is clear enough to try on the real Song, ask for explanation, or leave it alone.",
                ),
            )
            return InteractionDepthPlan(
                **common,
                n0te_role=(
                    "Give a concrete read-only walkthrough of the current test, including baseline, proposed change, comparison target, and uncertainty."
                ),
                artist_role="Inspect the walkthrough, question it, and decide whether to try it.",
                next_step=steps[0].instruction,
                steps=steps,
                execution_requested=False,
            )
        if mode == "EXPLAIN_WHY":
            steps = (
                InteractionPlanStep(
                    "N0TE",
                    f"State the hypothesis plainly: '{episode.change_description}' is being tested in relation to '{episode.subject_ref}'. That relationship is a question, not established causation.",
                ),
                InteractionPlanStep(
                    "N0TE",
                    "Explain why keeping the test bounded matters: changing fewer variables makes the before/after easier to interpret and easier to reverse or revise.",
                ),
                InteractionPlanStep(
                    "N0TE",
                    "Use the represented consequence evidence below to distinguish what is known, what is artist-reported, and what remains uncertain.",
                ),
                InteractionPlanStep(
                    "YOU",
                    "Challenge the reasoning, add missing context, or decide the experiment is not worth doing. No action is required to justify the analysis.",
                ),
            )
            return InteractionDepthPlan(
                **common,
                n0te_role=(
                    "Explain the hypothesis, evidence, tradeoffs, and uncertainty behind this exact Learning job without turning explanation into mutation."
                ),
                artist_role="Challenge the reasoning and keep final authority over musical taste.",
                next_step=steps[0].instruction,
                steps=steps,
                execution_requested=False,
            )
        steps = (
            InteractionPlanStep(
                "YOU",
                f"Try only this bounded change yourself: {episode.change_description}.",
            ),
            InteractionPlanStep(
                "YOU",
                "Keep unrelated variables as steady as practical so the result is easier to judge.",
            ),
            InteractionPlanStep(
                "N0TE",
                "Stand back while you act. Do not mutate the project from this mode.",
            ),
            InteractionPlanStep(
                "YOU",
                "When you are ready, record what you actually noticed; N0TE can then help compare, explain, or decide without rewriting your experience.",
            ),
        )
        return InteractionDepthPlan(
            **common,
            n0te_role=(
                "Preserve the exact job context, stand back while the artist acts, and be ready to help observe or interpret afterward."
            ),
            artist_role=(
                f"Try '{episode.change_description}' yourself, then describe or capture what actually changed."
            ),
            next_step=steps[0].instruction,
            steps=steps,
            execution_requested=False,
        )

    def plan(
        self,
        binding: InteractionDepthBinding,
        mode: str,
    ) -> InteractionDepthPlan:
        normalized = self.normalize_mode(mode)
        episode = self._episode_for_binding(binding)
        return self._plan_for(episode, normalized)
