from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError
from .creative_suggestions import (
    CREATIVE_DIMENSIONS,
    SUGGESTION_DISTANCES,
    CreativeSuggestion,
    CreativeSuggestionError,
    CreativeSuggestionService,
)
from .host_runtime import ActiveToolProjection, ActiveToolProjectionError, project_active_tools
from .lineage import ValidationError
from .suggestion_deferral import (
    LATER_THIS_SONG,
    NEVER_SUGGEST_AGAIN,
    NEXT_SONG,
)

_DISTANCE_LABELS = {
    "FAMILIAR": "Familiar · small move",
    "ADJACENT": "Adjacent · change one dimension",
    "WILDCARD": "Wildcard · deliberate contrast",
}
_DEFERRAL_CHOICES = {
    LATER_THIS_SONG: (
        "Not now · later this Song",
        "Defer this suggestion until a later work Session in this Song",
        "N0TE will remember this through relaunch for the current work Session. A later Session in this Song can surface the idea again.",
    ),
    NEXT_SONG: (
        "Save for next Song",
        "Defer this suggestion until the Artist moves to another Song",
        "N0TE will hold this exact idea pattern until you move to another Song. Once that horizon is crossed, the pattern can become eligible again. This does not create a broader taste rule.",
    ),
    NEVER_SUGGEST_AGAIN: (
        "Never suggest this again",
        "Suppress this exact suggestion pattern across this Artist profile",
        "N0TE will suppress this exact idea pattern across your Artist profile. This is an explicit choice, not an inferred dislike of a broader style or technique.",
    ),
}
_DEFERRAL_NOTICES = {
    LATER_THIS_SONG: "Not now remembered for this Song work Session. A later Session can surface that idea again.",
    NEXT_SONG: "Saved until the next Song. This exact idea pattern is held until you move to another Song.",
    NEVER_SUGGEST_AGAIN: "Never suggest again remembered for this Artist. This exact idea pattern is now suppressed across Songs.",
}


def _service(shell: ConsumerShell) -> CreativeSuggestionService:
    hq = shell.runtime.headquarters
    return CreativeSuggestionService(hq.store, hq.sessions)


def _suggestion_action(shell: ConsumerShell, song_id: str) -> str:
    return shell._new_action("song-suggest", song_id)


def _result_binding(result: CreativeSuggestion) -> str:
    return "|".join((result.song_id, result.session_id or "", result.semantic_key))


def _deferral_binding(scope: str, result: CreativeSuggestion) -> str:
    return scope + "|" + _result_binding(result)


def _not_now_action(
    shell: ConsumerShell, result: CreativeSuggestion, scope: str
) -> str:
    return shell._new_action("suggestion-not-now", _deferral_binding(scope, result))


def _deferral_forms(shell: ConsumerShell, result: CreativeSuggestion) -> str:
    scopes = [NEXT_SONG, NEVER_SUGGEST_AGAIN]
    if result.session_id is not None:
        scopes.insert(0, LATER_THIS_SONG)
    forms = []
    for scope in scopes:
        label, aria_label, help_text = _DEFERRAL_CHOICES[scope]
        forms.append(
            '<form class="stack" method="post" action="/suggestion/not-now" '
            f'aria-label="{html.escape(aria_label)}">'
            + shell._hidden(_not_now_action(shell, result, scope))
            + f'<button type="submit">{html.escape(label)}</button>'
            + f'<p class="muted">{html.escape(help_text)}</p>'
            + "</form>"
        )
    return (
        '<fieldset class="stack"><legend>Not now</legend>'
        '<p class="muted">Choose the horizon you actually mean. These choices change suggestion visibility only and grant no action authority.</p>'
        + "".join(forms)
        + "</fieldset>"
    )


_MAX_ACTIVE_TOOL_CONTEXT = 12
_MAX_SEMANTIC_TOOL_CONTEXT = 8
_MAX_SEMANTIC_CAPABILITY_CONTEXT = 6
_MAX_TOOL_CONTEXT_TEXT = 160
_HOST_LABELS = {
    "ABLETON_LIVE": "Ableton Live",
    "FL_STUDIO": "FL Studio",
    "LOGIC_PRO": "Logic Pro",
    "REAPER": "REAPER",
    "STUDIO_ONE": "Studio One",
    "PRO_TOOLS": "Pro Tools",
}


