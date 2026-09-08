from __future__ import annotations

import html
from typing import Callable

from .activity_timeline import SongActivityTimeline
from .consumer_shell import ConsumerShell, ConsumerShellError


def _activity_card(shell: ConsumerShell, song) -> str:
    timeline = SongActivityTimeline(
        shell.runtime.headquarters.store,
        shell.runtime.headquarters.activity,
    )
    items = timeline.for_song(song.id, newest_first=True)
    if not items:
        body = '<p class="muted">No recorded Song changes yet.</p>'
    else:
        rows: list[str] = []
        for item in items:
            detail = "" if item.detail is None else f'<p class="muted">{html.escape(item.detail)}</p>'
            rows.append(
                '<li class="stack">'
                f'<p><strong>{html.escape(item.summary)}</strong></p>'
                f'{detail}'
                '</li>'
            )
        body = (
            '<p class="muted">Newest recorded change first. N0TE shows canonical order only; '
            'this Activity history does not claim wall-clock times.</p>'
            f'<ol class="stack" aria-label="Song activity timeline">{"".join(rows)}</ol>'
        )
    return '<div class="card"><h2>What changed?</h2>' + body + '</div>'


def install_song_activity_timeline() -> None:
    """Add the read-only Song Activity card to the canonical consumer shell once.

    The existing shell remains the owner of Song rendering. This bounded extension
    appends one read-only card immediately before the existing Song grid closes and
    does not alter action authority, routing, Session, Version, material or Focus
    semantics. The installation marker lives on ConsumerShell itself so even a
    module reload cannot wrap the renderer twice.
    """

    if getattr(ConsumerShell, "_song_activity_timeline_installed", False):
        return

    original: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content

    def with_activity(self: ConsumerShell, state) -> str:
        rendered = original(self, state)
        store = self.runtime.headquarters.store
        song = store.active_song()
        if song is None or song.title != state.song_title:
            raise ConsumerShellError("active Song changed while preparing Activity history")
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError("Song page structure changed before Activity could be attached safely")
        return rendered[: -len(marker)] + _activity_card(self, song) + marker

    ConsumerShell._song_content = with_activity
    ConsumerShell._song_activity_timeline_installed = True
