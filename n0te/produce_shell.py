from __future__ import annotations

import html
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping, Protocol

from .authority import ActionIntent, AuthorityService
from .consumer_shell import ConsumerShell, ConsumerShellError, _PageState, _clean_human_text
from .focus import FocusContext, FocusError
from .hosts import HostRuntimeIdentity
from .musical_plan import (
    HostCompilation,
    MusicalPlan,
    MusicalPlanError,
    MusicalPlanService,
)
from .musical_transactions import (
    CompiledActionDriver,
    MusicalTransactionError,
    MusicalTransactionService,
)
from .song_transactions import SongTransactionError, SongTransactionService

_MAX_PRODUCE_INTENT = 1200


class ProduceReachabilityError(RuntimeError):
    """The consumer Produce journey cannot safely reach an existing execution owner."""


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ProduceReachabilityError(f"{field} must not be empty")
    return text


def _text_tuple(values, field: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ProduceReachabilityError(f"{field} must be a sequence")
    try:
        result = tuple(_text(value, field) for value in values)
    except TypeError as exc:
        raise ProduceReachabilityError(f"{field} must be a sequence") from exc
    if not allow_empty and not result:
        raise ProduceReachabilityError(f"{field} must not be empty")
    if len(set(result)) != len(result):
        raise ProduceReachabilityError(f"{field} must not contain duplicates")
    return result


@dataclass(frozen=True)
class ProduceRoute:
    """Observed host-specific inputs required by existing Musical Plan owners.

    The route does not execute anything. A DAW/host integration supplies current
    observed context plus an already-selected compiler/driver pair; the consumer
    shell still performs Musical Plan preparation, exact authority binding,
    transaction execution and verification through their canonical owners.
    """

    context: FocusContext
    runtime: HostRuntimeIdentity
    compiler: object
    driver: CompiledActionDriver
    required_dimensions: tuple[str, ...]
    verification_refs: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    locked_element_refs: tuple[str, ...] = ()
    editable_element_refs: tuple[str, ...] = ()
    provenance_refs: tuple[str, ...] = ()
    route_evidence_ref: str = "produce-route:observed"

    def __post_init__(self) -> None:
        if not isinstance(self.context, FocusContext):
            raise TypeError("context must be FocusContext")
        if not isinstance(self.runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        if self.context.host_runtime_fingerprint != self.runtime.fingerprint:
            raise ProduceReachabilityError("Produce route context/runtime fingerprint mismatch")
        for name in ("compiler_id", "compiler_version", "host_family", "compile"):
            if not hasattr(self.compiler, name):
                raise TypeError("compiler must implement HostPlanCompiler")
        if not callable(getattr(self.compiler, "compile")):
            raise TypeError("compiler.compile must be callable")
        for name in (
            "prepare_snapshot",
            "execute_action",
            "verify_action",
            "compensate_action",
            "success_receipt",
        ):
            if not callable(getattr(self.driver, name, None)):
                raise TypeError("driver must implement CompiledActionDriver")
        object.__setattr__(
            self,
            "required_dimensions",
            _text_tuple(self.required_dimensions, "required_dimensions", allow_empty=False),
        )
        object.__setattr__(
            self,
            "verification_refs",
            _text_tuple(self.verification_refs, "verification_refs", allow_empty=False),
        )
        for field in ("constraints", "locked_element_refs", "editable_element_refs", "provenance_refs"):
            object.__setattr__(self, field, _text_tuple(getattr(self, field), field))
        object.__setattr__(
            self,
            "route_evidence_ref",
            _text(self.route_evidence_ref, "route_evidence_ref"),
        )


class ProduceRouteProvider(Protocol):
    """Host integration boundary: observe/resolve, never grant authority or execute."""

    def resolve(
        self,
        headquarters,
        *,
        song_id: str,
        artist_intent: str,
    ) -> ProduceRoute: ...


@dataclass(frozen=True)
class _ProducePreview:
    song_id: str
    route: ProduceRoute
    plan: MusicalPlan
    compilation: HostCompilation
    intent: ActionIntent


def bind_produce_route_provider(shell: ConsumerShell, provider: ProduceRouteProvider) -> None:
    """Attach one runtime host-integration provider to this consumer shell instance."""

    if not isinstance(shell, ConsumerShell):
        raise TypeError("shell must be ConsumerShell")
    if not callable(getattr(provider, "resolve", None)):
        raise TypeError("provider must implement resolve(headquarters, song_id, artist_intent)")
    shell._produce_route_provider = provider
    shell._produce_preview = None


def _provider(shell: ConsumerShell):
    provider = getattr(shell, "_produce_route_provider", None)
    if provider is None:
        return None
    if not callable(getattr(provider, "resolve", None)):
        raise ProduceReachabilityError("configured Produce route provider is invalid")
    return provider


def _services(shell: ConsumerShell) -> tuple[MusicalPlanService, MusicalTransactionService]:
    headquarters = shell.runtime.headquarters
    plans = MusicalPlanService(headquarters.focus)
    transactions = MusicalTransactionService(
        plans,
        SongTransactionService(headquarters.transactions),
    )
    return plans, transactions


def _prepare_preview(shell: ConsumerShell, artist_intent: str) -> _ProducePreview:
    if shell.runtime.state != "RUNNING":
        raise ProduceReachabilityError("Open an Artist workspace before preparing a DAW change.")
    headquarters = shell.runtime.headquarters
    song = headquarters.store.active_song()
    if song is None:
        raise ProduceReachabilityError("Start or select a Song before preparing a DAW change.")
    provider = _provider(shell)
    if provider is None:
        raise ProduceReachabilityError(
            "No verified DAW execution route is connected for this N0TE session."
        )
    intent_text = _clean_human_text(
        artist_intent,
        "Produce intent",
        maximum=_MAX_PRODUCE_INTENT,
    )
    route = provider.resolve(
        headquarters,
        song_id=song.id,
        artist_intent=intent_text,
    )
    if not isinstance(route, ProduceRoute):
        raise ProduceReachabilityError("Produce route provider returned an invalid route")
    if route.context.song_id != song.id:
        raise ProduceReachabilityError("Produce route belongs to a different Song")

    plans, transactions = _services(shell)
    plan = plans.prepare(
        route.context,
        artist_intent=intent_text,
        required_dimensions=route.required_dimensions,
        desired_change=intent_text,
        constraints=route.constraints,
        locked_element_refs=route.locked_element_refs,
        editable_element_refs=route.editable_element_refs,
        verification_refs=route.verification_refs,
        provenance_refs=(route.route_evidence_ref, *route.provenance_refs),
    )
    compilation = plans.compile(
        plan,
        route.context,
        route.runtime,
        route.compiler,
    )
    action_intent = transactions.preview(plan, route.context, compilation)
    return _ProducePreview(song.id, route, plan, compilation, action_intent)


def _preview_token(shell: ConsumerShell, preview: _ProducePreview) -> str:
    return shell._new_action(
        "produce-execute",
        preview.intent.intent_fingerprint,
    )


def _targets_markup(plan: MusicalPlan) -> str:
    rows = []
    for target in plan.targets:
        refs = ", ".join(target.refs)
        rows.append(
            f"<li><strong>{html.escape(target.dimension.title().replace('_', ' '))}</strong>: "
            f"{html.escape(refs)}</li>"
        )
    return '<ul class="stack" aria-label="Exact Musical Plan targets">' + "".join(rows) + "</ul>"


def _actions_markup(compilation: HostCompilation) -> str:
    rows = []
    for action in compilation.actions:
        rows.append(
            '<li class="stack">'
            f'<strong>{html.escape(action.capability)}</strong>'
            f'<span class="status">{html.escape(action.route_kind.replace("_", " ").title())}</span>'
            f'<span class="muted">Verify: {html.escape(action.postcondition_ref)}</span>'
            '</li>'
        )
    return '<ol class="stack" aria-label="Compiled bounded actions">' + "".join(rows) + "</ol>"


def _preview_markup(shell: ConsumerShell, preview: _ProducePreview) -> str:
    token = _preview_token(shell, preview)
    runtime = preview.route.runtime
    return (
        '<div class="card stack"><h2>Review this exact change</h2>'
        '<p class="status caution">Nothing has been changed in the DAW yet.</p>'
        f'<p><strong>Intent:</strong> {html.escape(preview.plan.artist_intent)}</p>'
        f'<p><strong>Host:</strong> {html.escape(runtime.canonical_display_name)} '
        f'{html.escape(runtime.version)} · {html.escape(runtime.edition)}</p>'
        f'<p><strong>Compiler:</strong> {html.escape(preview.compilation.compiler_id)} '
        f'v{html.escape(preview.compilation.compiler_version)}</p>'
        '<h3>Exact targets</h3>'
        f'{_targets_markup(preview.plan)}'
        '<h3>Bounded actions</h3>'
        f'{_actions_markup(preview.compilation)}'
        '<p>Execute binds your approval to this exact Musical Plan and compilation. '
        'If the Song, workspace, focus or host state has moved, the existing stale-state gates refuse the write.</p>'
        '<form method="post" action="/produce/execute">'
        f'{shell._hidden(token)}'
        '<button class="primary" type="submit">Execute this exact change</button>'
        '</form>'
        '<p class="muted">This Produce path proves consumer reachability into the existing Musical Plan and transactional execution owners. It does not by itself prove a real DAW adapter, major-DAW acceptance, audition, Keep/Reject/Undo, or whole Personal Production acceptance.</p>'
        '</div>'
    )


def _produce_content(shell: ConsumerShell) -> str:
    headquarters = shell.runtime.headquarters
    song = headquarters.store.active_song()
    if song is None:
        return (
            '<section class="grid"><div class="card"><h2>No active Song</h2>'
            '<p>Start or select a Song before preparing a production change.</p>'
            '<a class="button primary" href="/song">Go to Song</a></div></section>'
        )

    provider = _provider(shell)
    if provider is None:
        return (
            '<section class="grid">'
            '<div class="card"><h2>Produce with N0TE</h2>'
            f'<p class="song-name">{html.escape(song.title)}</p>'
            '<p class="status caution">No verified DAW execution route connected</p>'
            '<p>Your Artist and Song remain fully available. N0TE will not pretend manual guidance or an unverified host feature is executable automation.</p>'
            '</div></section>'
        )

    token = shell._new_action("produce-preview", song.id)
    form = (
        '<div class="card stack"><h2>What do you want to change?</h2>'
        f'<p class="song-name">{html.escape(song.title)}</p>'
        '<form class="stack" method="post" action="/produce/preview">'
        f'{shell._hidden(token)}'
        f'<div><label for="produce-intent">Describe the musical result, not the plumbing</label>'
        f'<textarea id="produce-intent" name="artist_intent" maxlength="{_MAX_PRODUCE_INTENT}" rows="4" required></textarea></div>'
        '<button class="primary" type="submit">Prepare change</button>'
        '</form><p class="muted">Prepare is read-only with respect to DAW mutation. It captures current focus, creates durable Musical Plan meaning, compiles the selected verified route and shows the exact bounded action before approval.</p>'
        '</div>'
    )
    preview = getattr(shell, "_produce_preview", None)
    preview_markup = ""
    if isinstance(preview, _ProducePreview) and preview.song_id == song.id:
        preview_markup = _preview_markup(shell, preview)
    return f'<section class="grid">{form}{preview_markup}</section>'


def _produce_song_card(shell: ConsumerShell) -> str:
    if shell.runtime.state != "RUNNING":
        return ""
    song = shell.runtime.headquarters.store.active_song()
    if song is None:
        return ""
    provider = _provider(shell)
    state = (
        '<p class="status good">Host route available for bounded Produce preparation</p>'
        if provider is not None
        else '<p class="status caution">DAW execution route not connected yet</p>'
    )
    return (
        '<div class="card"><h2>Produce with N0TE</h2>'
        '<p>Turn one musical intention into an exact host-neutral Musical Plan, review the compiled bounded action, then explicitly approve execution through the existing transaction and verification path.</p>'
        f'{state}<a class="button primary" href="/produce">Open Produce</a>'
        '<p class="muted">N0TE never treats a host feature, compiler object, or command return as a verified DAW result by itself.</p>'
        '</div>'
    )


def _produce_state(shell: ConsumerShell) -> _PageState:
    artist = shell.runtime.headquarters.store.artist()
    song = shell.runtime.headquarters.store.active_song()
    return _PageState(
        "running-produce",
        "Produce with N0TE",
        "Intent · Plan · Execute · Verify",
        (
            "Describe the musical result. N0TE preserves your intent above the DAW, shows the exact compiled change, and binds execution to explicit approval."
            if song is not None
            else "Start or select a Song before preparing a production change."
        ),
        artist_name=artist.display_name,
        song_title=None if song is None else song.title,
    )


def _post_preview(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "produce-preview")
    if action is None or action.value is None:
        raise ProduceReachabilityError("That Produce preparation was already handled or expired.")
    song = shell.runtime.headquarters.store.active_song()
    if song is None or song.id != action.value:
        raise ProduceReachabilityError("The active Song changed. Reload Produce before preparing the change.")
    shell._produce_preview = _prepare_preview(shell, form.get("artist_intent", ""))
    shell._consumer_notice = "Musical Plan prepared from current observed focus. No DAW change has been made."
    shell._redirect(handler, "/produce")


def _post_execute(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    form: Mapping[str, str],
) -> None:
    action = shell._consume_action(form.get("action", ""), "produce-execute")
    preview = getattr(shell, "_produce_preview", None)
    if (
        action is None
        or action.value is None
        or not isinstance(preview, _ProducePreview)
        or action.value != preview.intent.intent_fingerprint
    ):
        raise ProduceReachabilityError("That Produce approval was already handled, changed, or expired.")

    _, transactions = _services(shell)
    approval = AuthorityService.bind_approval(
        preview.intent,
        f"consumer-shell:produce:{preview.plan.plan_id}",
    )
    prepared = transactions.prepare(
        preview.plan,
        preview.route.context,
        preview.compilation,
        approval,
        idempotency_key=f"consumer-shell:produce:{preview.plan.plan_id}",
        claim_evidence_ref=f"consumer-shell:produce:approval:{approval.approval_id}",
    )
    result = transactions.run(
        prepared,
        preview.plan,
        preview.route.context,
        preview.compilation,
        preview.route.driver,
    )
    shell._produce_preview = None
    if result.status == "COMPLETE":
        shell._consumer_notice = (
            f"Produce execution verified complete. Transaction {result.transaction_id} has durable execution evidence."
        )
    elif result.status == "COMPENSATED":
        shell._consumer_notice = (
            f"Produce execution did not pass as requested. N0TE restored the prior bounded state and recorded transaction {result.transaction_id}."
        )
    else:
        shell._consumer_notice = (
            f"Produce result is {result.status}. N0TE will not retry blindly; transaction {result.transaction_id} requires truthful recovery/reconciliation."
        )
    shell._redirect(handler, "/produce")


def install_song_produce() -> None:
    """Attach consumer Produce reachability to existing plan/transaction owners once."""

    if getattr(ConsumerShell, "_song_produce_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_state_content: Callable[[ConsumerShell, object], str] = ConsumerShell._state_content
    original_get: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_get
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_produce_card(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError("Song page structure changed before Produce could attach safely")
        return rendered[: -len(marker)] + _produce_song_card(self) + marker

    def with_produce_content(self: ConsumerShell, state) -> str:
        if state.kind != "running-produce":
            return original_state_content(self, state)
        return _produce_content(self)

    def with_produce_get(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        if self._path(handler) != "/produce":
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
            self._send_html(handler, 200, self._render_state(_produce_state(self), path="/song"))
        except (ProduceReachabilityError, FocusError, MusicalPlanError, MusicalTransactionError, SongTransactionError, ConsumerShellError) as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error("N0TE stopped Produce before uncertain host state could be presented as executable."),
            )

    def with_produce_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path not in {"/produce/preview", "/produce/execute"}:
            original_post(self, handler)
            return
        if not self._request_host_is_exact(handler) or not self._post_origin_is_allowed(handler):
            self._send_html(
                handler,
                403,
                self._simple_error("That Produce action did not come from this N0TE window."),
            )
            return
        form = self._read_form(handler)
        if form is None or not self._form_authorized(form):
            self._send_html(
                handler,
                403,
                self._simple_error("That Produce action expired. Reload Produce and try again."),
            )
            return
        try:
            if path == "/produce/preview":
                _post_preview(self, handler, form)
            else:
                _post_execute(self, handler, form)
        except (ProduceReachabilityError, FocusError, MusicalPlanError, MusicalTransactionError, SongTransactionError, ConsumerShellError) as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error("N0TE stopped that Produce action before it could become an unclear or over-authorized DAW state."),
            )

    ConsumerShell._song_content = with_produce_card
    ConsumerShell._state_content = with_produce_content
    ConsumerShell._handle_get = with_produce_get
    ConsumerShell._handle_post = with_produce_post
    ConsumerShell._song_produce_installed = True
