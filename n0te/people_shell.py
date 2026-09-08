from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from . import consumer_shell as consumer_shell_module
from .consumer_shell import ConsumerShell, ConsumerShellError, _PageState
from .lineage import NotFoundError, ValidationError
from .people import FOLLOWUP_RESPONSIBILITIES, FollowUp, Person

_MAX_PERSON_NAME = 160
_MAX_RELATIONSHIP_CONTEXT = 800
_MAX_FOLLOWUP_SUMMARY = 600
_MAX_RESOLUTION_NOTE = 1000

_RESPONSIBILITY_LABELS = {
    "ARTIST_OWES": "I owe this",
    "WAITING_ON_OTHER": "Waiting on them",
    "MUTUAL": "We both have a next step",
}


def _clean_human_text(value: str, field: str, *, maximum: int) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise ConsumerShellError(f"{field} must not be empty")
    if len(text) > maximum:
        raise ConsumerShellError(f"{field} is too long")
    return text


def _optional_human_text(value: str | None, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > maximum:
        raise ConsumerShellError(f"{field} is too long")
    return text


def _person_action(shell: ConsumerShell) -> str:
    return shell._new_action("people-create")


def _followup_action(shell: ConsumerShell, person: Person) -> str:
    song = shell.runtime.headquarters.store.active_song()
    song_marker = "~" if song is None else song.id
    return shell._new_action("people-followup-create", f"{person.id}|{song_marker}")


def _close_action(shell: ConsumerShell, followup: FollowUp, kind: str) -> str:
    return shell._new_action(kind, followup.id)


def _person_form(shell: ConsumerShell) -> str:
    return (
        '<div class="card"><h2>Add someone you work with</h2>'
        '<p>Keep the relationship context here without pretending a provider account proves identity. N0TE will not merge this person with email, Contacts, social or credits automatically.</p>'
        '<form class="stack" method="post" action="/people/create" aria-label="Add a person">'
        f'{shell._hidden(_person_action(shell))}'
        f'<div><label for="person-name">Name</label><input id="person-name" name="display_name" type="text" maxlength="{_MAX_PERSON_NAME}" autocomplete="off" required></div>'
        f'<div><label for="person-context">Relationship context <span class="muted">optional</span></label><textarea id="person-context" name="relationship_context" maxlength="{_MAX_RELATIONSHIP_CONTEXT}" rows="3" placeholder="Producer on the EP, venue contact, manager candidate..."></textarea></div>'
        '<button class="primary" type="submit">Add person</button>'
        '<p class="muted">Local record only. This does not send, invite, connect, merge or sync anything.</p>'
        '</form></div>'
    )


def _responsibility_options() -> str:
    return "".join(
        f'<option value="{html.escape(value, quote=True)}">{html.escape(_RESPONSIBILITY_LABELS[value])}</option>'
        for value in ("ARTIST_OWES", "WAITING_ON_OTHER", "MUTUAL")
    )


def _followup_form(shell: ConsumerShell, person: Person) -> str:
    song = shell.runtime.headquarters.store.active_song()
    song_binding = ""
    if song is not None:
        song_binding = (
            '<label><input type="checkbox" name="bind_song" value="1"> '
            f'Bind this follow-up to current Song: <strong>{html.escape(song.title)}</strong></label>'
        )
    return (
        '<form class="stack" method="post" action="/people/followup/create" '
        f'aria-label="Add follow-up for {html.escape(person.display_name, quote=True)}">'
        f'{shell._hidden(_followup_action(shell, person))}'
        f'<div><label>Follow-up<textarea name="summary" maxlength="{_MAX_FOLLOWUP_SUMMARY}" rows="3" required></textarea></label></div>'
        '<div><label>Who has the next move?<select name="responsibility" required>'
        f'{_responsibility_options()}</select></label></div>'
        '<div><label>Due date <span class="muted">optional</span><input name="due_on" type="date"></label></div>'
        f'{song_binding}'
        '<button type="submit">Remember follow-up</button>'
        '<p class="muted">Remembering a follow-up is not a message, calendar event, reminder delivery or external action.</p>'
        '</form>'
    )


def _followup_status(followup: FollowUp) -> str:
    responsibility = _RESPONSIBILITY_LABELS[followup.responsibility]
    due = "No due date" if followup.due_on is None else f"Due {followup.due_on}"
    return f"{responsibility} · {due}"


def _followup_song(shell: ConsumerShell, followup: FollowUp) -> str:
    if followup.song_id is None:
        return "Artist-wide"
    song = shell.runtime.headquarters.store.get_song(followup.song_id)
    if song is None:
        raise ConsumerShellError("A follow-up references Song state that is no longer readable")
    return f"Song: {song.title}"


def _followup_markup(shell: ConsumerShell, followup: FollowUp) -> str:
    resolve = _close_action(shell, followup, "people-followup-resolve")
    cancel = _close_action(shell, followup, "people-followup-cancel")
    return (
        '<li class="stack">'
        f'<p><strong>{html.escape(followup.summary)}</strong></p>'
        f'<p class="status caution">{html.escape(_followup_status(followup))}</p>'
        f'<p class="muted">{html.escape(_followup_song(shell, followup))}</p>'
        '<form class="stack" method="post" action="/people/followup/resolve">'
        f'{shell._hidden(resolve)}'
        f'<div><label>What closed the loop?<input name="resolution_note" type="text" maxlength="{_MAX_RESOLUTION_NOTE}" required></label></div>'
        '<button class="primary" type="submit">Mark resolved</button>'
        '</form>'
        '<details><summary>Cancel this follow-up</summary>'
        '<form class="stack" method="post" action="/people/followup/cancel">'
        f'{shell._hidden(cancel)}'
        f'<div><label>Why is it no longer needed?<input name="resolution_note" type="text" maxlength="{_MAX_RESOLUTION_NOTE}" required></label></div>'
        '<button type="submit">Cancel follow-up</button>'
        '</form></details>'
        '</li>'
    )


def _person_markup(shell: ConsumerShell, person: Person) -> str:
    followups = shell.runtime.headquarters.people.open_followups(person_id=person.id)
    context = (
        ""
        if person.relationship_context is None
        else f'<p>{html.escape(person.relationship_context)}</p>'
    )
    if followups:
        rows = "".join(_followup_markup(shell, item) for item in followups)
        open_html = (
            f'<h3>Open loops</h3><ol class="stack" aria-label="Open follow-ups for {html.escape(person.display_name, quote=True)}">{rows}</ol>'
        )
    else:
        open_html = '<p class="status good">No open follow-ups</p>'
    return (
        '<div class="card stack">'
        f'<h2>{html.escape(person.display_name)}</h2>'
        f'{context}{open_html}'
        '<details><summary>Add a follow-up</summary>'
        f'{_followup_form(shell, person)}'
        '</details></div>'
    )


def _people_content(shell: ConsumerShell) -> str:
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("People requires an open Artist workspace")
    people = shell.runtime.headquarters.people.people()
    people_html = (
        '<div class="card"><h2>Your people</h2><p class="muted">No people recorded yet. Add only the relationship context you actually know.</p></div>'
        if not people
        else "".join(_person_markup(shell, person) for person in people)
    )
    return f'<section class="grid">{_person_form(shell)}{people_html}</section>'


def _post_create_person(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "people-create")
    if action is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That add-person action was already handled or expired."),
        )
        return
    name = _clean_human_text(
        form.get("display_name", ""),
        "Person name",
        maximum=_MAX_PERSON_NAME,
    )
    context = _optional_human_text(
        form.get("relationship_context"),
        "Relationship context",
        maximum=_MAX_RELATIONSHIP_CONTEXT,
    )
    person = shell.runtime.headquarters.people.create_person(
        name,
        relationship_context=context,
    )
    shell._consumer_notice = f"{person.display_name} added to your local People context. Nothing was sent or merged."
    shell._redirect(handler, "/people")


