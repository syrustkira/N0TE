from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from . import consumer_shell as consumer_shell_module
from .consumer_shell import ConsumerShell, ConsumerShellError, _PageState
from .direct_fan import CONTACT_PURPOSES, DirectFanError, DirectFanService
from .fan_journey import CONSENT_CHANNELS, CONSENT_STATES, FanJourneyError
from .lineage import LineageCorruptionError, NotFoundError, ValidationError
from .people import Person

_MAX_ENDPOINT = 500
_MAX_NOTE = 1200
_PURPOSE_LABELS = {
    "RELEASE_NOTIFICATION": "Release notification",
    "PRE_SAVE_INVITE": "Pre-save invite",
}
_STATE_LABELS = {
    "REVIEWABLE": "Reviewable plan",
    "NO_CONTACT_POINT": "Blocked: no current contact point",
    "NO_CURRENT_CONSENT": "Blocked: no current consent evidence",
    "CONSENT_REVOKED": "Blocked: consent opted out",
    "CONSENT_CHANGED": "Blocked: consent changed since this plan",
    "CONTACT_CHANGED": "Blocked: contact point changed since this plan",
}
_CHANNEL_LABELS = {
    "EMAIL": "Email",
    "SMS": "SMS",
    "DM": "Direct message",
    "COMMUNITY": "Community",
    "OTHER": "Other",
}


def _service(shell: ConsumerShell) -> DirectFanService:
    hq = shell.runtime.headquarters
    return DirectFanService(hq.store, hq.people, hq.evidence)


