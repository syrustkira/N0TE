from __future__ import annotations

import html
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError
from .lineage import NotFoundError, ValidationError
from .songwriting import (
    MAX_SONGWRITING_SECTION_CHARS,
    MAX_SONGWRITING_SURFACE_TEXT_CHARS,
    SONGWRITING_ASPECTS,
    SONGWRITING_ENTRY_KINDS,
    SongwritingCaseHistoryError,
    SongwritingCaseHistoryIntegrityError,
    SongwritingCaseHistoryService,
    SongwritingEntry,
    StaleSongwritingCaseHistoryError,
)

_MAX_VISIBLE_HISTORY = 24
_NONE_MARKER = "~"

_ASPECT_LABELS = {
    "LYRICS": "Lyrics",
    "TOPLINE": "Topline",
    "MELODY": "Melody",
    "PHRASING": "Phrasing",
    "TAKE_COMP": "Takes / comp",
    "LYRIC_ALIGNMENT": "Lyric alignment",
    "PITCH_TIMING": "Pitch / timing",
    "DOUBLES": "Doubles",
    "HARMONIES": "Harmonies",
    "AD_LIBS": "Ad-libs",
    "PERFORMANCE": "Performance",
    "VOCAL_PRODUCTION": "Vocal production",
}

_KIND_LABELS = {
    "MARK": "Note",
    "OBSERVATION": "Observation",
    "DECISION": "Decision",
    "REJECTED_IDEA": "Rejected idea",
    "UNRESOLVED": "Unresolved question",
}

_CAPTURE_KIND_ORDER = (
    "OBSERVATION",
    "DECISION",
    "UNRESOLVED",
    "REJECTED_IDEA",
    "MARK",
)


@dataclass(frozen=True)
class _CaptureBinding:
    song_id: str
    session_id: str
    session_version_id: str | None
    current_version_id: str | None


@dataclass(frozen=True)
class _PromotionBinding:
    song_id: str
    session_id: str
    item_id: str
    entry_version_id: str | None
    current_version_id: str | None


def _service(shell: ConsumerShell) -> SongwritingCaseHistoryService:
    hq = shell.runtime.headquarters
    return SongwritingCaseHistoryService(hq.store, hq.sessions)


def _marker(value: str | None) -> str:
    return _NONE_MARKER if value is None else value


def _unmarker(value: str) -> str | None:
    return None if value == _NONE_MARKER else value


def _encode_capture(binding: _CaptureBinding) -> str:
    return json.dumps(
        [
            binding.song_id,
            binding.session_id,
            _marker(binding.session_version_id),
            _marker(binding.current_version_id),
        ],
        separators=(",", ":"),
    )


