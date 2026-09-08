from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te.app_runtime import ApplicationRuntime
from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment
from n0te.shell_design import (
    PROHIBITED_PRIMARY_TOKENS,
    REPRESENTATIVE_SHELL_STATES,
    SHELL_CSS,
    ShellStateContract,
)


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
    return [form for form in parser.forms if form.action == action]


def create_profile(root: Path, name: str, song: str | None = None) -> str:
    headquarters = HeadquartersMemory.create(root, name)
    try:
        if song is not None:
            headquarters.store.create_song(song)
        return headquarters.store.profile_id
    finally:
        headquarters.close()


def quit_shell(shell: ConsumerShell) -> None:
    if not shell.is_running:
        return
    status, settings = request(shell, "/settings")
    assert status == 200
    matches = forms(settings, "/quit")
    assert len(matches) == 1
    status, _ = request(
        shell,
        "/quit",
        method="POST",
        fields=matches[0].values,
        origin=shell.address.origin,
    )
    assert status == 200
    assert shell.wait_stopped(timeout=2.0)


def contract(key: str) -> ShellStateContract:
    matches = [item for item in REPRESENTATIVE_SHELL_STATES if item.key == key]
    assert len(matches) == 1
    return matches[0]


def assert_contract(key: str, page: str) -> None:
    item = contract(key)
    assert 'class="skip-link"' in page
    assert '<main id="main" tabindex="-1">' in page
    assert 'aria-labelledby="page-title"' in page
    if item.state_kind.startswith("running-"):
        assert 'aria-label="Headquarters"' in page
        assert 'aria-current="page"' in page
    for text in item.required_text:
        assert text in page
    for action in item.required_actions:
        assert f'action="{action}"' in page
    lowered = page.lower()
    for token in PROHIBITED_PRIMARY_TOKENS:
        assert token.lower() not in lowered


def test_representative_contract_contains_only_current_truthful_states() -> None:
    assert [state.key for state in REPRESENTATIVE_SHELL_STATES] == [
        "first-profile",
        "profile-selection",
        "no-song",
        "active-song",
        "no-focus",
        "active-focus",
        "settings",
        "blocked-ownership",
        "recovery",
    ]
    assert {state.state_kind for state in REPRESENTATIVE_SHELL_STATES} == {
        "create-profile",
        "select-profile",
        "running-no-song",
        "running-home",
        "running-now",
        "running-settings",
        "blocked",
        "recovery",
    }
    fixture_text = " ".join(state.key for state in REPRESENTATIVE_SHELL_STATES).lower()
    for not_yet_real in ("provider-offline", "loading", "release-job", "approval-center"):
        assert not_yet_real not in fixture_text


def test_shell_css_has_explicit_accessibility_and_narrow_layout_contract() -> None:
    assert "--target-min: 44px" in SHELL_CSS
    assert "min-height: 44px" in SHELL_CSS
    assert "@media (prefers-reduced-motion: reduce)" in SHELL_CSS
    assert "@media (prefers-contrast: more)" in SHELL_CSS
    assert "@media (forced-colors: active)" in SHELL_CSS
    assert "grid-auto-columns: minmax(5.5rem, 1fr)" in SHELL_CSS
    assert "overflow-x: auto" in SHELL_CSS
    assert "overflow-wrap: anywhere" in SHELL_CSS
    assert "max-width: 100%" in SHELL_CSS
    assert 'button[aria-pressed="true"]' in SHELL_CSS
    assert "background: var(--color-surface);" in SHELL_CSS
    assert "background-image: none" in SHELL_CSS


