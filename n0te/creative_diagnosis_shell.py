from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .belief_sources import present_belief_source
from .consumer_shell import ConsumerShell, ConsumerShellError
from .creative_diagnosis import (
    CreativeDiagnosis,
    CreativeDiagnosisError,
    CreativeDiagnosisService,
    DiagnosisFact,
    DiagnosisHypothesis,
    InterventionPath,
)
from .creative_suggestions import CREATIVE_DIMENSIONS
from .lineage import ValidationError


_MEASURED_DIAGNOSIS_FACT_LABELS = frozenset(
    {"Sample peak", "RMS", "Crest factor", "Stereo correlation"}
)


def _service(shell: ConsumerShell) -> CreativeDiagnosisService:
    hq = shell.runtime.headquarters
    return CreativeDiagnosisService(hq.store, hq.sessions, hq.materials)


def _diagnosis_action(shell: ConsumerShell, song_id: str) -> str:
    return shell._new_action("song-diagnose", song_id)


def _locked_dimensions(form: Mapping[str, str]) -> tuple[str, ...]:
    locks = []
    for dimension in CREATIVE_DIMENSIONS:
        key = "diagnosis_lock_" + dimension.lower()
        value = form.get(key)
        if value is None:
            continue
        if value != "1":
            raise ValidationError(f"invalid diagnosis lock value for {dimension}")
        locks.append(dimension)
    return tuple(locks)


def _fact_source_kind(fact: DiagnosisFact) -> str:
    if fact.truth_kind == "OBSERVED" and fact.label in _MEASURED_DIAGNOSIS_FACT_LABELS:
        return "MEASURED"
    return fact.truth_kind


def _fact_markup(fact: DiagnosisFact) -> str:
    source = present_belief_source(_fact_source_kind(fact))
    css = "status" if source.source_kind == "USER_DECLARED" else "status good"
    return (
        '<li class="stack">'
        f'<p class="{css}">{html.escape(source.label)}</p>'
        f'<p><strong>{html.escape(fact.label)}</strong><br>{html.escape(fact.value)}</p>'
        f'<p class="muted">{html.escape(source.explanation)}</p>'
        f'<p class="muted">Scope: {html.escape(fact.scope)}</p>'
        '</li>'
    )


def _hypothesis_markup(hypothesis: DiagnosisHypothesis) -> str:
    source = present_belief_source("INFERRED")
    return (
        '<li class="stack">'
        f'<p class="status caution">{html.escape(source.label)}</p>'
        f'<p><strong>{html.escape(hypothesis.label)} · {html.escape(hypothesis.test_dimension.title())}</strong></p>'
        f'<p>{html.escape(hypothesis.statement)}</p>'
        f'<p class="muted">{html.escape(source.explanation)}</p>'
        '<p class="muted">Hypothesis, not observation.</p>'
        '</li>'
    )


def _intervention_markup(index: int, path: InterventionPath) -> str:
    steps = "".join(f'<li>{html.escape(step)}</li>' for step in path.steps)
    preserves = ""
    if path.preserves:
        preserves = (
            '<p class="status good">Preserve: '
            + html.escape(", ".join(item.title() for item in path.preserves))
            + '</p>'
        )
    return (
        '<li class="stack">'
        f'<p class="status good">Path {index} · {html.escape(path.dimension.title())}</p>'
        f'<p><strong>{html.escape(path.title)}</strong></p>'
        f'<p>{html.escape(path.rationale)}</p>'
        f'{preserves}'
        f'<ol class="stack">{steps}</ol>'
        '</li>'
    )


def _belief_sources_markup(result: CreativeDiagnosis) -> str:
    kinds: list[str] = []
    for fact in result.facts:
        kind = _fact_source_kind(fact)
        if kind not in kinds:
            kinds.append(kind)
    if result.hypotheses and "INFERRED" not in kinds:
        kinds.append("INFERRED")
    rows = []
    for kind in kinds:
        source = present_belief_source(kind)
        rows.append(
            '<li class="stack">'
            f'<p><strong>{html.escape(source.label)}</strong></p>'
            f'<p class="muted">{html.escape(source.explanation)}</p>'
            '</li>'
        )
    return (
        '<details><summary>Why does N0TE think that?</summary>'
        '<p class="muted">These labels describe where each belief came from. They do not upgrade its authority or certainty.</p>'
        f'<ul class="stack">{"".join(rows)}</ul>'
        '</details>'
    )


