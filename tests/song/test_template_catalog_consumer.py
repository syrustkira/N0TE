from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment
from n0te.template_catalog import TemplateCatalog


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process(pid: int, token: str) -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=token,
    )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass
class Form:
    action: str
    values: dict[str, str]
    text: str = ""


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self.current: Form | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form":
            self.current = Form(str(values.get("action", "")), {})
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and values.get("name"):
            self.current.values[str(values["name"])] = str(values.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.text += data


def request(
    shell: ConsumerShell,
    path: str,
    *,
    method: str = "GET",
    fields: dict[str, str] | None = None,
    origin: str | None = None,
) -> tuple[int, str]:
    headers: dict[str, str] = {}
    data = None
    if fields is not None:
        data = urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if origin is not None:
        headers["Origin"] = origin
    req = Request(shell.address.origin + path, data=data, method=method, headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=5.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def forms(page: str, action: str) -> list[Form]:
    parser = FormParser()
    parser.feed(page)
    return [candidate for candidate in parser.forms if candidate.action == action]


def create_profile(data_root: Path, artist: str, song_title: str) -> tuple[str, str]:
    headquarters = HeadquartersMemory.create(data_root, artist)
    try:
        song = headquarters.store.create_song(song_title)
        return headquarters.store.profile_id, song.id
    finally:
        headquarters.close()


def catalog_table_exists(data_root: Path, profile_id: str) -> bool:
    db = data_root / "profiles" / profile_id / "lineage.sqlite3"
    conn = sqlite3.connect(db)
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='template_definitions'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def template_count(data_root: Path, profile_id: str) -> int:
    headquarters = HeadquartersMemory.open(data_root, profile_id)
    try:
        return len(TemplateCatalog(headquarters.store).templates())
    finally:
        headquarters.close()


def quit_shell(shell: ConsumerShell) -> None:
    status, settings = request(shell, "/settings")
    assert status == 200
    matches = forms(settings, "/quit")
    assert len(matches) == 1
    status, closed = request(
        shell,
        "/quit",
        method="POST",
        fields=matches[0].values,
        origin=shell.address.origin,
    )
    assert status == 200
    assert "N0TE closed safely." in closed
    assert shell.wait_stopped(timeout=2.0)


def save_payload(page: str, *, name: str = "Vocal Start") -> dict[str, str]:
    matches = forms(page, "/template/save")
    assert len(matches) == 1
    payload = dict(matches[0].values)
    payload.update(
        {
            "family": "VOCAL",
            "name": name,
            "intent": "Start vocal production from semantic roles, independent of the DAW",
            "role_1_capability": "  VOCAL.TIGHTEN  ",
            "role_1_description": "Tighten the lead while preserving performance intent",
            "role_1_tags": "lead, editing",
            "role_1_required": "1",
            "role_2_capability": "vocal.harmony.build",
            "role_2_description": "Build optional supporting harmonies",
            "role_2_tags": "harmony",
        }
    )
    payload.pop("role_2_required", None)
    return payload


def test_artist_can_save_select_clear_and_relaunch_provider_neutral_template(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id = create_profile(data_root, "Template Artist", "Template Song")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8701, "template-save-select-clear"),
        probe=Probe(),
    )
    shell.start()

    status, page = request(shell, "/song")
    assert status == 200
    assert "Reusable starts" in page
    assert "No Template selected for this Song" in page
    assert "Save a reusable Template" in page
    assert not catalog_table_exists(data_root, profile_id)

    status, _ = request(
        shell,
        "/template/save",
        method="POST",
        fields=save_payload(page),
        origin=shell.address.origin,
    )
    assert status == 303
    assert catalog_table_exists(data_root, profile_id)

    status, saved_page = request(shell, "/song")
    assert status == 200
    assert "Vocal Start" in saved_page
    assert "Tighten the lead while preserving performance intent" in saved_page
    assert "Build optional supporting harmonies" in saved_page
    assert "Available reusable start" in saved_page

    headquarters = HeadquartersMemory.open(data_root, profile_id)
    try:
        catalog = TemplateCatalog(headquarters.store)
        definitions = catalog.templates()
        assert len(definitions) == 1
        definition = definitions[0]
        assert definition.family == "VOCAL"
        assert tuple(role.required for role in definition.roles) == (True, False)
        assert tuple(role.capability for role in definition.roles) == (
            "vocal.tighten",
            "vocal.harmony.build",
        )
        assert catalog.current_selection(song_id) is None
        song = headquarters.store.get_song(song_id)
        assert song is not None
        assert song.current_version_id is None
        assert song.approved_version_id is None
        assert headquarters.sessions.latest_for_song(song_id) is None
    finally:
        headquarters.close()
    assert definition.template_id not in saved_page

    select = forms(saved_page, "/template/select")
    assert len(select) == 1
    status, _ = request(
        shell,
        "/template/select",
        method="POST",
        fields=select[0].values,
        origin=shell.address.origin,
    )
    assert status == 303
    status, selected_page = request(shell, "/song")
    assert status == 200
    assert "Selected for this Song" in selected_page
    assert "Selection is durable Song context only" in selected_page
    assert "Clear Template selection" in selected_page
    assert definition.template_id not in selected_page
    clear = forms(selected_page, "/template/clear")
    assert len(clear) == 1

    headquarters = HeadquartersMemory.open(data_root, profile_id)
    try:
        catalog = TemplateCatalog(headquarters.store)
        selected = catalog.selected_template(song_id)
        assert selected == definition
        selection = catalog.current_selection(song_id)
        assert selection is not None
        assert selection.source_kind == "ARTIST_DECLARED"
        song = headquarters.store.get_song(song_id)
        assert song is not None
        assert song.current_version_id is None
        assert song.approved_version_id is None
        assert headquarters.sessions.latest_for_song(song_id) is None
    finally:
        headquarters.close()

    status, _ = request(
        shell,
        "/template/clear",
        method="POST",
        fields=clear[0].values,
        origin=shell.address.origin,
    )
    assert status == 303
    status, cleared_page = request(shell, "/song")
    assert status == 200
    assert "No Template selected for this Song" in cleared_page
    assert "Available reusable start" in cleared_page
    assert "Selected for this Song" not in cleared_page
    assert forms(cleared_page, "/template/clear") == []

    headquarters = HeadquartersMemory.open(data_root, profile_id)
    try:
        catalog = TemplateCatalog(headquarters.store)
        assert catalog.current_selection(song_id) is None
        assert catalog.selected_template(song_id) is None
        assert tuple(item.template_id for item in catalog.selection_history(song_id)) == (
            definition.template_id,
            None,
        )
        song = headquarters.store.get_song(song_id)
        assert song is not None
        assert song.current_version_id is None
        assert song.approved_version_id is None
        assert headquarters.sessions.latest_for_song(song_id) is None
    finally:
        headquarters.close()

    quit_shell(shell)
    reopened = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8702, "template-relaunch-cleared"),
        probe=Probe(),
    )
    reopened.start()
    status, resumed = request(reopened, "/song")
    assert status == 200
    assert "Vocal Start" in resumed
    assert "No Template selected for this Song" in resumed
    assert "Selected for this Song" not in resumed
    assert forms(resumed, "/template/clear") == []
    assert len(forms(resumed, "/template/select")) == 1
    quit_shell(reopened)


