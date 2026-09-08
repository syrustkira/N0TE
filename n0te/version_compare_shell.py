from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError, _MediaGrant, _PageState
from .lineage import ValidationError
from .version_compare import (
    VersionCompareError,
    VersionCompareService,
    VersionCompareSide,
    VersionComparison,
)
from .version_compare_decision import (
    StaleVersionCompareDecisionError,
    VersionCompareDecisionBinding,
    VersionCompareDecisionError,
    VersionCompareDecisionMemory,
)

_DECISION_LABELS = {
    "KEEP": "Keep working from Current",
    "REVERT": "Reference is the direction to return to",
    "REVISE": "Current needs another revision",
    "INCONCLUSIVE": "Not enough to decide",
}


def _service(shell: ConsumerShell) -> VersionCompareService:
    hq = shell.runtime.headquarters
    return VersionCompareService(hq.store, hq.materials)


def _format_dbfs(value: float | None) -> str:
    return "silent / unavailable" if value is None else f"{value:.2f} dBFS"


def _format_lufs(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.2f} LUFS"


def _loudness_state_label(side: VersionCompareSide) -> str:
    if side.loudness_measured:
        return "Measured"
    state = side.loudness_state
    if state is None:
        return "Unavailable for this media"
    labels = {
        "TOO_SHORT": "Unavailable: render is too short for the bounded integrated-loudness measurement",
        "SILENT": "Unavailable: render is silent",
        "BOUNDED_OUT": "Unavailable: render exceeds the bounded loudness-analysis size",
        "UNSUPPORTED_CHANNEL_LAYOUT": "Unavailable: channel layout is outside the bounded loudness contract",
        "BACKEND_UNAVAILABLE": "Unavailable: loudness measurement backend is not available",
        "BACKEND_ERROR": "Unavailable: loudness measurement could not be completed safely",
    }
    return labels.get(state, f"Unavailable: {state}")


def _side_markup(shell: ConsumerShell, comparison: VersionComparison, side: VersionCompareSide, role: str) -> str:
    status = {
        "AUDITIONABLE_MEASURED": '<p class="status good">Verified local audio · exact PCM engineering evidence</p>',
        "AUDITIONABLE_UNMEASURED": '<p class="status good">Verified local audio · engineering evidence unavailable</p>',
        "NO_AUDITIONABLE_AUDIO": '<p class="status caution">No supported local audio for this Version</p>',
        "INTEGRITY_BLOCKED": '<p class="status caution">Local material could not be re-verified safely</p>',
    }[side.status]
    audition = ""
    if side.auditionable:
        assert side.asset_id is not None and side.asset_sha256 is not None
        token = shell._new_media_grant(
            _MediaGrant(
                song_id=comparison.song_id,
                version_id=side.version_id,
                asset_id=side.asset_id,
                sha256=side.asset_sha256,
            )
        )
        src = html.escape(f"/media/song-version/{token}", quote=True)
        aria = html.escape(f"Audition Version {side.ordinal}: {side.label}", quote=True)
        audition = (
            f'<audio controls preload="metadata" src="{src}" aria-label="{aria}">'
            'Your browser cannot play this local audio Version.'
            '</audio>'
        )
    evidence = ""
    if side.measured:
        loudness = _loudness_state_label(side)
        standard = "" if side.loudness_standard is None else f" · {side.loudness_standard}"
        evidence = (
            '<dl>'
            '<dt>Integrated programme loudness</dt>'
            f'<dd>{html.escape(_format_lufs(side.integrated_lufs))}</dd>'
            '<dt>Loudness measurement state</dt>'
            f'<dd>{html.escape(loudness + standard)}</dd>'
            '<dt>Whole-render RMS</dt>'
            f'<dd>{html.escape(_format_dbfs(side.rms_dbfs))}</dd>'
            '<dt>Sample peak</dt>'
            f'<dd>{html.escape(_format_dbfs(side.sample_peak_dbfs))}</dd>'
            '</dl>'
            '<p class="muted">Observed from this exact verified integer PCM WAV. Integrated loudness is standards-based when measured. RMS is not LUFS and remains secondary signal-level evidence.</p>'
        )
    elif side.auditionable:
        evidence = (
            '<p class="muted">You can audition this exact verified local file, but N0TE has no supported exact PCM engineering measurement for it.</p>'
        )
    return (
        '<div class="card stack">'
        f'<p class="eyebrow">{html.escape(role)}</p>'
        f'<h2>Version {side.ordinal}: {html.escape(side.label)}</h2>'
        f'{status}{audition}{evidence}'
        '</div>'
    )


