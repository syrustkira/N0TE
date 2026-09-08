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


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=8406,
        start_token="song-session-unicode-bounds",
    )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass
class Form:
    action: str
    values: dict[str, str]


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


def request(
    shell: ConsumerShell,
    path: str,
    *,
    method: str = "GET",
    fields: dict[str, str] | None = None,
) -> tuple[int, str]:
    headers: dict[str, str] = {}
    data = None
    if fields is not None:
        data = urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = shell.address.origin
    req = Request(shell.address.origin + path, data=data, method=method, headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def form(page: str, action: str) -> Form:
    parser = Parser()
    parser.feed(page)
    matches = [candidate for candidate in parser.forms if candidate.action == action]
    assert len(matches) == 1
    return matches[0]


def test_maximum_unicode_session_fields_fit_transport_envelope(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    headquarters = HeadquartersMemory.create(data_root, "Unicode Artist")
    try:
        song = headquarters.store.create_song("Unicode Song")
        profile_id = headquarters.store.profile_id
    finally:
        headquarters.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(),
        probe=Probe(),
    )
    shell.start()

    status, song_page = request(shell, "/song")
    assert status == 200
    start = form(song_page, "/session/start")
    start_payload = dict(start.values)
    start_payload["objective"] = "Preserve maximum valid Unicode Session input"
    status, _ = request(shell, "/session/start", method="POST", fields=start_payload)
    assert status == 303

    status, open_page = request(shell, "/song")
    assert status == 200
    finish = form(open_page, "/session/finish")
    debrief = "🎛" * 1600
    next_action = "🎵" * 500
    finish_payload = dict(finish.values)
    finish_payload.update({"debrief": debrief, "next_action": next_action})
    encoded = urlencode(finish_payload).encode("utf-8")
    assert len(encoded) > 16384
    assert len(encoded) <= 32768

    status, _ = request(shell, "/session/finish", method="POST", fields=finish_payload)
    assert status == 303
    inspect = HeadquartersMemory.open(data_root, profile_id)
    try:
        closed = inspect.sessions.latest_for_song(song.id)
        assert closed is not None
        assert closed.state == "CLOSED"
        assert closed.debrief_summary == debrief
        assert closed.next_action == next_action
    finally:
        inspect.close()

    status, settings = request(shell, "/settings")
    assert status == 200
    quit_form = form(settings, "/quit")
    status, _ = request(shell, "/quit", method="POST", fields=quit_form.values)
    assert status == 200
    assert shell.wait_stopped(timeout=2.0)

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        durable = reopened.sessions.latest_for_song(song.id)
        assert durable is not None
        assert durable.debrief_summary == debrief
        assert durable.next_action == next_action
    finally:
        reopened.close()
