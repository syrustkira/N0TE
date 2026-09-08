from __future__ import annotations

import html
from typing import Callable

from .consumer_shell import ConsumerShell, ConsumerShellError


def _thread_card(shell: ConsumerShell) -> str:
    """Render one read-only continuation card from canonical Artist/Song state."""
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("Pick-up context requires an open Artist workspace")

    store = shell.runtime.headquarters.store
    song = store.active_song()
    if song is None:
        return (
            '<div class="card"><h2>Pick up the thread</h2>'
            '<p class="status caution">No active Song yet</p>'
            '<p>Start a Song when you are ready. N0TE will keep the work Session and next action attached to that Song.</p>'
            '<a class="button primary" href="/">Start a Song</a></div>'
        )

    title = html.escape(song.title)
    latest = shell.runtime.headquarters.sessions.latest_for_song(song.id)
    if latest is None:
        return (
            '<div class="card"><h2>Pick up the thread</h2>'
            f'<p class="song-name">{title}</p>'
            '<p class="status caution">No work Session yet</p>'
            '<p>Give this stretch of work one clear objective so leaving and returning does not erase the intent.</p>'
            '<a class="button primary" href="/song">Start a work Session</a></div>'
        )

    if latest.state == "OPEN":
        return (
            '<div class="card"><h2>Pick up the thread</h2>'
            f'<p class="song-name">{title}</p>'
            '<p class="status good">Work Session open</p>'
            '<p>Current objective</p>'
            f'<p><strong>{html.escape(latest.objective)}</strong></p>'
            '<a class="button primary" href="/song">Continue this Song</a></div>'
        )

    if latest.next_action is None:
        raise ConsumerShellError("closed work Session is missing its next action")
    return (
        '<div class="card"><h2>Pick up the thread</h2>'
        f'<p class="song-name">{title}</p>'
        '<p class="status good">Last Session closed</p>'
        '<p>Next action</p>'
        f'<p><strong>{html.escape(latest.next_action)}</strong></p>'
        '<a class="button primary" href="/song">Pick up this Song</a></div>'
    )


def install_now_thread() -> None:
    """Put canonical Song continuity before Focus controls on the real NOW page."""
    if getattr(ConsumerShell, "_now_thread_installed", False):
        return

    original_focus: Callable[[ConsumerShell, object], str] = ConsumerShell._focus_content

    def with_now_thread(self: ConsumerShell, state) -> str:
        rendered = original_focus(self, state)
        marker = '<section class="grid">'
        if not rendered.startswith(marker):
            raise ConsumerShellError(
                "NOW page structure changed before Pick up the thread could attach safely"
            )
        return marker + _thread_card(self) + rendered[len(marker) :]

    ConsumerShell._focus_content = with_now_thread
    ConsumerShell._now_thread_installed = True
