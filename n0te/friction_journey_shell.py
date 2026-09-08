from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError
from .friction_journey import (
    FrictionCaptureBinding,
    FrictionJourneyError,
    FrictionObservationView,
    SongFrictionJourney,
    StaleFrictionJourneyError,
)
from .lineage import ValidationError

_CONFIDENCE_CHOICES = (
    ("LOW", "Low confidence"),
    ("MEDIUM", "Medium confidence"),
    ("HIGH", "High confidence"),
)


def _confidence_options() -> str:
    return "".join(
        f'<option value="{key}"{" selected" if key == "MEDIUM" else ""}>{html.escape(label)}</option>'
        for key, label in _CONFIDENCE_CHOICES
    )


def _confidence_text(value: float) -> str:
    return f"{round(value * 100)}% confidence"


def _capture_action(shell: ConsumerShell, binding: FrictionCaptureBinding) -> str:
    return shell._new_action(
        "friction-record",
        json.dumps([binding.song_id, binding.episode_id], separators=(",", ":")),
    )


def _observation(item: FrictionObservationView, *, include_hint: bool = True) -> str:
    hint = (
        ""
        if not include_hint or item.prevention_hint is None
        else f'<p class="muted"><strong>Prevention idea you recorded:</strong> {html.escape(item.prevention_hint)}</p>'
    )
    return (
        '<li class="stack">'
        f'<p><strong>{html.escape(item.key)}</strong> · {html.escape(item.description)}</p>'
        f'<p class="muted">{html.escape(item.source_label)} · {_confidence_text(item.confidence)}</p>'
        f'{hint}'
        '</li>'
    )


def _pattern(pattern) -> str:
    hints = (
        ""
        if not pattern.prevention_hints
        else '<p><strong>Prevention ideas you recorded</strong></p><ul>'
        + "".join(f'<li>{html.escape(item)}</li>' for item in pattern.prevention_hints)
        + "</ul>"
    )
    return (
        '<li class="stack">'
        f'<p><strong>{html.escape(pattern.key)}</strong></p>'
        f'<p class="status">Recurring across {pattern.session_count} work Sessions</p>'
        f'<p class="muted">{pattern.occurrence_count} explicit record{"" if pattern.occurrence_count == 1 else "s"}. '
        'Recurrence means the same blocker name appeared in distinct Sessions; it does not prove one universal cause.</p>'
        '<ul class="stack" aria-label="Evidence for recurring blocker">'
        + "".join(_observation(item, include_hint=False) for item in pattern.occurrences)
        + "</ul>"
        + hints
        + "</li>"
    )


def _episode(shell: ConsumerShell, service: SongFrictionJourney, episode) -> str:
    history = (
        '<p class="muted">No friction recorded against this Learning experiment.</p>'
        if not episode.observations
        else '<ul class="stack" aria-label="Friction recorded for this Learning experiment">'
        + "".join(_observation(item) for item in episode.observations)
        + "</ul>"
    )
    binding = service.capture_binding(episode.episode_id)
    label = f"Record friction for {episode.subject}: {episode.change}"
    form = (
        '<form class="stack" method="post" action="/friction/record" '
        f'aria-label="{html.escape(label, quote=True)}">'
        f'{shell._hidden(_capture_action(shell, binding))}'
        '<div><label>Short blocker name'
        '<input name="friction_key" type="text" maxlength="120" required></label></div>'
        '<p class="muted">Use the same blocker name when the same friction happens again. N0TE only calls it recurring after it appears in distinct work Sessions.</p>'
        '<div><label>What got in the way?'
        '<textarea name="description" maxlength="1200" rows="3" required></textarea></label></div>'
        '<div><label>How confident are you that this describes the friction you experienced?'
        f'<select name="confidence" required>{_confidence_options()}</select></label></div>'
        '<div><label>Prevention idea you want to remember (optional)'
        '<textarea name="prevention_hint" maxlength="600" rows="2"></textarea></label></div>'
        '<button type="submit">Record this blocker</button>'
        '<p class="muted">This records your report. It does not turn the blocker into a rule or prove why it happened.</p>'
        '</form>'
    )
    return (
        '<li class="stack">'
        f'<p><strong>{html.escape(episode.domain)} · {html.escape(episode.subject)}</strong></p>'
        f'<p><strong>Change being learned from:</strong> {html.escape(episode.change)}</p>'
        f'{history}{form}'
        '</li>'
    )


def _friction_card(shell: ConsumerShell) -> str:
    service = SongFrictionJourney(shell.runtime.headquarters.friction)
    patterns = service.recurring_for_active_song()
    pattern_body = (
        '<p class="muted">No recurring blocker is established for this Song yet. One incident remains one incident.</p>'
        if not patterns
        else '<ul class="stack" aria-label="Recurring blockers for this Song">'
        + "".join(_pattern(item) for item in patterns)
        + "</ul>"
    )
    episodes = tuple(reversed(service.episodes_for_active_song()))
    episode_body = (
        '<p class="muted">No Learning experiment exists for this Song yet. Start one above before attaching explicit Friction evidence to real work.</p>'
        if not episodes
        else '<ul class="stack" aria-label="Learning experiments available for Friction capture">'
        + "".join(_episode(shell, service, item) for item in episodes)
        + "</ul>"
    )
    return (
        '<div class="card"><h2>What keeps getting in the way?</h2>'
        '<p>Record explicit friction against real Learning episodes. N0TE keeps individual incidents visible, '
        'but only surfaces a recurring blocker when the same blocker name is evidenced across at least two distinct work Sessions.</p>'
        f'<h3>Recurring blockers</h3>{pattern_body}'
        f'<h3>Record friction from real work</h3>{episode_body}'
        '</div>'
    )


def _decode_binding(value: str) -> FrictionCaptureBinding:
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, list) or len(decoded) != 2 or not all(
            isinstance(item, str) and item for item in decoded
        ):
            raise ValueError
        return FrictionCaptureBinding(song_id=decoded[0], episode_id=decoded[1])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise StaleFrictionJourneyError("That Friction action is no longer valid.") from exc


def _post_record(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "friction-record")
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That Friction action was already handled or expired."),
        )
        return
    service = SongFrictionJourney(shell.runtime.headquarters.friction)
    observation = service.record(
        _decode_binding(action.value),
        friction_key=form.get("friction_key", ""),
        description=form.get("description", ""),
        confidence=form.get("confidence", ""),
        prevention_hint=form.get("prevention_hint"),
    )
    shell._consumer_notice = f"Recorded {observation.friction_key} as your Friction evidence. It becomes recurring only with evidence from another work Session."
    shell._redirect(handler, "/song")


def install_song_friction_journey() -> None:
    """Attach Song-scoped Friction capture/history/recurrence and one POST exactly once."""
    if getattr(ConsumerShell, "_song_friction_journey_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_friction_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before Friction Memory could be attached safely"
            )
        return rendered[: -len(marker)] + _friction_card(self) + marker

    def with_friction_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path != "/friction/record":
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
            _post_record(self, handler, form)
        except StaleFrictionJourneyError as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
        except (ValidationError, FrictionJourneyError) as exc:
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
                    "N0TE stopped that Friction action before it could become an unclear consumer state."
                ),
            )

    ConsumerShell._song_content = with_friction_card
    ConsumerShell._handle_post = with_friction_post
    ConsumerShell._song_friction_journey_installed = True
