from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .career_state import (
    CAREER_STATES,
    CareerStateMemory,
    career_state_definition,
    career_state_definitions,
)
from .consumer_shell import ConsumerShell, ConsumerShellError, _PageState
from .lineage import LineageCorruptionError, ValidationError

_MAX_RATIONALE = 1000
_TOPIC_LABELS = {
    "ESSENTIAL_INCOME": "dependable income",
    "OBLIGATIONS": "real obligations",
    "CREATION": "your own creative work",
    "FINISHING": "finishing decisions",
    "RELEASE": "release work",
    "AUDIENCE": "audience development",
    "OPPORTUNITY": "career opportunities",
    "LIVE": "live delivery",
    "LOGISTICS": "touring logistics",
    "CLIENT_WORK": "client work",
    "RECOVERY": "recovery",
    "LEARNING": "learning",
    "EXPERIMENTATION": "experimentation",
    "SYSTEMS": "repeatable systems",
    "PORTFOLIO": "portfolio evidence",
    "OPTIONAL_EXPANSION": "optional expansion",
    "HIGH_LOAD_WORK": "high-load optional work",
}


def _career(shell: ConsumerShell) -> CareerStateMemory:
    return CareerStateMemory(shell.runtime.headquarters.store)


def _topic_list(topics: tuple[str, ...]) -> str:
    if not topics:
        return "Nothing is automatically pushed down."
    return ", ".join(_TOPIC_LABELS[item] for item in topics)


def _history(shell: ConsumerShell, memory: CareerStateMemory) -> str:
    entries = memory.history()
    if len(entries) <= 1:
        return ""
    rows: list[str] = []
    for entry in reversed(entries[:-1]):
        definition = career_state_definition(entry.state)
        rationale = (
            ""
            if entry.rationale is None
            else f'<br><span class="muted">Context: {html.escape(entry.rationale)}</span>'
        )
        rows.append(
            '<li>'
            f'<strong>{html.escape(definition.label)}</strong>{rationale}'
            '</li>'
        )
    return (
        '<details><summary>Prior Career State history</summary>'
        '<ul class="stack">' + "".join(rows) + '</ul>'
        '<p class="muted">Changing Career State adds context. It does not rewrite the seasons that came before it.</p>'
        '</details>'
    )


def _catalog() -> str:
    rows = "".join(
        '<p><strong>'
        + html.escape(definition.label)
        + '</strong> · '
        + html.escape(definition.summary)
        + '</p>'
        for definition in career_state_definitions()
    )
    return (
        '<details><summary>What the Career States mean</summary>'
        + rows
        + '<p class="muted">These are working seasons, not personality types, seniority levels, diagnoses, or permanent identities.</p>'
        '</details>'
    )


