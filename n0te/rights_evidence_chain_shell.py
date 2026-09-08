from __future__ import annotations

import hashlib
import html
import json
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError, _PageState
from .credits import CreditsMemory
from .lineage import LineageCorruptionError, NotFoundError, ValidationError
from .rights_evidence_chain import (
    RIGHTS_ASSERTIONS,
    RightsEvidenceChainError,
    RightsEvidenceChainService,
    RightsEvidenceSnapshot,
)

_MAX_SOURCE_REF = 1000
_MAX_NOTE = 2000
_RECORDABLE_STAGES = ("COMMUNICATION_CONFIRMATION", "SIGNED_DOCUMENT")
_STAGE_LABELS = {
    "USER_DECLARATION": "User declaration",
    "COMMUNICATION_CONFIRMATION": "Communication confirmation",
    "SIGNED_DOCUMENT": "Signed document",
    "PROVIDER_RECEIPT": "Provider receipt / acknowledgment",
}
_STATUS_LABELS = {
    "UNKNOWN": "No supporting evidence recorded",
    "UNVERIFIED": "Artist-entered reference only; external evidence not observed or verified",
    "SUPPORTED": "Supported by current external evidence",
    "CONTRADICTED": "Current external evidence contradicts this stage",
    "CONFLICT": "Conflicting evidence is preserved",
}


def _credits(shell: ConsumerShell) -> CreditsMemory:
    return CreditsMemory(
        shell.runtime.headquarters.store,
        shell.runtime.headquarters.people,
    )


def _service(shell: ConsumerShell, credits: CreditsMemory) -> RightsEvidenceChainService:
    hq = shell.runtime.headquarters
    return RightsEvidenceChainService(hq.store, credits, hq.evidence)


def _pack(*parts: str) -> str:
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=True)


def _unpack(value: str | None, size: int) -> tuple[str, ...]:
    if value is None:
        raise ConsumerShellError("That rights evidence action is no longer valid")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConsumerShellError("That rights evidence action is no longer valid") from exc
    if not isinstance(parsed, list) or len(parsed) != size:
        raise ConsumerShellError("That rights evidence action is no longer valid")
    result: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or not item:
            raise ConsumerShellError("That rights evidence action is no longer valid")
        result.append(item)
    return tuple(result)


