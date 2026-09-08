from __future__ import annotations

import html
from typing import Callable

from .audio_engineering import (
    AudioEngineeringError,
    EngineeringEvidenceBinding,
    UnsupportedEngineeringMedia,
)
from .audition import UnsupportedAuditionMedia, inspect_audition_media
from .consumer_shell import ConsumerShell, ConsumerShellError
from .material import SongMaterialError
from .performance_timing import (
    MIN_MEASURED_CANDIDATES,
    TIMING_BOUNDED_OUT,
    TIMING_INSUFFICIENT_EVENTS,
    TIMING_MEASURED,
    TIMING_SILENT,
    TIMING_TOO_SHORT,
    PerformanceTimingEvidence,
    analyze_performance_timing,
)


def _format_density(value: float | None) -> str:
    return "not available" if value is None else f"{value:.2f} candidates/s"


def _timing_metrics(evidence: PerformanceTimingEvidence) -> str:
    if evidence.state == TIMING_MEASURED:
        assert evidence.median_spacing_ms is not None
        assert evidence.spacing_mad_ms is not None
        return (
            '<p class="status good">Descriptive timing evidence</p>'
            '<ul class="stack" aria-label="Performance timing measurements">'
            '<li><strong>Energy-change candidates</strong><br>'
            f'{evidence.candidate_count}</li>'
            '<li><strong>Candidate density</strong><br>'
            f'{html.escape(_format_density(evidence.candidate_density_per_second))}</li>'
            '<li><strong>Median candidate spacing</strong><br>'
            f'{evidence.median_spacing_ms:.1f} ms</li>'
            '<li><strong>Spacing variability</strong><br>'
            f'{evidence.spacing_mad_ms:.1f} ms median absolute deviation</li>'
            '</ul>'
        )
    if evidence.state == TIMING_INSUFFICIENT_EVENTS:
        return (
            '<p class="status caution">Not enough energy-change candidates for spacing statistics</p>'
            '<ul class="stack" aria-label="Performance timing measurements">'
            '<li><strong>Energy-change candidates</strong><br>'
            f'{evidence.candidate_count}</li>'
            '<li><strong>Candidate density</strong><br>'
            f'{html.escape(_format_density(evidence.candidate_density_per_second))}</li>'
            '<li><strong>Spacing statistics</strong><br>'
            f'not reported · fewer than {MIN_MEASURED_CANDIDATES} high-confidence candidates</li>'
            '</ul>'
        )
    if evidence.state == TIMING_SILENT:
        return (
            '<p class="status caution">Timing evidence unavailable</p>'
            '<p>Digital silence contains no energy-change candidates to describe.</p>'
        )
    if evidence.state == TIMING_TOO_SHORT:
        return (
            '<p class="status caution">Timing evidence unavailable</p>'
            '<p>This exact PCM material is shorter than the bounded 250 ms timing-analysis window.</p>'
        )
    if evidence.state == TIMING_BOUNDED_OUT:
        return (
            '<p class="status caution">Timing evidence unavailable</p>'
            '<p>This exact PCM material exceeds the bounded timing-analysis sample budget.</p>'
        )
    return (
        '<p class="status caution">Timing evidence unavailable</p>'
        '<p>N0TE has no trustworthy timing state for this material.</p>'
    )


def _timing_asset(shell: ConsumerShell, song_id: str, version_id: str, view) -> str:
    name = html.escape(view.asset.name)
    if view.status == "INTEGRITY_ERROR":
        return (
            '<li class="stack">'
            f'<p><strong>{name}</strong></p>'
            '<p class="status caution">Performance timing evidence unavailable</p>'
            '<p>N0TE could not re-verify this exact local material, so it showed no stale timing statistics.</p>'
            '</li>'
        )
    if view.status != "VERIFIED_MANAGED":
        return ""

    try:
        material = shell.runtime.headquarters.materials.resolve_asset(view.asset)
        media = inspect_audition_media(material.path)
    except UnsupportedAuditionMedia:
        return ""
    except SongMaterialError:
        return (
            '<li class="stack">'
            f'<p><strong>{name}</strong></p>'
            '<p class="status caution">Performance timing evidence unavailable</p>'
            '<p>N0TE could not re-verify this exact local material, so it showed no stale timing statistics.</p>'
            '</li>'
        )

    if media.content_type != "audio/wav":
        return ""

    try:
        evidence = analyze_performance_timing(
            material.path,
            binding=EngineeringEvidenceBinding(
                song_id=song_id,
                version_id=version_id,
                asset_id=view.asset.id,
                sha256=view.asset.sha256,
                source_size_bytes=material.size_bytes,
            ),
        )
    except UnsupportedEngineeringMedia:
        return (
            '<li class="stack">'
            f'<p><strong>{name}</strong></p>'
            '<p class="muted">This WAV encoding is outside the bounded Performance Timing Evidence contract. '
            'N0TE shows no invented substitute timing statistics.</p>'
            '</li>'
        )
    except AudioEngineeringError:
        return (
            '<li class="stack">'
            f'<p><strong>{name}</strong></p>'
            '<p class="status caution">Performance timing evidence unavailable</p>'
            '<p>The exact local bytes changed or could not be read consistently, so N0TE showed no stale timing statistics.</p>'
            '</li>'
        )

    return (
        '<li class="stack">'
        f'<p><strong>{name}</strong></p>'
        f'{_timing_metrics(evidence)}'
        '<p class="muted">Bound to this exact verified current Version material and recomputed from its local PCM bytes. '
        'N0TE keeps the Song, Version, Asset fingerprint and analyzer identity internal instead of exposing private identifiers here.</p>'
        '</li>'
    )


def _performance_timing_card(shell: ConsumerShell) -> str:
    store = shell.runtime.headquarters.store
    song = store.active_song()
    if song is None or song.current_version_id is None:
        return ""
    version = store.get_version(song.current_version_id)
    if version is None or version.song_id != song.id:
        raise ConsumerShellError(
            "current Song Version is missing while preparing performance timing evidence"
        )

    rows = [
        rendered
        for view in shell.runtime.headquarters.materials.version_materials(version.id)
        if (rendered := _timing_asset(shell, song.id, version.id, view))
    ]
    if not rows:
        return ""

    return (
        '<div class="card"><h2>Performance Timing Evidence</h2>'
        '<p>Read-only short-time energy evidence for exact current-Version PCM material. '
        'It can help investigate feel or pocket questions, but it does not grade the performance.</p>'
        '<ul class="stack" aria-label="Current Version performance timing evidence">'
        + "".join(rows)
        + '</ul>'
        '<p class="muted"><strong>Timing boundary:</strong> An energy-change candidate is not a beat, note, drum hit, '
        'syllable, performer action, intentional accent, or mistake. Spacing variability is descriptive, not a quality score. '
        'Without a trusted musical grid or reference, N0TE does not infer BPM, meter, beat phase, early/late, ahead/behind, '
        'swing, tight/sloppy, humanization quality, or needed quantization. Mixed or full-render audio can also reflect arrangement '
        'changes, edits, effects, fills, silence, and mastering. This evidence does not authorize timing correction or make an artistic decision.</p>'
        '</div>'
    )


def install_song_performance_timing() -> None:
    """Attach the read-only current-Version Performance Timing card exactly once."""
    if getattr(ConsumerShell, "_song_performance_timing_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content

    def with_performance_timing_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        card = _performance_timing_card(self)
        if not card:
            return rendered
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before Performance Timing Evidence could be attached safely"
            )
        return rendered[: -len(marker)] + card + marker

    ConsumerShell._song_content = with_performance_timing_card
    ConsumerShell._song_performance_timing_installed = True