def _bounded_context_text(value: str, *, maximum: int = _MAX_TOOL_CONTEXT_TEXT) -> str:
    text = " ".join(str(value).split())
    if len(text) <= maximum:
        return text
    return text[: maximum - 1].rstrip() + "…"


def _latest_active_tool_projection(shell: ConsumerShell) -> ActiveToolProjection | None:
    hq = shell.runtime.headquarters
    song = hq.store.active_song()
    if song is None:
        return None
    latest_workspace_id = None
    for event in reversed(hq.activity.for_song(song.id)):
        if event.object_type == "WORKSPACE":
            latest_workspace_id = event.object_id
            break
    if latest_workspace_id is None:
        return None
    try:
        projection = project_active_tools(
            hq.workspaces,
            hq.shadow,
            latest_workspace_id,
        )
    except ActiveToolProjectionError:
        return None
    if projection.song_id != song.id:
        raise ConsumerShellError(
            "active DAW tool context crossed the current Song boundary"
        )
    return projection


def _active_tool_context_markup(shell: ConsumerShell, dimension: str) -> str:
    projection = _latest_active_tool_projection(shell)
    if projection is None:
        return ""

    host = _HOST_LABELS.get(
        projection.host_family,
        projection.host_family.replace("_", " ").title(),
    )
    rows: list[str] = []
    observed_names: list[str] = []
    total_tools = sum(len(scope.tools) for scope in projection.scopes)
    remaining = _MAX_ACTIVE_TOOL_CONTEXT

    for scope in projection.scopes:
        if remaining <= 0:
            break
        scope_name = scope.parent_name or scope.parent_ref
        coverage = "complete chain" if scope.coverage == "COMPLETE" else "observed tools"
        tools = scope.tools[:remaining]
        remaining -= len(tools)
        if not tools:
            rows.append(
                "<li><strong>"
                + html.escape(scope_name)
                + "</strong> · "
                + html.escape(coverage)
                + " · no active device/plugin observed</li>"
            )
            continue

        labels: list[str] = []
        for tool in tools:
            observed_names.append(tool.name)
            position = ""
            if tool.position is not None:
                position_label = (
                    "slot" if tool.position_kind == "SLOT" else "position"
                )
                position = f" ({position_label} {tool.position + 1})"
            labels.append(html.escape(tool.name + position))
        rows.append(
            "<li><strong>"
            + html.escape(scope_name)
            + "</strong> · "
            + html.escape(coverage)
            + ": "
            + ", ".join(labels)
            + "</li>"
        )

    omitted = max(0, total_tools - _MAX_ACTIVE_TOOL_CONTEXT)
    omitted_note = (
        ""
        if omitted == 0
        else f'<p class="muted">+{omitted} more active device(s) omitted from this bounded view.</p>'
    )
    context_nudge = ""
    if dimension in {"SOUND", "DYNAMICS"} and observed_names:
        context_nudge = (
            '<p class="status good">DAW-aware option: before adding another device, '
            "check whether one of the already-active tools can perform this experiment.</p>"
        )

    scope_markup = (
        '<p class="muted">No device/plugin chain was observed in the latest DAW workspace.</p>'
        if not projection.scopes
        else "<ul>" + "".join(rows) + "</ul>"
    )
    return (
        '<div class="stack"><h4>Latest observed DAW tools</h4>'
        f'<p class="muted">{html.escape(host)} · verified current Host Shadow</p>'
        f"{context_nudge}{scope_markup}{omitted_note}"
        '<p class="muted">DAW observation only. Active presence does not prove installed '
        "inventory, ownership, exact product identity, or controllable parameters.</p>"
        "</div>"
    )


