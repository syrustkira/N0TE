from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from . import creative_suggestions_shell as suggestions_shell
from .consumer_shell import ConsumerShell, ConsumerShellError
from .creative_suggestions import CreativeSuggestion
from .lineage import ValidationError

_FEEDBACK_ACTION_KIND = "suggestion-feedback"


def _feedback_binding(result: CreativeSuggestion, direction: str) -> str:
    return json.dumps(
        {
            "dimension": result.dimension,
            "direction": direction,
            "distance": result.distance,
            "semantic_key": result.semantic_key,
            "session_id": result.session_id,
            "song_id": result.song_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _feedback_action(
    shell: ConsumerShell, result: CreativeSuggestion, direction: str
) -> str:
    return shell._new_action(
        _FEEDBACK_ACTION_KIND, _feedback_binding(result, direction)
    )


def _feedback_controls(shell: ConsumerShell, result: CreativeSuggestion) -> str:
    if result.session_id is None:
        return (
            '<div class="stack">'
            '<p class="muted">Start a work Session to remember More/Less feedback '
            'for this suggestion.</p></div>'
        )
    more = _feedback_action(shell, result, "MORE")
    less = _feedback_action(shell, result, "LESS")
    return (
        '<div class="stack" aria-label="Suggestion feedback">'
        '<p><strong>Shape future context, softly</strong></p>'
        '<div class="actions">'
        '<form method="post" action="/suggestion/feedback" '
        'aria-label="More like this suggestion">'
        f'{shell._hidden(more)}'
        '<button type="submit">More like this</button></form>'
        '<form method="post" action="/suggestion/feedback" '
        'aria-label="Less like this suggestion">'
        f'{shell._hidden(less)}'
        '<button type="submit">Less like this</button></form>'
        '</div>'
        '<p class="muted">This records context for the exact idea you were shown. '
        'It does not silently become a taste rule, change the Song, or alter '
        'suggestion weighting by itself.</p>'
        '</div>'
    )


def _decode_binding(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("suggestion feedback binding is invalid") from exc
    if not isinstance(payload, dict):
        raise ValidationError("suggestion feedback binding is invalid")
    expected = {
        "dimension",
        "direction",
        "distance",
        "semantic_key",
        "session_id",
        "song_id",
    }
    if set(payload) != expected:
        raise ValidationError("suggestion feedback binding is incomplete")
    return payload


def _post_feedback(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), _FEEDBACK_ACTION_KIND)
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error(
                "That suggestion feedback action was already handled or expired."
            ),
        )
        return

    binding = _decode_binding(action.value)
    result = getattr(shell, "_creative_suggestion_result", None)
    if not isinstance(result, CreativeSuggestion):
        shell._send_html(
            handler,
            409,
            shell._simple_error(
                "That suggestion is no longer current. Ask for a suggestion again."
            ),
        )
        return

    expected = {
        "dimension": result.dimension,
        "distance": result.distance,
        "semantic_key": result.semantic_key,
        "session_id": result.session_id,
        "song_id": result.song_id,
    }
    for key, value in expected.items():
        if binding.get(key) != value:
            shell._send_html(
                handler,
                409,
                shell._simple_error(
                    "The current suggestion changed before that feedback was recorded."
                ),
            )
            return
    direction = binding.get("direction")
    if direction not in {"MORE", "LESS"}:
        raise ValidationError("suggestion feedback direction is invalid")

    event = shell.runtime.headquarters.suggestion_feedback.record(
        result, direction=direction
    )
    phrase = "More like this" if event.direction == "MORE" else "Less like this"
    shell._consumer_notice = (
        f"{phrase} remembered as context for this exact suggestion. "
        "No taste rule or automatic weighting was created."
    )
    shell._redirect(handler, "/song")


def install_song_suggestion_feedback() -> None:
    """Compose contextual More/Less feedback without owning the suggestion shell."""

    if getattr(ConsumerShell, "_song_suggestion_feedback_installed", False):
        return

    original_markup = suggestions_shell._result_markup
    original_post: Callable[
        [ConsumerShell, BaseHTTPRequestHandler], None
    ] = ConsumerShell._handle_post

    def with_feedback_markup(
        shell: ConsumerShell, result: CreativeSuggestion | None
    ) -> str:
        rendered = original_markup(shell, result)
        if result is None:
            return rendered
        return rendered + _feedback_controls(shell, result)

    def with_feedback_post(
        self: ConsumerShell, handler: BaseHTTPRequestHandler
    ) -> None:
        if self._path(handler) != "/suggestion/feedback":
            original_post(self, handler)
            return
        if not self._request_host_is_exact(handler) or not self._post_origin_is_allowed(
            handler
        ):
            self._send_html(
                handler,
                403,
                self._simple_error(
                    "That feedback action did not come from this N0TE window."
                ),
            )
            return
        form = self._read_form(handler)
        if form is None or not self._form_authorized(form):
            self._send_html(
                handler,
                403,
                self._simple_error(
                    "That feedback action expired. Reload N0TE and try again."
                ),
            )
            return
        try:
            _post_feedback(self, handler, form)
        except (ValidationError, ConsumerShellError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/song")
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that feedback action before it could become "
                    "unclear suggestion memory."
                ),
            )

    suggestions_shell._result_markup = with_feedback_markup
    ConsumerShell._handle_post = with_feedback_post
    ConsumerShell._song_suggestion_feedback_installed = True
