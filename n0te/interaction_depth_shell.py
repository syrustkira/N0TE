from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError
from .interaction_depth import (
    INTERACTION_DEPTH_MODES,
    InteractionDepthBinding,
    InteractionDepthError,
    InteractionDepthPlan,
    InteractionDepthService,
    StaleInteractionDepthError,
)
from .lineage import ValidationError

_MODE_COPY = {
    "DO_IT": ("DO IT", "Ask N0TE to lead as far as verified capability and separate authority allow."),
    "WITH_ME": ("WITH ME", "Work one bounded step at a time with N0TE beside you."),
    "SHOW_ME": ("SHOW ME", "See a read-only demonstration or walkthrough before changing anything."),
    "EXPLAIN_WHY": ("EXPLAIN WHY", "Understand the reasoning, tradeoffs, evidence, and uncertainty first."),
    "LET_ME_TRY": ("LET ME TRY", "You act first; N0TE stands back and helps observe or compare afterward."),
}


def _action(shell: ConsumerShell, binding: InteractionDepthBinding) -> str:
    return shell._new_action(
        "interaction-depth",
        json.dumps(
            [
                binding.song_id,
                binding.episode_id,
                list(binding.expected_consequence_ids),
                binding.expected_decision_id,
            ],
            separators=(",", ":"),
        ),
    )