def test_template_actions_are_origin_checked_one_shot_and_stale_selection_safe(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id = create_profile(data_root, "Authority Artist", "First Song")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8703, "template-authority"),
        probe=Probe(),
    )
    shell.start()

    status, page = request(shell, "/song")
    assert status == 200
    status, _ = request(
        shell,
        "/template/save",
        method="POST",
        fields=save_payload(page),
        origin=shell.address.origin,
    )
    assert status == 303
    status, page = request(shell, "/song")
    select = forms(page, "/template/select")[0]

    rejected, _ = request(
        shell,
        "/template/select",
        method="POST",
        fields=select.values,
        origin="https://attacker.example",
    )
    assert rejected == 403

    accepted, _ = request(
        shell,
        "/template/select",
        method="POST",
        fields=select.values,
        origin=shell.address.origin,
    )
    assert accepted == 303
    replay, _ = request(
        shell,
        "/template/select",
        method="POST",
        fields=select.values,
        origin=shell.address.origin,
    )
    assert replay == 409

    status, selected_page = request(shell, "/song")
    assert status == 200
    stale_clear = forms(selected_page, "/template/clear")[0]
    rejected_clear, _ = request(
        shell,
        "/template/clear",
        method="POST",
        fields=stale_clear.values,
        origin="https://attacker.example",
    )
    assert rejected_clear == 403

    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        catalog = TemplateCatalog(changer.store)
        current = catalog.current_selection(song_id)
        assert current is not None
        template_id = current.template_id
        assert template_id is not None
        newer = catalog.select_for_song(song_id=song_id, template_id=template_id)
        assert newer.id != current.id
    finally:
        changer.close()

    stale, _ = request(
        shell,
        "/template/clear",
        method="POST",
        fields=stale_clear.values,
        origin=shell.address.origin,
    )
    assert stale == 409

    status, refreshed = request(shell, "/song")
    assert status == 200
    fresh_clear = forms(refreshed, "/template/clear")[0]
    cleared, _ = request(
        shell,
        "/template/clear",
        method="POST",
        fields=fresh_clear.values,
        origin=shell.address.origin,
    )
    assert cleared == 303
    replay_clear, _ = request(
        shell,
        "/template/clear",
        method="POST",
        fields=fresh_clear.values,
        origin=shell.address.origin,
    )
    assert replay_clear == 409

    headquarters = HeadquartersMemory.open(data_root, profile_id)
    try:
        catalog = TemplateCatalog(headquarters.store)
        assert catalog.current_selection(song_id) is None
        history = catalog.selection_history(song_id)
        assert len(history) == 3
        assert history[-1].template_id is None
        song = headquarters.store.get_song(song_id)
        assert song is not None
        assert song.current_version_id is None
        assert song.approved_version_id is None
        assert headquarters.sessions.latest_for_song(song_id) is None
    finally:
        headquarters.close()

    status, current_page = request(shell, "/song")
    stale_save = forms(current_page, "/template/save")[0]
    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.store.create_song("Second Song")
    finally:
        changer.close()
    payload = dict(stale_save.values)
    payload.update(save_payload(current_page))
    payload["action"] = stale_save.values["action"]
    stale_song, _ = request(
        shell,
        "/template/save",
        method="POST",
        fields=payload,
        origin=shell.address.origin,
    )
    assert stale_song == 409
    assert template_count(data_root, profile_id) == 1
    quit_shell(shell)


def test_invalid_template_capability_does_not_create_durable_schema(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _ = create_profile(data_root, "Validation Artist", "Validation Song")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8704, "template-validation"),
        probe=Probe(),
    )
    shell.start()

    status, page = request(shell, "/song")
    assert status == 200
    payload = dict(forms(page, "/template/save")[0].values)
    payload.update(
        {
            "family": "VOCAL",
            "name": "Typo Start",
            "intent": "This must fail before durable catalog initialization",
            "role_1_capability": "vocal.tigten",
            "role_1_description": "Typo must fail before durable catalog initialization",
        }
    )
    status, _ = request(
        shell,
        "/template/save",
        method="POST",
        fields=payload,
        origin=shell.address.origin,
    )
    assert status == 303
    assert not catalog_table_exists(data_root, profile_id)
    assert template_count(data_root, profile_id) == 0
    quit_shell(shell)
