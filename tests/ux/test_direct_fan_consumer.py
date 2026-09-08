from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te.consumer_shell import ConsumerShell
from n0te.direct_fan import DirectFanService
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


def seed_profile(root: Path) -> tuple[str, str, str]:
    hq = HeadquartersMemory.create(root, "Direct Fan UX Artist")
    try:
        song = hq.store.create_song("Direct Fan Release")
        person = hq.people.create_person(
            "Listener UX",
            relationship_context="Direct fan who explicitly shared contact preferences",
        )
        return hq.store.profile_id, song.id, person.id
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


def _record_email_and_opt_in(shell: ConsumerShell) -> None:
    status, page = request(shell, "/audience")
    assert status == 200
    contact = forms(page, "/audience/contact")[0]
    contact.values["channel"] = "EMAIL"
    contact.values["endpoint"] = "listener@example.test"
    contact.values["note"] = "Shared directly for release updates"
    assert post_form(shell, contact)[0] == 303

    status, page = request(shell, "/audience")
    assert status == 200
    consent = forms(page, "/audience/consent")[0]
    consent.values["channel"] = "EMAIL"
    consent.values["status"] = "OPTED_IN"
    consent.values["note"] = "Explicitly opted in"
    assert post_form(shell, consent)[0] == 303


def test_audience_records_contact_consent_and_reviewable_release_intent(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, person_id = seed_profile(data_root)
    shell = new_shell(data_root, state_root, 9981, "direct-fan-journey")
    try:
        status, page = request(shell, "/audience")
        assert status == 200
        assert "Audience and Direct Fan" in page
        assert "No provider sending is enabled here" in page
        assert "will not infer opt-in from follows" in page
        assert "permission to send" in page
        assert "person_" not in page
        assert "claim_" not in page
        assert "song_" not in page

        _record_email_and_opt_in(shell)

        status, page = request(shell, "/audience")
        assert status == 200
        assert "listener@example.test" in page
        assert "OPTED_IN" in page
        assert "Current Song:" in page
        assert "Direct Fan Release" in page
        intent_forms = forms(page, "/audience/intent")
        assert len(intent_forms) == 2
        assert "Plan release notification via Email" in page
        assert "Plan pre-save invite via Email" in page
        assert "Planning is not scheduling or sending" in page

        status, _ = post_form(shell, intent_forms[0])
        assert status == 303
        status, page = request(shell, "/audience")
        assert status == 200
        assert "Release notification" in page
        assert "Reviewable plan" in page
        assert "No message is scheduled or sent" in page
        assert "Delivery and pre-save remain unverified" in page
        assert ">Send<" not in page
        assert 'action="/audience/send"' not in page
        assert "person_" not in page
        assert "claim_" not in page
        assert "song_" not in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        direct = DirectFanService(reopened.store, reopened.people, reopened.evidence)
        intents = direct.intents_for_person(person_id)
        assert len(intents) == 1
        assert intents[0].song_id == song_id
        assert intents[0].purpose == "RELEASE_NOTIFICATION"
        assessment = direct.assess_intent(intents[0].claim_id)
        assert assessment.state == "REVIEWABLE"
        assert assessment.send_authority_granted is False
        assert assessment.scheduling_authority_granted is False
        assert assessment.provider_authority_granted is False
        assert assessment.delivery_verified is False
    finally:
        reopened.close()


def test_audience_authority_rejects_foreign_origin_and_replayed_contact_actions(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _, person_id = seed_profile(data_root)
    shell = new_shell(data_root, state_root, 9982, "direct-fan-authority")
    try:
        status, page = request(shell, "/audience")
        assert status == 200
        contact = forms(page, "/audience/contact")[0]
        contact.values["channel"] = "EMAIL"
        contact.values["endpoint"] = "authority@example.test"

        status, denied = post_form(shell, contact, origin="https://attacker.invalid")
        assert status == 403
        assert "did not come from this N0TE window" in denied

        assert post_form(shell, contact)[0] == 303
        assert post_form(shell, contact)[0] == 303
        status, page = request(shell, "/audience")
        assert status == 200
        assert "already handled or expired" in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        direct = DirectFanService(reopened.store, reopened.people, reopened.evidence)
        history = direct.contact_history(person_id)
        assert len(history) == 1
        assert history[0].endpoint == "authority@example.test"
    finally:
        reopened.close()


def test_stale_plan_token_cannot_record_intent_after_consent_revocation(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _, person_id = seed_profile(data_root)
    shell = new_shell(data_root, state_root, 9983, "direct-fan-stale")
    try:
        _record_email_and_opt_in(shell)
        status, page = request(shell, "/audience")
        assert status == 200
        stale_plan = forms(page, "/audience/intent")[0]
        revoke = forms(page, "/audience/consent")[0]
        revoke.values["channel"] = "EMAIL"
        revoke.values["status"] = "OPTED_OUT"
        revoke.values["note"] = "Revoked before the rendered plan was submitted"
        assert post_form(shell, revoke)[0] == 303

        assert post_form(shell, stale_plan)[0] == 303
        status, page = request(shell, "/audience")
        assert status == 200
        assert "Consent changed" in page
        assert forms(page, "/audience/intent") == []
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        direct = DirectFanService(reopened.store, reopened.people, reopened.evidence)
        assert direct.intents_for_person(person_id) == ()
    finally:
        reopened.close()


def test_opt_out_after_recorded_intent_blocks_old_plan_without_erasing_history(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _, person_id = seed_profile(data_root)
    shell = new_shell(data_root, state_root, 9984, "direct-fan-optout")
    try:
        _record_email_and_opt_in(shell)
        status, page = request(shell, "/audience")
        assert status == 200
        assert post_form(shell, forms(page, "/audience/intent")[0])[0] == 303

        status, page = request(shell, "/audience")
        assert status == 200
        consent = forms(page, "/audience/consent")[0]
        consent.values["channel"] = "EMAIL"
        consent.values["status"] = "OPTED_OUT"
        consent.values["note"] = "Explicitly unsubscribed"
        assert post_form(shell, consent)[0] == 303

        status, page = request(shell, "/audience")
        assert status == 200
        assert "OPTED_OUT" in page
        assert "Blocked: consent opted out" in page
        assert "No message is scheduled or sent" in page
        assert forms(page, "/audience/intent") == []
        assert ">Send<" not in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        direct = DirectFanService(reopened.store, reopened.people, reopened.evidence)
        intents = direct.intents_for_person(person_id)
        assert len(intents) == 1
        assessment = direct.assess_intent(intents[0].claim_id)
        assert assessment.state == "CONSENT_REVOKED"
        assert assessment.reviewable is False
        assert assessment.send_authority_granted is False
    finally:
        reopened.close()
