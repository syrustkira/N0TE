from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping

from .consumer_shell import ConsumerShell, ConsumerShellError
from .lineage import LineageCorruptionError, NotFoundError, ValidationError
from .template_catalog import TemplateCatalog, TemplateCatalogError
from .templates import TEMPLATE_FAMILIES, TemplateRole, TemplateValidationError

_MAX_NAME = 120
_MAX_INTENT = 1000
_MAX_CAPABILITY = 200
_MAX_DESCRIPTION = 600
_MAX_TAG_TEXT = 400
_MAX_CONSUMER_ROLES = 3


def _catalog(shell: ConsumerShell) -> TemplateCatalog:
    return TemplateCatalog(shell.runtime.headquarters.store)


def _role_summary(role: TemplateRole) -> str:
    posture = "required" if role.required else "optional"
    tags = "" if not role.tags else " · " + ", ".join(role.tags)
    return (
        f"<li><strong>{html.escape(role.description)}</strong> "
        f'<span class="muted">({html.escape(role.capability)} · {posture}{html.escape(tags)})</span></li>'
    )


def _family_options() -> str:
    return "".join(
        f'<option value="{html.escape(family, quote=True)}">{html.escape(family.replace("_", " ").title())}</option>'
        for family in sorted(TEMPLATE_FAMILIES)
    )


def _role_editor(index: int) -> str:
    required = " checked" if index == 1 else ""
    return (
        '<fieldset class="stack">'
        f"<legend>Semantic role {index}</legend>"
        f'<div><label for="template-role-{index}-capability">Capability</label>'
        f'<input id="template-role-{index}-capability" name="role_{index}_capability" '
        f'type="text" maxlength="{_MAX_CAPABILITY}" placeholder="example: vocal.tighten"></div>'
        f'<div><label for="template-role-{index}-description">What this role is for</label>'
        f'<input id="template-role-{index}-description" name="role_{index}_description" '
        f'type="text" maxlength="{_MAX_DESCRIPTION}" placeholder="Tighten the lead while preserving performance intent"></div>'
        f'<div><label for="template-role-{index}-tags">Tags <span class="muted">optional, comma separated</span></label>'
        f'<input id="template-role-{index}-tags" name="role_{index}_tags" '
        f'type="text" maxlength="{_MAX_TAG_TEXT}" placeholder="lead, editing"></div>'
        f'<label><input name="role_{index}_required" type="checkbox" value="1"{required}> Required for this reusable start</label>'
        "</fieldset>"
    )