def _parse_followup_binding(value: str) -> tuple[str, str | None]:
    person_id, separator, song_marker = value.partition("|")
    if not separator or not person_id or not song_marker:
        raise ConsumerShellError("That follow-up action is no longer valid")
    return person_id, None if song_marker == "~" else song_marker


def _post_create_followup(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "people-followup-create")
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That follow-up action was already handled or expired."),
        )
        return
    person_id, rendered_song_id = _parse_followup_binding(action.value)
    bind_song = form.get("bind_song")
    if bind_song not in {None, "1"}:
        raise ConsumerShellError("That Song binding choice is invalid")
    song_id: str | None = None
    if bind_song == "1":
        active = shell.runtime.headquarters.store.active_song()
        if active is None or rendered_song_id is None or active.id != rendered_song_id:
            raise ConsumerShellError(
                "The active Song changed. Reload People before binding this follow-up to a Song."
            )
        song_id = active.id
    responsibility = str(form.get("responsibility", "")).strip().upper()
    if responsibility not in FOLLOWUP_RESPONSIBILITIES:
        raise ConsumerShellError("Choose who has the next move")
    summary = _clean_human_text(
        form.get("summary", ""),
        "Follow-up",
        maximum=_MAX_FOLLOWUP_SUMMARY,
    )
    due_on = _optional_human_text(form.get("due_on"), "Due date", maximum=10)
    shell.runtime.headquarters.people.create_followup(
        person_id,
        summary,
        responsibility=responsibility,
        song_id=song_id,
        due_on=due_on,
    )
    shell._consumer_notice = "Follow-up remembered locally. N0TE did not message anyone or create an external reminder."
    shell._redirect(handler, "/people")


