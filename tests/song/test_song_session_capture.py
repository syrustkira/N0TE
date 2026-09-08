from __future__ import annotations

from dataclasses import dataclass, field
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
class Submit:
    name: str
    value: str
    text: str = ""


@dataclass
class Form:
    action: str
    values: dict[str, str]
    buttons: list[Submit] = field(default_factory=list)
    text: str = ""


class Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self.current: Form | None = None
        self.current_button: Submit | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form":
            self.current = Form(str(values.get("action", "")), {})
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and values.get("name"):
            self.current.values[str(values["name"])] = str(values.get("value", ""))
        elif tag == "button" and self.current is not None and values.get("name"):
            self.current_button = Submit(
                name=str(values["name"]),
                value=str(values.get("value", "")),
            )
            self.current.buttons.append(self.current_button)

    def handle_endtag(self, tag: str) -> None:
        if tag == "button":
            self.current_button = None
        elif tag == "form":
            self.current = None
            self.current_button = None

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.text += data
        if self.current_button is not None:
            self.current_button.text += data


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
        headers["Origin"] = shell.address.origin if origin is None else origin
    req = Request(shell.address.origin + path, data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def forms(page: str, action: str) -> list[Form]:
    parser = Parser()
    parser.feed(page)
    return [candidate for candidate in parser.forms if candidate.action == action]


def form(page: str, action: str) -> Form:
    matches = forms(page, action)
    assert len(matches) == 1
    return matches[0]


def capture_payload(page: str, label: str, body: str) -> dict[str, str]:
    capture = form(page, "/session/capture")
    matches = [button for button in capture.buttons if button.text.strip() == label]
    assert len(matches) == 1
    button = matches[0]
    values = dict(capture.values)
    values[button.name] = button.value
    values["body"] = body
    return values


def quit_shell(shell: ConsumerShell) -> None:
    status, settings = request(shell, "/settings")
    assert status == 200
    quit_form = form(settings, "/quit")
    status, closed = request(shell, "/quit", method="POST", fields=quit_form.values)
    assert status == 200
    assert "N0TE closed safely." in closed
    assert shell.wait_stopped(timeout=2.0)


def make_open_session(
    data_root: Path,
    *,
    artist: str,
    song_title: str,
    objective: str,
) -> tuple[str, str, str]:
    hq = HeadquartersMemory.create(data_root, artist)
    try:
        song = hq.store.create_song(song_title)
        session = hq.sessions.start_session(song_id=song.id, objective=objective)
        return hq.store.profile_id, song.id, session.id
    finally:
        hq.close()


def session_items(data_root: Path, profile_id: str, session_id: str):
    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        return hq.sessions.items_for_session(session_id)
    finally:
        hq.close()


def test_all_capture_kinds_are_chronological_durable_and_visible_after_close(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, session_id = make_open_session(
        data_root,
        artist="Capture Artist",
        song_title="Capture Song",
        objective="Build the chorus without losing the useful decisions",
    )
    proc = process(8501, "song-01b-capture-history")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    shell.start()

    expected = [
        ("Observation", "The sparse first half gives the vocal more room"),
        ("Decision", "Keep the chorus drums dry until the final four bars"),
        ("Rejected idea", "Do not double the guitar hook an octave up"),
        ("Unresolved", "Does the last chord need the third in the vocal stack?"),
        ("MARK", "Bar 17 transition feels worth revisiting"),
    ]
    expected_kinds = ["OBSERVATION", "DECISION", "REJECTED_IDEA", "UNRESOLVED", "MARK"]

    for label, body in expected:
        status, page = request(shell, "/song")
        assert status == 200
        assert "Capture what matters" in page
        assert [button.text.strip() for button in form(page, "/session/capture").buttons] == [
            "Observation",
            "Decision",
            "Rejected idea",
            "Unresolved",
            "MARK",
        ]
        payload = capture_payload(page, label, body)
        status, _ = request(shell, "/session/capture", method="POST", fields=payload)
        assert status == 303

    inspect = HeadquartersMemory.open(data_root, profile_id)
    try:
        items = inspect.sessions.items_for_session(session_id)
        assert [item.kind for item in items] == expected_kinds
        assert [item.body for item in items] == [body for _, body in expected]
        assert all(inspect.sessions.promotion_for_item(item.id) is None for item in items)
        assert inspect.store._conn.execute(
            "SELECT COUNT(*) FROM evidence_claims"
        ).fetchone()[0] == 0
        scratch_events = [
            event
            for event in inspect.activity.for_song(song_id)
            if event.event_type == "SESSION_SCRATCH_ADDED"
        ]
        assert len(scratch_events) == len(expected)
    finally:
        inspect.close()

    status, page = request(shell, "/song")
    positions = [page.index(body) for _, body in expected]
    assert positions == sorted(positions)
    assert session_id not in page
    assert all(item.id not in page for item in items)
    assert "sitem_" not in page
    assert "prf_" not in page
    assert "session-promotion:" not in page

    quit_shell(shell)
    reopened = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    reopened.start()
    status, page = request(reopened, "/song")
    assert status == 200
    assert "Current work Session" in page
    assert [page.index(body) for _, body in expected] == sorted(page.index(body) for _, body in expected)

    finish = form(page, "/session/finish")
    finish_values = dict(finish.values)
    finish_values.update(
        {
            "debrief": "The chorus shape is clear and the useful choices are captured.",
            "next_action": "Resolve the last-chord vocal stack before adding production layers",
        }
    )
    status, _ = request(reopened, "/session/finish", method="POST", fields=finish_values)
    assert status == 303
    status, closed_page = request(reopened, "/song")
    assert status == 200
    assert "Last Session history" in closed_page
    assert forms(closed_page, "/session/capture") == []
    assert [closed_page.index(body) for _, body in expected] == sorted(
        closed_page.index(body) for _, body in expected
    )
    quit_shell(reopened)

    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        durable_items = hq.sessions.items_for_session(session_id)
        assert [item.kind for item in durable_items] == expected_kinds
        assert [item.body for item in durable_items] == [body for _, body in expected]
        assert hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0] == 0
    finally:
        hq.close()


def test_capture_action_is_origin_checked_one_shot_and_stale_safe(tmp_path: Path) -> None:
    data_root = (tmp_path / "data-authority").resolve()
    state_root = (tmp_path / "state-authority").resolve()
    profile_id, song_id, session_id = make_open_session(
        data_root,
        artist="Authority Artist",
        song_title="Authority Song",
        objective="Keep capture authority bound to this exact work Session",
    )
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8502, "song-01b-capture-authority"),
        probe=Probe(),
    )
    shell.start()

    status, page = request(shell, "/song")
    assert status == 200
    decision = capture_payload(page, "Decision", "Keep the first chorus short")
    status, _ = request(
        shell,
        "/session/capture",
        method="POST",
        fields=decision,
        origin="https://attacker.example",
    )
    assert status == 403
    assert session_items(data_root, profile_id, session_id) == ()

    status, _ = request(shell, "/session/capture", method="POST", fields=decision)
    assert status == 303
    items = session_items(data_root, profile_id, session_id)
    assert len(items) == 1 and items[0].kind == "DECISION"

    status, _ = request(shell, "/session/capture", method="POST", fields=decision)
    assert status == 409
    assert session_items(data_root, profile_id, session_id) == items

    # A token rendered for Song A cannot append after canonical active Song changes.
    status, page = request(shell, "/song")
    stale_song = capture_payload(page, "Observation", "Must not cross Songs")
    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        second_song = changer.store.create_song("Other Song")
    finally:
        changer.close()
    status, _ = request(shell, "/session/capture", method="POST", fields=stale_song)
    assert status == 409
    assert session_items(data_root, profile_id, session_id) == items
    inspect = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert inspect.sessions.latest_for_song(second_song.id) is None
    finally:
        inspect.close()

    # A token rendered for an open Session cannot append after that Session closes.
    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.store.select_song(song_id)
    finally:
        changer.close()
    status, page = request(shell, "/song")
    stale_closed = capture_payload(page, "MARK", "Must not append after close")
    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.sessions.close_session(
            session_id,
            debrief_summary="Authority red-team complete",
            next_action="Move to the next bounded Song decision",
        )
    finally:
        changer.close()
    status, _ = request(shell, "/session/capture", method="POST", fields=stale_closed)
    assert status == 409
    assert session_items(data_root, profile_id, session_id) == items
    quit_shell(shell)


