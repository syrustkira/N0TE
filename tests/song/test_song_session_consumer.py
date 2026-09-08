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
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
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


def latest_session(data_root: Path, profile_id: str, song_id: str):
    headquarters = HeadquartersMemory.open(data_root, profile_id)
    try:
        return headquarters.sessions.latest_for_song(song_id)
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


def test_song_work_session_survives_quit_and_resumes_next_action(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id = create_profile(data_root, "Session Artist", "Session Song")
    proc = process(8401, "song-session-resume")

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    shell.start()
    status, song_page = request(shell, "/song")
    assert status == 200
    assert "Start this work Session" in song_page
    assert "What are you trying to accomplish?" in song_page
    assert "sess_" not in song_page
    start = forms(song_page, "/session/start")
    assert len(start) == 1

    payload = dict(start[0].values)
    payload["objective"] = "Finish the chorus arrangement without mixing"
    status, _ = request(
        shell,
        "/session/start",
        method="POST",
        fields=payload,
        origin=shell.address.origin,
    )
    assert status == 303
    opened = latest_session(data_root, profile_id, song_id)
    assert opened is not None
    assert opened.state == "OPEN"
    assert opened.objective == "Finish the chorus arrangement without mixing"

    status, open_page = request(shell, "/song")
    assert status == 200
    assert "Current work Session" in open_page
    assert "Finish the chorus arrangement without mixing" in open_page
    assert "What changed or became clear?" in open_page
    assert "What should you do next?" in open_page
    assert opened.id not in open_page
    finish = forms(open_page, "/session/finish")
    assert len(finish) == 1

    # An open Session is canonical Song state, not browser state.
    quit_shell(shell)
    reopened = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    reopened.start()
    status, resumed_open = request(reopened, "/song")
    assert status == 200
    assert "Current work Session" in resumed_open
    assert "Finish the chorus arrangement without mixing" in resumed_open
    finish = forms(resumed_open, "/session/finish")
    assert len(finish) == 1

    finish_payload = dict(finish[0].values)
    finish_payload.update(
        {
            "debrief": "Chorus is arranged; the second half needs a stronger counterline.",
            "next_action": "Write the chorus counterline before touching the mix",
        }
    )
    status, _ = request(
        reopened,
        "/session/finish",
        method="POST",
        fields=finish_payload,
        origin=reopened.address.origin,
    )
    assert status == 303
    closed = latest_session(data_root, profile_id, song_id)
    assert closed is not None
    assert closed.state == "CLOSED"
    assert closed.debrief_summary == "Chorus is arranged; the second half needs a stronger counterline."
    assert closed.next_action == "Write the chorus counterline before touching the mix"

    status, closed_page = request(reopened, "/song")
    assert status == 200
    assert "Pick up the thread" in closed_page
    assert "Chorus is arranged; the second half needs a stronger counterline." in closed_page
    assert "Write the chorus counterline before touching the mix" in closed_page
    assert "Start work Session" in closed_page
    assert closed.id not in closed_page

    # Closing a Session does not silently promote its next action into canonical evidence.
    db = data_root / "profiles" / profile_id / "lineage.sqlite3"
    conn = sqlite3.connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM evidence_claims WHERE key='next.action'"
        ).fetchone()[0]
        assert count == 0
    finally:
        conn.close()

    quit_shell(reopened)
    again = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    again.start()
    status, resumed_closed = request(again, "/song")
    assert status == 200
    assert "Pick up the thread" in resumed_closed
    assert "Write the chorus counterline before touching the mix" in resumed_closed
    quit_shell(again)


def test_session_actions_are_one_shot_origin_checked_and_do_not_duplicate(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id = create_profile(data_root, "Authority Artist", "Authority Song")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8402, "song-session-authority"),
        probe=Probe(),
    )
    shell.start()

    status, page = request(shell, "/song")
    assert status == 200
    start = forms(page, "/session/start")[0]
    payload = dict(start.values)
    payload["objective"] = "Track the verse vocal"

    rejected, _ = request(
        shell,
        "/session/start",
        method="POST",
        fields=payload,
        origin="https://attacker.example",
    )
    assert rejected == 403
    assert latest_session(data_root, profile_id, song_id) is None

    # Origin rejection occurs before action consumption, so the local action still works once.
    accepted, _ = request(
        shell,
        "/session/start",
        method="POST",
        fields=payload,
        origin=shell.address.origin,
    )
    assert accepted == 303
    first = latest_session(data_root, profile_id, song_id)
    assert first is not None and first.state == "OPEN"

    replay, _ = request(
        shell,
        "/session/start",
        method="POST",
        fields=payload,
        origin=shell.address.origin,
    )
    assert replay == 409
    assert latest_session(data_root, profile_id, song_id) == first

    status, open_page = request(shell, "/song")
    finish = forms(open_page, "/session/finish")[0]
    finish_payload = dict(finish.values)
    finish_payload.update({"debrief": "Verse vocal tracked", "next_action": "Comp the verse"})
    accepted, _ = request(
        shell,
        "/session/finish",
        method="POST",
        fields=finish_payload,
        origin=shell.address.origin,
    )
    assert accepted == 303
    closed = latest_session(data_root, profile_id, song_id)
    assert closed is not None and closed.state == "CLOSED"

    replay, _ = request(
        shell,
        "/session/finish",
        method="POST",
        fields=finish_payload,
        origin=shell.address.origin,
    )
    assert replay == 409
    assert latest_session(data_root, profile_id, song_id) == closed
    quit_shell(shell)


