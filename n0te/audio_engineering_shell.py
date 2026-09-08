from __future__ import annotations

import html
from typing import Callable

from .audio_engineering import (
    AudioEngineeringError,
    EngineeringEvidenceBinding,
    EngineeringSnapshot,
    LOUDNESS_BACKEND_ERROR,
    LOUDNESS_BACKEND_UNAVAILABLE,
    LOUDNESS_BOUNDED_OUT,
    LOUDNESS_MEASURED,
    LOUDNESS_SILENT,
    LOUDNESS_TOO_SHORT,
    LOUDNESS_UNSUPPORTED_CHANNEL_LAYOUT,
    UnsupportedEngineeringMedia,
    analyze_pcm_wave,
)
from .audition import UnsupportedAuditionMedia, inspect_audition_media
from .consumer_shell import ConsumerShell, ConsumerShellError
from .material import SongMaterialError


def _format_rate(sample_rate_hz: int) -> str:
    if sample_rate_hz % 1000 == 0:
        return f"{sample_rate_hz // 1000} kHz"
    return f"{sample_rate_hz / 1000.0:.1f} kHz"


def _format_channels(channels: int) -> str:
    if channels == 1:
        return "Mono"
    if channels == 2:
        return "Stereo"
    return f"{channels} channels"


def _format_duration(seconds: float) -> str:
    minutes = int(seconds // 60.0)
    remainder = seconds - (minutes * 60.0)
    if minutes:
        return f"{minutes}:{remainder:04.1f}"
    return f"{remainder:.1f} s"


def _format_db(value: float | None) -> str:
    return "silent" if value is None else f"{value:.2f} dBFS"


def _format_crest(value: float | None) -> str:
    return "not defined for silence" if value is None else f"{value:.2f} dB"


def _format_loudness(snapshot: EngineeringSnapshot) -> str:
    if snapshot.loudness_state == LOUDNESS_MEASURED and snapshot.integrated_lufs is not None:
        return f"{snapshot.integrated_lufs:.2f} LUFS"
    if snapshot.loudness_state == LOUDNESS_TOO_SHORT:
        return "not measured · shorter than the 400 ms integrated-loudness window"
    if snapshot.loudness_state == LOUDNESS_SILENT:
        return "silent · no finite integrated loudness"
    if snapshot.loudness_state == LOUDNESS_BOUNDED_OUT:
        return "not measured · exceeds the bounded loudness-analysis sample budget"
    if snapshot.loudness_state == LOUDNESS_UNSUPPORTED_CHANNEL_LAYOUT:
        return "not measured · this loudness slice currently supports mono or stereo"
    if snapshot.loudness_state == LOUDNESS_BACKEND_UNAVAILABLE:
        return "not measured · the standards loudness meter is unavailable"
    if snapshot.loudness_state == LOUDNESS_BACKEND_ERROR:
        return "not measured · the standards loudness meter could not produce a trustworthy result"
    return "not measured · loudness evidence state is unknown"


def _snapshot_metrics(snapshot: EngineeringSnapshot) -> str:
    correlation = ""
    if snapshot.channels == 2:
        correlation_value = (
            "not defined for this signal"
            if snapshot.stereo_correlation is None
            else f"{snapshot.stereo_correlation:+.3f}"
        )
        correlation = (
            '<li><strong>Stereo correlation</strong><br>'
            f'{html.escape(correlation_value)}</li>'
        )
    return (
        '<ul class="stack" aria-label="Engineering measurements">'
        '<li><strong>Format</strong><br>'
        f'{html.escape(_format_rate(snapshot.sample_rate_hz))} · '
        f'{html.escape(_format_channels(snapshot.channels))} · '
        f'{snapshot.bits_per_sample}-bit integer PCM</li>'
        '<li><strong>Duration</strong><br>'
        f'{html.escape(_format_duration(snapshot.duration_seconds))}</li>'
        '<li><strong>Sample peak</strong><br>'
        f'{html.escape(_format_db(snapshot.sample_peak_dbfs))}</li>'
        '<li><strong>RMS</strong><br>'
        f'{html.escape(_format_db(snapshot.rms_dbfs))}</li>'
        '<li><strong>Integrated loudness</strong><br>'
        f'{html.escape(_format_loudness(snapshot))}<br>'
        f'<span class="muted">{html.escape(snapshot.loudness_standard)} programme loudness</span></li>'
        '<li><strong>Crest factor</strong><br>'
        f'{html.escape(_format_crest(snapshot.crest_factor_db))}</li>'
        '<li><strong>DC offset</strong><br>'
        f'{snapshot.dc_offset_percent:.4f}% max absolute channel mean</li>'
        f'{correlation}'
        '</ul>'
    )


def _engineering_asset(shell: ConsumerShell, song_id: str, version_id: str, view) -> str:
    if view.status != "VERIFIED_MANAGED":
        return ""
    try:
        material = shell.runtime.headquarters.materials.resolve_asset(view.asset)
        media = inspect_audition_media(material.path)
    except UnsupportedAuditionMedia:
        return ""
    except SongMaterialError:
        return (
            '<li class="stack"><p class="status caution">Engineering evidence unavailable</p>'
            '<p>N0TE could not re-verify this exact local material, so it showed no signal measurements.</p></li>'
        )

    name = html.escape(view.asset.name)
    if media.content_type != "audio/wav":
        return (
            '<li class="stack">'
            f'<p><strong>{name}</strong></p>'
            '<p class="muted">Engineering Snapshot currently measures verified integer-PCM WAV only. '
            'This local audio can still be auditioned without pretending unsupported measurements exist.</p>'
            '</li>'
        )

    try:
        snapshot = analyze_pcm_wave(
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
            '<p class="muted">This WAV encoding is not yet inside the bounded Engineering Snapshot contract. '
            'N0TE shows no invented substitute measurements.</p>'
            '</li>'
        )
    except AudioEngineeringError:
        return (
            '<li class="stack">'
            f'<p><strong>{name}</strong></p>'
            '<p class="status caution">Engineering evidence unavailable</p>'
            '<p>The exact local bytes changed or could not be read consistently, so N0TE showed no stale measurements.</p>'
            '</li>'
        )

    return (
        '<li class="stack">'
        f'<p><strong>{name}</strong></p>'
        '<p class="status good">Exact local signal evidence</p>'
        f'{_snapshot_metrics(snapshot)}'
        '<p class="muted">Bound to this exact verified current Version material and recomputed from its local bytes. '
        'The binding includes the Song, Version, Asset fingerprint and analyzer version internally; N0TE does not expose those private identifiers here.</p>'
        '</li>'
    )


def _engineering_card(shell: ConsumerShell) -> str:
    store = shell.runtime.headquarters.store
    song = store.active_song()
    if song is None or song.current_version_id is None:
        return ""
    version = store.get_version(song.current_version_id)
    if version is None or version.song_id != song.id:
        raise ConsumerShellError("current Song Version is missing while preparing engineering evidence")

    rows = [
        rendered
        for view in shell.runtime.headquarters.materials.version_materials(version.id)
        if (rendered := _engineering_asset(shell, song.id, version.id, view))
    ]
    if not rows:
        return ""
    return (
        '<div class="card"><h2>Engineering Snapshot</h2>'
        '<p>Read-only signal evidence for the exact current Version you are hearing. '
        'Measurements can help an engineer inspect the signal; they do not decide whether the music is good or finished.</p>'
        '<ul class="stack" aria-label="Current Version engineering evidence">'
        + "".join(rows)
        + '</ul>'
        '<p class="muted"><strong>Measurement boundary:</strong> Sample peak is not true peak. RMS is not LUFS. '
        'Integrated loudness is measured separately as ITU-R BS.1770-4 programme loudness when this bounded mono/stereo contract can support it. '
        'This is not a conformance certification, mastering target, mix score, or artistic recommendation.</p>'
        '</div>'
    )


def install_song_audio_engineering() -> None:
    """Attach the read-only current-Version Engineering Snapshot exactly once."""
    from .mix_relationship_shell import install_song_mix_relationships
    from .performance_timing_shell import install_song_performance_timing

    if getattr(ConsumerShell, "_song_audio_engineering_installed", False):
        install_song_mix_relationships()
        install_song_performance_timing()
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content

    def with_engineering_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        card = _engineering_card(self)
        if not card:
            return rendered
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before Engineering Snapshot could be attached safely"
            )
        return rendered[: -len(marker)] + card + marker

    ConsumerShell._song_content = with_engineering_card
    ConsumerShell._song_audio_engineering_installed = True
    install_song_mix_relationships()
    install_song_performance_timing()
