from __future__ import annotations

import html
import json
from typing import Callable

from .consumer_shell import ConsumerShell, ConsumerShellError
from .retention import RetainedFact, SongRetentionBrief


def _value_text(value) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            text = "Represented value unavailable"
    text = " ".join(text.split())
    return text if len(text) <= 280 else text[:277] + "..."


def _facts(brief: SongRetentionBrief) -> str:
    if not brief.durable_facts:
        return '<p class="muted">No promoted Artist/Song/current-Version facts are active yet.</p>'
    rows = []
    for fact in brief.durable_facts[:10]:
        rows.append(
            '<li class="stack">'
            f'<p><strong>{html.escape(fact.key)}</strong>: {html.escape(_value_text(fact.value))}</p>'
            f'<p class="muted">{html.escape(fact.scope.title())} · '
            f'{html.escape(fact.source_label)} · confidence {round(fact.confidence * 100)}% · '
            f'{html.escape(fact.twin_domain.title())}</p>'
            '</li>'
        )
    more = len(brief.durable_facts) - len(rows)
    suffix = (
        ""
        if more <= 0
        else f'<p class="muted">{more} more active durable fact{"" if more == 1 else "s"} remain consultable.</p>'
    )
    return '<ul class="stack" aria-label="Active durable memory">' + "".join(rows) + "</ul>" + suffix


def _thread(brief: SongRetentionBrief) -> str:
    latest = brief.latest_session
    if latest is None:
        return '<p class="muted">No work Session has been recorded for this Song yet.</p>'
    pieces = [f'<p><strong>Last objective:</strong> {html.escape(latest.objective)}</p>']
    if latest.state == "OPEN":
        pieces.append('<p class="status good">Work Session still open</p>')
    else:
        if latest.debrief_summary:
            pieces.append(
                f'<p><strong>What became clear:</strong> {html.escape(latest.debrief_summary)}</p>'
            )
        if latest.next_action:
            pieces.append(
                f'<p class="song-name">Next: {html.escape(latest.next_action)}</p>'
            )
    return "".join(pieces)


def _retention_card(shell: ConsumerShell) -> str:
    brief = shell.runtime.headquarters.retention.for_active_song()
    if brief is None:
        return ""
    recurring_keys = sorted(
        {
            item.key
            for item in brief.friction
            if item.recurring_session_count >= 2
        }
    )
    decided = sum(1 for item in brief.learning if item.decision is not None)
    kept = sum(1 for item in brief.learning if item.decision == "KEEP")
    session_notes = sum(len(item.items) for item in brief.sessions)
    summary = (
        f'{len(brief.sessions)} work Session{"" if len(brief.sessions) == 1 else "s"} · '
        f'{session_notes} captured note{"" if session_notes == 1 else "s"} · '
        f'{len(brief.learning)} Learning episode{"" if len(brief.learning) == 1 else "s"} · '
        f'{decided} decided · {kept} kept · '
        f'{len(brief.durable_facts)} active durable fact{"" if len(brief.durable_facts) == 1 else "s"}'
    )
    recurring = (
        '<p class="muted">No blocker has enough distinct-Session evidence to call recurring.</p>'
        if not recurring_keys
        else '<p><strong>Recurring blockers:</strong> '
        + html.escape(", ".join(recurring_keys))
        + '</p>'
    )
    imported = (
        ""
        if not brief.imported_context
        else f'<p class="muted">{len(brief.imported_context)} imported/synced context entr'
        f'{"y" if len(brief.imported_context) == 1 else "ies"} remain evidence-only and consultable.</p>'
    )
    skills = (
        ""
        if not brief.skills
        else '<p><strong>Artist skill context:</strong> '
        + html.escape(", ".join(f"{item.skill_id} = {item.level}" for item in brief.skills[:8]))
        + (' ...' if len(brief.skills) > 8 else '')
        + '</p>'
    )
    return (
        '<div class="card"><h2>What N0TE remembers</h2>'
        '<p>N0TE consults the canonical Song history instead of starting from a blank slate. '
        'Session notes stay history; promoted evidence stays source/confidence-labeled; Learning outcomes '
        'remain association-only; recurring friction requires repeated distinct Sessions.</p>'
        f'<p class="status good">Retention active · {html.escape(summary)}</p>'
        f'{_thread(brief)}'
        f'{recurring}{skills}{imported}'
        '<p><strong>Active durable memory</strong></p>'
        f'{_facts(brief)}'
        '<p class="muted">This view is read-only. Remembering does not grant N0TE permission to act, '
        'and one kept result does not become permanent taste doctrine or a causal rule.</p>'
        '</div>'
    )


def install_song_retention() -> None:
    """Attach canonical retention consultation to the Song page exactly once."""
    if getattr(ConsumerShell, "_song_retention_installed", False):
        return
    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content

    def with_retention(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before retention could be attached safely"
            )
        return rendered[: -len(marker)] + _retention_card(self) + marker

    ConsumerShell._song_content = with_retention
    ConsumerShell._song_retention_installed = True
