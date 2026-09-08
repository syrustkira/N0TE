from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError
from .learning_experiment import (
    LearningDecisionBinding,
    LearningExperimentError,
    LearningExperimentService,
    LearningStartBinding,
    StaleLearningExperimentError,
)
from .lineage import ValidationError

_CONFIDENCE_CHOICES = (
    ("LOW", "Low confidence"),
    ("MEDIUM", "Medium confidence"),
    ("HIGH", "High confidence"),
)
_DECISION_LABELS = {
    "KEEP": "Keep this change",
    "REVERT": "Revert this change",
    "REVISE": "Revise and try again",
    "INCONCLUSIVE": "Inconclusive for now",
}
_SOURCE_LABELS = {
    "USER_DECLARED": "You reported this",
    "OBSERVED": "Observed in real work",
    "MEASURED": "Measured evidence",
    "PROVIDER_VERIFIED": "Provider-verified evidence",
    "REMEMBERED": "Remembered context",
    "INFERRED": "Inferred, not directly observed",
}


def _confidence_options(*, selected: str = "MEDIUM") -> str:
    return "".join(
        f'<option value="{key}"{" selected" if key == selected else ""}>'
        f'{html.escape(label)}</option>'
        for key, label in _CONFIDENCE_CHOICES
    )


def _confidence_text(value: float) -> str:
    return f"Confidence {round(value * 100)}%"


def _start_action(shell: ConsumerShell, binding: LearningStartBinding) -> str:
    return shell._new_action(
        "learning-start",
        json.dumps([binding.song_id, binding.session_id], separators=(",", ":")),
    )


def _observation_action(shell: ConsumerShell, episode_id: str) -> str:
    return shell._new_action("learning-observe", episode_id)


def _decision_action(shell: ConsumerShell, binding: LearningDecisionBinding) -> str:
    return shell._new_action(
        "learning-decide",
        json.dumps(
            [binding.episode_id, list(binding.expected_consequence_ids)],
            separators=(",", ":"),
        ),
    )


def _episode_markup(shell: ConsumerShell, service: LearningExperimentService, episode) -> str:
    observations: list[str] = []
    for item in episode.consequences:
        details: list[str] = [
            _SOURCE_LABELS.get(item.source_kind, "Recorded evidence"),
            _confidence_text(item.confidence),
        ]
        if item.conditions:
            details.append("Conditions: " + "; ".join(item.conditions))
        if item.confounders:
            details.append("Possible confounders: " + "; ".join(item.confounders))
        observations.append(
            '<li class="stack">'
            f'<p>{html.escape(item.observation)}</p>'
            f'<p class="muted">{html.escape(" · ".join(details))}</p>'
            '</li>'
        )
    observed = (
        '<p class="muted">Nothing observed yet.</p>'
        if not observations
        else '<ul class="stack" aria-label="Observed after the change">' + "".join(observations) + "</ul>"
    )

    if episode.decision is None:
        observe = (
            '<form class="stack" method="post" action="/learning/observe" '
            f'aria-label="Record observation for {html.escape(episode.subject_ref, quote=True)}">'
            f'{shell._hidden(_observation_action(shell, episode.id))}'
            '<div><label>What did you observe afterward?'
            '<textarea name="observation" maxlength="1200" rows="3" required></textarea></label></div>'
            '<div><label>How confident are you in that observation?'
            '<select name="confidence" required>'
            f'{_confidence_options()}</select></label></div>'
            '<div><label>Conditions worth remembering (optional)'
            '<textarea name="conditions" maxlength="600" rows="2"></textarea></label></div>'
            '<div><label>What else might have affected the result? (optional)'
            '<textarea name="confounders" maxlength="600" rows="2"></textarea></label></div>'
            '<button type="submit">Record what I observed</button>'
            '</form>'
        )
        decision = ""
        if episode.consequences:
            binding = service.decision_binding(episode.id)
            decision_options = "".join(
                f'<option value="{kind}">{html.escape(label)}</option>'
                for kind, label in _DECISION_LABELS.items()
            )
            decision = (
                '<form class="stack" method="post" action="/learning/decide" '
                f'aria-label="Decide {html.escape(episode.subject_ref, quote=True)} Learning experiment">'
                f'{shell._hidden(_decision_action(shell, binding))}'
                '<div><label>What do you decide from what you saw?'
                f'<select name="decision" required>{decision_options}</select></label></div>'
                '<div><label>Why?'
                '<textarea name="rationale" maxlength="1200" rows="3" required></textarea></label></div>'
                '<div><label>How confident are you in this decision?'
                '<select name="confidence" required>'
                f'{_confidence_options()}</select></label></div>'
                '<button type="submit">Record this decision</button>'
                '<p class="muted">A decision closes this Learning experiment. New observations will require a new experiment.</p>'
                '</form>'
            )
        controls = observe + decision
        final = '<p class="status">Decision still open</p>'
    else:
        controls = ""
        decision_label = _DECISION_LABELS.get(episode.decision.decision, episode.decision.decision.title())
        final = (
            f'<p class="status">Decision: {html.escape(decision_label)}</p>'
            f'<p>{html.escape(episode.decision.rationale)}</p>'
            f'<p class="muted">{html.escape(_confidence_text(episode.decision.confidence))}</p>'
        )

    return (
        '<li class="stack">'
        f'<p><strong>{html.escape(episode.domain)} · {html.escape(episode.subject_ref)}</strong></p>'
        f'<p><strong>Change tried:</strong> {html.escape(episode.change_description)}</p>'
        '<p><strong>Observed after the change:</strong></p>'
        f'{observed}{final}{controls}'
        '</li>'
    )