def _template_catalog_card(shell: ConsumerShell, song) -> str:
    catalog = _catalog(shell)
    definitions = catalog.templates()
    selection = catalog.current_selection(song.id)
    selected_id = None if selection is None else selection.template_id

    if not definitions:
        catalog_html = (
            '<p class="muted">No reusable Templates saved yet. Save N0TE-level intent and roles here; host-specific instantiation stays separate.</p>'
        )
    else:
        cards: list[str] = []
        for definition in definitions:
            selected = definition.template_id == selected_id
            status = (
                '<p class="status good">Selected for this Song</p>'
                if selected
                else '<p class="muted">Available reusable start</p>'
            )
            if selected:
                if selection is None:
                    raise ConsumerShellError(
                        "Template selection state changed while rendering the Song"
                    )
                token = shell._new_action(
                    "template-clear",
                    json.dumps(
                        {"song_id": song.id, "selection_id": selection.id},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                action = (
                    '<form method="post" action="/template/clear">'
                    + shell._hidden(token)
                    + '<button type="submit">Clear Template selection</button></form>'
                )
            else:
                token = shell._new_action(
                    "template-select",
                    json.dumps(
                        {"song_id": song.id, "template_id": definition.template_id},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                action = (
                    '<form method="post" action="/template/select">'
                    + shell._hidden(token)
                    + '<button type="submit">Select for this Song</button></form>'
                )
            roles = "".join(_role_summary(role) for role in definition.roles)
            cards.append(
                '<div class="card stack">'
                f"<h3>{html.escape(definition.name)}</h3>{status}"
                f'<p><strong>{html.escape(definition.family.replace("_", " ").title())}</strong> · {html.escape(definition.intent)}</p>'
                f"<ul>{roles}</ul>{action}"
                "</div>"
            )
        catalog_html = "".join(cards)

    save_token = shell._new_action("template-save", song.id)
    editors = "".join(_role_editor(index) for index in range(1, _MAX_CONSUMER_ROLES + 1))
    save_form = (
        "<details><summary>Save a reusable Template</summary>"
        '<form class="stack" method="post" action="/template/save">'
        + shell._hidden(save_token)
        + '<div><label for="template-family">Family</label><select id="template-family" name="family">'
        + _family_options()
        + "</select></div>"
        + f'<div><label for="template-name">Name</label><input id="template-name" name="name" type="text" maxlength="{_MAX_NAME}" required></div>'
        + f'<div><label for="template-intent">Reusable intent</label><textarea id="template-intent" name="intent" maxlength="{_MAX_INTENT}" required></textarea></div>'
        + editors
        + '<button type="submit">Save Template meaning</button>'
        + "</form></details>"
    )
    selected_copy = (
        '<p class="status caution">No Template selected for this Song.</p>'
        if selected_id is None
        else '<p class="muted">Selection is durable Song context only. Clear it to return to a blank start. Nothing has been instantiated, authorized, or changed in a DAW.</p>'
    )
    return (
        '<div class="card stack" aria-label="Reusable Templates">'
        "<h2>Reusable starts</h2>"
        "<p>Keep the musical or operational meaning above any DAW, provider, plug-in, or track layout. Select a Template here before a later capability-aware application step.</p>"
        + selected_copy
        + catalog_html
        + save_form
        + '<p class="muted">Saving, selecting, or clearing a Template does not execute a Recipe, call a provider, mutate a Version, start a Session, or grant external action authority.</p>'
        + "</div>"
    )


def _clean_text(value: str | None, field: str, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise TemplateCatalogError(f"{field} must not be empty")
    if len(text) > maximum:
        raise TemplateCatalogError(f"{field} is too long")
    return text


def _clean_tags(value: str | None) -> tuple[str, ...]:
    if value is None or not str(value).strip():
        return ()
    tags: list[str] = []
    for item in str(value).split(","):
        tag = " ".join(item.split())
        if not tag:
            continue
        if len(tag) > 80:
            raise TemplateCatalogError("Template role tags must be at most 80 characters each")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > 8:
        raise TemplateCatalogError("Template role may contain at most 8 tags")
    return tuple(tags)


def _roles_from_form(form: Mapping[str, str]) -> tuple[TemplateRole, ...]:
    roles: list[TemplateRole] = []
    for index in range(1, _MAX_CONSUMER_ROLES + 1):
        capability_raw = str(form.get(f"role_{index}_capability", "")).strip()
        description_raw = str(form.get(f"role_{index}_description", "")).strip()
        tags_raw = str(form.get(f"role_{index}_tags", "")).strip()
        if not capability_raw and not description_raw and not tags_raw:
            continue
        capability = _clean_text(capability_raw, f"Role {index} capability", _MAX_CAPABILITY)
        description = _clean_text(description_raw, f"Role {index} description", _MAX_DESCRIPTION)
        roles.append(
            TemplateRole(
                role_id=f"role-{index}",
                capability=capability,
                description=description,
                required=form.get(f"role_{index}_required") == "1",
                tags=_clean_tags(tags_raw),
            )
        )
    if not roles:
        raise TemplateCatalogError("Add at least one semantic Template role")
    return tuple(roles)


def _require_bound_song(shell: ConsumerShell, expected_song_id: str):
    song = shell.runtime.headquarters.store.active_song()
    if song is None or song.id != expected_song_id:
        raise ConsumerShellError(
            "The active Song changed. Reload the Song before changing Template context."
        )
    return song


def _save_template(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "template-save")
    if action is None or action.value is None:
        raise ConsumerShellError("That Template save action was already handled or expired")
    _require_bound_song(shell, action.value)
    family = _clean_text(form.get("family"), "Template family", 40).upper()
    if family not in TEMPLATE_FAMILIES:
        raise TemplateCatalogError("Choose a supported Template family")
    definition = _catalog(shell).create(
        family=family,
        name=_clean_text(form.get("name"), "Template name", _MAX_NAME),
        intent=_clean_text(form.get("intent"), "Template intent", _MAX_INTENT),
        roles=_roles_from_form(form),
    )
    shell._consumer_notice = (
        f"Saved {definition.name} as provider-neutral Template meaning. "
        "Nothing was applied to a DAW or provider."
    )


def _select_template(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "template-select")
    if action is None or action.value is None:
        raise ConsumerShellError("That Template selection was already handled or expired")
    try:
        binding = json.loads(action.value)
    except json.JSONDecodeError as exc:
        raise ConsumerShellError("Template selection binding is unreadable") from exc
    if not isinstance(binding, dict) or set(binding) != {"song_id", "template_id"}:
        raise ConsumerShellError("Template selection binding is invalid")
    song = _require_bound_song(shell, str(binding["song_id"]))
    catalog = _catalog(shell)
    definition = catalog.get(str(binding["template_id"]))
    if definition is None:
        raise ConsumerShellError("That Template is no longer available in this Artist profile")
    catalog.select_for_song(song_id=song.id, template_id=definition.template_id)
    shell._consumer_notice = (
        f"Selected {definition.name} for {song.title} as Artist-declared reusable context. "
        "Selection is not execution or authorization."
    )


def _clear_template(shell: ConsumerShell, form: Mapping[str, str]) -> None:
    action = shell._consume_action(form.get("action", ""), "template-clear")
    if action is None or action.value is None:
        raise ConsumerShellError("That Template clear action was already handled or expired")
    try:
        binding = json.loads(action.value)
    except json.JSONDecodeError as exc:
        raise ConsumerShellError("Template clear binding is unreadable") from exc
    if not isinstance(binding, dict) or set(binding) != {"song_id", "selection_id"}:
        raise ConsumerShellError("Template clear binding is invalid")
    song = _require_bound_song(shell, str(binding["song_id"]))
    try:
        _catalog(shell).clear_selection_for_song(
            song_id=song.id,
            expected_selection_id=str(binding["selection_id"]),
        )
    except TemplateCatalogError as exc:
        raise ConsumerShellError(str(exc)) from exc
    shell._consumer_notice = (
        f"Cleared the Template selection for {song.title}. "
        "History remains append-only and no DAW or provider action occurred."
    )


def install_song_template_catalog() -> None:
    """Attach durable provider-neutral Template save/select/clear to the Song surface."""
    if getattr(ConsumerShell, "_song_template_catalog_installed", False):
        return

    original_song: Callable[[ConsumerShell, object], str] = ConsumerShell._song_content
    original_post: Callable[[ConsumerShell, BaseHTTPRequestHandler], None] = ConsumerShell._handle_post

    def with_templates(self: ConsumerShell, state) -> str:
        rendered = original_song(self, state)
        song = self.runtime.headquarters.store.active_song()
        if song is None or song.title != state.song_title:
            raise ConsumerShellError(
                "active Song changed while preparing reusable Template context"
            )
        marker = "</section>"
        if not rendered.endswith(marker):
            raise ConsumerShellError(
                "Song page structure changed before reusable Templates could be attached safely"
            )
        return rendered[: -len(marker)] + _template_catalog_card(self, song) + marker

    def with_template_post(self: ConsumerShell, handler: BaseHTTPRequestHandler) -> None:
        path = self._path(handler)
        if path not in {"/template/save", "/template/select", "/template/clear"}:
            original_post(self, handler)
            return
        if not self._request_host_is_exact(handler) or not self._post_origin_is_allowed(handler):
            self._send_html(
                handler,
                403,
                self._simple_error("That action did not come from this N0TE window."),
            )
            return
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before changing Template context."),
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
            if path == "/template/save":
                _save_template(self, form)
            elif path == "/template/select":
                _select_template(self, form)
            else:
                _clear_template(self, form)
        except ConsumerShellError as exc:
            self._send_html(handler, 409, self._simple_error(str(exc)))
            return
        except (TemplateCatalogError, TemplateValidationError, ValidationError, NotFoundError) as exc:
            self._consumer_notice = str(exc)
            self._redirect(handler, "/song")
            return
        except LineageCorruptionError:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE found unreadable Template history and stopped before rewriting it."
                ),
            )
            return
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that Template action before its meaning could become ambiguous."
                ),
            )
            return
        self._redirect(handler, "/song")

    ConsumerShell._song_content = with_templates
    ConsumerShell._handle_post = with_template_post
    ConsumerShell._song_template_catalog_installed = True
