from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te.app_runtime import LaunchResult
from n0te.consumer_shell import ConsumerShell
from n0te.instance import InstanceLeaseManager, ProcessIdentity
from n0te.platforms import PlatformEnvironment


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Windows", "x86_64"),
        pid=99101,
        start_token="profile-create-response",
    )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


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


def request(
    shell: ConsumerShell,
    path: str,
    *,
    method: str = "GET",
    values: dict[str, str] | None = None,
) -> tuple[int, str]:
    payload = None if values is None else urlencode(values).encode("utf-8")
    headers = {}
    if values is not None:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": shell.address.origin,
        }
    req = Request(shell.address.origin + path, data=payload, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def create_form(page: str) -> Form:
    parser = FormParser()
    parser.feed(page)
    matches = [form for form in parser.forms if form.action == "/profile/create"]
    assert len(matches) == 1
    return matches[0]


def quit_shell(shell: ConsumerShell) -> None:
    status, settings = request(shell, "/settings")
    assert status == 200
    parser = FormParser()
    parser.feed(settings)
    forms = [form for form in parser.forms if form.action == "/quit"]
    assert len(forms) == 1
    status, closed = request(shell, "/quit", method="POST", values=forms[0].values)
    assert status == 200
    assert "N0TE closed safely." in closed
    assert shell.wait_stopped(timeout=2.0)


def test_profile_create_redirects_before_runtime_open_and_launches_on_followup_get(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(),
        probe=Probe(),
    )
    shell.start()

    status, welcome = request(shell, "/")
    assert status == 200
    form = create_form(welcome)
    form.values["artist_name"] = "Redirect First Artist"

    real_launch = shell.runtime.launch
    launch_allowed = False
    launch_calls: list[str] = []

    def guarded_launch(*, profile_id: str, process: ProcessIdentity, probe: Probe):
        launch_calls.append(profile_id)
        if not launch_allowed:
            raise AssertionError("profile-create POST must not open Headquarters runtime")
        return real_launch(profile_id=profile_id, process=process, probe=probe)

    shell.runtime.launch = guarded_launch  # type: ignore[method-assign]

    status, _ = request(shell, "/profile/create", method="POST", values=form.values)
    assert status == 303
    assert launch_calls == []
    assert shell.runtime.state == "STOPPED"

    replay_status, _ = request(shell, "/profile/create", method="POST", values=form.values)
    assert replay_status == 409
    assert launch_calls == []

    snapshot = shell.profiles.discover()
    assert len(snapshot.profiles) == 1
    profile_id = snapshot.profiles[0].profile_id
    assert InstanceLeaseManager(state_root).inspect("__profile_bootstrap__") is None
    assert InstanceLeaseManager(state_root).inspect(profile_id) is None

    launch_allowed = True
    status, home = request(shell, "/")
    assert status == 200
    assert "Redirect First Artist" in home
    assert launch_calls == [profile_id]
    assert shell.runtime.state == "RUNNING"
    assert InstanceLeaseManager(state_root).inspect(profile_id) is not None

    quit_shell(shell)
    assert InstanceLeaseManager(state_root).inspect(profile_id) is None


def test_deferred_runtime_open_failure_is_rendered_on_redirect_target(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(),
        probe=Probe(),
    )
    shell.start()

    status, welcome = request(shell, "/")
    assert status == 200
    form = create_form(welcome)
    form.values["artist_name"] = "Deferred Failure Artist"
    calls: list[str] = []

    def fail_launch(*, profile_id: str, process: ProcessIdentity, probe: Probe) -> LaunchResult:
        calls.append(profile_id)
        return LaunchResult(
            "START_FAILED",
            profile_id,
            None,
            reason="synthetic deferred open failure",
        )

    shell.runtime.launch = fail_launch  # type: ignore[method-assign]

    status, _ = request(shell, "/profile/create", method="POST", values=form.values)
    assert status == 303
    assert calls == []
    assert shell.runtime.state == "STOPPED"

    status, page = request(shell, "/")
    assert status == 200
    assert len(calls) == 1
    assert "This Artist workspace could not open safely" in page
    assert "Recovery needed" in page
    assert shell.runtime.state == "STOPPED"
    assert InstanceLeaseManager(state_root).inspect(calls[0]) is None

    shell.stop(timeout=2.0)
    assert shell.wait_stopped(timeout=2.0)
