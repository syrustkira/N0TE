from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
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


def form_for(page: str, action: str) -> Form:
    parser = FormParser()
    parser.feed(page)
    matches = [form for form in parser.forms if form.action == action]
    assert len(matches) == 1
    return matches[0]


def request(
    shell: ConsumerShell,
    path: str,
    *,
    method: str = "GET",
    fields: dict[str, str] | None = None,
    payload: bytes | None = None,
    content_type: str | None = None,
    origin: str | None = None,
) -> tuple[int, str]:
    if fields is not None and payload is not None:
        raise ValueError("use fields or payload, not both")
    headers: dict[str, str] = {}
    data = payload
    if fields is not None:
        data = urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None and content_type is not None:
        headers["Content-Type"] = content_type
    if origin is not None:
        headers["Origin"] = origin
    req = Request(shell.address.origin + path, data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def quit_shell(shell: ConsumerShell) -> None:
    status, settings = request(shell, "/settings")
    assert status == 200
    quit_form = form_for(settings, "/quit")
    status, closed = request(
        shell,
        "/quit",
        method="POST",
        fields=quit_form.values,
        origin=shell.address.origin,
    )
    assert status == 200
    assert "N0TE closed safely." in closed
    assert shell.wait_stopped(timeout=2.0)


def test_bounded_rejected_posts_remain_http_responses_and_preserve_action(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9701, "windows-loopback-rejected-posts"),
        probe=Probe(),
    )
    shell.start()

    status, welcome = request(shell, "/")
    assert status == 200
    create = form_for(welcome, "/profile/create")
    fields = dict(create.values)
    fields["artist_name"] = "Transport Artist"

    # This is the historical WinError 10053 shape: a normal form body is already
    # in flight when Origin policy rejects it before form parsing. Do not catch
    # socket exceptions here. Any transport abort must fail the regression.
    for _ in range(64):
        status, _ = request(
            shell,
            "/profile/create",
            method="POST",
            fields=fields,
            origin="https://attacker.example",
        )
        assert status == 403

    # Unknown routes also reject before form parsing. Their body is transport
    # cleanup only and must not consume the still-valid profile action.
    for _ in range(16):
        status, _ = request(
            shell,
            "/not-a-n0te-action",
            method="POST",
            fields=fields,
            origin=shell.address.origin,
        )
        assert status == 404

    # Unsupported media on a normal form-sized body follows the same rule: the
    # bytes may be discarded, but they are never parsed into authority.
    for _ in range(16):
        status, _ = request(
            shell,
            "/profile/create",
            method="POST",
            payload=b'{"action":"not-authority"}',
            content_type="application/json",
            origin=shell.address.origin,
        )
        assert status == 403

    assert shell.profiles.discover().profiles == ()

    # Every rejected request above left the exact one-shot action untouched.
    accepted, _ = request(
        shell,
        "/profile/create",
        method="POST",
        fields=fields,
        origin=shell.address.origin,
    )
    assert accepted == 303

    replay, _ = request(
        shell,
        "/profile/create",
        method="POST",
        fields=fields,
        origin=shell.address.origin,
    )
    assert replay == 409

    quit_shell(shell)
