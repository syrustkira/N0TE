from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te.consumer_shell import ConsumerShell
from n0te.instance import InstanceLeaseManager, ProcessIdentity
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


@dataclass(frozen=True)
class Response:
    status: int
    text: str


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
) -> Response:
    headers: dict[str, str] = {}
    payload = None
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


def form(page: str, action: str) -> Form:
    parser = Parser()
    parser.feed(page)
    matches = [item for item in parser.forms if item.action == action]
    assert len(matches) == 1
    return matches[0]


def create_profile(data_root: Path, artist_name: str) -> str:
    headquarters = HeadquartersMemory.create(data_root, artist_name)
    try:
        return headquarters.store.profile_id
    finally:
        headquarters.close()


def test_failed_quit_stays_protected_and_exposes_safe_cleanup_retry(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id = create_profile(data_root, "Recovery Artist")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8301, "ux01a-recovery-retry"),
        probe=Probe(),
    )
    shell.start()
    assert request(shell, "/").status == 200
    assert shell.runtime.profile_id == profile_id

    headquarters = shell.runtime.headquarters
    original_close = headquarters.close
    attempts = {"count": 0}

    def fail_once() -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("simulated close failure")
        original_close()

    monkeypatch.setattr(headquarters, "close", fail_once)

    settings = request(shell, "/settings")
    quit_form = form(settings.text, "/quit")
    first = request(shell, "/quit", method="POST", fields=dict(quit_form.values))
    assert first.status == 503
    assert "N0TE could not finish quitting safely" in first.text
    assert "N0TE closed safely." not in first.text
    assert shell.runtime.state == "RECOVERY_REQUIRED"
    assert shell.is_running
    lease = InstanceLeaseManager(state_root).inspect(profile_id)
    assert lease is not None

    recovery = request(shell, "/")
    assert recovery.status == 200
    assert "Your Artist workspace needs recovery" in recovery.text
    assert "Retry safe close" in recovery.text
    retry_form = form(recovery.text, "/quit")

    second = request(shell, "/quit", method="POST", fields=dict(retry_form.values))
    assert second.status == 200
    assert "N0TE closed safely." in second.text
    assert shell.wait_stopped(timeout=2.0)
    assert shell.runtime.state == "STOPPED"
    assert InstanceLeaseManager(state_root).inspect(profile_id) is None
    assert attempts["count"] == 2


def test_artist_and_song_names_are_rendered_as_text_not_markup(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8302, "ux01a-escaped-text"),
        probe=Probe(),
    )
    shell.start()

    welcome = request(shell, "/")
    create = form(welcome.text, "/profile/create")
    artist_name = '<img src=x onerror="alert(1)"> Artist'
    create.values["artist_name"] = artist_name
    assert request(shell, "/profile/create", method="POST", fields=create.values).status == 303

    home = request(shell, "/")
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; Artist" in home.text
    assert "<img src=x" not in home.text

    song_page = request(shell, "/song")
    start = form(song_page.text, "/song/start")
    song_name = "<script>alert(2)</script> Song"
    start.values["song_title"] = song_name
    assert request(shell, "/song/start", method="POST", fields=start.values).status == 303

    song = request(shell, "/song")
    assert "&lt;script&gt;alert(2)&lt;/script&gt; Song" in song.text
    assert "<script>alert(2)</script>" not in song.text

    settings = request(shell, "/settings")
    quit_form = form(settings.text, "/quit")
    assert request(shell, "/quit", method="POST", fields=quit_form.values).status == 200
    assert shell.wait_stopped(timeout=2.0)