def test_blank_and_oversized_capture_fail_without_history_or_evidence(tmp_path: Path) -> None:
    data_root = (tmp_path / "data-validation").resolve()
    state_root = (tmp_path / "state-validation").resolve()
    profile_id, _, session_id = make_open_session(
        data_root,
        artist="Validation Artist",
        song_title="Validation Song",
        objective="Reject malformed capture text cleanly",
    )
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8503, "song-01b-capture-validation"),
        probe=Probe(),
    )
    shell.start()

    status, page = request(shell, "/song")
    blank = capture_payload(page, "Observation", "   \n  ")
    status, _ = request(shell, "/session/capture", method="POST", fields=blank)
    assert status == 303
    assert session_items(data_root, profile_id, session_id) == ()
    status, page = request(shell, "/song")
    assert "Session capture must not be empty" in page

    oversized = capture_payload(page, "MARK", "x" * 1201)
    status, _ = request(shell, "/session/capture", method="POST", fields=oversized)
    assert status == 303
    assert session_items(data_root, profile_id, session_id) == ()
    status, page = request(shell, "/song")
    assert "Session capture is too long" in page
    inspect = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert inspect.store._conn.execute(
            "SELECT COUNT(*) FROM evidence_claims"
        ).fetchone()[0] == 0
    finally:
        inspect.close()
    quit_shell(shell)


