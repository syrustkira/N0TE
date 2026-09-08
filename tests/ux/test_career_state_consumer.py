from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te.career_state import CareerStateMemory
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


def new_profile(root: Path) -> str:
    hq = HeadquartersMemory.create(root, "Career State UX Artist")
    try:
        hq.store.create_song("Career State Song")
        return hq.store.profile_id
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


def test_now_surface_records_reviews_and_relaunches_career_state(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id = new_profile(data_root)
    shell = new_shell(data_root, state_root, 9971, "career-state-journey")
    try:
        status, page = request(shell, "/now")
        assert status == 200
        assert "Your Career State" in page
        assert "No Career State set" in page
        assert "will not guess your career season" in page
        assert "Neutral recommendation posture" in page
        assert "working seasons, not personality types" in page
        assert "career_state_" not in page

        career_forms = forms(page, "/career-state/set")
        assert len(career_forms) == 1
        career_forms[0].values["state"] = "CREATING"
        career_forms[0].values["rationale"] = "Protect the writing run before release planning expands."
        status, _ = post_form(shell, career_forms[0])
        assert status == 303

        status, page = request(shell, "/now")
        assert status == 200
        assert "Creating Career State" in page
        assert "Protect the writing run before release planning expands." in page
        assert "Put more weight on:" in page
        assert "your own creative work" in page
        assert "Recommendation posture" in page
        assert "grants no external action authority" in page
        assert "never sends, spends, publishes, purchases, connects, or mutates a DAW" in page
        assert "career_state_" not in page

        change = forms(page, "/career-state/set")[0]
        change.values["state"] = "RELEASING"
        change.values["rationale"] = "The single is finished and the release cycle is active."
        status, _ = post_form(shell, change)
        assert status == 303

        status, page = request(shell, "/now")
        assert status == 200
        assert "Releasing Career State" in page
        assert "The single is finished and the release cycle is active." in page
        assert "Prior Career State history" in page
        assert "Creating" in page
        assert "Protect the writing run before release planning expands." in page
        assert "Artist-declared local context" in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        career = CareerStateMemory(reopened.store)
        current = career.current_state()
        assert current is not None
        assert current.state == "RELEASING"
        assert [entry.state for entry in career.history()] == ["CREATING", "RELEASING"]
    finally:
        reopened.close()


def test_career_state_browser_authority_rejects_foreign_stale_and_replayed_actions(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id = new_profile(data_root)
    shell = new_shell(data_root, state_root, 9972, "career-state-authority")
    try:
        status, page = request(shell, "/now")
        assert status == 200
        foreign = forms(page, "/career-state/set")[0]
        foreign.values["state"] = "BUILDING"
        status, denied = post_form(shell, foreign, origin="https://attacker.invalid")
        assert status == 403
        assert "did not come from this N0TE window" in denied

        status, _ = post_form(shell, foreign)
        assert status == 303
        status, page = request(shell, "/now")
        assert status == 200
        assert "Building Career State" in page

        replay = forms(page, "/career-state/set")[0]
        replay.values["state"] = "GROWING"
        status, _ = post_form(shell, replay)
        assert status == 303
        status, _ = post_form(shell, replay)
        assert status == 303
        status, page = request(shell, "/now")
        assert status == 200
        assert "already handled or expired" in page
        assert "Growing Career State" in page

        stale_page_status, stale_page = request(shell, "/now")
        assert stale_page_status == 200
        stale = forms(stale_page, "/career-state/set")[0]
        stale.values["state"] = "TOURING"
        status, fresh_page = request(shell, "/now")
        assert status == 200
        fresh = forms(fresh_page, "/career-state/set")[0]
        fresh.values["state"] = "CLIENT_HEAVY"
        status, _ = post_form(shell, fresh)
        assert status == 303
        status, _ = post_form(shell, stale)
        assert status == 303
        status, page = request(shell, "/now")
        assert status == 200
        assert "already handled or expired" in page
        assert "Client-heavy Career State" in page
        assert "Touring Career State" not in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        career = CareerStateMemory(reopened.store)
        current = career.current_state()
        assert current is not None
        assert current.state == "CLIENT_HEAVY"
        assert [entry.state for entry in career.history()] == [
            "BUILDING",
            "GROWING",
            "CLIENT_HEAVY",
        ]
    finally:
        reopened.close()