def _rms_fallback_statement(comparison: VersionComparison) -> str:
    delta = comparison.rms_delta_db
    if delta is None:
        return ""
    if abs(delta) < 0.05:
        return "The two measured whole-render RMS levels are within 0.05 dB."
    if delta > 0:
        return f"The Current Version whole-render RMS is {abs(delta):.2f} dB higher than the Reference Version."
    return f"The Reference Version whole-render RMS is {abs(delta):.2f} dB higher than the Current Version."


def _level_markup(comparison: VersionComparison) -> str:
    loudness_delta = comparison.integrated_loudness_delta_lu
    if loudness_delta is not None:
        if abs(loudness_delta) < 0.05:
            statement = "The two measured integrated loudness values are within 0.05 LU."
        elif loudness_delta > 0:
            statement = f"The Current Version integrated loudness is {abs(loudness_delta):.2f} LU higher than the Reference Version."
        else:
            statement = f"The Reference Version integrated loudness is {abs(loudness_delta):.2f} LU higher than the Current Version."
        rms = _rms_fallback_statement(comparison)
        secondary = "" if not rms else f'<p class="muted">Secondary RMS context: {html.escape(rms)} RMS is not LUFS.</p>'
        return (
            f'<p class="status caution">{html.escape(statement)}</p>'
            '<p>That measured programme-loudness difference can bias an A/B toward the louder render. N0TE has applied no gain or normalization. Level-match deliberately before deciding.</p>'
            f'{secondary}'
        )

    rms = _rms_fallback_statement(comparison)
    if rms:
        return (
            '<p class="status caution">Comparable integrated programme loudness is unavailable for this exact pair.</p>'
            f'<p>{html.escape(rms)} Use that only as a fallback signal-level cue. RMS is not LUFS or perceptual loudness, and N0TE will not invent a loudness match.</p>'
        )
    return (
        '<p class="status caution">No trustworthy pair-level difference is available for this exact pair.</p>'
        '<p>Match playback or monitor level by ear before judging. N0TE will not invent integrated loudness, RMS, or a loudness match.</p>'
    )


def _decision_binding(comparison: VersionComparison) -> VersionCompareDecisionBinding:
    if comparison.reference is None or comparison.current is None:
        raise VersionCompareDecisionError("an exact two-Version comparison is required")
    return VersionCompareDecisionBinding(
        song_id=comparison.song_id,
        reference_version_id=comparison.reference.version_id,
        current_version_id=comparison.current.version_id,
    )


def _decision_action(shell: ConsumerShell, binding: VersionCompareDecisionBinding) -> str:
    return shell._new_action(
        "version-compare-decide",
        json.dumps(
            [binding.song_id, binding.reference_version_id, binding.current_version_id],
            separators=(",", ":"),
        ),
    )


def _decode_decision_binding(value: str) -> VersionCompareDecisionBinding:
    try:
        decoded = json.loads(value)
        if (
            not isinstance(decoded, list)
            or len(decoded) != 3
            or not all(isinstance(item, str) and item for item in decoded)
        ):
            raise ValueError
        return VersionCompareDecisionBinding(decoded[0], decoded[1], decoded[2])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StaleVersionCompareDecisionError(
            "That A/B decision action is no longer valid. Reload the comparison."
        ) from exc