def test_capture_is_profile_isolated_and_does_not_mutate_execution_or_song_state(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data-isolation").resolve()
    state_root = (tmp_path / "state-isolation").resolve()

    first = HeadquartersMemory.create(data_root, "Artist One")
    try:
        song_one = first.store.create_song("Song One")
        session_one = first.sessions.start_session(song_id=song_one.id, objective="Private work")
        first.sessions.append_scratch(
            session_one.id,
            kind="MARK",
            body="Private Artist One MARK",
        )
    finally:
        first.close()

    second = HeadquartersMemory.create(data_root, "Artist Two")
    try:
        song_two = second.store.create_song("Song Two")
        asset = second.store.attach_asset(
            song_two.id,
            name="source.wav",
            sha256="b" * 64,
            source_uri="file:///source.wav",
        )
        version = second.store.create_version(song_two.id, label="v1", asset_ids=(asset.id,))
        session_two = second.sessions.start_session(
            song_id=song_two.id,
            objective="Capture without changing the project",
        )
        profile_two = second.store.profile_id
    finally:
        second.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8504, "song-01b-capture-isolation"),
        probe=Probe(),
    )
    shell.start()
    status, selection = request(shell, "/")
    assert status == 200
    selection_forms = forms(selection, "/profile/select")
    chosen = next(candidate for candidate in selection_forms if "Artist Two" in candidate.text)
    status, _ = request(shell, "/profile/select", method="POST", fields=chosen.values)
    assert status == 303

    inspect = HeadquartersMemory.open(data_root, profile_two)
    try:
        before_song = inspect.store.get_song(song_two.id)
        before_asset = inspect.store.get_asset(asset.id)
        before_version = inspect.store.get_version(version.id)
        before_evidence = int(inspect.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0])
        before_operations = int(inspect.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0])
        focus = inspect.attention.start_focus("MAKE", song_id=song_two.id)
    finally:
        inspect.close()

    status, page = request(shell, "/song")
    assert status == 200
    assert "Private Artist One MARK" not in page
    payload = capture_payload(page, "Observation", "The current version already has enough low-end weight")
    status, _ = request(shell, "/session/capture", method="POST", fields=payload)
    assert status == 303

    inspect = HeadquartersMemory.open(data_root, profile_two)
    try:
        captured = inspect.sessions.items_for_session(session_two.id)
        assert len(captured) == 1
        assert captured[0].kind == "OBSERVATION"
        assert captured[0].body == "The current version already has enough low-end weight"
        assert inspect.store.get_song(song_two.id) == before_song
        assert inspect.store.get_asset(asset.id) == before_asset
        assert inspect.store.get_version(version.id) == before_version
        assert int(inspect.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0]) == before_evidence
        assert int(inspect.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]) == before_operations
        assert inspect.attention.active_focus() == focus
        assert inspect.sessions.promotion_for_item(captured[0].id) is None
    finally:
        inspect.close()

    status, page = request(shell, "/song")
    assert "Private Artist One MARK" not in page
    assert profile_two not in page
    assert session_two.id not in page
    assert captured[0].id not in page
    quit_shell(shell)