def _diagnosis_markup(result: CreativeDiagnosis | None) -> str:
    if result is None:
        return ""
    facts = "".join(_fact_markup(fact) for fact in result.facts)
    hypotheses = "".join(_hypothesis_markup(item) for item in result.hypotheses)
    interventions = "".join(
        _intervention_markup(index, path)
        for index, path in enumerate(result.interventions, start=1)
    )
    limitations = "".join(f'<li>{html.escape(item)}</li>' for item in result.limitations)
    evidence_label = {
        "OBSERVED_PCM": "Verified current-render signal evidence available",
        "NO_CURRENT_VERSION": "No current Song Version to measure",
        "NO_SUPPORTED_AUDIO": "No supported exact PCM measurement available",
        "INTEGRITY_BLOCKED": "Current material could not be re-verified safely",
    }[result.evidence_status]
    return (
        '<div class="stack" aria-live="polite">'
        '<h3>What I know</h3>'
        f'<p class="muted">{html.escape(evidence_label)}</p>'
        f'<ul class="stack">{facts}</ul>'
        '<h3>What I’m inferring</h3>'
        f'<ul class="stack">{hypotheses}</ul>'
        f'{_belief_sources_markup(result)}'
        '<h3>Two ways to test it</h3>'
        f'<ol class="stack">{interventions}</ol>'
        '<p class="status caution"><strong>Nothing changed yet.</strong> These are two bounded tests to compare, not edits that N0TE already made.</p>'
        '<details><summary>Evidence boundary</summary>'
        f'<ul class="stack">{limitations}</ul>'
        '</details>'
        '</div>'
    )


def _diagnosis_card(shell: ConsumerShell) -> str:
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("Song diagnosis requires an open Artist workspace")
    hq = shell.runtime.headquarters
    song = hq.store.active_song()
    if song is None:
        return ""
    result = getattr(shell, "_creative_diagnosis_result", None)
    if result is not None and not isinstance(result, CreativeDiagnosis):
        raise ConsumerShellError("creative diagnosis shell state is invalid")
    if result is not None and not _service(shell).is_current(result):
        shell._creative_diagnosis_result = None
        result = None

    latest = hq.sessions.latest_for_song(song.id)
    objective = "" if latest is None else latest.objective
    placeholder = (
        "What are you trying to solve in this Song?"
        if not objective
        else f"Current Session objective: {objective}"
    )
    locks = "".join(
        '<label><input type="checkbox" name="diagnosis_lock_'
        + dimension.lower()
        + '" value="1"> Keep '
        + html.escape(dimension.title())
        + " unchanged</label>"
        for dimension in CREATIVE_DIMENSIONS
    )
    return (
        '<div class="card"><h2>Diagnose a Song problem</h2>'
        '<p>Tell N0TE what feels wrong or what you want to improve. N0TE will keep your statement separate from what it can actually observe, then give you two materially different ways to test the problem.</p>'
        '<form class="stack" method="post" action="/diagnosis/create" aria-label="Diagnose a Song problem">'
        f'{shell._hidden(_diagnosis_action(shell, song.id))}'
        '<div><label>Problem to test'
        f'<textarea name="problem" maxlength="800" rows="4" placeholder="{html.escape(placeholder, quote=True)}"></textarea>'
        '</label></div>'
        '<fieldset><legend>Keep these dimensions unchanged (optional)</legend>'
        f'<div class="stack">{locks}</div></fieldset>'
        '<button class="primary" type="submit">Give me two ways to test it</button>'
        '<p class="muted">Leaving the problem blank uses the latest Song work objective when one exists. This step is read-only: no provider call, DAW edit, Song Version, preference or Learning result is created.</p>'
        '</form>'
        f'{_diagnosis_markup(result)}'
        '</div>'
    )


def _post_diagnosis(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "song-diagnose")
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That diagnosis action was already handled or expired."),
        )
        return
    song = shell.runtime.headquarters.store.active_song()
    if song is None or song.id != action.value:
        shell._send_html(
            handler,
            409,
            shell._simple_error("The active Song changed. Reload the Song before diagnosing it."),
        )
        return
    result = _service(shell).diagnose(
        problem=form.get("problem", ""),
        locked_dimensions=_locked_dimensions(form),
    )
    shell._creative_diagnosis_result = result
    shell._consumer_notice = "Two bounded diagnosis paths prepared from the current Song context. Nothing was changed."
    shell._redirect(handler, "/song")


def install_song_creative_diagnosis() -> None:
    """Attach the evidence-grounded Song diagnosis bridge exactly once."""
    if getattr(ConsumerShell, "_song_creative_diagnosis_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_diagnosis_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before creative diagnosis could attach safely"
            )
        return rendered[: -len(marker)] + _diagnosis_card(self) + marker

    def with_diagnosis_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        if self._path(handler) != "/diagnosis/create":
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
            _post_diagnosis(self, handler, form)
        except (ValidationError, CreativeDiagnosisError, ConsumerShellError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/song")
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that diagnosis before uncertain state could be presented as creative truth."
                ),
            )

    ConsumerShell._song_content = with_diagnosis_card
    ConsumerShell._handle_post = with_diagnosis_post
    ConsumerShell._song_creative_diagnosis_installed = True
