from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pytest

from n0te.app_runtime import ApplicationRuntime
from n0te.consumer_shell import ConsumerShell
from n0te.instance import InstanceLeaseManager, ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment


class Probe:
    def __init__(self, default: str = "UNKNOWN") -> None:
        self.default = default
        self.values: dict[str, str] = {}

    def set(self, process: ProcessIdentity, state: str) -> None:
        self.values[process.fingerprint] = state

    def status(self, process: ProcessIdentity) -> str:
        return self.values.get(process.fingerprint, self.default)


def process(pid: int, token: str) -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=token,
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(frozen=True)
class WebResponse:
    status: int
    headers: dict[str, str]
    text: str


@dataclass
class ParsedForm:
    action: str
    method: str
    values: dict[str, str]
    text: str = ""


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[ParsedForm] = []
        self._current: ParsedForm | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form":
            self._current = ParsedForm(
                action=str(values.get("action", "")),
                method=str(values.get("method", "get")).lower(),
                values={},
            )
            self.forms.append(self._current)
            return
        if tag == "input" and self._current is not None:
            name = values.get("name")
            if name:
                self._current.values[str(name)] = str(values.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.text += data


def forms(text: str, action: str | None = None) -> list[ParsedForm]:
    parser = FormParser()
    parser.feed(text)
    if action is None:
        return parser.forms
    return [form for form in parser.forms if form.action == action]


def request(
    shell: ConsumerShell,
    path: str,
    *,
    method: str = "GET",
    fields: dict[str, str] | None = None,
    origin: str | None = None,
    host: str | None = None,
) -> WebResponse:
    headers: dict[str, str] = {}
    payload = None
    if fields is not None:
        payload = urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    req = Request(shell.address.origin + path, data=payload, headers=headers, method=method)
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=2.0) as response:
            body = response.read().decode("utf-8")
            return WebResponse(response.status, dict(response.headers.items()), body)
    except HTTPError as exc:
        return WebResponse(exc.code, dict(exc.headers.items()), exc.read().decode("utf-8"))


def create_profile(data_root: Path, artist_name: str, song_title: str | None = None) -> str:
    headquarters = HeadquartersMemory.create(data_root, artist_name)
    try:
        if song_title is not None:
            headquarters.store.create_song(song_title)
        return headquarters.store.profile_id
    finally:
        headquarters.close()


def form_fields(form: ParsedForm, **extra: str) -> dict[str, str]:
    result = dict(form.values)
    result.update(extra)
    return result


def quit_shell(shell: ConsumerShell) -> WebResponse:
    settings = request(shell, "/settings")
    assert settings.status == 200
    quit_forms = forms(settings.text, "/quit")
    assert len(quit_forms) == 1
    response = request(
        shell,
        "/quit",
        method="POST",
        fields=form_fields(quit_forms[0]),
        origin=shell.address.origin,
    )
    assert shell.wait_stopped(timeout=2.0)
    return response