def _decision_markup(shell: ConsumerShell, comparison: VersionComparison) -> str:
    binding = _decision_binding(comparison)
    memory = VersionCompareDecisionMemory(shell.runtime.headquarters.store, create=False)
    latest = memory.latest_for_pair(
        binding.song_id,
        binding.reference_version_id,
        binding.current_version_id,
    )
    prior = ""
    if latest is not None:
        label = _DECISION_LABELS.get(latest.decision, latest.decision.title())
        rationale = (
            ""
            if latest.rationale is None
            else f'<p><strong>Your note:</strong> {html.escape(latest.rationale)}</p>'
        )
        prior = (
            '<div class="stack">'
            f'<p class="status good">Latest judgment for this exact pair: {html.escape(label)}</p>'
            f'{rationale}'
            '<p class="muted">That record is memory of your judgment only. It did not change Current, Approved, audio, Learning, a provider, or a DAW.</p>'
            '</div>'
        )

    if not comparison.both_auditionable:
        controls = (
            '<p class="status caution">Decision recording is unavailable while both exact Versions cannot be auditioned safely.</p>'
            '<p>Restore a trustworthy two-sided comparison first. N0TE will not turn partial evidence into an artistic decision.</p>'
        )
    else:
        token = _decision_action(shell, binding)
        buttons = "".join(
            f'<button type="submit" name="decision" value="{kind}">{html.escape(label)}</button>'
            for kind, label in _DECISION_LABELS.items()
        )
        controls = (
            '<form class="stack" method="post" action="/compare/decide" aria-label="Record artist judgment for this exact Version pair">'
            f'{shell._hidden(token)}'
            '<div><label>Why? <span class="muted">Optional, but useful to future you.</span>'
            '<textarea name="rationale" maxlength="1200" rows="3"></textarea></label></div>'
            f'<div class="row">{buttons}</div>'
            '</form>'
            '<p class="muted"><strong>Decision only.</strong> KEEP does not approve. REVERT does not resume the Reference. REVISE does not create a Version. INCONCLUSIVE stays uncertain.</p>'
        )

    return (
        '<div class="card stack"><h2>Your decision</h2>'
        '<p>After you listen, record what you decide about this exact Reference/Current pair. This is artist judgment, not execution authority.</p>'
        f'{prior}{controls}'
        '<a class="button" href="/song">Back to Song history</a></div>'
    )


def _compare_content(shell: ConsumerShell) -> str:
    comparison = _service(shell).prepare()
    if comparison.status == "NO_CURRENT_VERSION":
        return (
            '<section class="grid"><div class="card"><h2>No current Version to compare</h2>'
            '<p>Add real Song material first. Comparison is read-only and does not create placeholder Versions.</p>'
            '<a class="button primary" href="/song">Return to Song</a></div></section>'
        )
    assert comparison.current is not None
    if comparison.status == "NO_REFERENCE_VERSION":
        return (
            '<section class="grid">'
            f'{_side_markup(shell, comparison, comparison.current, "Current")}'
            '<div class="card"><h2>A second Version is needed</h2>'
            '<p>Preserve another Version of this Song before A/B. N0TE will not manufacture a fake alternate merely to fill the comparison.</p>'
            '<a class="button primary" href="/song">Return to Song</a></div></section>'
        )
    assert comparison.reference is not None
    limitations = "".join(f'<li>{html.escape(item)}</li>' for item in comparison.limitations)
    readiness = (
        '<p class="status good">Both exact Versions are locally auditionable</p>'
        if comparison.both_auditionable
        else '<p class="status caution">This pair is only partially auditionable</p>'
    )
    return (
        '<section class="grid" aria-label="Version A/B comparison">'
        '<div class="card stack"><h2>Before you judge</h2>'
        f'{readiness}{_level_markup(comparison)}'
        '<p><strong>Nothing has been chosen.</strong> Listening here does not approve, reject, revert, branch, learn a preference, or change the current Version.</p>'
        f'<details><summary>Evidence boundary</summary><ul class="stack">{limitations}</ul></details>'
        '</div>'
        f'{_side_markup(shell, comparison, comparison.reference, "Reference")}'
        f'{_side_markup(shell, comparison, comparison.current, "Current")}'
        '<div class="card"><h2>Artist decision remains next</h2>'
        '<p>Use this surface to hear the exact pair and reduce obvious level bias. KEEP, REVERT, REVISE, or INCONCLUSIVE remains a separate explicit decision step.</p></div>'
        f'{_decision_markup(shell, comparison)}'
        '</section>'
    )


def _compare_song_card(shell: ConsumerShell) -> str:
    if shell.runtime.state != "RUNNING":
        return ""
    song = shell.runtime.headquarters.store.active_song()
    if song is None or song.current_version_id is None:
        return ""
    versions = shell.runtime.headquarters.store.versions_for_song(song.id)
    if len(versions) < 2:
        return ""
    return (
        '<div class="card"><h2>Compare Versions</h2>'
        '<p>Hear the current Version beside one exact lineage neighbor. When both exact PCM WAVs support it, N0TE shows standards-based integrated loudness difference to flag possible level bias; RMS remains secondary fallback context.</p>'
        '<a class="button primary" href="/compare">Open A/B compare</a>'
        '<p class="muted">Listening is read-only. An explicit judgment on the compare page records only your decision; no gain, approval, Learning promotion, provider call, or DAW change is applied.</p>'
        '</div>'
    )


