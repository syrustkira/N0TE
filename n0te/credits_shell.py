from __future__ import annotations

import hashlib
import html
import json
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError, _PageState
from .credits import (
    CONFIRMATION_STATUSES,
    CompositionSplitSheet,
    CreditsMemory,
)
from .lineage import LineageCorruptionError, NotFoundError, ValidationError

_MAX_ROLE = 120
_MAX_ROLE_CONTEXT = 500
_MAX_CONFIRMATION_NOTE = 1000
_MAX_VOID_REASON = 1000

_CONFIRMATION_LABELS = {
    "RECORDED_CONFIRMED": "Record that they confirmed",
    "RECORDED_DISPUTED": "Record that they disputed it",
}


def _credits(shell: ConsumerShell) -> CreditsMemory:
    return CreditsMemory(
        shell.runtime.headquarters.store,
        shell.runtime.headquarters.people,
    )


def _pack(*parts: object) -> str:
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=True)


def _unpack(value: str | None, size: int) -> tuple[str, ...]:
    if value is None:
        raise ConsumerShellError("That Credits action is no longer valid")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConsumerShellError("That Credits action is no longer valid") from exc
    if not isinstance(parsed, list) or len(parsed) != size:
        raise ConsumerShellError("That Credits action is no longer valid")
    result: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or not item:
            raise ConsumerShellError("That Credits action is no longer valid")
        result.append(item)
    return tuple(result)


def _clean(value: str, field: str, *, maximum: int) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise ConsumerShellError(f"{field} must not be empty")
    if len(text) > maximum:
        raise ConsumerShellError(f"{field} is too long")
    return text