def test_fresh_consumer_creates_artist_starts_song_navigates_quits_and_reopens(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    probe = Probe()
    proc = process(8101, "ux01a-fresh")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=probe,
    )
    address = shell.start()
    assert address.host == "127.0.0.1"

    welcome = request(shell, "/")
    assert welcome.status == 200
    assert "Welcome to your Headquarters" in welcome.text
    assert "Artist name" in welcome.text
    assert "Open my Headquarters" in welcome.text
    assert "You do not need to connect a DAW, AI provider, account, or service to begin." in welcome.text
    assert "prf_" not in welcome.text
    create_forms = forms(welcome.text, "/profile/create")
    assert len(create_forms) == 1

    created = request(
        shell,
        "/profile/create",
        method="POST",
        fields=form_fields(create_forms[0], artist_name="UX Test Artist"),
        origin=shell.address.origin,
    )
    assert created.status == 303
    home = request(shell, "/")
    assert home.status == 200
    assert "UX Test Artist" in home.text
    assert "What are we making today?" in home.text
    assert "Start a Song" in home.text
    assert "prf_" not in home.text

    song_page = request(shell, "/song")
    start_forms = forms(song_page.text, "/song/start")
    assert len(start_forms) == 1
    started = request(
        shell,
        "/song/start",
        method="POST",
        fields=form_fields(start_forms[0], song_title="First Durable Song"),
        origin=shell.address.origin,
    )
    assert started.status == 303

    song = request(shell, "/song")
    assert "First Durable Song" in song.text
    assert "Active Song" in song.text
    now = request(shell, "/now")
    settings = request(shell, "/settings")
    home_again = request(shell, "/")
    for page in (now, settings, home_again):
        assert page.status == 200
        assert "UX Test Artist" in page.text
        assert "First Durable Song" in page.text
        assert "prf_" not in page.text
    assert 'aria-label="Headquarters"' in home_again.text

    profile_id = shell.runtime.profile_id
    assert profile_id is not None
    assert InstanceLeaseManager(state_root).inspect(profile_id) is not None
    closed = quit_shell(shell)
    assert closed.status == 200
    assert "N0TE closed safely." in closed.text
    assert "<style>" not in closed.text
    assert InstanceLeaseManager(state_root).inspect(profile_id) is None

    reopened = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=probe,
    )
    reopened.start()
    resumed = request(reopened, "/")
    assert resumed.status == 200
    assert "UX Test Artist" in resumed.text
    assert "First Durable Song" in resumed.text
    assert "Pick up where you left off" in resumed.text
    quit_shell(reopened)


def test_multiple_profiles_stay_isolated_and_raw_profile_ids_never_render(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_a = create_profile(data_root, "Artist Alpha", "Alpha Song")
    profile_b = create_profile(data_root, "Artist Beta", "Beta Song")

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8102, "ux01a-isolation"),
        probe=Probe(),
    )
    shell.start()
    selection = request(shell, "/")
    assert "Who are you working as today?" in selection.text
    assert "Artist Alpha" in selection.text
    assert "Artist Beta" in selection.text
    assert profile_a not in selection.text
    assert profile_b not in selection.text
    assert "Alpha Song" not in selection.text
    assert "Beta Song" not in selection.text
    assert "prf_" not in selection.text

    choices = forms(selection.text, "/profile/select")
    beta = next(form for form in choices if "Artist Beta" in form.text)
    chosen = request(
        shell,
        "/profile/select",
        method="POST",
        fields=form_fields(beta),
        origin=shell.address.origin,
    )
    assert chosen.status == 303
    home = request(shell, "/")
    assert "Artist Beta" in home.text
    assert "Beta Song" in home.text
    assert "Artist Alpha" not in home.text
    assert "Alpha Song" not in home.text
    assert shell.runtime.profile_id == profile_b
    quit_shell(shell)


def test_foreign_origin_and_wrong_host_cannot_gain_local_write_authority(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8103, "ux01a-origin"),
        probe=Probe(),
    )
    shell.start()

    wrong_host = request(shell, "/", host="localhost:9999")
    assert wrong_host.status == 421

    welcome = request(shell, "/")
    create_form = forms(welcome.text, "/profile/create")[0]
    rejected = request(
        shell,
        "/profile/create",
        method="POST",
        fields=form_fields(create_form, artist_name="Should Not Exist"),
        origin="https://attacker.example",
    )
    assert rejected.status == 403
    assert shell.profiles.discover().profiles == ()

    accepted = request(
        shell,
        "/profile/create",
        method="POST",
        fields=form_fields(create_form, artist_name="Local Artist"),
        origin=shell.address.origin,
    )
    assert accepted.status == 303
    quit_shell(shell)