def _learning_card(shell: ConsumerShell) -> str:
    service = LearningExperimentService(shell.runtime.headquarters.learning)
    episodes = tuple(reversed(service.episodes_for_active_song()))
    history = (
        '<p class="muted">No Learning experiments are recorded for this Song yet.</p>'
        if not episodes
        else '<ul class="stack" aria-label="Song Learning history">'
        + "".join(_episode_markup(shell, service, episode) for episode in episodes)
        + "</ul>"
    )
    binding = service.start_binding()
    if binding is None:
        start = (
            '<p class="muted">Open a work Session on this Song to start a new Learning experiment. '
            'Existing Learning history remains available after the Session closes.</p>'
        )
    else:
        start = (
            '<form class="stack" method="post" action="/learning/start" aria-label="Start Learning experiment">'
            f'{shell._hidden(_start_action(shell, binding))}'
            '<div><label>Area you are testing'
            '<input name="domain" type="text" maxlength="120" required></label></div>'
            '<div><label>What are you changing?'
            '<input name="subject" type="text" maxlength="160" required></label></div>'
            '<div><label>Concrete change you are trying'
            '<textarea name="change" maxlength="1200" rows="3" required></textarea></label></div>'
            '<button type="submit">Start this Learning experiment</button>'
            '</form>'
        )
    return (
        '<div class="card"><h2>What happened after that change?</h2>'
        '<p>Keep the chain honest: record the change, what you observed afterward, then your decision. '
        'This preserves sequence and judgment; it does not prove the change caused the outcome.</p>'
        f'{history}<h3>Try one change deliberately</h3>{start}</div>'
    )


def _decode_start(value: str) -> LearningStartBinding:
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, list) or len(decoded) != 2 or not all(
            isinstance(item, str) and item for item in decoded
        ):
            raise ValueError
        return LearningStartBinding(decoded[0], decoded[1])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise StaleLearningExperimentError("That Learning action is no longer valid.") from exc


def _decode_decision(value: str) -> LearningDecisionBinding:
    try:
        decoded = json.loads(value)
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or not isinstance(decoded[0], str)
            or not decoded[0]
            or not isinstance(decoded[1], list)
            or not decoded[1]
            or not all(isinstance(item, str) and item for item in decoded[1])
        ):
            raise ValueError
        return LearningDecisionBinding(decoded[0], tuple(decoded[1]))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise StaleLearningExperimentError("That Learning decision is no longer valid.") from exc


def _post_start(shell: ConsumerShell, handler: BaseHTTPRequestHandler, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "learning-start")
    if action is None or action.value is None:
        shell._send_html(handler, 409, shell._simple_error("That Learning action was already handled or expired."))
        return
    service = LearningExperimentService(shell.runtime.headquarters.learning)
    service.start_episode(
        _decode_start(action.value),
        domain=form.get("domain", ""),
        subject=form.get("subject", ""),
        change_description=form.get("change", ""),
    )
    shell._consumer_notice = "Learning experiment started. Record what you actually observe afterward."
    shell._redirect(handler, "/song")


def _post_observe(shell: ConsumerShell, handler: BaseHTTPRequestHandler, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "learning-observe")
    if action is None or action.value is None:
        shell._send_html(handler, 409, shell._simple_error("That observation action was already handled or expired."))
        return
    service = LearningExperimentService(shell.runtime.headquarters.learning)
    service.append_observation(
        action.value,
        observation=form.get("observation", ""),
        confidence=form.get("confidence", ""),
        conditions=form.get("conditions"),
        confounders=form.get("confounders"),
    )
    shell._consumer_notice = "Observation recorded as something you noticed after the change, not proof of cause."
    shell._redirect(handler, "/song")


def _post_decide(shell: ConsumerShell, handler: BaseHTTPRequestHandler, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "learning-decide")
    if action is None or action.value is None:
        shell._send_html(handler, 409, shell._simple_error("That Learning decision was already handled or expired."))
        return
    service = LearningExperimentService(shell.runtime.headquarters.learning)
    decision = service.decide(
        _decode_decision(action.value),
        decision=form.get("decision", ""),
        rationale=form.get("rationale", ""),
        confidence=form.get("confidence", ""),
    )
    shell._consumer_notice = f"Learning decision recorded: {_DECISION_LABELS.get(decision.decision, decision.decision.title())}."
    shell._redirect(handler, "/song")


def install_song_learning_experiments() -> None:
    """Attach bounded Learning history/forms and three artist-authority POSTs once."""
    if getattr(ConsumerShell, "_song_learning_experiments_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_learning_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError("Song page structure changed before Learning could be attached safely")
        return rendered[: -len(marker)] + _learning_card(self) + marker

    def with_learning_posts(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path not in {"/learning/start", "/learning/observe", "/learning/decide"}:
            original_post(self, handler)
            return
        if not self._request_host_is_exact(handler) or not self._post_origin_is_allowed(handler):
            self._send_html(handler, 403, self._simple_error("That action did not come from this N0TE window."))
            return
        form = self._read_form(handler)
        if form is None or not self._form_authorized(form):
            self._send_html(handler, 403, self._simple_error("That action expired. Reload N0TE and try again."))
            return
        try:
            if path == "/learning/start":
                _post_start(self, handler, form)
            elif path == "/learning/observe":
                _post_observe(self, handler, form)
            else:
                _post_decide(self, handler, form)
        except StaleLearningExperimentError as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
        except (ValidationError, LearningExperimentError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/song")
        except ConsumerShellError as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/song")
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error("N0TE stopped that Learning action before it could become an unclear consumer state."),
            )

    ConsumerShell._song_content = with_learning_card
    ConsumerShell._handle_post = with_learning_posts
    ConsumerShell._song_learning_experiments_installed = True
