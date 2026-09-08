from __future__ import annotations

import html
from itertools import combinations
from pathlib import Path
from typing import Callable

from .audio_engineering import (
    AudioEngineeringError,
    EngineeringEvidenceBinding,
    EngineeringSnapshot,
    UnsupportedEngineeringMedia,
    analyze_pcm_wave,
)
from .audition import UnsupportedAuditionMedia, inspect_audition_media
from .consumer_shell import ConsumerShell, ConsumerShellError
from .material import SongMaterialError
from .mix_relationship import (
    SPECTRAL_BACKEND_ERROR,
    SPECTRAL_BACKEND_UNAVAILABLE,
    SPECTRAL_MEASURED,
    SPECTRAL_SILENT,
    SPECTRAL_TOO_SHORT,
    SPECTRAL_UNSUPPORTED_CHANNEL_LAYOUT,
    MixRelationshipError,
    MixRelationshipEvidence,
    analyze_mix_relationship,
)

MAX_RELATIONSHIP_ASSETS = 4


def _format_delta(
    value: float | None,
    *,
    left_name: str,
    right_name: str,
    unit: str,
) -> str:
    if value is None:
        return "not available for this exact pair"
    if abs(value) < 0.005:
        return f"0.00 {unit} · no resolved difference at two decimals"
    higher = left_name if value > 0.0 else right_name
    return f"{abs(value):.2f} {unit} · {higher} higher"


def _format_crest_delta(
    value: float | None,
    *,
    left_name: str,
    right_name: str,
) -> str:
    if value is None:
        return "not available for this exact pair"
    if abs(value) < 0.005:
        return "0.00 dB · no resolved crest-factor difference at two decimals"
    larger = left_name if value > 0.0 else right_name
    return f"{abs(value):.2f} dB · {larger} has the larger crest factor"


def _format_correlation(value: float | None) -> str:
    return "not defined" if value is None else f"{value:+.3f}"


def _spectral_state_text(state: str) -> str:
    if state == SPECTRAL_TOO_SHORT:
        return "too short for the bounded spectral window"
    if state == SPECTRAL_SILENT:
        return "silent in the sampled spectral evidence"
    if state == SPECTRAL_UNSUPPORTED_CHANNEL_LAYOUT:
        return "channel layout is outside this mono/stereo spectral slice"
    if state == SPECTRAL_BACKEND_UNAVAILABLE:
        return "spectral backend is unavailable"
    if state == SPECTRAL_BACKEND_ERROR:
        return "spectral backend could not produce trustworthy evidence"
    return "spectral evidence state is unavailable"


def _format_spectral(
    evidence: MixRelationshipEvidence,
    *,
    left_name: str,
    right_name: str,
) -> str:
    if (
        evidence.left_spectral_state == SPECTRAL_MEASURED
        and evidence.right_spectral_state == SPECTRAL_MEASURED
        and evidence.spectral_overlap_ratio is not None
    ):
        bands = ""
        if evidence.shared_spectral_bands:
            bands = (
                "<br><span class=\"muted\">Bands carrying at least 10% of each sampled "
                "distribution: "
                + html.escape(", ".join(evidence.shared_spectral_bands))
                + "</span>"
            )
        else:
            bands = (
                "<br><span class=\"muted\">No broad band carried at least 10% of both "
                "sampled distributions.</span>"
            )
        return (
            f"{evidence.spectral_overlap_ratio * 100.0:.1f}% sampled broad-band "
            f"energy-distribution overlap{bands}"
        )
    return (
        f"{html.escape(left_name)}: "
        f"{html.escape(_spectral_state_text(evidence.left_spectral_state))} · "
        f"{html.escape(right_name)}: "
        f"{html.escape(_spectral_state_text(evidence.right_spectral_state))}"
    )


