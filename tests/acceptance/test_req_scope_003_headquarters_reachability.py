from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te.consumer_shell import ConsumerShell
from n0te.instance import InstanceLeaseManager, ProcessIdentity
from n0te.platforms import PlatformEnvironment
from n0te.resume import SongResumeService


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=172003,
        start_token="req-scope-003-headquarters-reachability",
    )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(frozen=True)
class Response:
    status: int
    text: str


@dataclass
class Form:
    action: str
    values: dict[str, str]


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


def forms(page: str, action: str) -> list[Form]:
    parser = FormParser()
    parser.feed(page)
    return [form for form in parser.forms if form.action == action]


def request(
    shell: ConsumerShell,
    path: str,
    *,
    method: str = "GET",
    fields: dict[str, str] | None = None,
) -> Response:
    payload = None
    headers: dict[str, str] = {}
    if fields is not None:
        payload = urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = shell.address.origin
    req = Request(shell.address.origin + path, data=payload, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return Response(response.status, response.read().decode("utf-8"))
    except HTTPError as exc:
        return Response(exc.code, exc.read().decode("utf-8"))


def submit(shell: ConsumerShell, page: str, action: str, **fields: str) -> Response:
    matching = forms(page, action)
    assert len(matching) == 1
    payload = dict(matching[0].values)
    payload.update(fields)
    return request(shell, action, method="POST", fields=payload)


def quit_normally(shell: ConsumerShell) -> None:
    settings = request(shell, "/settings")
    assert settings.status == 200
    result = submit(shell, settings.text, "/quit")
    assert result.status == 200
    assert "N0TE closed safely." in result.text
    assert shell.wait_stopped(timeout=2.0)


def test_normal_customer_can_create_headquarters_start_song_quit_and_resume_same_truth(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    proc = process()

    first = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    first.start()

    welcome = request(first, "/")
    assert welcome.status == 200
    assert "Welcome to your Headquarters" in welcome.text
    created = submit(
        first,
        welcome.text,
        "/profile/create",
        artist_name="Reachability Artist",
    )
    assert created.status == 303

    song_page = request(first, "/song")
    assert song_page.status == 200
    started = submit(
        first,
        song_page.text,
        "/song/start",
        song_title="Reachability Song",
    )
    assert started.status == 303

    home = request(first, "/")
    song = request(first, "/song")
    assert home.status == 200 and song.status == 200
    for page in (home.text, song.text):
        assert "Reachability Artist" in page
        assert "Reachability Song" in page
        assert "prf_" not in page

    profile_id = first.runtime.profile_id
    assert profile_id is not None
    brief_before = SongResumeService(first.runtime.headquarters).brief()
    song_id = brief_before.song_id
    assert brief_before.artist_name == "Reachability Artist"
    assert brief_before.song_title == "Reachability Song"
    assert brief_before.is_active_song is True
    assert InstanceLeaseManager(state_root).inspect(profile_id) is not None

    quit_normally(first)
    assert InstanceLeaseManager(state_root).inspect(profile_id) is None

    reopened = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    reopened.start()
    resumed = request(reopened, "/")
    assert resumed.status == 200
    assert "Reachability Artist" in resumed.text
    assert "Reachability Song" in resumed.text
    assert "Pick up where you left off" in resumed.text

    brief_after = SongResumeService(reopened.runtime.headquarters).brief()
    assert brief_after.song_id == song_id
    assert brief_after.artist_name == brief_before.artist_name
    assert brief_after.song_title == brief_before.song_title
    assert brief_after.is_active_song is True

    quit_normally(reopened)