def _clean(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ConsumerShellError(f"{field} must be text")
    text = " ".join(value.split())
    if not text:
        raise ConsumerShellError(f"{field} must not be empty")
    if len(text) > maximum:
        raise ConsumerShellError(f"{field} is too long")
    return text


def _optional(value: object | None, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConsumerShellError(f"{field} must be text")
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > maximum:
        raise ConsumerShellError(f"{field} is too long")
    return text


def _active_song(shell: ConsumerShell, expected_song_id: str):
    song = shell.runtime.headquarters.store.active_song()
    if song is None or song.id != expected_song_id:
        raise ConsumerShellError(
            "The active Song changed. Reload People before recording rights evidence."
        )
    return song


def _split_fingerprint(credits: CreditsMemory, sheet_id: str) -> str:
    sheet = credits.get_split_sheet(sheet_id)
    if sheet is None:
        raise ConsumerShellError("That composition split no longer exists")
    allocations = "|".join(
        f"{item.sequence}:{item.person_id}:{item.basis_points}"
        for item in credits.split_allocations(sheet.id)
    )
    confirmations = "|".join(
        f"{item.sequence}:{item.person_id}:{item.status}"
        for item in credits.confirmation_history(sheet.id)
    )
    material = f"{sheet.sequence}|{sheet.state}|{sheet.closure_note or ''}|{allocations}|{confirmations}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _target_fingerprint(credits: CreditsMemory, target_kind: str, target_id: str) -> str:
    if target_kind == "COMPOSITION_SPLIT":
        return _split_fingerprint(credits, target_id)
    credit = credits.get_credit(target_id)
    if credit is None:
        raise ConsumerShellError("That Song credit no longer exists")
    material = (
        f"{credit.sequence}|{credit.artist_id}|{credit.song_id}|{credit.person_id}|"
        f"{credit.role}|{credit.role_context or ''}|{credit.truth_type}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _stage_class(status: str) -> str:
    if status == "SUPPORTED":
        return "good"
    if status in {"UNVERIFIED", "CONTRADICTED", "CONFLICT"}:
        return "caution"
    return ""


def _item_markup(item) -> str:
    source = html.escape(item.source_kind.replace("_", " ").title())
    provenance = (
        ""
        if item.source_ref is None
        else f'<span class="muted"> · provenance: {html.escape(item.source_ref)}</span>'
    )
    note = "" if item.note is None else f'<br><span class="muted">{html.escape(item.note)}</span>'
    assertion = html.escape(item.assertion.title())
    return f'<li><strong>{assertion}</strong> · {source}{provenance}{note}</li>'


def _record_form(
    shell: ConsumerShell,
    *,
    song_id: str,
    target_kind: str,
    target_id: str,
    target_fingerprint: str,
    stage: str,
) -> str:
    action = shell._new_action(
        "rights-evidence-record",
        _pack(song_id, target_kind, target_id, target_fingerprint, stage),
    )
    assertion_options = "".join(
        f'<option value="{value}">{html.escape(value.title())}</option>'
        for value in RIGHTS_ASSERTIONS
    )
    label = _STAGE_LABELS[stage]
    return (
        '<details><summary>Record your '
        + html.escape(label.lower())
        + ' reference</summary>'
        '<form class="stack" method="post" action="/credits/rights/evidence">'
        f'{shell._hidden(action)}'
        '<div><label>Does your reference support or contradict the target?'
        f'<select name="assertion" required>{assertion_options}</select></label></div>'
        f'<div><label>Source / provenance reference<input name="source_ref" type="text" maxlength="{_MAX_SOURCE_REF}" placeholder="Email thread, document hash, local reference..." required></label></div>'
        f'<div><label>Context <span class="muted">optional</span><input name="note" type="text" maxlength="{_MAX_NOTE}" placeholder="What you say this reference contains"></label></div>'
        '<button type="submit">Record artist-entered reference</button>'
        '<p class="muted">This records what you say exists and where to find it. N0TE has not observed or verified the communication, document, signature, ownership, registration, payment, or provider acceptance.</p>'
        '</form></details>'
    )


def _snapshot_markup(
    shell: ConsumerShell,
    credits: CreditsMemory,
    snapshot: RightsEvidenceSnapshot,
    *,
    label: str,
) -> str:
    fingerprint = _target_fingerprint(credits, snapshot.target_kind, snapshot.target_id)
    stages: list[str] = []
    for view in snapshot.stages:
        evidence = (
            '<p class="muted">No evidence at this stage.</p>'
            if not view.items
            else '<ul class="stack">' + "".join(_item_markup(item) for item in view.items) + '</ul>'
        )
        form = ""
        if view.stage in _RECORDABLE_STAGES:
            form = _record_form(
                shell,
                song_id=snapshot.song_id,
                target_kind=snapshot.target_kind,
                target_id=snapshot.target_id,
                target_fingerprint=fingerprint,
                stage=view.stage,
            )
        stages.append(
            '<li class="stack">'
            f'<p><strong>{html.escape(_STAGE_LABELS[view.stage])}</strong></p>'
            f'<p class="status {_stage_class(view.status)}">{html.escape(_STATUS_LABELS[view.status])}</p>'
            f'{evidence}{form}</li>'
        )
    contiguous = (
        "No contiguous evidence stage"
        if snapshot.highest_contiguous_supported_stage is None
        else _STAGE_LABELS[snapshot.highest_contiguous_supported_stage]
    )
    return (
        '<details class="card"><summary>Rights evidence chain · '
        + html.escape(label)
        + '</summary><div class="stack">'
        f'<p>Highest contiguous supported stage: <strong>{html.escape(contiguous)}</strong></p>'
        '<ol class="stack">' + "".join(stages) + '</ol>'
        '<p class="muted">Evidence strength is provenance, not legal certainty. A manual reference does not become external observation, a later-stage receipt does not fill missing earlier stages, and conflicting evidence stays visible.</p>'
        '<p class="muted">N0TE does not infer ownership, clearance, registration, royalty entitlement, payment, or permission to act from this chain.</p>'
        '</div></details>'
    )


def _rights_content(shell: ConsumerShell) -> str:
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("Rights evidence requires an open Artist workspace")
    song = shell.runtime.headquarters.store.active_song()
    if song is None:
        return ""
    credits = _credits(shell)
    service = _service(shell, credits)
    cards: list[str] = []
    for credit in credits.credits_for_song(song.id):
        person = shell.runtime.headquarters.people.get_person(credit.person_id)
        person_name = "Unknown person" if person is None else person.display_name
        snapshot = service.snapshot("CREDIT", credit.id, expected_song_id=song.id)
        cards.append(
            _snapshot_markup(
                shell,
                credits,
                snapshot,
                label=f"{person_name} · {credit.role}",
            )
        )
    active = credits.active_split_for_song(song.id)
    if active is not None:
        snapshot = service.snapshot(
            "COMPOSITION_SPLIT",
            active.id,
            expected_song_id=song.id,
        )
        cards.append(
            _snapshot_markup(
                shell,
                credits,
                snapshot,
                label=f"Composition split proposal · {active.state.replace('_', ' ').title()}",
            )
        )
    if not cards:
        return (
            '<section><div class="card"><h2>Rights evidence chain</h2>'
            '<p class="muted">Record a Song credit or composition split proposal first. The evidence chain begins from that explicit local declaration.</p>'
            '</div></section>'
        )
    return (
        '<section class="stack" aria-label="Rights evidence chain">'
        '<div class="card"><h2>Rights evidence chain</h2>'
        '<p>Review why each credit or split claim is merely declared, externally supported, contradicted, or backed by stronger provenance without turning evidence into a legal verdict.</p>'
        '</div>'
        + "".join(cards)
        + '</section>'
    )


def _post_rights_evidence(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "rights-evidence-record")
    if action is None:
        raise ConsumerShellError("That rights evidence action was already handled or expired")
    song_id, target_kind, target_id, expected_fingerprint, stage = _unpack(action.value, 5)
    _active_song(shell, song_id)
    if stage not in _RECORDABLE_STAGES:
        raise ConsumerShellError("That rights evidence stage cannot be recorded from this surface")
    credits = _credits(shell)
    if _target_fingerprint(credits, target_kind, target_id) != expected_fingerprint:
        raise ConsumerShellError(
            "That credit or split context changed. Reload People before attaching evidence to stale rights context."
        )
    assertion = _clean(form.get("assertion", ""), "Evidence assertion", maximum=32).upper()
    if assertion not in RIGHTS_ASSERTIONS:
        raise ConsumerShellError("Choose whether your reference supports or contradicts the target")
    source_ref = _clean(
        form.get("source_ref", ""),
        "Evidence source / provenance reference",
        maximum=_MAX_SOURCE_REF,
    )
    note = _optional(form.get("note"), "Evidence context", maximum=_MAX_NOTE)
    _service(shell, credits).record_user_declared_reference(
        target_kind,
        target_id,
        stage=stage,
        assertion=assertion,
        source_ref=source_ref,
        note=note,
        expected_song_id=song_id,
    )
    shell._consumer_notice = (
        "Artist-entered rights reference recorded as USER_DECLARED. N0TE did not observe or verify the communication, document, signature, ownership, registration, payment, or provider acceptance."
    )


def install_rights_evidence_chain() -> None:
    """Attach reviewable rights evidence to the existing People/Credits journey."""
    if getattr(ConsumerShell, "_rights_evidence_chain_installed", False):
        return

    original_state_content: Callable[[ConsumerShell, _PageState], str] = ConsumerShell._state_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_rights_content(self: ConsumerShell, state: _PageState) -> str:
        content = original_state_content(self, state)
        if state.kind != "running-people":
            return content
        return content + _rights_content(self)

    def with_rights_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path != "/credits/rights/evidence":
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
                self._simple_error("Open an Artist workspace before recording rights evidence."),
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
            _post_rights_evidence(self, form)
        except (NotFoundError, ValidationError, RightsEvidenceChainError, ConsumerShellError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/people")
            return
        except LineageCorruptionError:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE found malformed rights evidence and stopped before turning it into a stronger claim."
                ),
            )
            return
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that rights evidence action before it could become unclear rights state."
                ),
            )
            return
        self._redirect(handler, "/people")

    ConsumerShell._state_content = with_rights_content
    ConsumerShell._handle_post = with_rights_post
    ConsumerShell._rights_evidence_chain_installed = True