def _relationship_pair(
    left_name: str,
    left_snapshot: EngineeringSnapshot,
    left_path: Path,
    right_name: str,
    right_snapshot: EngineeringSnapshot,
    right_path: Path,
) -> str:
    left_safe = html.escape(left_name)
    right_safe = html.escape(right_name)
    try:
        evidence = analyze_mix_relationship(
            left_path,
            right_path,
            left_snapshot=left_snapshot,
            right_snapshot=right_snapshot,
        )
    except (AudioEngineeringError, MixRelationshipError):
        return (
            '<li class="stack">'
            f"<h3>{left_safe} ↔ {right_safe}</h3>"
            '<p class="status caution">Relationship evidence unavailable</p>'
            "<p>The exact local bytes changed or the pair could not be compared "
            "inside this bounded evidence contract, so N0TE showed no stale substitute.</p>"
            "</li>"
        )

    rms = html.escape(
        _format_delta(
            evidence.rms_delta_db,
            left_name=left_name,
            right_name=right_name,
            unit="dB RMS",
        )
    )
    loudness = html.escape(
        _format_delta(
            evidence.integrated_loudness_delta_lu,
            left_name=left_name,
            right_name=right_name,
            unit="LU integrated loudness",
        )
    )
    crest = html.escape(
        _format_crest_delta(
            evidence.crest_factor_delta_db,
            left_name=left_name,
            right_name=right_name,
        )
    )
    left_corr = html.escape(_format_correlation(evidence.left_stereo_correlation))
    right_corr = html.escape(_format_correlation(evidence.right_stereo_correlation))
    spectral = _format_spectral(
        evidence,
        left_name=left_name,
        right_name=right_name,
    )
    return (
        '<li class="stack">'
        f"<h3>{left_safe} ↔ {right_safe}</h3>"
        '<ul class="stack" aria-label="Pair relationship measurements">'
        "<li><strong>Whole-render RMS difference</strong><br>"
        f"{rms}</li>"
        "<li><strong>Integrated loudness difference</strong><br>"
        f"{loudness}</li>"
        "<li><strong>Dynamics contrast</strong><br>"
        f"{crest}</li>"
        "<li><strong>Stereo correlation</strong><br>"
        f"{left_safe}: {left_corr} · {right_safe}: {right_corr}</li>"
        "<li><strong>Spectral overlap</strong><br>"
        f"{spectral}</li>"
        "</ul>"
        "</li>"
    )


def _relationship_assets(shell: ConsumerShell, song_id: str, version_id: str):
    supported: list[tuple[str, str, EngineeringSnapshot, Path]] = []
    for view in shell.runtime.headquarters.materials.version_materials(version_id):
        if view.status != "VERIFIED_MANAGED":
            continue
        try:
            material = shell.runtime.headquarters.materials.resolve_asset(view.asset)
            media = inspect_audition_media(material.path)
        except (UnsupportedAuditionMedia, SongMaterialError):
            continue
        if media.content_type != "audio/wav":
            continue
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
        except (UnsupportedEngineeringMedia, AudioEngineeringError):
            continue
        supported.append(
            (
                view.asset.name,
                view.asset.id,
                snapshot,
                material.path,
            )
        )
    supported.sort(key=lambda item: (item[0].casefold(), item[1]))
    return tuple(supported)


def _mix_relationship_card(shell: ConsumerShell) -> str:
    store = shell.runtime.headquarters.store
    song = store.active_song()
    if song is None or song.current_version_id is None:
        return ""
    version = store.get_version(song.current_version_id)
    if version is None or version.song_id != song.id:
        raise ConsumerShellError(
            "current Song Version is missing while preparing mix relationships"
        )

    supported = _relationship_assets(shell, song.id, version.id)
    if len(supported) < 2:
        return ""

    bounded = supported[:MAX_RELATIONSHIP_ASSETS]
    rows = [
        _relationship_pair(
            left[0],
            left[2],
            left[3],
            right[0],
            right[2],
            right[3],
        )
        for left, right in combinations(bounded, 2)
    ]
    if not rows:
        return ""

    bounded_note = ""
    if len(supported) > MAX_RELATIONSHIP_ASSETS:
        bounded_note = (
            '<p class="muted">This bounded view compares the first four supported '
            "current-Version PCM assets in deterministic name order. Other assets "
            "remain visible in Engineering Snapshot without inventing an unbounded "
            "pair matrix.</p>"
        )
    return (
        '<div class="card"><h2>Mix Relationships</h2>'
        "<p>Read-only relationships among exact verified PCM assets on this current "
        "Version. This is an inspection lens, not a mixer and not an instruction to "
        "change the sound.</p>"
        '<ul class="stack" aria-label="Current Version mix relationships">'
        + "".join(rows)
        + "</ul>"
        + bounded_note
        + '<p class="muted"><strong>Relationship boundary:</strong> Spectral overlap '
        "is a sampled broad-band energy-distribution cue, not proof of audible "
        "masking. Stereo correlation is not a width-quality score. Level and crest "
        "differences are not gain, compression, pan, EQ, mastering, or artistic "
        "recommendations. Arrangement intent, audibility, temporal interaction, "
        "monitoring context and the artist's ear still decide what matters.</p>"
        "</div>"
    )


def install_song_mix_relationships() -> None:
    """Attach the read-only current-Version Mix Relationships card exactly once."""
    if getattr(ConsumerShell, "_song_mix_relationship_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content

    def with_mix_relationship_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        card = _mix_relationship_card(self)
        if not card:
            return rendered
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before Mix Relationships could be attached safely"
            )
        return rendered[: -len(marker)] + card + marker

    ConsumerShell._song_content = with_mix_relationship_card
    ConsumerShell._song_mix_relationship_installed = True
