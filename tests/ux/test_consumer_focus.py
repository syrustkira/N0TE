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


def post_form(shell: ConsumerShell, form: Form, *, origin: str | None = None) -> tuple[int, str]:
    return request(
        shell,
        form.action,
        method="POST",
        fields=dict(form.values),
        origin=shell.address.origin if origin is None else origin,
    )


def choose_focus(page: str, label: str) -> Form:
    matches = [form for form in forms(page, "/focus/set") if label in form.text]
    assert len(matches) == 1
    return matches[0]


def quit_shell(shell: ConsumerShell) -> None:
    status, settings = request(shell, "/settings")
    assert status == 200
    quit_forms = forms(settings, "/quit")
    assert len(quit_forms) == 1
    status, closed = post_form(shell, quit_forms[0])
    assert status == 200
    assert "N0TE closed safely." in closed
    assert shell.wait_stopped(timeout=2.0)


def profile_with_song(root: Path, artist: str, song: str) -> tuple[str, str]:
    headquarters = HeadquartersMemory.create(root, artist)
    try:
        created = headquarters.store.create_song(song)
        return headquarters.store.profile_id, created.id
    finally:
        headquarters.close()


def focus_state(data_root: Path, profile_id: str):
    headquarters = HeadquartersMemory.open(data_root, profile_id)
    try:
        return headquarters.attention.active_focus(), headquarters.attention.history()
    finally:
        headquarters.close()


def test_consumer_can_set_switch_end_and_relaunch_durable_focus(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id = profile_with_song(data_root, "Focus Artist", "Focus Song")
    proc = process(9101, "ux01b-focus")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    shell.start()

    status, now = request(shell, "/now")
    assert status == 200
    assert "No Focus Session active" in now
    assert all(label in now for label in ("Make", "Finish", "Manage", "Release", "Perform"))
    make_form = choose_focus(now, "Make")

    # The server-bound action token owns the mode. Extra client fields cannot
    # rewrite Make into another mode.
    make_form.values["mode"] = "PERFORM"
    status, _ = post_form(shell, make_form)
    assert status == 303

    status, focused = request(shell, "/now")
    assert status == 200
    assert "Make Focus active" in focused
    assert "Focused Song:" in focused
    assert "Focus Song" in focused
    assert "focus_" not in focused
    active, _ = focus_state(data_root, profile_id)
    assert active is not None
    assert active.mode == "MAKE"
    assert active.song_id == song_id

    for path in ("/", "/song", "/settings"):
        status, page = request(shell, path)
        assert status == 200
        assert "Make Focus" in page
        assert "focus_" not in page

    status, now = request(shell, "/now")
    manage_form = choose_focus(now, "Manage")
    status, _ = post_form(shell, manage_form)
    assert status == 303
    active, history = focus_state(data_root, profile_id)
    assert active is not None
    assert active.mode == "MANAGE"
    assert active.song_id is None
    assert len(history) == 2
    assert history[0].mode == "MAKE"
    assert history[0].state == "ENDED"
    assert history[0].end_reason == "SWITCHED"

    quit_shell(shell)

    relaunched = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    relaunched.start()
    status, resumed = request(relaunched, "/now")
    assert status == 200
    assert relaunched.runtime.profile_id == profile_id
    assert "Manage Focus active" in resumed
    assert "Scope: your Artist Headquarters." in resumed

    end_forms = forms(resumed, "/focus/end")
    assert len(end_forms) == 1
    status, _ = post_form(relaunched, end_forms[0])
    assert status == 303
    active, _ = focus_state(data_root, profile_id)
    assert active is None
    status, open_now = request(relaunched, "/now")
    assert "No Focus Session active" in open_now
    assert "Manage Focus" not in open_now
    quit_shell(relaunched)


def test_focus_write_rejects_foreign_origin_and_one_shot_replay(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _ = profile_with_song(data_root, "Authority Artist", "Authority Song")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9102, "ux01b-authority"),
        probe=Probe(),
    )
    shell.start()

    _, now = request(shell, "/now")
    finish_form = choose_focus(now, "Finish")

    # Isolate the Origin gate from request-body transport. The shell rejects
    # foreign Origin before parsing form data, so sending an attack body is not
    # required to prove the security boundary. On Windows, rejecting before an
    # unread body is drained can surface as a connection reset in urllib even
    # though the server failed closed. Assert the actual origin-specific 403
    # response without weakening the security expectation.
    status, rejected = request(
        shell,
        finish_form.action,
        method="POST",
        origin="https://attacker.example",
    )
    assert status == 403
    assert "That action did not come from this N0TE window." in rejected
    active, _ = focus_state(data_root, profile_id)
    assert active is None

    status, _ = post_form(shell, finish_form)
    assert status == 303
    status, _ = post_form(shell, finish_form)
    assert status == 409
    _, history = focus_state(data_root, profile_id)
    assert len(history) == 1
    assert history[0].mode == "FINISH"
    quit_shell(shell)


def test_focus_history_isolated_between_artist_profiles(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_a, song_a = profile_with_song(data_root, "Focused Alpha", "Alpha Song")
    profile_b, _ = profile_with_song(data_root, "Quiet Beta", "Beta Song")

    alpha = HeadquartersMemory.open(data_root, profile_a)
    try:
        alpha.attention.start_focus("MAKE", song_id=song_a)
    finally:
        alpha.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9103, "ux01b-isolation"),
        probe=Probe(),
    )
    shell.start()
    status, selection = request(shell, "/")
    assert status == 200
    choices = forms(selection, "/profile/select")
    beta = next(form for form in choices if "Quiet Beta" in form.text)
    status, _ = post_form(shell, beta)
    assert status == 303
    assert shell.runtime.profile_id == profile_b

    status, now = request(shell, "/now")
    assert status == 200
    assert "No Focus Session active" in now
    assert "Make Focus" not in now
    assert "Alpha Song" not in now
    _, history = focus_state(data_root, profile_b)
    assert history == ()
    quit_shell(shell)