def _post_close_followup(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
    *,
    action_kind: str,
    close_state: str,
) -> None:
    action = shell._consume_action(form.get("action", ""), action_kind)
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That follow-up close action was already handled or expired."),
        )
        return
    note = _clean_human_text(
        form.get("resolution_note", ""),
        "Closure note",
        maximum=_MAX_RESOLUTION_NOTE,
    )
    if close_state == "RESOLVED":
        shell.runtime.headquarters.people.resolve_followup(
            action.value,
            resolution_note=note,
        )
        shell._consumer_notice = "Follow-up resolved and preserved in local history."
    else:
        shell.runtime.headquarters.people.cancel_followup(action.value, reason=note)
        shell._consumer_notice = "Follow-up canceled and preserved in local history."
    shell._redirect(handler, "/people")


def install_people_headquarters() -> None:
    """Attach the local People/Follow-up Headquarters surface exactly once."""
    if getattr(ConsumerShell, "_people_headquarters_installed", False):
        return

    consumer_shell_module._NAV_ROUTES["/people"] = "People"
    original_running_state: Callable[[ConsumerShell, str], _PageState] = ConsumerShell._running_state
    original_state_content: Callable[[ConsumerShell, _PageState], str] = ConsumerShell._state_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_people_state(self: ConsumerShell, path: str) -> _PageState:
        if path != "/people":
            return original_running_state(self, path)
        artist = self.runtime.headquarters.store.artist()
        song = self.runtime.headquarters.store.active_song()
        return _PageState(
            "running-people",
            "People and open loops",
            "Artist Headquarters",
            "Remember who matters, what was promised, what you are waiting on, and what needs a follow-up without turning local context into an external action.",
            artist_name=artist.display_name,
            song_title=None if song is None else song.title,
        )

    def with_people_content(self: ConsumerShell, state: _PageState) -> str:
        if state.kind == "running-people":
            return _people_content(self)
        return original_state_content(self, state)

    def with_people_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path not in {
            "/people/create",
            "/people/followup/create",
            "/people/followup/resolve",
            "/people/followup/cancel",
        }:
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
                self._simple_error("Open an Artist workspace before changing People context."),
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
            if path == "/people/create":
                _post_create_person(self, handler, form)
            elif path == "/people/followup/create":
                _post_create_followup(self, handler, form)
            elif path == "/people/followup/resolve":
                _post_close_followup(
                    self,
                    handler,
                    form,
                    action_kind="people-followup-resolve",
                    close_state="RESOLVED",
                )
            else:
                _post_close_followup(
                    self,
                    handler,
                    form,
                    action_kind="people-followup-cancel",
                    close_state="CANCELED",
                )
        except (NotFoundError, ValidationError, ConsumerShellError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/people")
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that People action before it could become unclear relationship state."
                ),
            )

    ConsumerShell._running_state = with_people_state
    ConsumerShell._state_content = with_people_content
    ConsumerShell._handle_post = with_people_post
    ConsumerShell._people_headquarters_installed = True
