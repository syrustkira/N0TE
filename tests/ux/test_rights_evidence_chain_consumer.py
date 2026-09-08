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
        elif tag in {"input", "select"} and self.current is not None and values.get("name"):
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


def new_profile(root: Path, *, people: int = 1) -> tuple[str, str]:
    hq = HeadquartersMemory.create(root, "Rights UX Artist")
    try:
        song = hq.store.create_song("Rights UX Song")
        for index in range(people):
            hq.people.create_person(f"Writer {index + 1}")
        return hq.store.profile_id, song.id
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


def test_credit_rights_chain_preserves_manual_reference_without_verification(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    new_profile(data_root)
    shell = new_shell(data_root, state_root, 9981, "rights-credit")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        assert "Rights evidence chain" in page
        assert "begins from that explicit local declaration" in page

        credit_form = forms(page, "/credits/record")[0]
        credit_form.values["role"] = "Songwriter"
        credit_form.values["role_context"] = "Topline contribution"
        status, _ = post_form(shell, credit_form)
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "Rights evidence chain · Writer 1 · Songwriter" in page
        assert "Highest contiguous supported stage: <strong>User declaration</strong>" in page
        assert "Provider receipt / acknowledgment" in page
        assert "N0TE does not infer ownership, clearance, registration, royalty entitlement, payment, or permission to act" in page
        assert "Record artist-entered reference" in page
        assert "N0TE has not observed or verified the communication" in page

        rights_forms = forms(page, "/credits/rights/evidence")
        assert len(rights_forms) == 2
        for candidate in rights_forms:
            assert set(candidate.values) == {"csrf", "action", "assertion", "source_ref", "note"}
            assert "stage" not in candidate.values
            assert "source_kind" not in candidate.values
            assert "provider" not in candidate.values

        communication = rights_forms[0]
        communication.values["assertion"] = "SUPPORTS"
        communication.values["source_ref"] = "email-thread:rights-ux"
        communication.values["note"] = "Writer acknowledged the credit"
        status, _ = post_form(shell, communication)
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert page.count("email-thread:rights-ux") == 1
        assert "Writer acknowledged the credit" in page
        assert "User Declared" in page
        assert "Artist-entered reference only; external evidence not observed or verified" in page
        assert "Highest contiguous supported stage: <strong>User declaration</strong>" in page
        assert "Artist-entered rights reference recorded as USER_DECLARED" in page
        assert "Observed rights evidence recorded" not in page

        # A consumed action cannot be replayed into a second declaration.
        status, _ = post_form(shell, communication)
        assert status == 303
        status, page = request(shell, "/people")
        assert status == 200
        assert page.count("email-thread:rights-ux") == 1
        assert "already handled or expired" in page
    finally:
        shell.stop()


def test_split_rights_action_fails_closed_when_split_changes_after_render(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    new_profile(data_root)
    shell = new_shell(data_root, state_root, 9982, "rights-split-stale")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        create = forms(page, "/credits/split/create")[0]
        status, _ = post_form(shell, create)
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        stale_rights = forms(page, "/credits/rights/evidence")[0]
        save = forms(page, "/credits/split/save")[0]
        save.values["share_0"] = "100"
        status, _ = post_form(shell, save)
        assert status == 303

        stale_rights.values["assertion"] = "SUPPORTS"
        stale_rights.values["source_ref"] = "email-thread:stale"
        stale_rights.values["note"] = "Must not attach after split mutation"
        status, _ = post_form(shell, stale_rights)
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "changed. Reload People before attaching evidence to stale rights context" in page
        assert "email-thread:stale" not in page
        assert "Highest contiguous supported stage: <strong>User declaration</strong>" in page
    finally:
        shell.stop()


def test_rights_evidence_route_rejects_cross_origin_post(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    new_profile(data_root)
    shell = new_shell(data_root, state_root, 9983, "rights-origin")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        credit_form = forms(page, "/credits/record")[0]
        credit_form.values["role"] = "Producer"
        status, _ = post_form(shell, credit_form)
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        rights_form = forms(page, "/credits/rights/evidence")[0]
        rights_form.values["assertion"] = "SUPPORTS"
        rights_form.values["source_ref"] = "email-thread:cross-origin"
        status, body = post_form(shell, rights_form, origin="https://attacker.invalid")
        assert status == 403
        assert "did not come from this N0TE window" in body
    finally:
        shell.stop()
