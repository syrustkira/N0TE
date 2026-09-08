from __future__ import annotations

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


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class ResumeFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_resume = False
        self.resume_forms: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form" and values.get("action") == "/song/version/resume":
            self.in_resume = True
            self.current = {}
            self.resume_forms.append(self.current)
        elif tag == "input" and self.in_resume and self.current is not None and values.get("name"):
            self.current[str(values["name"])] = str(values.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.in_resume:
            self.in_resume = False
            self.current = None


def get(shell: ConsumerShell, path: str) -> tuple[int, str]:
    with build_opener(NoRedirect()).open(shell.address.origin + path, timeout=2.0) as response:
        return response.status, response.read().decode("utf-8")


def post(shell: ConsumerShell, path: str, fields: dict[str, str]) -> tuple[int, str]:
    request = Request(
        shell.address.origin + path,
        data=urlencode(fields).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": shell.address.origin,
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_bad_csrf_cannot_move_current_version(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    headquarters = HeadquartersMemory.create(data_root, "CSRF Artist")
    try:
        song = headquarters.store.create_song("CSRF Song")
        first = headquarters.store.create_version(song.id, label="First")
        second = headquarters.store.create_version(
            song.id,
            label="Second",
            parent_version_id=first.id,
        )
        profile_id = headquarters.store.profile_id
    finally:
        headquarters.close()

    process = ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=9303,
        start_token="version-resume-csrf",
    )
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process,
        probe=Probe(),
    )
    shell.start()
    status, page = get(shell, "/song")
    assert status == 200
    parser = ResumeFormParser()
    parser.feed(page)
    assert len(parser.resume_forms) == 1
    fields = dict(parser.resume_forms[0])
    fields["csrf"] = "not-the-shell-token"

    rejected, _ = post(shell, "/song/version/resume", fields)
    assert rejected == 403

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        unchanged = check.store.get_song(song.id)
        assert unchanged is not None
        assert unchanged.current_version_id == second.id
    finally:
        check.close()

    shell.stop(timeout=2.0)