def _decode_capture(value: str) -> _CaptureBinding:
    try:
        decoded = json.loads(value)
        if (
            not isinstance(decoded, list)
            or len(decoded) != 4
            or not all(isinstance(item, str) and item for item in decoded)
        ):
            raise ValueError
        return _CaptureBinding(
            song_id=decoded[0],
            session_id=decoded[1],
            session_version_id=_unmarker(decoded[2]),
            current_version_id=_unmarker(decoded[3]),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StaleSongwritingCaseHistoryError(
            "That Write & Vocal capture is no longer valid. Reload the Song and try again."
        ) from exc


def _encode_promotion(binding: _PromotionBinding) -> str:
    return json.dumps(
        [
            binding.song_id,
            binding.session_id,
            binding.item_id,
            _marker(binding.entry_version_id),
            _marker(binding.current_version_id),
        ],
        separators=(",", ":"),
    )


def _decode_promotion(value: str) -> _PromotionBinding:
    try:
        decoded = json.loads(value)
        if (
            not isinstance(decoded, list)
            or len(decoded) != 5
            or not all(isinstance(item, str) and item for item in decoded)
        ):
            raise ValueError
        return _PromotionBinding(
            song_id=decoded[0],
            session_id=decoded[1],
            item_id=decoded[2],
            entry_version_id=_unmarker(decoded[3]),
            current_version_id=_unmarker(decoded[4]),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StaleSongwritingCaseHistoryError(
            "That songwriting decision action is no longer valid. Reload the Song and try again."
        ) from exc


def _aspect_options() -> str:
    return "".join(
        f'<option value="{aspect}">{html.escape(_ASPECT_LABELS[aspect])}</option>'
        for aspect in SONGWRITING_ASPECTS
    )


def _kind_options() -> str:
    return "".join(
        f'<option value="{kind}"{" selected" if kind == "OBSERVATION" else ""}>{html.escape(_KIND_LABELS[kind])}</option>'
        for kind in _CAPTURE_KIND_ORDER
        if kind in SONGWRITING_ENTRY_KINDS
    )


def _capture_form(shell: ConsumerShell, song, session) -> str:
    binding = _CaptureBinding(
        song_id=song.id,
        session_id=session.id,
        session_version_id=session.version_id,
        current_version_id=song.current_version_id,
    )
    action = shell._new_action("songwriting-capture", _encode_capture(binding))
    return (
        '<form class="stack" method="post" action="/songwriting/capture" aria-label="Capture a Write and Vocal note">'
        f'{shell._hidden(action)}'
        '<div><label>What are you working on?'
        f'<select name="aspect" required>{_aspect_options()}</select></label></div>'
        '<div><label>What kind of note is this?'
        f'<select name="kind" required>{_kind_options()}</select></label></div>'
        '<div><label>Section or moment (optional)'
        f'<input name="section" type="text" maxlength="{MAX_SONGWRITING_SECTION_CHARS}" placeholder="Chorus, verse 2, final word"></label></div>'
        '<div><label>Your note'
        f'<textarea name="text" maxlength="{MAX_SONGWRITING_SURFACE_TEXT_CHARS}" rows="4" required></textarea></label></div>'
        '<button type="submit">Capture this writing note</button>'
        '<p class="muted">N0TE stores exactly what you enter as Song work history. '
        'An Observation is your observation, not proof that N0TE heard the performance. '
        'A Decision remains Session history until you explicitly choose to remember it for the Song.</p>'
        '</form>'
    )


def _promotion_form(shell: ConsumerShell, song, entry: SongwritingEntry) -> str:
    binding = _PromotionBinding(
        song_id=song.id,
        session_id=entry.session_id,
        item_id=entry.item_id,
        entry_version_id=entry.version_id,
        current_version_id=song.current_version_id,
    )
    action = shell._new_action("songwriting-promote", _encode_promotion(binding))
    return (
        '<form method="post" action="/songwriting/promote" aria-label="Remember this writing decision for the Song">'
        f'{shell._hidden(action)}'
        '<button type="submit">Remember this decision for the Song</button>'
        '<p class="muted">This promotes your explicit decision into Song creative evidence. '
        'It does not approve a Version, edit audio, send anything, or grant DAW/provider authority.</p>'
        '</form>'
    )


def _history_entry(shell: ConsumerShell, song, entry: SongwritingEntry) -> str:
    aspect = _ASPECT_LABELS.get(entry.aspect, entry.aspect.replace("_", " ").title())
    kind = _KIND_LABELS.get(entry.kind, entry.kind.replace("_", " ").title())
    section = "" if entry.section is None else f' · {html.escape(entry.section)}'
    provenance = "Current work Session" if entry.session_state == "OPEN" else "Earlier work Session"
    promotion = ""
    if entry.kind == "DECISION":
        if entry.promoted:
            promotion = (
                '<p class="status good">Remembered for this Song</p>'
                '<p class="muted">This is still your declared creative decision, not an observed fact or Version approval.</p>'
            )
        else:
            promotion = _promotion_form(shell, song, entry)
    return (
        '<li class="stack">'
        f'<p><strong>{html.escape(aspect)}</strong> · {html.escape(kind)}{section}</p>'
        f'<p>{html.escape(entry.text)}</p>'
        f'<p class="muted">You recorded this · {html.escape(provenance)}</p>'
        f'{promotion}'
        '</li>'
    )


def _history(shell: ConsumerShell, song, entries: tuple[SongwritingEntry, ...]) -> str:
    if not entries:
        return (
            '<p class="muted">No Write & Vocal case history is recorded for this Song yet. '
            'Start a work Session and capture only what you actually tried, noticed, decided, rejected, or still need to answer.</p>'
        )
    visible = tuple(reversed(entries[-_MAX_VISIBLE_HISTORY:]))
    omitted = len(entries) - len(visible)
    note = (
        ""
        if omitted <= 0
        else f'<p class="muted">Showing the latest {len(visible)} of {len(entries)} writing/vocal notes. Earlier history remains preserved.</p>'
    )
    return (
        f'{note}<ul class="stack" aria-label="Write and Vocal case history">'
        + "".join(_history_entry(shell, song, entry) for entry in visible)
        + "</ul>"
    )


def _songwriting_card(shell: ConsumerShell) -> str:
    store = shell.runtime.headquarters.store
    song = store.active_song()
    if song is None:
        return ""
    service = _service(shell)
    try:
        entries = service.entries_for_song(song.id)
    except SongwritingCaseHistoryIntegrityError:
        return (
            '<div class="card"><h2>Write &amp; Vocal</h2>'
            '<p class="status caution">Writing history needs recovery</p>'
            '<p>N0TE found writing/vocal case-history data it cannot verify, so it is showing no stale notes and accepting no new writing capture here.</p>'
            '<p class="muted">No audio, Song Version, provider, or DAW state was changed.</p>'
            '</div>'
        )

    latest = shell.runtime.headquarters.sessions.latest_for_song(song.id)
    capture = ""
    if latest is not None and latest.state == "OPEN":
        capture = (
            '<h3>Capture what is happening in this pass</h3>'
            f'{_capture_form(shell, song, latest)}'
        )
    else:
        capture = (
            '<h3>Capture what is happening in this pass</h3>'
            '<p class="status caution">Start or resume a work Session first</p>'
            '<p class="muted">Write & Vocal capture is Session-bound. Use the work Session controls above so each lyric, topline, take, phrasing or vocal-production note keeps the right Song context.</p>'
        )

    return (
        '<div class="card"><h2>Write &amp; Vocal</h2>'
        '<p>Keep lyrics, topline, melody, phrasing, takes, lyric alignment, pitch/timing notes, doubles, harmonies, ad-libs, performance choices and vocal-production decisions attached to this Song.</p>'
        '<p class="muted"><strong>Creative truth boundary:</strong> This is artist-entered case history. '
        'N0TE does not claim it heard, transcribed, tuned, comped, generated, cloned or edited a voice here. '
        'Pitch/timing notes are notes, not automatic correction authority. Voice cloning is a separate capability and is not part of this surface.</p>'
        '<h3>Case history</h3>'
        f'{_history(shell, song, entries)}'
        f'{capture}'
        '</div>'
    )


def _retention_value_text(value: str) -> str:
    """Mirror retention's read-only whitespace and length presentation exactly."""
    text = " ".join(str(value).split())
    return text if len(text) <= 280 else text[:277] + "..."


def _sanitize_owned_storage(shell: ConsumerShell, rendered: str) -> str:
    """Hide N0TE's songwriting storage envelope from the final Song document."""
    song = shell.runtime.headquarters.store.active_song()
    if song is None:
        return rendered
    service = _service(shell)
    for raw, visible in service.presentation_replacements_for_song(song.id):
        replacement = (
            "Writing history hidden pending recovery."
            if visible is None
            else visible
        )
        # Generic Session history escapes the exact stored body.
        rendered = rendered.replace(html.escape(raw), html.escape(replacement))
        # Retention deliberately collapses whitespace and caps durable values at
        # 280 characters before escaping. Match that exact presentation without
        # changing canonical Evidence or teaching retention private storage syntax.
        rendered = rendered.replace(
            html.escape(_retention_value_text(raw)),
            html.escape(_retention_value_text(replacement)),
        )
    return rendered


def _surface_text(value: str) -> str:
    text = str(value)
    if len(text) > MAX_SONGWRITING_SURFACE_TEXT_CHARS:
        raise ValidationError("songwriting note exceeds the safe local form limit")
    return text


def _post_capture(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "songwriting-capture")
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That Write & Vocal capture was already handled or expired."),
        )
        return
    binding = _decode_capture(action.value)
    entry = _service(shell).capture_bound(
        song_id=binding.song_id,
        session_id=binding.session_id,
        expected_session_version_id=binding.session_version_id,
        expected_current_version_id=binding.current_version_id,
        aspect=form.get("aspect", ""),
        kind=form.get("kind", ""),
        section=form.get("section"),
        text=_surface_text(form.get("text", "")),
    )
    shell._consumer_notice = (
        f"Captured your {_KIND_LABELS.get(entry.kind, entry.kind.lower())} for "
        f"{_ASPECT_LABELS.get(entry.aspect, entry.aspect.lower())}."
    )
    shell._redirect(handler, "/song")


def _post_promote(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "songwriting-promote")
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That writing decision action was already handled or expired."),
        )
        return
    binding = _decode_promotion(action.value)
    promoted = _service(shell).promote_decision_bound(
        binding.item_id,
        expected_song_id=binding.song_id,
        expected_session_id=binding.session_id,
        expected_entry_version_id=binding.entry_version_id,
        expected_current_version_id=binding.current_version_id,
        scope_kind="SONG",
    )
    if promoted.claim.scope_id != binding.song_id:
        raise SongwritingCaseHistoryIntegrityError(
            "Songwriting decision promotion crossed the active Song binding"
        )
    shell._consumer_notice = (
        "Remembered your writing decision for this Song. It remains your declared creative decision; "
        "no Version was approved and no audio was changed."
    )
    shell._redirect(handler, "/song")