def _semantic_tool_context_markup(shell: ConsumerShell) -> str:
    profiles = shell.runtime.headquarters.tool_inventory.active_profiles()
    if not profiles:
        return ""

    rows: list[str] = []
    for profile in profiles[:_MAX_SEMANTIC_TOOL_CONTEXT]:
        formats = sorted({endpoint.format_kind for endpoint in profile.endpoints})
        capabilities = sorted(
            {binding.candidate.capability for binding in profile.capabilities}
        )
        visible_capabilities = capabilities[:_MAX_SEMANTIC_CAPABILITY_CONTEXT]
        details = [" / ".join(formats)]
        if visible_capabilities:
            details.append(
                ", ".join(
                    _bounded_context_text(capability)
                    for capability in visible_capabilities
                )
            )
        else:
            details.append("no declared capability bindings")
        omitted_capabilities = len(capabilities) - len(visible_capabilities)
        if omitted_capabilities:
            details.append(f"+{omitted_capabilities} capability binding(s)")
        rows.append(
            "<li><strong>"
            + html.escape(_bounded_context_text(profile.display_name))
            + "</strong> · "
            + html.escape(" · ".join(details))
            + "</li>"
        )

    omitted_tools = len(profiles) - len(rows)
    omitted_note = (
        ""
        if omitted_tools == 0
        else f'<p class="muted">+{omitted_tools} more declared Tool(s) omitted from this bounded view.</p>'
    )
    return (
        '<div class="stack"><h4>Explicit Tool inventory</h4>'
        '<p class="muted">Persistent semantic Tool identities recorded through explicit Artist declaration, import, or correction.</p>'
        "<ul>"
        + "".join(rows)
        + "</ul>"
        + omitted_note
        + '<p class="muted">Inventory presence does not prove that a Tool is loaded in the current DAW, currently licensed, or safe to control. N0TE has not matched these entries to the observed device chain.</p>'
        + "</div>"
    )


def _result_markup(shell: ConsumerShell, result: CreativeSuggestion | None) -> str:
    if result is None:
        return ""
    objective = (
        ""
        if result.session_objective is None
        else '<p class="muted">Current work objective: '
        + html.escape(result.session_objective)
        + "</p>"
    )
    daw_context = _active_tool_context_markup(shell, result.dimension)
    inventory_context = _semantic_tool_context_markup(shell)
    return (
        '<div class="stack" aria-live="polite">'
        '<h3>One prompt to try</h3>'
        f'<p class="status good">{html.escape(result.distance.title())} · {html.escape(result.dimension.title())}</p>'
        f'<p><strong>{html.escape(result.title)}</strong></p>'
        f'<p>{html.escape(result.prompt)}</p>'
        f'<p class="muted">{html.escape(result.distance_explanation)}</p>'
        f'{objective}'
        f'{daw_context}'
        f'{inventory_context}'
        '<p class="muted">Generated locally and deterministically. No AI provider was called, no project was changed, and this is not a claim about what your Song needs.</p>'
        f'{_deferral_forms(shell, result)}'
        '</div>'
    )


def _suggestion_card(shell: ConsumerShell) -> str:
    if shell.runtime.state != "RUNNING":
        raise ConsumerShellError("Creative suggestions require an open Artist workspace")
    hq = shell.runtime.headquarters
    song = hq.store.active_song()
    if song is None:
        return ""

    distance_options = "".join(
        f'<option value="{mode}">{html.escape(_DISTANCE_LABELS[mode])}</option>'
        for mode in SUGGESTION_DISTANCES
    )
    locks = "".join(
        '<label><input type="checkbox" name="lock_'
        + dimension.lower()
        + '" value="1"> Keep '
        + html.escape(dimension.title())
        + " unchanged</label>"
        for dimension in CREATIVE_DIMENSIONS
    )
    result = getattr(shell, "_creative_suggestion_result", None)
    if result is not None and not isinstance(result, CreativeSuggestion):
        raise ConsumerShellError("creative suggestion shell state is invalid")
    if result is not None:
        latest = hq.sessions.latest_for_song(song.id)
        latest_session_id = None if latest is None else latest.id
        if result.song_id != song.id or result.session_id != latest_session_id:
            shell._creative_suggestion_result = None
            result = None

    return (
        '<div class="card"><h2>Suggest something</h2>'
        '<p>Ask N0TE for one bounded experiment around this Song. Choose how far to move and lock anything you do not want varied.</p>'
        '<form class="stack" method="post" action="/suggestion/create" aria-label="Create a Song suggestion">'
        f'{shell._hidden(_suggestion_action(shell, song.id))}'
        '<div><label>How far should the idea move?'
        f'<select name="distance" required>{distance_options}</select></label></div>'
        '<fieldset><legend>Keep these dimensions unchanged (optional)</legend>'
        f'<div class="stack">{locks}</div></fieldset>'
        '<button class="primary" type="submit">Suggest something</button>'
        '<p class="muted">The first suggestion layer is deliberately AI-off and non-personalized. “Familiar” means a smaller move around this Song, not a claim that N0TE already knows your taste.</p>'
        '</form>'
        f'{_result_markup(shell, result)}</div>'
    )