def _decode_binding(value: str) -> InteractionDepthBinding:
    try:
        raw = json.loads(value)
        if (
            not isinstance(raw, list)
            or len(raw) != 4
            or not isinstance(raw[0], str)
            or not raw[0]
            or not isinstance(raw[1], str)
            or not raw[1]
            or not isinstance(raw[2], list)
            or not all(isinstance(item, str) and item for item in raw[2])
            or (raw[3] is not None and (not isinstance(raw[3], str) or not raw[3]))
        ):
            raise ValueError
        return InteractionDepthBinding(
            song_id=raw[0],
            episode_id=raw[1],
            expected_consequence_ids=tuple(raw[2]),
            expected_decision_id=raw[3],
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise StaleInteractionDepthError("That interaction choice is no longer valid.") from exc


def _plan_markup(plan: InteractionDepthPlan) -> str:
    execution = (
        '<p class="status">Execution requested, but not granted here</p>'
        '<p class="muted">DO IT does not grant capability, approval, eligibility, or mutation authority. '
        'This surface will not claim the project changed unless a separate verified execution path actually does the work.</p>'
        if plan.execution_requested
        else '<p class="status">No execution requested by this interaction mode</p>'
    )
    evidence = (
        '<ul class="stack" aria-label="Evidence already represented for this Learning job">'
        + "".join(f'<li>{html.escape(item)}</li>' for item in plan.evidence_summary)
        + "</ul>"
    )
    steps = (
        '<ol class="stack" aria-label="Guided steps for this working style">'
        + "".join(
            '<li>'
            f'<strong>{html.escape(step.actor)}:</strong> {html.escape(step.instruction)}'
            '</li>'
            for step in plan.steps
        )
        + "</ol>"
    )
    return (
        '<div class="stack" aria-label="Selected interaction plan">'
        f'<p class="status">Working style: {html.escape(plan.label)}</p>'
        f'<p><strong>Current Learning job:</strong> {html.escape(plan.subject)}</p>'
        f'<p><strong>Change under test:</strong> {html.escape(plan.change)}</p>'
        f'<p><strong>N0TE role:</strong> {html.escape(plan.n0te_role)}</p>'
        f'<p><strong>Your role:</strong> {html.escape(plan.artist_role)}</p>'
        '<p><strong>Evidence already represented</strong></p>'
        f'{evidence}'
        '<p><strong>Work it this way</strong></p>'
        f'{steps}'
        f'<p><strong>Next step:</strong> {html.escape(plan.next_step)}</p>'
        f'{execution}'
        '<p class="muted">Interaction depth and action authority are separate. Choosing a teaching/collaboration mode never approves a mutation.</p>'
        '</div>'
    )


def _selected_plan(shell: ConsumerShell, service: InteractionDepthService) -> str:
    selected = getattr(shell, "_interaction_depth_selection", None)
    if not isinstance(selected, tuple) or len(selected) != 2:
        return ""
    binding, mode = selected
    if not isinstance(binding, InteractionDepthBinding) or not isinstance(mode, str):
        shell._interaction_depth_selection = None
        return ""
    try:
        return _plan_markup(service.plan(binding, mode))
    except (InteractionDepthError, ValidationError):
        shell._interaction_depth_selection = None
        if service.current_binding() is None:
            return (
                '<p class="muted">That working style ended with the previous Learning job. '
                'Start a new Learning experiment when there is another real job to work through.</p>'
            )
        return (
            '<p class="muted">The Learning job changed after that working style was chosen. '
            'Choose a mode again from the current job below.</p>'
        )


def _mode_form(shell: ConsumerShell, binding: InteractionDepthBinding, mode: str) -> str:
    label, description = _MODE_COPY[mode]
    return (
        '<form class="stack" method="post" action="/interaction/depth" '
        f'aria-label="Choose {html.escape(label, quote=True)} interaction depth">'
        f'{shell._hidden(_action(shell, binding))}'
        f'<input type="hidden" name="mode" value="{html.escape(mode, quote=True)}">'
        f'<p><strong>{html.escape(label)}</strong> · {html.escape(description)}</p>'
        f'<button type="submit">Use {html.escape(label)}</button>'
        '</form>'
    )


def _interaction_card(shell: ConsumerShell) -> str:
    service = InteractionDepthService(shell.runtime.headquarters.learning)
    binding = service.current_binding()
    selected = _selected_plan(shell, service)
    if binding is None:
        controls = (
            '<p class="muted">Start an open Learning experiment for this Song to choose how N0TE should work with you on that exact job.</p>'
        )
    else:
        episode = service.learning.get_episode(binding.episode_id)
        if episode is None:
            raise ConsumerShellError("Current Learning job disappeared before interaction controls rendered")
        controls = (
            f'<p><strong>Choose how to work on:</strong> {html.escape(episode.subject_ref)}</p>'
            '<div class="stack" aria-label="Interaction depth choices">'
            + "".join(_mode_form(shell, binding, mode) for mode in INTERACTION_DEPTH_MODES)
            + "</div>"
        )
    return (
        '<div class="card"><h2>How should N0TE work with you?</h2>'
        '<p>Choose collaboration depth for the current Learning job. This is separate from ASK / ADVISE / TRY / DO authority, '
        'so a teaching mode can never silently approve a project change.</p>'
        f'{selected}{controls}'
        '</div>'
    )


def _post_depth(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "interaction-depth")
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That interaction choice was already handled or expired."),
        )
        return
    service = InteractionDepthService(shell.runtime.headquarters.learning)
    binding = _decode_binding(action.value)
    mode = form.get("mode", "")
    plan = service.plan(binding, mode)
    shell._interaction_depth_selection = (binding, plan.mode)
    shell._consumer_notice = f"Working style selected: {plan.label}. Action authority is unchanged."
    shell._redirect(handler, "/song")


def install_song_interaction_depth() -> None:
    """Attach five collaboration-depth choices to the current Song Learning job."""
    if getattr(ConsumerShell, "_song_interaction_depth_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_interaction_depth(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before interaction depth could be attached safely"
            )
        return rendered[: -len(marker)] + _interaction_card(self) + marker

    def with_interaction_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path != "/interaction/depth":
            original_post(self, handler)
            return
        if not self._request_host_is_exact(handler) or not self._post_origin_is_allowed(handler):
            self._send_html(
                handler,
                403,
                self._simple_error("That action did not come from this N0TE window."),
            )
            return
        form = self._read_form(handler)
        if form is None or not self._form_authorized(form):
            self._send_html(
                handler,
                403,
                self._simple_error("That action expired. Reload N0TE and try again."),
            )
            return
        try:
            _post_depth(self, handler, form)
        except StaleInteractionDepthError as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
        except (ValidationError, InteractionDepthError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/song")
        except ConsumerShellError as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/song")
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that interaction choice before it could become an unclear consumer state."
                ),
            )

    ConsumerShell._song_content = with_interaction_depth
    ConsumerShell._handle_post = with_interaction_post
    ConsumerShell._song_interaction_depth_installed = True
