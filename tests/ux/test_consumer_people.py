from __future__ import annotations

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


class Parser(HTMLParser):
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


def forms(page: str, action: str) -> list[Form]:
    parser = Parser()
    parser.feed(page)
    return [candidate for candidate in parser.forms if candidate.action == action]


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
    req = Request(shell.address.origin + path, data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post_form(
    shell: ConsumerShell,
    form: Form,
    *,
    origin: str | None = None,
) -> tuple[int, str]:
    return request(
        shell,
        form.action,
        method="POST",
        fields=dict(form.values),
        origin=shell.address.origin if origin is None else origin,
    )


def new_profile_with_song(root: Path) -> tuple[str, str]:
    hq = HeadquartersMemory.create(root, "People UX Artist")
    try:
        song = hq.store.create_song("People UX Song")
        return hq.store.profile_id, song.id
    finally:
        hq.close()


def new_shell(data_root: Path, state_root: Path, pid: int, token: str) -> ConsumerShell:
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(pid, token),
        probe=Probe(),
    )
    shell.start()
    return shell


def test_artist_can_add_person_track_song_bound_followup_and_resolve_it(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id = new_profile_with_song(data_root)
    shell = new_shell(data_root, state_root, 9951, "people-journey")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        assert "People and open loops" in page
        assert 'href="/people" aria-current="page">People</a>' in page
        assert "No people recorded yet" in page
        assert "Local record only" in page

        add_person = forms(page, "/people/create")
        assert len(add_person) == 1
        add_person[0].values["display_name"] = "Maya Rivera"
        add_person[0].values["relationship_context"] = (
            "Producer helping finish the bridge"
        )
        status, _ = post_form(shell, add_person[0])
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "Maya Rivera" in page
        assert "Producer helping finish the bridge" in page
        assert "No open follow-ups" in page
        assert "person_" not in page
        assert "followup_" not in page

        add_followup = forms(page, "/people/followup/create")
        assert len(add_followup) == 1
        add_followup[0].values["summary"] = (
            "Send the cleaned bridge stems after comp approval"
        )
        add_followup[0].values["responsibility"] = "ARTIST_OWES"
        add_followup[0].values["due_on"] = "2026-09-12"
        add_followup[0].values["bind_song"] = "1"
        status, _ = post_form(shell, add_followup[0])
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "Send the cleaned bridge stems after comp approval" in page
        assert "I owe this" in page
        assert "Due 2026-09-12" in page
        assert "Song: People UX Song" in page
        assert "did not message anyone or create an external reminder" in page
        assert "person_" not in page
        assert "followup_" not in page

        resolve = forms(page, "/people/followup/resolve")
        assert len(resolve) == 1
        resolve[0].values["resolution_note"] = "Approved stems were delivered"
        status, _ = post_form(shell, resolve[0])
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "No open follow-ups" in page
        assert "Send the cleaned bridge stems after comp approval" not in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        people = reopened.people.people()
        assert len(people) == 1
        assert people[0].display_name == "Maya Rivera"
        history = reopened.people.followups(person_id=people[0].id)
        assert len(history) == 1
        assert history[0].song_id == song_id
        assert history[0].state == "RESOLVED"
        assert history[0].resolution_note == "Approved stems were delivered"
    finally:
        reopened.close()


def test_people_write_rejects_foreign_origin_and_action_replay(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    new_profile_with_song(data_root)
    shell = new_shell(data_root, state_root, 9952, "people-authority")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        add_person = forms(page, "/people/create")[0]
        add_person.values["display_name"] = "Alex"

        status, denied = post_form(
            shell,
            add_person,
            origin="https://attacker.invalid",
        )
        assert status == 403
        assert "did not come from this N0TE window" in denied

        status, _ = post_form(shell, add_person)
        assert status == 303
        status, replay = post_form(shell, add_person)
        assert status == 409
        assert "already handled or expired" in replay

        status, page = request(shell, "/people")
        assert status == 200
        assert page.count("<h2>Alex</h2>") == 1
    finally:
        shell.stop()


def test_people_install_is_idempotent_and_does_not_duplicate_navigation(
    tmp_path: Path,
) -> None:
    from n0te.people_shell import install_people_headquarters

    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    new_profile_with_song(data_root)

    install_people_headquarters()
    install_people_headquarters()
    shell = new_shell(data_root, state_root, 9953, "people-idempotent")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        assert page.count('href="/people"') == 1
        assert page.count("Add someone you work with") == 1
    finally:
        shell.stop()