def test_blank_oversized_and_stale_session_actions_fail_without_history_corruption(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, first_song_id = create_profile(data_root, "Validation Artist", "First Song")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8403, "song-session-validation"),
        probe=Probe(),
    )
    shell.start()

    status, page = request(shell, "/song")
    start = forms(page, "/session/start")[0]
    blank = dict(start.values)
    blank["objective"] = "   "
    status, _ = request(
        shell,
        "/session/start",
        method="POST",
        fields=blank,
        origin=shell.address.origin,
    )
    assert status == 303
    assert latest_session(data_root, profile_id, first_song_id) is None

    status, page = request(shell, "/song")
    start = forms(page, "/session/start")[0]
    oversized = dict(start.values)
    oversized["objective"] = "x" * 501
    status, _ = request(
        shell,
        "/session/start",
        method="POST",
        fields=oversized,
        origin=shell.address.origin,
    )
    assert status == 303
    assert latest_session(data_root, profile_id, first_song_id) is None

    # A server-bound action for Song A becomes stale if canonical active Song changes.
    status, page = request(shell, "/song")
    stale_start = forms(page, "/session/start")[0]
    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        second_song = changer.store.create_song("Second Song")
    finally:
        changer.close()
    stale_payload = dict(stale_start.values)
    stale_payload["objective"] = "This must not land on either Song"
    status, _ = request(
        shell,
        "/session/start",
        method="POST",
        fields=stale_payload,
        origin=shell.address.origin,
    )
    assert status == 409
    assert latest_session(data_root, profile_id, first_song_id) is None
    assert latest_session(data_root, profile_id, second_song.id) is None

    status, second_page = request(shell, "/song")
    fresh = forms(second_page, "/session/start")[0]
    payload = dict(fresh.values)
    payload["objective"] = "Arrange the bridge"
    status, _ = request(
        shell,
        "/session/start",
        method="POST",
        fields=payload,
        origin=shell.address.origin,
    )
    assert status == 303
    current = latest_session(data_root, profile_id, second_song.id)
    assert current is not None and current.state == "OPEN"

    status, open_page = request(shell, "/song")
    finish = forms(open_page, "/session/finish")[0]
    invalid_finish = dict(finish.values)
    invalid_finish.update({"debrief": "", "next_action": "Continue"})
    status, _ = request(
        shell,
        "/session/finish",
        method="POST",
        fields=invalid_finish,
        origin=shell.address.origin,
    )
    assert status == 303
    still_open = latest_session(data_root, profile_id, second_song.id)
    assert still_open == current
    quit_shell(shell)


def test_session_context_is_isolated_between_artist_profiles(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()

    first = HeadquartersMemory.create(data_root, "Artist One")
    try:
        song_one = first.store.create_song("One Song")
        first.sessions.start_session(song_id=song_one.id, objective="Private Artist One objective")
    finally:
        first.close()

    second = HeadquartersMemory.create(data_root, "Artist Two")
    try:
        second.store.create_song("Two Song")
    finally:
        second.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8404, "song-session-isolation"),
        probe=Probe(),
    )
    shell.start()
    status, selection = request(shell, "/")
    assert status == 200
    choices = forms(selection, "/profile/select")
    chosen = next(candidate for candidate in choices if "Artist Two" in candidate.text)
    status, _ = request(
        shell,
        "/profile/select",
        method="POST",
        fields=chosen.values,
        origin=shell.address.origin,
    )
    assert status == 303

    status, page = request(shell, "/song")
    assert status == 200
    assert "Two Song" in page
    assert "Private Artist One objective" not in page
    assert "Start this work Session" in page
    assert "sess_" not in page
    assert "prf_" not in page
    quit_shell(shell)


def test_session_textareas_inherit_shell_accessibility_contract(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    create_profile(data_root, "Textarea Artist", "Textarea Song")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8405, "song-session-css"),
        probe=Probe(),
    )
    shell.start()
    status, css = request(shell, "/assets/shell.css")
    assert status == 200
    assert 'input[type="text"], textarea' in css
    assert "textarea:focus-visible" in css
    assert "textarea { resize: vertical" in css
    quit_shell(shell)
