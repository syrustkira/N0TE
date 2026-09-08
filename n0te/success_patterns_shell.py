from __future__ import annotations

import html
from typing import Callable

from .consumer_shell import ConsumerShell, ConsumerShellError
from .success_patterns import SongSuccessPatterns, SuccessPatternView

_STATE_LABELS = {
    "NO_COMPLETED_EVIDENCE": "No completed evidence yet",
    "SINGLE_OBSERVATION": "One retained example",
    "SUCCESS_ONLY": "Retained examples only",
    "MIXED": "Mixed evidence",
    "NO_KEEP_EVIDENCE": "No retained-as-is example",
    "INCONCLUSIVE_ONLY": "Inconclusive evidence only",
}


def _percent(value: float | None) -> str:
    return "Not established" if value is None else f"{round(value * 100)}%"


def _terms(title: str, items) -> str:
    if not items:
        return ""
    rows = "".join(
        f'<li>{html.escape(item.term)} <span class="muted">({item.episode_count} comparable episode'
        f'{"" if item.episode_count == 1 else "s"})</span></li>'
        for item in items
    )
    return f'<p><strong>{html.escape(title)}</strong></p><ul>{rows}</ul>'


def _observations(pattern: SuccessPatternView) -> str:
    if not pattern.observations:
        return '<p class="muted">No completed observed consequences are available for this exact pattern yet.</p>'
    rows: list[str] = []
    for item in pattern.observations:
        sources = ", ".join(item.source_labels)
        rows.append(
            '<li class="stack">'
            f'<p>{html.escape(item.observation)}</p>'
            f'<p class="muted">Recorded {item.count} time{"" if item.count == 1 else "s"} · '
            f'{html.escape(sources)} · mean confidence {_percent(item.confidence_mean)}</p>'
            '</li>'
        )
    return '<ul class="stack" aria-label="Observed consequences in comparable Learning episodes">' + "".join(rows) + "</ul>"


def _pattern(pattern: SuccessPatternView) -> str:
    state = _STATE_LABELS.get(pattern.humility_state, "Evidence summary")
    counts = (
        f'{pattern.completed_count} completed · {pattern.pending_count} pending · '
        f'{pattern.keep_count} keep · {pattern.revert_count} revert · '
        f'{pattern.revise_count} revise · {pattern.inconclusive_count} inconclusive'
    )
    causal_label = "Association only" if pattern.causal_status == "ASSOCIATION_ONLY" else "Causal status unavailable"
    return (
        '<li class="stack">'
        f'<p><strong>{html.escape(pattern.domain)} · {html.escape(pattern.subject)}</strong></p>'
        f'<p><strong>Exact change:</strong> {html.escape(pattern.change)}</p>'
        f'<p class="status">{html.escape(state)}</p>'
        f'<p>{html.escape(pattern.warning)}</p>'
        f'<p class="muted">{html.escape(counts)}</p>'
        f'<p class="muted">{html.escape(causal_label)}. This pattern is prior evidence, not a recipe or prediction.</p>'
        f'{_observations(pattern)}'
        f'{_terms("Conditions recorded with this pattern", pattern.conditions)}'
        f'{_terms("Alternative explanations / confounders", pattern.alternative_explanations)}'
        '<p class="muted">Mean observation confidence: '
        f'{_percent(pattern.observation_confidence_mean)} · Mean decision confidence: '
        f'{_percent(pattern.decision_confidence_mean)}</p>'
        '</li>'
    )


def _success_card(shell: ConsumerShell) -> str:
    views = SongSuccessPatterns(
        shell.runtime.headquarters.store,
        shell.runtime.headquarters.success,
    ).for_active_song()
    body = (
        '<p class="muted">No comparable Learning patterns exist for this Song yet. '
        'Complete Learning experiments to build evidence.</p>'
        if not views
        else '<ul class="stack" aria-label="Prior Learning patterns">'
        + "".join(_pattern(item) for item in views)
        + "</ul>"
    )
    return (
        '<div class="card"><h2>What does your past work suggest?</h2>'
        '<p>N0TE can summarize exact Learning patterns, but it does not know that a change caused an outcome. '
        'Counterexamples, uncertainty, pending evidence and alternative explanations stay visible.</p>'
        f'{body}</div>'
    )


def install_song_success_patterns() -> None:
    """Attach one read-only Success Memory projection to the Song page exactly once."""
    if getattr(ConsumerShell, "_song_success_patterns_installed", False):
        return
    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content

    def with_success_patterns(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError("Song page structure changed before Success patterns could be attached safely")
        return rendered[: -len(marker)] + _success_card(self) + marker

    ConsumerShell._song_content = with_success_patterns
    ConsumerShell._song_success_patterns_installed = True