def test_real_http_representative_shell_states_follow_contract(tmp_path: Path) -> None:
    # First profile plus the real canonical CSS endpoint.
    first_data = (tmp_path / "first-data").resolve()
    first_state = (tmp_path / "first-state").resolve()
    first = ConsumerShell(
        data_root=first_data,
        state_root=first_state,
        process=process(8301, "ux01c-first"),
        probe=Probe(),
    )
    first.start()
    status, page = request(first, "/")
    assert status == 200
    assert_contract("first-profile", page)
    status, css = request(first, "/assets/shell.css")
    assert status == 200
    assert css == SHELL_CSS
    first.stop()

    # Profile selection.
    select_data = (tmp_path / "select-data").resolve()
    select_state = (tmp_path / "select-state").resolve()
    create_profile(select_data, "Long Artist Alpha " + "A" * 64)
    create_profile(select_data, "Long Artist Beta " + "B" * 64)
    selector = ConsumerShell(
        data_root=select_data,
        state_root=select_state,
        process=process(8302, "ux01c-select"),
        probe=Probe(),
    )
    selector.start()
    status, page = request(selector, "/")
    assert status == 200
    assert_contract("profile-selection", page)
    selector.stop()

    # One Artist: no Song -> active Song -> no Focus -> active Focus -> Settings.
    running_data = (tmp_path / "running-data").resolve()
    running_state = (tmp_path / "running-state").resolve()
    create_profile(running_data, "Responsive Artist " + "R" * 72)
    running = ConsumerShell(
        data_root=running_data,
        state_root=running_state,
        process=process(8303, "ux01c-running"),
        probe=Probe(),
    )
    running.start()
    status, no_song = request(running, "/")
    assert status == 200
    assert_contract("no-song", no_song)

    status, song_page = request(running, "/song")
    assert status == 200
    start_forms = forms(song_page, "/song/start")
    assert len(start_forms) == 1
    fields = dict(start_forms[0].values)
    fields["song_title"] = "A Very Long Durable Song Title " + "S" * 120
    status, _ = request(
        running,
        "/song/start",
        method="POST",
        fields=fields,
        origin=running.address.origin,
    )
    assert status == 303

    status, active_song = request(running, "/")
    assert status == 200
    assert_contract("active-song", active_song)
    assert "A Very Long Durable Song Title" in active_song

    status, no_focus = request(running, "/now")
    assert status == 200
    assert_contract("no-focus", no_focus)
    assert no_focus.count('aria-pressed="false"') == 5
    assert 'aria-pressed="true"' not in no_focus

    make_forms = forms(no_focus, "/focus/set")
    assert len(make_forms) == 5
    make = next(candidate for candidate in make_forms if "Make" in candidate.text)
    status, _ = request(
        running,
        "/focus/set",
        method="POST",
        fields=make.values,
        origin=running.address.origin,
    )
    assert status == 303

    status, active_focus = request(running, "/now")
    assert status == 200
    assert_contract("active-focus", active_focus)
    assert active_focus.count('aria-pressed="true"') == 1
    assert active_focus.count('aria-pressed="false"') == 4
    assert "Make Focus active" in active_focus

    status, settings = request(running, "/settings")
    assert status == 200
    assert_contract("settings", settings)
    assert "Make Focus" in settings
    quit_shell(running)

    # Blocked ownership.
    blocked_data = (tmp_path / "blocked-data").resolve()
    blocked_state = (tmp_path / "blocked-state").resolve()
    create_profile(blocked_data, "Free Artist", "Free Song")
    blocked_profile = create_profile(blocked_data, "Busy Artist", "Busy Song")
    probe = Probe()
    owner = process(8304, "ux01c-owner")
    probe.set(owner, "ALIVE")
    owner_runtime = ApplicationRuntime(data_root=blocked_data, state_root=blocked_state)
    assert owner_runtime.launch(
        profile_id=blocked_profile,
        process=owner,
        probe=probe,
    ).status == "STARTED"
    blocked_shell = ConsumerShell(
        data_root=blocked_data,
        state_root=blocked_state,
        process=process(8305, "ux01c-blocked"),
        probe=probe,
    )
    blocked_shell.start()
    status, selection = request(blocked_shell, "/")
    assert status == 200
    choices = forms(selection, "/profile/select")
    assert len(choices) == 2
    busy = next(candidate for candidate in choices if "Busy Artist" in candidate.text)
    status, _ = request(
        blocked_shell,
        "/profile/select",
        method="POST",
        fields=busy.values,
        origin=blocked_shell.address.origin,
    )
    assert status == 303
    status, blocked = request(blocked_shell, "/")
    assert status == 200
    assert_contract("blocked-ownership", blocked)
    blocked_shell.stop()
    assert owner_runtime.quit().status == "STOPPED"

    # Recovery.
    recovery_data = (tmp_path / "recovery-data").resolve()
    recovery_state = (tmp_path / "recovery-state").resolve()
    bad_profile = "prf_" + "a" * 32
    (recovery_data / "profiles" / bad_profile).mkdir(parents=True)
    recovery = ConsumerShell(
        data_root=recovery_data,
        state_root=recovery_state,
        process=process(8306, "ux01c-recovery"),
        probe=Probe(),
    )
    recovery.start()
    status, page = request(recovery, "/")
    assert status == 200
    assert_contract("recovery", page)
    recovery.stop()
