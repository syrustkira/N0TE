from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError
from .lineage import ValidationError
from .skill_model import (
    SkillModelBinding,
    SkillModelError,
    SkillModelService,
    StaleSkillModelError,
)

_LEVEL_LABELS = {
    "UNKNOWN": "Unknown",
    "INTRODUCED": "Introduced",
    "PRACTICED": "Practiced",
    "APPLIED": "Applied",
    "INDEPENDENT": "Independent",
}
_DECLARATION_LEVELS = ("INTRODUCED", "PRACTICED", "APPLIED", "INDEPENDENT")
_CORRECTION_LEVELS = ("UNKNOWN", "INTRODUCED", "PRACTICED", "APPLIED", "INDEPENDENT")
_ASSISTANCE_CHOICES = (
    ("HIGH", "I needed a lot of guidance"),
    ("SOME", "I used some guidance"),
    ("NONE", "I did this without guidance"),
)
_CONFIDENCE_CHOICES = (
    ("LOW", "Low confidence"),
    ("MEDIUM", "Medium confidence"),
    ("HIGH", "High confidence"),
)


def _options(values: tuple[str, ...], *, selected: str | None = None) -> str:
    result = []
    for value in values:
        selected_attr = ' selected' if value == selected else ""
        result.append(
            f'<option value="{html.escape(value, quote=True)}"{selected_attr}>'
            f'{html.escape(_LEVEL_LABELS[value])}</option>'
        )
    return "".join(result)


def _assistance_options(selected_value: float | None = None) -> str:
    selected_key = None
    if selected_value is not None:
        selected_key = "NONE" if selected_value == 0.0 else "SOME" if selected_value <= 0.5 else "HIGH"
    return "".join(
        f'<option value="{key}"{" selected" if key == selected_key else ""}>'
        f'{html.escape(label)}</option>'
        for key, label in _ASSISTANCE_CHOICES
    )


def _confidence_options(selected_value: float | None = None) -> str:
    if selected_value is None:
        selected_key = "MEDIUM"
    else:
        selected_key = "LOW" if selected_value <= 0.4 else "MEDIUM" if selected_value < 1.0 else "HIGH"
    return "".join(
        f'<option value="{key}"{" selected" if key == selected_key else ""}>'
        f'{html.escape(label)}</option>'
        for key, label in _CONFIDENCE_CHOICES
    )