def _optional(value: str | None, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > maximum:
        raise ConsumerShellError(f"{field} is too long")
    return text


def _active_song(shell: ConsumerShell, expected_song_id: str):
    song = shell.runtime.headquarters.store.active_song()
    if song is None or song.id != expected_song_id:
        raise ConsumerShellError(
            "The active Song changed. Reload People before changing credits or split context."
        )
    return song


def _allocation_fingerprint(credits: CreditsMemory, sheet_id: str) -> str:
    material = "|".join(
        f"{item.sequence}:{item.person_id}:{item.basis_points}"
        for item in credits.split_allocations(sheet_id)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _confirmation_fingerprint(credits: CreditsMemory, sheet_id: str) -> str:
    material = "|".join(
        f"{item.sequence}:{item.person_id}:{item.status}"
        for item in credits.confirmation_history(sheet_id)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _sheet_fingerprint(credits: CreditsMemory, sheet: CompositionSplitSheet) -> str:
    material = (
        f"{sheet.state}|{sheet.closure_note or ''}|"
        f"{_allocation_fingerprint(credits, sheet.id)}|"
        f"{_confirmation_fingerprint(credits, sheet.id)}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _person_roster(shell: ConsumerShell) -> tuple[object, ...]:
    return tuple(shell.runtime.headquarters.people.people())


def _roster_fingerprint(people: tuple[object, ...]) -> str:
    material = "|".join(str(getattr(person, "id")) for person in people)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _percent(basis_points: int) -> str:
    return f"{basis_points / 100:.2f}%"


def _percent_input(basis_points: int | None) -> str:
    if basis_points is None:
        return ""
    return f"{basis_points / 100:.2f}"


def _parse_percent(value: str, field: str) -> int | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        percentage = Decimal(text)
    except InvalidOperation as exc:
        raise ConsumerShellError(f"{field} must be a percentage from 0 to 100") from exc
    if not percentage.is_finite() or percentage < 0 or percentage > 100:
        raise ConsumerShellError(f"{field} must be a percentage from 0 to 100")
    basis_points = percentage * Decimal(100)
    if basis_points != basis_points.to_integral_value():
        raise ConsumerShellError(f"{field} supports at most two decimal places")
    result = int(basis_points)
    return None if result == 0 else result


def _person_name(shell: ConsumerShell, person_id: str) -> str:
    person = shell.runtime.headquarters.people.get_person(person_id)
    if person is None:
        raise ConsumerShellError("A Credits record references a person that is no longer readable")
    return person.display_name


def _credit_forms(shell: ConsumerShell, credits: CreditsMemory, song_id: str) -> str:
    people = _person_roster(shell)
    if not people:
        return (
            '<div class="card"><h3>Song credits</h3>'
            '<p>Add collaborators to People first, then record only the credit information you actually know.</p>'
            '<p class="muted">A local credit record is not identity verification, legal ownership certification, registration or payment authority.</p></div>'
        )

    roster = credits.credits_for_song(song_id)
    if roster:
        rows = []
        for credit in roster:
            name = _person_name(shell, credit.person_id)
            context = "" if credit.role_context is None else f" · {credit.role_context}"
            rows.append(
                '<li>'
                f'<strong>{html.escape(name)}</strong> — {html.escape(credit.role)}'
                f'<span class="muted">{html.escape(context)}</span>'
                '</li>'
            )
        roster_html = (
            '<h3>Recorded Song credits</h3><ul class="stack">'
            + "".join(rows)
            + '</ul><p class="muted">Source: artist-entered local context. These entries are not provider-verified credits or ownership findings.</p>'
        )
    else:
        roster_html = (
            '<h3>Recorded Song credits</h3>'
            '<p class="muted">No Song credits recorded yet.</p>'
        )

    forms = []
    for person in people:
        action = shell._new_action("credits-record", _pack(song_id, getattr(person, "id")))
        forms.append(
            '<details><summary>'
            f'Record another role for {html.escape(str(getattr(person, "display_name")))}'
            '</summary>'
            '<form class="stack" method="post" action="/credits/record">'
            f'{shell._hidden(action)}'
            f'<div><label>Role<input name="role" type="text" maxlength="{_MAX_ROLE}" placeholder="Songwriter, producer, mixer..." required></label></div>'
            f'<div><label>Context <span class="muted">optional</span><input name="role_context" type="text" maxlength="{_MAX_ROLE_CONTEXT}" placeholder="What this credit means, if useful"></label></div>'
            '<button type="submit">Record Song credit</button>'
            '</form></details>'
        )
    return '<div class="card stack">' + roster_html + "".join(forms) + '</div>'


def _split_history(shell: ConsumerShell, credits: CreditsMemory, song_id: str) -> str:
    history = [item for item in credits.split_history(song_id) if item.state == "VOIDED"]
    if not history:
        return ""
    rows: list[str] = []
    for sheet in history:
        allocations = credits.split_allocations(sheet.id)
        split = ", ".join(
            f"{_person_name(shell, item.person_id)} {_percent(item.basis_points)}"
            for item in allocations
        ) or "No allocations recorded"
        rows.append(
            '<li>'
            f'<strong>Voided proposal:</strong> {html.escape(split)}'
            f'<br><span class="muted">Reason: {html.escape(sheet.closure_note or "unknown")}</span>'
            '</li>'
        )
    return (
        '<details><summary>Prior split proposals</summary>'
        '<ul class="stack">' + "".join(rows) + '</ul>'
        '<p class="muted">Voided proposals remain in history instead of being rewritten into a different agreement.</p>'
        '</details>'
    )


def _new_split(shell: ConsumerShell, song_id: str) -> str:
    action = shell._new_action("credits-split-create", _pack(song_id))
    return (
        '<div class="card stack"><h3>Composition split</h3>'
        '<p>No active composition split proposal exists for this Song.</p>'
        '<form method="post" action="/credits/split/create">'
        f'{shell._hidden(action)}'
        '<button type="submit">Start split draft</button>'
        '</form>'
        '<p class="muted">A split draft is a local proposal. N0TE does not infer ownership, agreement, signatures, registration or royalty entitlement from it.</p>'
        '</div>'
    )


def _draft_split(
    shell: ConsumerShell,
    credits: CreditsMemory,
    song_id: str,
    sheet: CompositionSplitSheet,
) -> str:
    people = _person_roster(shell)
    if not people:
        return (
            '<div class="card"><h3>Composition split draft</h3>'
            '<p>Add the relevant people before allocating composition shares.</p></div>'
        )
    existing = {item.person_id: item.basis_points for item in credits.split_allocations(sheet.id)}
    allocation_fingerprint = _allocation_fingerprint(credits, sheet.id)
    roster_fingerprint = _roster_fingerprint(people)
    save = shell._new_action(
        "credits-split-save",
        _pack(song_id, sheet.id, allocation_fingerprint, roster_fingerprint),
    )
    submit = shell._new_action(
        "credits-split-submit",
        _pack(song_id, sheet.id, allocation_fingerprint),
    )
    fields: list[str] = []
    for index, person in enumerate(people):
        person_id = str(getattr(person, "id"))
        name = html.escape(str(getattr(person, "display_name")))
        fields.append(
            '<div><label>'
            f'{name} %'
            f'<input name="share_{index}" type="number" min="0" max="100" step="0.01" value="{_percent_input(existing.get(person_id))}">'
            '</label></div>'
        )
    total = sum(existing.values())
    total_class = "good" if total == 10000 else "caution"
    return (
        '<div class="card stack"><h3>Composition split draft</h3>'
        f'<p class="status {total_class}">Current total: {_percent(total)}</p>'
        '<p>Draft shares can change until you submit. After submission, the exact participant/share proposal freezes and revisions require voiding it and creating another proposal.</p>'
        '<form class="stack" method="post" action="/credits/split/save">'
        f'{shell._hidden(save)}'
        + "".join(fields)
        + '<button type="submit">Save draft shares</button></form>'
        '<form method="post" action="/credits/split/submit">'
        f'{shell._hidden(submit)}'
        '<button class="primary" type="submit">Submit exact 100% proposal for confirmation tracking</button>'
        '</form>'
        '<p class="muted">100.00% only proves arithmetic completeness. It does not prove legal validity or contributor consent.</p>'
        '</div>'
    )


def _confirmation_label(status: str) -> str:
    if status == "PENDING":
        return "Pending: no confirmation state recorded"
    if status == "RECORDED_CONFIRMED":
        return "Artist records this participant as confirmed"
    if status == "RECORDED_DISPUTED":
        return "Artist records this participant as disputed"
    return "Unknown confirmation state"


def _submitted_split(
    shell: ConsumerShell,
    credits: CreditsMemory,
    song_id: str,
    sheet: CompositionSplitSheet,
) -> str:
    allocations = credits.split_allocations(sheet.id)
    confirmation_fingerprint = _confirmation_fingerprint(credits, sheet.id)
    rows: list[str] = []
    for allocation in allocations:
        name = _person_name(shell, allocation.person_id)
        status = credits.confirmation_state(sheet.id, allocation.person_id)
        action = shell._new_action(
            "credits-split-confirm",
            _pack(song_id, sheet.id, allocation.person_id, confirmation_fingerprint),
        )
        options = "".join(
            f'<option value="{value}">{html.escape(_CONFIRMATION_LABELS[value])}</option>'
            for value in ("RECORDED_CONFIRMED", "RECORDED_DISPUTED")
        )
        rows.append(
            '<li class="stack">'
            f'<p><strong>{html.escape(name)}</strong> — {_percent(allocation.basis_points)}</p>'
            f'<p class="status caution">{html.escape(_confirmation_label(status))}</p>'
            '<form class="stack" method="post" action="/credits/split/confirm">'
            f'{shell._hidden(action)}'
            f'<div><label>Record state<select name="status" required>{options}</select></label></div>'
            f'<div><label>Source/context note<input name="note" type="text" maxlength="{_MAX_CONFIRMATION_NOTE}" placeholder="What did you observe or receive?" required></label></div>'
            '<button type="submit">Record artist-declared confirmation state</button>'
            '</form></li>'
        )
    if credits.all_recorded_confirmed(sheet.id):
        aggregate = (
            '<p class="status good">Artist records every participant as confirmed.</p>'
            '<p class="muted">This remains artist-declared evidence, not independent identity, signature, provider or legal verification.</p>'
        )
    else:
        aggregate = (
            '<p class="status caution">This proposal does not currently have an artist-recorded confirmed state for every participant.</p>'
        )
    void_action = shell._new_action(
        "credits-split-void",
        _pack(song_id, sheet.id, _sheet_fingerprint(credits, sheet)),
    )
    return (
        '<div class="card stack"><h3>Submitted composition split proposal</h3>'
        '<p>The submitted participant/share proposal is frozen. Confirmation states below record what the artist says was observed; they are not signatures or provider verification.</p>'
        f'{aggregate}<ol class="stack">{"".join(rows)}</ol>'
        '<details><summary>Void this proposal</summary>'
        '<form class="stack" method="post" action="/credits/split/void">'
        f'{shell._hidden(void_action)}'
        f'<div><label>Reason<input name="reason" type="text" maxlength="{_MAX_VOID_REASON}" required></label></div>'
        '<button type="submit">Void without rewriting history</button>'
        '</form></details></div>'
    )


def _credits_content(shell: ConsumerShell) -> str:
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("Credits requires an open Artist workspace")
    song = shell.runtime.headquarters.store.active_song()
    if song is None:
        return (
            '<section><div class="card"><h2>Credits & composition splits</h2>'
            '<p>Open or resume a Song to keep its contributor and composition-share context exact.</p>'
            '<p class="muted">People remain Artist-wide; credits and splits are Song-bound.</p>'
            '</div></section>'
        )
    credits = _credits(shell)
    active = credits.active_split_for_song(song.id)
    split_html = (
        _new_split(shell, song.id)
        if active is None
        else _draft_split(shell, credits, song.id, active)
        if active.state == "DRAFT"
        else _submitted_split(shell, credits, song.id, active)
    )
    return (
        '<section class="stack" aria-label="Song credits and composition splits">'
        f'<div class="card"><h2>Credits & composition splits · {html.escape(song.title)}</h2>'
        '<p>Keep Song contributor context and composition-share proposals here without turning local declarations into legal or provider truth.</p></div>'
        f'{_credit_forms(shell, credits, song.id)}'
        f'{split_html}'
        f'{_split_history(shell, credits, song.id)}'
        '</section>'
    )


def _consume(shell: ConsumerShell, form: Mapping[str, str], kind: str):
    action = shell._consume_action(form.get("action", ""), kind)
    if action is None:
        raise ConsumerShellError("That Credits action was already handled or expired")
    return action


def _post_record_credit(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = _consume(shell, form, "credits-record")
    song_id, person_id = _unpack(action.value, 2)
    _active_song(shell, song_id)
    role = _clean(form.get("role", ""), "Role", maximum=_MAX_ROLE)
    context = _optional(form.get("role_context"), "Role context", maximum=_MAX_ROLE_CONTEXT)
    _credits(shell).record_credit(song_id, person_id, role, role_context=context)
    shell._consumer_notice = (
        "Song credit recorded as artist-entered local context. N0TE did not verify identity, ownership, registration or payment."
    )


def _post_create_split(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = _consume(shell, form, "credits-split-create")
    (song_id,) = _unpack(action.value, 1)
    _active_song(shell, song_id)
    _credits(shell).create_split_draft(song_id)
    shell._consumer_notice = "Composition split draft created locally. No agreement or external action occurred."


def _post_save_split(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = _consume(shell, form, "credits-split-save")
    song_id, sheet_id, expected_allocations, expected_roster = _unpack(action.value, 4)
    _active_song(shell, song_id)
    credits = _credits(shell)
    sheet = credits.get_split_sheet(sheet_id)
    if sheet is None or sheet.song_id != song_id or sheet.state != "DRAFT":
        raise ConsumerShellError("That split draft changed. Reload People before editing it.")
    people = _person_roster(shell)
    if _allocation_fingerprint(credits, sheet.id) != expected_allocations:
        raise ConsumerShellError("That split draft changed in another view. Reload before replacing newer shares.")
    if _roster_fingerprint(people) != expected_roster:
        raise ConsumerShellError("The People roster changed. Reload before saving split participants.")
    allocations: dict[str, int] = {}
    for index, person in enumerate(people):
        basis_points = _parse_percent(
            form.get(f"share_{index}", ""),
            str(getattr(person, "display_name")),
        )
        if basis_points is not None:
            allocations[str(getattr(person, "id"))] = basis_points
    credits.set_draft_allocations(sheet.id, allocations)
    shell._consumer_notice = "Split shares saved as an editable local draft. They are not an agreement."


def _post_submit_split(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = _consume(shell, form, "credits-split-submit")
    song_id, sheet_id, expected_allocations = _unpack(action.value, 3)
    _active_song(shell, song_id)
    credits = _credits(shell)
    sheet = credits.get_split_sheet(sheet_id)
    if sheet is None or sheet.song_id != song_id or sheet.state != "DRAFT":
        raise ConsumerShellError("That split draft changed. Reload before submitting it.")
    if _allocation_fingerprint(credits, sheet.id) != expected_allocations:
        raise ConsumerShellError("That split draft changed in another view. Reload before submitting stale shares.")
    credits.submit_split(sheet.id)
    shell._consumer_notice = (
        "Exact split proposal submitted locally and frozen for confirmation tracking. 100% arithmetic is not legal validation or consent."
    )


def _post_confirmation(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = _consume(shell, form, "credits-split-confirm")
    song_id, sheet_id, person_id, expected_history = _unpack(action.value, 4)
    _active_song(shell, song_id)
    credits = _credits(shell)
    sheet = credits.get_split_sheet(sheet_id)
    if sheet is None or sheet.song_id != song_id or sheet.state != "OPEN_CONFIRMATION":
        raise ConsumerShellError("That submitted split changed. Reload before recording confirmation context.")
    if _confirmation_fingerprint(credits, sheet.id) != expected_history:
        raise ConsumerShellError("Confirmation history changed in another view. Reload before adding another state.")
    status = str(form.get("status", "")).strip().upper()
    if status not in CONFIRMATION_STATUSES:
        raise ConsumerShellError("Choose a valid confirmation state")
    note = _clean(
        form.get("note", ""),
        "Confirmation source/context note",
        maximum=_MAX_CONFIRMATION_NOTE,
    )
    credits.record_confirmation(sheet.id, person_id, status=status, note=note)
    shell._consumer_notice = (
        "Artist-declared confirmation state recorded. N0TE did not independently contact, authenticate or verify the contributor."
    )


def _post_void_split(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = _consume(shell, form, "credits-split-void")
    song_id, sheet_id, expected_sheet = _unpack(action.value, 3)
    _active_song(shell, song_id)
    credits = _credits(shell)
    sheet = credits.get_split_sheet(sheet_id)
    if sheet is None or sheet.song_id != song_id or sheet.state == "VOIDED":
        raise ConsumerShellError("That split proposal changed. Reload before voiding it.")
    if _sheet_fingerprint(credits, sheet) != expected_sheet:
        raise ConsumerShellError("That split proposal changed in another view. Reload before voiding newer history.")
    reason = _clean(form.get("reason", ""), "Void reason", maximum=_MAX_VOID_REASON)
    credits.void_split(sheet.id, reason=reason)
    shell._consumer_notice = "Split proposal voided without rewriting its prior allocations or confirmation history."


def install_credits_headquarters() -> None:
    """Extend the existing People surface with exact Song credit/split context."""
    if getattr(ConsumerShell, "_credits_headquarters_installed", False):
        return
    if not getattr(ConsumerShell, "_people_headquarters_installed", False):
        raise RuntimeError("Credits consumer installation requires People Headquarters first")

    original_state_content: Callable[[ConsumerShell, _PageState], str] = ConsumerShell._state_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_credits_content(self: ConsumerShell, state: _PageState) -> str:
        content = original_state_content(self, state)
        if state.kind != "running-people":
            return content
        return content + _credits_content(self)

    def with_credits_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        handlers = {
            "/credits/record": _post_record_credit,
            "/credits/split/create": _post_create_split,
            "/credits/split/save": _post_save_split,
            "/credits/split/submit": _post_submit_split,
            "/credits/split/confirm": _post_confirmation,
            "/credits/split/void": _post_void_split,
        }
        callback = handlers.get(path)
        if callback is None:
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
                self._simple_error("Open an Artist workspace before changing Song credits."),
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
            callback(self, form)
        except (NotFoundError, ValidationError, ConsumerShellError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/people")
            return
        except LineageCorruptionError:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE found unreadable Credits history and stopped before rewriting it."
                ),
            )
            return
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that Credits action before it could become unclear Song or contributor state."
                ),
            )
            return
        self._redirect(handler, "/people")

    ConsumerShell._state_content = with_credits_content
    ConsumerShell._handle_post = with_credits_post
    ConsumerShell._credits_headquarters_installed = True