def _compare_state(shell: ConsumerShell) -> _PageState:
    artist = shell.runtime.headquarters.store.artist()
    song = shell.runtime.headquarters.store.active_song()
    if song is None:
        return _PageState(
            "running-compare",
            "Compare Versions",
            "Hear · Compare · Decide",
            "Start a Song before comparing exact Versions.",
            artist_name=artist.display_name,
        )
    return _PageState(
        "running-compare",
        "Compare Versions",
        "Hear · Compare · Decide",
        "A/B exact local Song Versions, then record your judgment without confusing decision with execution.",
        artist_name=artist.display_name,
        song_title=song.title,
    )


def _post_compare_decision(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "version-compare-decide")
    if action is None or action.value is None:
        raise StaleVersionCompareDecisionError(
            "That A/B decision was already handled or expired. Reload the comparison."
        )
    binding = _decode_decision_binding(action.value)
    comparison = _service(shell).prepare()
    if (
        comparison.reference is None
        or comparison.current is None
        or not comparison.both_auditionable
        or comparison.song_id != binding.song_id
        or comparison.reference.version_id != binding.reference_version_id
        or comparison.current.version_id != binding.current_version_id
    ):
        raise StaleVersionCompareDecisionError(
            "The exact A/B pair changed or is no longer safely auditionable. Reload before deciding."
        )
    memory = VersionCompareDecisionMemory(shell.runtime.headquarters.store, create=True)
    result = memory.record(
        binding,
        decision=form.get("decision", ""),
        rationale=form.get("rationale"),
    )
    label = _DECISION_LABELS.get(result.decision, result.decision.title())
    shell._consumer_notice = (
        f"A/B decision recorded: {label}. No Version, approval, Learning result, provider, DAW, or audio state was changed."
    )
    shell._redirect(handler, "/compare")


def install_song_version_compare() -> None:
    """Attach exact Version A/B audition and explicit pair-bound artist judgment once."""
    if getattr(ConsumerShell, "_song_version_compare_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_state_content: Callable[[ConsumerShell, object], str] = ConsumerShell._state_content
    original_get: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_get
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_compare_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError("Song page structure changed before Version compare could attach safely")
        return rendered[: -len(marker)] + _compare_song_card(self) + marker

    def with_compare_content(self: ConsumerShell, state) -> str:
        if state.kind != "running-compare":
            return original_state_content(self, state)
        if state.song_title is None:
            return (
                '<section class="grid"><div class="card"><h2>No active Song</h2>'
                '<p>Start or select a Song before comparing Versions.</p>'
                '<a class="button primary" href="/song">Go to Song</a></div></section>'
            )
        return _compare_content(self)

    def with_compare_get(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        if self._path(handler) != "/compare":
            original_get(self, handler)
            return
        if not self._request_host_is_exact(handler):
            self._send_html(
                handler,
                421,
                self._simple_error("This N0TE window is available only from its exact local address."),
            )
            return
        try:
            if self.runtime.state != "RUNNING":
                state = self._ensure_runtime()
                if state is not None:
                    self._send_html(handler, 200, self._render_state(state, path="/"))
                    return
            self._send_html(handler, 200, self._render_state(_compare_state(self), path="/song"))
        except (VersionCompareError, VersionCompareDecisionError, ConsumerShellError):
            self._send_html(
                handler,
                409,
                self._simple_error("The Song Version state changed while A/B was being prepared. Reload the Song and try again."),
            )
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error("N0TE stopped the comparison before uncertain media, evidence, or decision memory could be presented as exact."),
            )

    def with_compare_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        if self._path(handler) != "/compare/decide":
            original_post(self, handler)
            return
        if not self._request_host_is_exact(handler) or not self._post_origin_is_allowed(handler):
            self._send_html(
                handler,
                403,
                self._simple_error("That A/B decision did not come from this N0TE window."),
            )
            return
        form = self._read_form(handler)
        if form is None or not self._form_authorized(form):
            self._send_html(
                handler,
                403,
                self._simple_error("That A/B decision expired. Reload the comparison and try again."),
            )
            return
        try:
            _post_compare_decision(self, handler, form)
        except StaleVersionCompareDecisionError as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
        except (ValidationError, VersionCompareDecisionError, VersionCompareError) as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
        except ConsumerShellError as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error("N0TE stopped that A/B decision before it could become unclear or over-authorized state."),
            )

    ConsumerShell._song_content = with_compare_card
    ConsumerShell._state_content = with_compare_content
    ConsumerShell._handle_get = with_compare_get
    ConsumerShell._handle_post = with_compare_post
    ConsumerShell._song_version_compare_installed = True