def _skill_card(shell: ConsumerShell) -> str:
    service = SkillModelService(shell.runtime.headquarters.skills)
    views = service.views()
    rows: list[str] = []
    for view in views:
        binding = service.binding_for(view.skill_id)
        token = shell._new_action(
            "skill-correct",
            json.dumps(
                [binding.skill_id, binding.expected_latest_assessment_id],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        evidence = (
            "No linked evidence claims"
            if view.evidence_count == 0
            else f"{view.evidence_count} linked evidence claim"
            + ("" if view.evidence_count == 1 else "s")
        )
        confidence = f"Confidence {round(view.confidence * 100)}%"
        correction_note = (
            ""
            if view.correction_note is None
            else f'<p class="muted">Your latest correction: {html.escape(view.correction_note)}</p>'
        )
        rows.append(
            '<li class="stack">'
            f'<p><strong>{html.escape(view.skill_id)}</strong></p>'
            f'<p class="status">{html.escape(_LEVEL_LABELS[view.level])}</p>'
            f'<p>{html.escape(view.source_label)} · {html.escape(view.assistance_label)} · '
            f'{html.escape(confidence)} · {html.escape(evidence)}</p>'
            f'{correction_note}'
            '<form class="stack" method="post" action="/skill/correct" '
            f'aria-label="Correct {html.escape(view.skill_id, quote=True)} Skill">'
            f'{shell._hidden(token)}'
            '<div><label>Correct N0TE level<select name="level" required>'
            f'{_options(_CORRECTION_LEVELS, selected=view.level)}</select></label></div>'
            '<div><label>Assistance for this corrected assessment<select name="assistance" required>'
            f'{_assistance_options(view.assistance_level)}</select></label></div>'
            '<p class="muted">If you correct the level to Unknown, assistance is treated as not established.</p>'
            '<div><label>How confident are you in this correction?<select name="confidence" required>'
            f'{_confidence_options(view.confidence)}</select></label></div>'
            '<div><label>Why are you correcting this?<textarea name="reason" maxlength="500" rows="2" required></textarea></label></div>'
            '<button type="submit">Correct this Skill</button>'
            '<p class="muted">Correction appends a new assessment. It does not erase the earlier history.</p>'
            '</form></li>'
        )
    history = (
        '<p class="muted">N0TE has no recorded Skill assessments yet. You can seed the model with what you know now.</p>'
        if not rows
        else '<ul class="stack" aria-label="Current Skill model">' + "".join(rows) + "</ul>"
    )
    declaration = (
        '<form class="stack" method="post" action="/skill/declare" aria-label="Add Skill self-assessment">'
        f'{shell._hidden(shell._new_action("skill-declare"))}'
        '<div><label for="skill-name">Skill</label><input id="skill-name" name="skill_name" type="text" maxlength="120" autocomplete="off" required></div>'
        '<div><label for="skill-level">Where are you now?</label><select id="skill-level" name="level" required>'
        f'{_options(_DECLARATION_LEVELS)}</select></div>'
        '<div><label for="skill-assistance">How much guidance did this assessment involve?</label><select id="skill-assistance" name="assistance" required>'
        f'{_assistance_options()}</select></div>'
        '<div><label for="skill-confidence">How confident are you in this self-assessment?</label><select id="skill-confidence" name="confidence" required>'
        f'{_confidence_options()}</select></div>'
        '<button type="submit">Add this Skill</button>'
        '<p class="muted">Independent means you can do it without guidance. N0TE will not mark itself as having observed or assessed mastery from this form.</p>'
        '</form>'
    )
    return (
        '<div class="card"><h2>What N0TE thinks you can do</h2>'
        '<p>This is your inspectable Skill Model. Artist statements and corrections stay distinct from N0TE assessments and observed real-work evidence.</p>'
        f'{history}<h3>Add your own assessment</h3>{declaration}</div>'
    )


def _post_declare(shell: ConsumerShell, handler: BaseHTTPRequestHandler, form: Mapping[str, str]) -> None:
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("Open an Artist workspace before updating the Skill Model.")
    action = shell._consume_action(form.get("action", ""), "skill-declare")
    if action is None:
        shell._send_html(handler, 409, shell._simple_error("That Skill action was already handled or expired."))
        return
    try:
        assessment = SkillModelService(shell.runtime.headquarters.skills).declare(
            skill_id=form.get("skill_name", ""),
            level=form.get("level", ""),
            assistance=form.get("assistance", ""),
            confidence=form.get("confidence", ""),
        )
    except (ValidationError, SkillModelError) as exc:
        shell._consumer_notice = str(exc)
        shell._redirect(handler, "/song")
        return
    shell._consumer_notice = f"Added {assessment.skill_id} as {_LEVEL_LABELS[assessment.level]}."
    shell._redirect(handler, "/song")


def _post_correct(shell: ConsumerShell, handler: BaseHTTPRequestHandler, form: Mapping[str, str]) -> None:
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("Open an Artist workspace before correcting the Skill Model.")
    action = shell._consume_action(form.get("action", ""), "skill-correct")
    if action is None or action.value is None:
        shell._send_html(handler, 409, shell._simple_error("That Skill correction was already handled or expired."))
        return
    try:
        decoded = json.loads(action.value)
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or not all(isinstance(item, str) and item for item in decoded)
        ):
            raise ValueError
        skill_id, expected_id = decoded
    except (ValueError, TypeError, json.JSONDecodeError):
        shell._send_html(handler, 409, shell._simple_error("That Skill correction is no longer valid."))
        return
    try:
        assessment = SkillModelService(shell.runtime.headquarters.skills).correct(
            SkillModelBinding(skill_id, expected_id),
            level=form.get("level", ""),
            assistance=form.get("assistance", ""),
            confidence=form.get("confidence", ""),
            reason=form.get("reason", ""),
        )
    except StaleSkillModelError:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That Skill changed. Reload the Song before correcting N0TE's current model."),
        )
        return
    except (ValidationError, SkillModelError) as exc:
        shell._consumer_notice = str(exc)
        shell._redirect(handler, "/song")
        return
    shell._consumer_notice = f"Corrected {assessment.skill_id} to {_LEVEL_LABELS[assessment.level]}. Earlier assessments were preserved."
    shell._redirect(handler, "/song")


def install_song_skill_model() -> None:
    """Attach the bounded Skill Model card and its two artist-authority POSTs once."""

    if getattr(ConsumerShell, "_song_skill_model_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_skill_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError("Song page structure changed before Skill Model could be attached safely")
        return rendered[: -len(marker)] + _skill_card(self) + marker

    def with_skill_posts(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path not in {"/skill/declare", "/skill/correct"}:
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
            if path == "/skill/declare":
                _post_declare(self, handler, form)
            else:
                _post_correct(self, handler, form)
        except ConsumerShellError as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/song")
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that Skill action before it could become an unclear consumer state."
                ),
            )

    ConsumerShell._song_content = with_skill_card
    ConsumerShell._handle_post = with_skill_posts
    ConsumerShell._song_skill_model_installed = True