def _career_state_content(shell: ConsumerShell) -> str:
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("Career State requires an open Artist workspace")
    memory = _career(shell)
    current = memory.current_state()
    if current is None:
        current_html = (
            '<p class="status caution">No Career State set</p>'
            '<p>N0TE will not guess your career season from activity, followers, revenue, credits, or time in the industry.</p>'
        )
        posture_html = (
            '<p class="status caution">Neutral recommendation posture</p>'
            '<p>Until you choose a Career State, this context adds no extra weighting to optional recommendations.</p>'
        )
        expected = "~"
    else:
        definition = career_state_definition(current.state)
        rationale = (
            ""
            if current.rationale is None
            else '<p>Why this fits now</p><p>' + html.escape(current.rationale) + '</p>'
        )
        current_html = (
            f'<p class="status good">{html.escape(definition.label)} Career State</p>'
            f'<p>{html.escape(definition.summary)}</p>{rationale}'
            '<p class="muted">Source: Artist-declared local context. N0TE did not infer or externally verify this state.</p>'
        )
        posture_html = (
            '<p><strong>Put more weight on:</strong> '
            + html.escape(_topic_list(definition.favor))
            + '.</p><p><strong>Protect:</strong> '
            + html.escape(_topic_list(definition.protect))
            + '.</p><p><strong>Optional work to deprioritize:</strong> '
            + html.escape(_topic_list(definition.defer_optional))
            + '.</p>'
        )
        expected = current.id

    token = shell._new_action("career-state-set", expected)
    buttons = "".join(
        '<button type="submit" name="state" value="'
        + html.escape(state, quote=True)
        + '">'
        + html.escape(career_state_definition(state).label)
        + '</button>'
        for state in CAREER_STATES
    )
    form = (
        '<form class="stack" method="post" action="/career-state/set">'
        + shell._hidden(token)
        + f'<div><label for="career-state-context">Context <span class="muted">optional</span></label><input id="career-state-context" name="rationale" type="text" maxlength="{_MAX_RATIONALE}" placeholder="What makes this the right season right now?"></div>'
        + '<div class="row">'
        + buttons
        + '</div></form>'
    )
    return (
        '<section class="stack" aria-label="Career State">'
        '<div class="card"><h2>Your Career State</h2>'
        + current_html
        + '</div>'
        '<div class="card stack"><h2>Review the season, not your identity</h2>'
        '<p>Choose the broader season N0TE should consider when ranking optional next steps. You can change it whenever reality changes.</p>'
        + form
        + _catalog()
        + '</div>'
        '<div class="card"><h2>Recommendation posture</h2>'
        + posture_html
        + '<p class="muted">Career State is qualitative weighting only. It never overrides an explicit request, safety, rights, stale-context checks, or action authority, and it never sends, spends, publishes, purchases, connects, or mutates a DAW.</p></div>'
        + _history(shell, memory)
        + '</section>'
    )


def _clean_rationale(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > _MAX_RATIONALE:
        raise ConsumerShellError("Career State context is too long")
    return text


def _post_career_state(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "career-state-set")
    if action is None or action.value is None:
        raise ConsumerShellError("That Career State action was already handled or expired")
    expected_current_id = None if action.value == "~" else action.value
    memory = _career(shell)
    current = memory.current_state()
    actual_current_id = None if current is None else current.id
    if actual_current_id != expected_current_id:
        raise ConsumerShellError(
            "Career State changed in another view. Reload NOW before replacing newer context."
        )
    selected = str(form.get("state", "")).strip()
    if not selected:
        raise ConsumerShellError("Choose a Career State")
    rationale = _clean_rationale(form.get("rationale"))
    result = memory.record_state(
        selected,
        rationale=rationale,
        expected_current_id=expected_current_id,
    )
    definition = career_state_definition(result.state)
    shell._consumer_notice = (
        f"{definition.label} Career State recorded as Artist-declared context. "
        "It changes recommendation posture only and grants no external action authority."
    )


def install_career_state_headquarters() -> None:
    """Add reviewable Career State context to the existing NOW surface."""
    if getattr(ConsumerShell, "_career_state_headquarters_installed", False):
        return

    original_state_content: Callable[[ConsumerShell, _PageState], str] = ConsumerShell._state_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_career_state_content(self: ConsumerShell, state: _PageState) -> str:
        content = original_state_content(self, state)
        if state.kind != "running-now":
            return content
        return content + _career_state_content(self)

    def with_career_state_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path != "/career-state/set":
            original_post(self, handler)
            return
        if not self._request_host_is_exact(handler) or not self._post_origin_is_allowed(handler):
            self._send_html(
                handler,
                403,
                self._simple_error("That action did not come from this N0TE window."),
            )
            return
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before changing Career State."),
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
            _post_career_state(self, form)
        except (ValidationError, ConsumerShellError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/now")
            return
        except LineageCorruptionError:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE found unreadable Career State history and stopped before rewriting it."
                ),
            )
            return
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that Career State action before it could become unclear Artist context."
                ),
            )
            return
        self._redirect(handler, "/now")

    ConsumerShell._state_content = with_career_state_content
    ConsumerShell._handle_post = with_career_state_post
    ConsumerShell._career_state_headquarters_installed = True