def _locked_dimensions(form: Mapping[str, str]) -> tuple[str, ...]:
    locks = []
    for dimension in CREATIVE_DIMENSIONS:
        key = "lock_" + dimension.lower()
        value = form.get(key)
        if value is None:
            continue
        if value != "1":
            raise ValidationError(f"invalid lock value for {dimension}")
        locks.append(dimension)
    return tuple(locks)


def _next_visible_suggestion(
    shell: ConsumerShell, *, distance: str, locked_dimensions: tuple[str, ...]
) -> CreativeSuggestion:
    service = _service(shell)
    deferrals = shell.runtime.headquarters.suggestion_deferrals
    for variation in range(1001):
        result = service.suggest(
            distance=distance,
            locked_dimensions=locked_dimensions,
            variation=variation,
        )
        if not deferrals.is_deferred_now(result.semantic_key):
            return result
    raise CreativeSuggestionError(
        "Every available local suggestion is currently suppressed by your Not Now choices."
    )


def _post_suggestion(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "song-suggest")
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That suggestion action was already handled or expired."),
        )
        return
    song = shell.runtime.headquarters.store.active_song()
    if song is None or song.id != action.value:
        shell._send_html(
            handler,
            409,
            shell._simple_error("The active Song changed. Reload the Song before asking for an idea."),
        )
        return
    result = _next_visible_suggestion(
        shell,
        distance=form.get("distance", ""),
        locked_dimensions=_locked_dimensions(form),
    )
    shell._creative_suggestion_result = result
    shell._consumer_notice = "Local creative prompt prepared. Nothing in the Song was changed."
    shell._redirect(handler, "/song")


def _post_not_now(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "suggestion-not-now")
    if action is None or action.value is None:
        shell._send_html(
            handler,
            409,
            shell._simple_error("That Not now action was already handled or expired."),
        )
        return
    scope, separator, _ = action.value.partition("|")
    result = getattr(shell, "_creative_suggestion_result", None)
    if (
        not separator
        or scope not in _DEFERRAL_CHOICES
        or not isinstance(result, CreativeSuggestion)
        or action.value != _deferral_binding(scope, result)
    ):
        shell._send_html(
            handler,
            409,
            shell._simple_error(
                "That suggestion or Not now horizon is no longer current. Reload before deferring it."
            ),
        )
        return
    hq = shell.runtime.headquarters
    song = hq.store.active_song()
    latest = None if song is None else hq.sessions.latest_for_song(song.id)
    latest_session_id = None if latest is None else latest.id
    if (
        song is None
        or song.id != result.song_id
        or latest_session_id != result.session_id
    ):
        shell._send_html(
            handler,
            409,
            shell._simple_error(
                "The Song work context changed before that suggestion could be deferred."
            ),
        )
        return
    if scope == LATER_THIS_SONG:
        if result.session_id is None:
            raise ValidationError(
                "Start a work Session before deferring a suggestion until later this Song"
            )
        hq.suggestion_deferrals.defer_later_this_song(result.semantic_key)
    elif scope == NEXT_SONG:
        hq.suggestion_deferrals.defer_until_next_song(result.semantic_key)
    else:
        hq.suggestion_deferrals.never_suggest_again(result.semantic_key)
    shell._creative_suggestion_result = None
    shell._consumer_notice = _DEFERRAL_NOTICES[scope]
    shell._redirect(handler, "/song")


def install_song_creative_suggestions() -> None:
    """Attach bounded creative suggestions and durable explicit deferral horizons."""
    if getattr(ConsumerShell, "_song_creative_suggestions_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_suggestion_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before creative suggestions could attach safely"
            )
        return rendered[: -len(marker)] + _suggestion_card(self) + marker

    def with_suggestion_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path not in {"/suggestion/create", "/suggestion/not-now"}:
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
            if path == "/suggestion/not-now":
                _post_not_now(self, handler, form)
            else:
                _post_suggestion(self, handler, form)
        except (ValidationError, CreativeSuggestionError, ConsumerShellError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/song")
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that suggestion action before it could become an unclear consumer state."
                ),
            )

    ConsumerShell._song_content = with_suggestion_card
    ConsumerShell._handle_post = with_suggestion_post
    ConsumerShell._song_creative_suggestions_installed = True