def install_songwriting_vocal_surface() -> None:
    """Attach Song-bound Write/Vocal history, capture, and decision promotion once."""
    if getattr(ConsumerShell, "_songwriting_vocal_surface_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post
    original_render_running: Callable[[ConsumerShell, str], str] = ConsumerShell._render_running

    def with_songwriting_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        card = _songwriting_card(self)
        if not card:
            return rendered
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before Write & Vocal could be attached safely"
            )
        return rendered[: -len(marker)] + card + marker

    def with_songwriting_render(self: ConsumerShell, path: str) -> str:
        # _render_running resolves the live _state_content/_song_content chain at
        # request time, so this sees cards installed before or after this module.
        rendered = original_render_running(self, path)
        if path != "/song":
            return rendered
        return _sanitize_owned_storage(self, rendered)

    def with_songwriting_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path not in {"/songwriting/capture", "/songwriting/promote"}:
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
            if path == "/songwriting/capture":
                _post_capture(self, handler, form)
            else:
                _post_promote(self, handler, form)
        except StaleSongwritingCaseHistoryError as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
        except (ValidationError, NotFoundError, SongwritingCaseHistoryError) as exc:
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
                    "N0TE stopped that Write & Vocal action before it could become an unclear consumer state."
                ),
            )

    ConsumerShell._song_content = with_songwriting_card
    ConsumerShell._render_running = with_songwriting_render
    ConsumerShell._handle_post = with_songwriting_post
    ConsumerShell._songwriting_vocal_surface_installed = True