def test_one_time_song_action_prevents_duplicate_consequential_submit(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id = create_profile(data_root, "Single Submit Artist")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8104, "ux01a-once"),
        probe=Probe(),
    )
    shell.start()
    song_page = request(shell, "/song")
    start = forms(song_page.text, "/song/start")[0]
    payload = form_fields(start, song_title="Only Once")

    first = request(
        shell,
        "/song/start",
        method="POST",
        fields=payload,
        origin=shell.address.origin,
    )
    second = request(
        shell,
        "/song/start",
        method="POST",
        fields=payload,
        origin=shell.address.origin,
    )
    assert first.status == 303
    assert second.status == 409
    quit_shell(shell)

    db = data_root / "profiles" / profile_id / "lineage.sqlite3"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 1
        assert conn.execute("SELECT title FROM songs").fetchone()[0] == "Only Once"
    finally:
        conn.close()


def test_blocked_profile_selection_is_rendered_before_any_retry(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    create_profile(data_root, "Free Artist", "Free Song")
    blocked_profile = create_profile(data_root, "Busy Artist", "Busy Song")
    probe = Probe()
    owner = process(8105, "ux01a-live-owner")
    shell_process = process(8106, "ux01a-blocked-shell")
    probe.set(owner, "ALIVE")

    owner_runtime = ApplicationRuntime(data_root=data_root, state_root=state_root)
    assert owner_runtime.launch(
        profile_id=blocked_profile,
        process=owner,
        probe=probe,
    ).status == "STARTED"

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=shell_process,
        probe=probe,
    )
    shell.start()
    selection = request(shell, "/")
    busy = next(form for form in forms(selection.text, "/profile/select") if "Busy Artist" in form.text)
    post = request(
        shell,
        "/profile/select",
        method="POST",
        fields=form_fields(busy),
        origin=shell.address.origin,
    )
    assert post.status == 303

    # If the redirect lost the blocked result, this DEAD flip would make a retry
    # replace the stale owner immediately instead of first showing the consumer
    # the exact result of the action they just took.
    probe.set(owner, "DEAD")
    blocked = request(shell, "/")
    assert "This Artist is already open" in blocked.text
    current = InstanceLeaseManager(state_root).inspect(blocked_profile)
    assert current is not None
    assert current.process.fingerprint == owner.fingerprint

    shell.stop()
    probe.set(owner, "ALIVE")
    assert owner_runtime.quit().status == "STOPPED"


def test_corrupt_profile_catalog_uses_consumer_recovery_language_without_identity_leak(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    bad_profile = "prf_" + "a" * 32
    (data_root / "profiles" / bad_profile).mkdir(parents=True)
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8107, "ux01a-recovery"),
        probe=Probe(),
    )
    shell.start()
    page = request(shell, "/")
    assert page.status == 200
    assert "A local Artist workspace needs recovery" in page.text
    assert "Nothing was overwritten" in page.text
    assert bad_profile not in page.text
    assert "sqlite" not in page.text.lower()
    assert "traceback" not in page.text.lower()
    shell.stop()


def test_security_and_accessibility_contract_is_present_on_real_shell_response(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    create_profile(data_root, "Accessible Artist")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8108, "ux01a-a11y"),
        probe=Probe(),
    )
    shell.start()

    page = request(shell, "/")
    assert page.status == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert not any(key.lower() == "access-control-allow-origin" for key in page.headers)
    assert 'class="skip-link"' in page.text
    assert '<main id="main" tabindex="-1">' in page.text
    assert 'aria-label="Headquarters"' in page.text
    assert 'aria-current="page"' in page.text

    css = request(shell, "/assets/shell.css")
    assert css.status == 200
    assert ":focus-visible" in css.text
    assert "prefers-reduced-motion" in css.text
    assert "min-height: 44px" in css.text
    assert "@media (max-width: 760px)" in css.text
    quit_shell(shell)


@pytest.mark.parametrize("path", ["/?debug=1", "/song?raw=1", "/unknown"])
def test_noncanonical_shell_paths_do_not_open_debug_or_internal_surfaces(
    tmp_path: Path,
    path: str,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    create_profile(data_root, "Path Artist")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(8200 + len(path), "ux01a-path-" + path),
        probe=Probe(),
    )
    shell.start()
    page = request(shell, path)
    assert page.status == 404
    assert "That N0TE page is not available." in page.text
    quit_shell(shell)