def _clean(value: str | None, *, field: str, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ConsumerShellError(f"{field} must not be empty")
    if len(text) > maximum:
        raise ConsumerShellError(f"{field} is too long")
    return text


def _optional(value: str | None, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > maximum:
        raise ConsumerShellError(f"{field} is too long")
    return text


def _select(name: str, allowed: tuple[str, ...], labels: Mapping[str, str]) -> str:
    options = "".join(
        f'<option value="{html.escape(value, quote=True)}">{html.escape(labels[value])}</option>'
        for value in allowed
    )
    return f'<select name="{html.escape(name, quote=True)}" required>{options}</select>'


def _contact_action(shell: ConsumerShell, person: Person) -> str:
    return shell._new_action("direct-fan-contact", person.id)


def _consent_action(shell: ConsumerShell, person: Person) -> str:
    return shell._new_action("direct-fan-consent", person.id)


def _intent_action(
    shell: ConsumerShell,
    *,
    person_id: str,
    song_id: str,
    channel: str,
    contact_claim_id: str,
    consent_claim_id: str,
    purpose: str,
) -> str:
    return shell._new_action(
        "direct-fan-intent",
        "|".join(
            (
                person_id,
                song_id,
                channel,
                contact_claim_id,
                consent_claim_id,
                purpose,
            )
        ),
    )


def _contact_form(shell: ConsumerShell, person: Person) -> str:
    return (
        '<details><summary>Record a contact point</summary>'
        '<form class="stack" method="post" action="/audience/contact">'
        + shell._hidden(_contact_action(shell, person))
        + '<label>Channel'
        + _select("channel", CONSENT_CHANNELS, _CHANNEL_LABELS)
        + '</label>'
        + f'<label>Contact point<input name="endpoint" type="text" maxlength="{_MAX_ENDPOINT}" autocomplete="off" required></label>'
        + f'<label>Context <span class="muted">optional</span><input name="note" type="text" maxlength="{_MAX_NOTE}" autocomplete="off"></label>'
        + '<button type="submit">Record contact point</button>'
        + '<p class="muted">Local evidence only. A contact address or handle is not identity proof, opt-in, a subscription, or permission to send.</p>'
        + '</form></details>'
    )


def _consent_form(shell: ConsumerShell, person: Person) -> str:
    status_labels = {"OPTED_IN": "Opted in", "OPTED_OUT": "Opted out / revoked"}
    return (
        '<details><summary>Record channel consent evidence</summary>'
        '<form class="stack" method="post" action="/audience/consent">'
        + shell._hidden(_consent_action(shell, person))
        + '<label>Channel'
        + _select("channel", CONSENT_CHANNELS, _CHANNEL_LABELS)
        + '</label><label>Status'
        + _select("status", CONSENT_STATES, status_labels)
        + '</label>'
        + f'<label>Basis/context <span class="muted">optional</span><input name="note" type="text" maxlength="{_MAX_NOTE}" autocomplete="off"></label>'
        + '<button type="submit">Record consent evidence</button>'
        + '<p class="muted">Record only explicit consent evidence you actually have. This does not subscribe, schedule, message, or contact anyone.</p>'
        + '</form></details>'
    )


def _channel_rows(shell: ConsumerShell, person: Person, service: DirectFanService) -> str:
    fan = service.fan_journey.snapshot(person.id)
    rows: list[str] = []
    for channel in CONSENT_CHANNELS:
        contact = service.current_contact_point(person.id, channel)
        consent = fan.consent_evidence(channel)
        if contact is None and consent is None:
            continue
        contact_text = "No contact point" if contact is None else contact.endpoint
        consent_text = "UNKNOWN" if consent is None else consent.status
        rows.append(
            '<li><strong>'
            + html.escape(_CHANNEL_LABELS[channel])
            + '</strong> · '
            + html.escape(contact_text)
            + ' · consent: '
            + html.escape(consent_text)
            + '</li>'
        )
    if not rows:
        return '<p class="muted">No direct-contact evidence recorded yet.</p>'
    return '<ul class="stack" aria-label="Direct fan contact evidence">' + "".join(rows) + '</ul>'


def _intent_forms(shell: ConsumerShell, person: Person, service: DirectFanService) -> str:
    song = shell.runtime.headquarters.store.active_song()
    if song is None:
        return '<p class="muted">Open a Song before recording a release or pre-save contact intent.</p>'
    fan = service.fan_journey.snapshot(person.id)
    forms: list[str] = []
    for channel in CONSENT_CHANNELS:
        contact = service.current_contact_point(person.id, channel)
        consent = fan.consent_evidence(channel)
        if contact is None or consent is None or consent.status != "OPTED_IN":
            continue
        for purpose in CONTACT_PURPOSES:
            token = _intent_action(
                shell,
                person_id=person.id,
                song_id=song.id,
                channel=channel,
                contact_claim_id=contact.claim_id,
                consent_claim_id=consent.claim_id,
                purpose=purpose,
            )
            forms.append(
                '<form method="post" action="/audience/intent">'
                + shell._hidden(token)
                + '<button type="submit">Plan '
                + html.escape(_PURPOSE_LABELS[purpose].lower())
                + ' via '
                + html.escape(_CHANNEL_LABELS[channel])
                + '</button></form>'
            )
    if not forms:
        return (
            '<p class="status caution">No channel is currently eligible even for planning.</p>'
            '<p class="muted">A plan requires both a current contact point and current explicit OPTED_IN evidence on the same channel.</p>'
        )
    return (
        '<p>Current Song: <strong>'
        + html.escape(song.title)
        + '</strong></p><div class="row">'
        + "".join(forms)
        + '</div><p class="muted">Planning is not scheduling or sending. Provider execution remains a separate future authorization and verification step.</p>'
    )


def _intent_history(shell: ConsumerShell, person: Person, service: DirectFanService) -> str:
    intents = service.intents_for_person(person.id)
    if not intents:
        return '<p class="muted">No contact intents recorded.</p>'
    rows: list[str] = []
    for intent in reversed(intents):
        song = shell.runtime.headquarters.store.get_song(intent.song_id)
        if song is None:
            raise DirectFanError("Direct Fan intent references unreadable Song state")
        assessment = service.assess_intent(intent.claim_id)
        rows.append(
            '<li><strong>'
            + html.escape(_PURPOSE_LABELS[intent.purpose])
            + '</strong> · '
            + html.escape(song.title)
            + ' · '
            + html.escape(_CHANNEL_LABELS[intent.channel])
            + '<br><span class="status '
            + ("good" if assessment.reviewable else "caution")
            + '">'
            + html.escape(_STATE_LABELS[assessment.state])
            + '</span><br><span class="muted">No message is scheduled or sent. Delivery and pre-save remain unverified.</span></li>'
        )
    return '<ol class="stack" aria-label="Direct fan contact intent history">' + "".join(rows) + '</ol>'


def _person_card(shell: ConsumerShell, person: Person, service: DirectFanService) -> str:
    context = (
        ""
        if person.relationship_context is None
        else '<p>' + html.escape(person.relationship_context) + '</p>'
    )
    return (
        '<div class="card stack"><h2>'
        + html.escape(person.display_name)
        + '</h2>'
        + context
        + '<h3>Contact and consent</h3>'
        + _channel_rows(shell, person, service)
        + _contact_form(shell, person)
        + _consent_form(shell, person)
        + '<h3>Release contact planning</h3>'
        + _intent_forms(shell, person, service)
        + '<h3>Intent history</h3>'
        + _intent_history(shell, person, service)
        + '</div>'
    )


def _audience_content(shell: ConsumerShell) -> str:
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("Audience requires an open Artist workspace")
    service = _service(shell)
    people = shell.runtime.headquarters.people.people()
    intro = (
        '<div class="card stack"><h2>Direct Fan</h2>'
        '<p>Keep contact points, consent evidence, and release-contact intent separate. N0TE will not infer opt-in from follows, engagement, purchases, or relationship stage.</p>'
        '<p class="status caution">No provider sending is enabled here.</p>'
        '<p class="muted">A reviewable plan is still not a send, schedule, subscription, smart-link publication, pre-save receipt, delivery receipt, or provider authorization.</p></div>'
    )
    if not people:
        empty = (
            '<div class="card"><p>No People records exist yet.</p>'
            '<p><a href="/people">Add someone in People</a> before recording direct-fan evidence.</p></div>'
        )
        return '<section class="grid">' + intro + empty + '</section>'
    cards = "".join(_person_card(shell, person, service) for person in people)
    return '<section class="grid">' + intro + cards + '</section>'


def _post_contact(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "direct-fan-contact")
    if action is None or action.value is None:
        raise ConsumerShellError("That contact-point action was already handled or expired")
    service = _service(shell)
    service.record_contact_point(
        action.value,
        _clean(form.get("channel"), field="Channel", maximum=32),
        _clean(form.get("endpoint"), field="Contact point", maximum=_MAX_ENDPOINT),
        source_kind="USER_DECLARED",
        note=_optional(form.get("note"), field="Context", maximum=_MAX_NOTE),
    )
    shell._consumer_notice = (
        "Contact point recorded as local Artist-declared evidence. It did not create consent, identity proof, or permission to send."
    )


def _post_consent(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "direct-fan-consent")
    if action is None or action.value is None:
        raise ConsumerShellError("That consent action was already handled or expired")
    service = _service(shell)
    service.fan_journey.record_consent(
        action.value,
        _clean(form.get("channel"), field="Channel", maximum=32),
        _clean(form.get("status"), field="Consent status", maximum=32),
        source_kind="USER_DECLARED",
        note=_optional(form.get("note"), field="Consent context", maximum=_MAX_NOTE),
    )
    shell._consumer_notice = (
        "Consent evidence recorded locally. N0TE did not subscribe, schedule, message, or contact anyone."
    )


def _parse_intent_binding(value: str) -> tuple[str, str, str, str, str, str]:
    parts = value.split("|")
    if len(parts) != 6 or any(not part for part in parts):
        raise ConsumerShellError("That Direct Fan plan action is no longer valid")
    return tuple(parts)  # type: ignore[return-value]


def _post_intent(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "direct-fan-intent")
    if action is None or action.value is None:
        raise ConsumerShellError("That Direct Fan plan action was already handled or expired")
    person_id, song_id, channel, contact_claim_id, consent_claim_id, purpose = _parse_intent_binding(
        action.value
    )
    service = _service(shell)
    active_song = shell.runtime.headquarters.store.active_song()
    if active_song is None or active_song.id != song_id:
        raise ConsumerShellError(
            "The active Song changed. Reload Audience before recording this contact plan."
        )
    current_contact = service.current_contact_point(person_id, channel)
    current_consent = service.fan_journey.snapshot(person_id).consent_evidence(channel)
    if current_contact is None or current_contact.claim_id != contact_claim_id:
        raise ConsumerShellError(
            "The contact point changed. Reload Audience before recording this plan."
        )
    if (
        current_consent is None
        or current_consent.status != "OPTED_IN"
        or current_consent.claim_id != consent_claim_id
    ):
        raise ConsumerShellError(
            "Consent changed. Reload Audience before recording this plan."
        )
    service.record_contact_intent(person_id, song_id, channel, purpose)
    shell._consumer_notice = (
        "Direct Fan contact intent recorded for review. No message was scheduled or sent, and no provider action occurred."
    )


def install_direct_fan_headquarters() -> None:
    """Attach the consent-bound Direct Fan Audience surface exactly once."""
    if getattr(ConsumerShell, "_direct_fan_headquarters_installed", False):
        return

    consumer_shell_module._NAV_ROUTES["/audience"] = "Audience"
    original_running_state: Callable[[ConsumerShell, str], _PageState] = ConsumerShell._running_state
    original_state_content: Callable[[ConsumerShell, _PageState], str] = ConsumerShell._state_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_audience_state(self: ConsumerShell, path: str) -> _PageState:
        if path != "/audience":
            return original_running_state(self, path)
        artist = self.runtime.headquarters.store.artist()
        song = self.runtime.headquarters.store.active_song()
        return _PageState(
            "running-audience",
            "Audience and Direct Fan",
            "Artist Headquarters",
            "Record only the direct-fan contact and consent evidence you actually have, then plan a Song contact intent without silently turning it into a send.",
            artist_name=artist.display_name,
            song_title=None if song is None else song.title,
        )

    def with_audience_content(self: ConsumerShell, state: _PageState) -> str:
        if state.kind == "running-audience":
            return _audience_content(self)
        return original_state_content(self, state)

    def with_audience_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path not in {"/audience/contact", "/audience/consent", "/audience/intent"}:
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
                self._simple_error("Open an Artist workspace before changing Audience context."),
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
            if path == "/audience/contact":
                _post_contact(self, form)
            elif path == "/audience/consent":
                _post_consent(self, form)
            else:
                _post_intent(self, form)
        except (ValidationError, NotFoundError, ConsumerShellError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/audience")
            return
        except (DirectFanError, FanJourneyError, LineageCorruptionError):
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE found Direct Fan evidence it could not represent safely and stopped before changing it."
                ),
            )
            return
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that Audience action before it could become unclear consent or contact authority."
                ),
            )
            return
        self._redirect(handler, "/audience")

    ConsumerShell._running_state = with_audience_state
    ConsumerShell._state_content = with_audience_content
    ConsumerShell._handle_post = with_audience_post
    ConsumerShell._direct_fan_headquarters_installed = True
