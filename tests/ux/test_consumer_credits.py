from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te.consumer_shell import ConsumerShell
from n0te.credits import CreditsMemory
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

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.text += data


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


def new_profile(root: Path) -> tuple[str, str, str, str]:
    hq = HeadquartersMemory.create(root, "Credits UX Artist")
    try:
        song = hq.store.create_song("Shared UX Song")
        maya = hq.people.create_person("Maya Rivera", relationship_context="Co-writer")
        alex = hq.people.create_person("Alex Chen", relationship_context="Producer")
        return hq.store.profile_id, song.id, maya.id, alex.id
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


def test_people_surface_completes_credit_and_split_confirmation_journey(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, maya_id, alex_id = new_profile(data_root)
    shell = new_shell(data_root, state_root, 9961, "credits-journey")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        assert "Credits & composition splits · Shared UX Song" in page
        assert "local declarations into legal or provider truth" in page
        assert "No Song credits recorded yet" in page
        assert "person_" not in page
        assert "credit_" not in page
        assert "split_" not in page

        credit_forms = forms(page, "/credits/record")
        assert len(credit_forms) == 2
        credit_forms[0].values["role"] = "Songwriter"
        credit_forms[0].values["role_context"] = "Composition contribution"
        status, _ = post_form(shell, credit_forms[0])
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "Maya Rivera" in page
        assert "Songwriter" in page
        assert "artist-entered local context" in page
        assert "not provider-verified credits or ownership findings" in page

        create = forms(page, "/credits/split/create")
        assert len(create) == 1
        status, _ = post_form(shell, create[0])
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        save = forms(page, "/credits/split/save")
        assert len(save) == 1
        save[0].values["share_0"] = "60.00"
        save[0].values["share_1"] = "40"
        status, _ = post_form(shell, save[0])
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "Current total: 100.00%" in page
        submit = forms(page, "/credits/split/submit")
        assert len(submit) == 1
        status, _ = post_form(shell, submit[0])
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "Submitted composition split proposal" in page
        assert "participant/share proposal is frozen" in page
        assert "not signatures or provider verification" in page
        confirmation_forms = forms(page, "/credits/split/confirm")
        assert len(confirmation_forms) == 2
        confirmation_forms[0].values["status"] = "RECORDED_CONFIRMED"
        confirmation_forms[0].values["note"] = "Artist records Maya confirming by email"
        status, _ = post_form(shell, confirmation_forms[0])
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        confirmation_forms = forms(page, "/credits/split/confirm")
        assert len(confirmation_forms) == 2
        confirmation_forms[1].values["status"] = "RECORDED_CONFIRMED"
        confirmation_forms[1].values["note"] = "Artist records Alex confirming by message"
        status, _ = post_form(shell, confirmation_forms[1])
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "Artist records every participant as confirmed" in page
        assert "not independent identity, signature, provider or legal verification" in page
        assert "person_" not in page
        assert "allocation_" not in page
        assert "confirmation_" not in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        credits = CreditsMemory(reopened.store, reopened.people)
        roster = credits.credits_for_song(song_id)
        assert [(item.person_id, item.role) for item in roster] == [(maya_id, "Songwriter")]
        active = credits.active_split_for_song(song_id)
        assert active is not None
        assert [(item.person_id, item.basis_points) for item in credits.split_allocations(active.id)] == [
            (maya_id, 6000),
            (alex_id, 4000),
        ]
        assert credits.all_recorded_confirmed(active.id) is True
        assert len(credits.confirmation_history(active.id)) == 2
    finally:
        reopened.close()


def test_stale_split_form_cannot_replace_newer_draft_allocations(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, maya_id, alex_id = new_profile(data_root)
    shell = new_shell(data_root, state_root, 9962, "credits-stale")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        status, _ = post_form(shell, forms(page, "/credits/split/create")[0])
        assert status == 303

        status, stale_page = request(shell, "/people")
        assert status == 200
        stale = forms(stale_page, "/credits/split/save")[0]
        stale.values["share_0"] = "50"
        stale.values["share_1"] = "50"

        status, fresh_page = request(shell, "/people")
        assert status == 200
        fresh = forms(fresh_page, "/credits/split/save")[0]
        fresh.values["share_0"] = "70"
        fresh.values["share_1"] = "30"
        status, _ = post_form(shell, fresh)
        assert status == 303

        status, _ = post_form(shell, stale)
        assert status == 303
        status, page = request(shell, "/people")
        assert status == 200
        assert "already handled or expired" in page
        assert 'value="70.00"' in page
        assert 'value="30.00"' in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        credits = CreditsMemory(reopened.store, reopened.people)
        sheet = credits.active_split_for_song(song_id)
        assert sheet is not None
        assert [(item.person_id, item.basis_points) for item in credits.split_allocations(sheet.id)] == [
            (maya_id, 7000),
            (alex_id, 3000),
        ]
    finally:
        reopened.close()


def test_credit_write_rejects_foreign_origin_replay_and_changed_active_song(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, first_song_id, maya_id, _ = new_profile(data_root)
    shell = new_shell(data_root, state_root, 9963, "credits-authority")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        credit = forms(page, "/credits/record")[0]
        credit.values["role"] = "Writer"

        status, denied = post_form(
            shell,
            credit,
            origin="https://attacker.invalid",
        )
        assert status == 403
        assert "did not come from this N0TE window" in denied

        song_start = Form(
            "/song/start",
            {
                "csrf": shell._csrf,
                "action": shell._new_action("song-start"),
                "song_title": "Different Active Song",
            },
        )
        status, _ = post_form(shell, song_start)
        assert status == 303

        status, _ = post_form(shell, credit)
        assert status == 303
        status, page = request(shell, "/people")
        assert status == 200
        assert "active Song changed" in page

        replay_status, _ = post_form(shell, credit)
        assert replay_status == 303
        status, page = request(shell, "/people")
        assert status == 200
        assert "already handled or expired" in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        credits = CreditsMemory(reopened.store, reopened.people)
        assert credits.credits_for_song(first_song_id) == ()
        assert reopened.people.get_person(maya_id) is not None
        assert reopened.store.active_song() is not None
        assert reopened.store.active_song().title == "Different Active Song"
    finally:
        reopened.close()


def test_void_keeps_prior_proposal_visible_and_allows_replacement(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    new_profile(data_root)
    shell = new_shell(data_root, state_root, 9964, "credits-void")
    try:
        status, page = request(shell, "/people")
        assert status == 200
        status, _ = post_form(shell, forms(page, "/credits/split/create")[0])
        assert status == 303
        status, page = request(shell, "/people")
        save = forms(page, "/credits/split/save")[0]
        save.values["share_0"] = "50"
        save.values["share_1"] = "50"
        status, _ = post_form(shell, save)
        assert status == 303
        status, page = request(shell, "/people")
        status, _ = post_form(shell, forms(page, "/credits/split/submit")[0])
        assert status == 303

        status, page = request(shell, "/people")
        void = forms(page, "/credits/split/void")[0]
        void.values["reason"] = "Participants want to replace this proposal"
        status, _ = post_form(shell, void)
        assert status == 303

        status, page = request(shell, "/people")
        assert status == 200
        assert "Prior split proposals" in page
        assert "Participants want to replace this proposal" in page
        assert "Voided proposals remain in history" in page
        create = forms(page, "/credits/split/create")
        assert len(create) == 1
    finally:
        shell.stop()
