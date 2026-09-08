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
    button_text: str = ""


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self.current: Form | None = None
        self.in_button = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form":
            self.current = Form(str(values.get("action", "")), {})
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and values.get("name"):
            self.current.values[str(values["name"])] = str(values.get("value", ""))
        elif tag == "button" and self.current is not None:
            self.in_button = True

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.in_button:
            self.current.button_text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "button":
            self.in_button = False
        elif tag == "form":
            self.current = None


def forms(page: str, action: str) -> list[Form]:
    parser = FormParser()
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
    req = Request(shell.address.origin + path, data=data, method=method, headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def quit_shell(shell: ConsumerShell) -> None:
    status, settings = request(shell, "/settings")
    assert status == 200
    quit_form = forms(settings, "/quit")
    assert len(quit_form) == 1
    status, closed = request(
        shell,
        "/quit",
        method="POST",
        fields=quit_form[0].values,
        origin=shell.address.origin,
    )
    assert status == 200
    assert "N0TE closed safely." in closed
    assert shell.wait_stopped(timeout=2.0)


def seed_history(data_root: Path) -> tuple[str, str, tuple[str, str, str]]:
    headquarters = HeadquartersMemory.create(data_root, "Approval Artist")
    try:
        song = headquarters.store.create_song("Approval Song")
        versions = []
        parent = None
        for ordinal, label in enumerate(("Sketch", "Rough", "Mix"), start=1):
            asset = headquarters.store.attach_asset(
                song.id,
                name=f"approval-{ordinal}.wav",
                sha256=f"{ordinal:x}" * 64,
                source_uri=f"file:///external/approval-{ordinal}.wav",
            )
            version = headquarters.store.create_version(
                song.id,
                label=label,
                parent_version_id=parent,
                asset_ids=(asset.id,),
            )
            versions.append(version)
            parent = version.id
        headquarters.store.approve_version(song.id, versions[1].id)
        return headquarters.store.profile_id, song.id, tuple(v.id for v in versions)
    finally:
        headquarters.close()


def form_for_label(page: str, label: str) -> Form:
    candidates = forms(page, "/song/version/approve")
    matches = [candidate for candidate in candidates if candidate.button_text.strip() == label]
    assert len(matches) == 1
    return matches[0]


def test_approval_surface_names_exact_version_and_preserves_current_across_restart(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, (first_id, rough_id, mix_id) = seed_history(data_root)
    proc = process(9501, "approval-restart")

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    shell.start()
    status, page = request(shell, "/song")
    assert status == 200
    assert page.count(">Approved<") == 1
    assert "Approve Version 1: Sketch" in page
    assert "Approve Version 3: Mix" in page
    assert "Approve Version 2: Rough" not in page
    assert "does not make it current or publish, release, send, or purchase anything" in page
    assert "prf_" not in page and "song_" not in page and "ver_" not in page and "asset_" not in page

    approval = form_for_label(page, "Approve Version 1: Sketch")
    before = HeadquartersMemory.open(data_root, profile_id)
    try:
        song = before.store.get_song(song_id)
        assert song is not None
        assert song.current_version_id == mix_id
        activity_before = len(before.activity.for_song(song_id))
        versions_before = before.store.versions_for_song(song_id)
        assets_before = tuple(before.store.version_asset_ids(v.id) for v in versions_before)
    finally:
        before.close()

    status, _ = request(
        shell,
        "/song/version/approve",
        method="POST",
        fields=approval.values,
        origin=shell.address.origin,
    )
    assert status == 303
    status, changed = request(shell, "/song")
    assert status == 200
    assert "Approved Sketch. The current Version was not changed." in changed
    assert "This approval does not publish or release anything." in changed
    assert "Approve Version 1: Sketch" not in changed
    assert "Approve Version 2: Rough" in changed
    assert changed.count(">Approved<") == 1
    assert changed.count(">Current<") == 1

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        song = check.store.get_song(song_id)
        assert song is not None
        assert song.approved_version_id == first_id
        assert song.current_version_id == mix_id
        assert check.store.versions_for_song(song_id) == versions_before
        assert tuple(check.store.version_asset_ids(v.id) for v in versions_before) == assets_before
        added = check.activity.for_song(song_id)[activity_before:]
        assert [event.event_type for event in added] == ["VERSION_APPROVED"]
        assert added[0].version_id == first_id
    finally:
        check.close()

    quit_shell(shell)

    reopened = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    reopened.start()
    status, persistent = request(reopened, "/song")
    assert status == 200
    assert "Approve Version 1: Sketch" not in persistent
    assert "Approve Version 2: Rough" in persistent
    assert persistent.count(">Approved<") == 1
    quit_shell(reopened)


def test_approval_action_is_origin_csrf_and_replay_protected(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, (first_id, rough_id, _) = seed_history(data_root)
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9502, "approval-authority"),
        probe=Probe(),
    )
    shell.start()
    status, page = request(shell, "/song")
    assert status == 200
    approval = form_for_label(page, "Approve Version 1: Sketch")

    blocked, body = request(
        shell,
        "/song/version/approve",
        method="POST",
        origin="https://attacker.example",
    )
    assert blocked == 403
    assert "That action did not come from this N0TE window." in body

    bad_csrf = dict(approval.values)
    bad_csrf["csrf"] = "wrong"
    blocked, _ = request(
        shell,
        "/song/version/approve",
        method="POST",
        fields=bad_csrf,
        origin=shell.address.origin,
    )
    assert blocked == 403

    accepted, _ = request(
        shell,
        "/song/version/approve",
        method="POST",
        fields=approval.values,
        origin=shell.address.origin,
    )
    assert accepted == 303
    replay, _ = request(
        shell,
        "/song/version/approve",
        method="POST",
        fields=approval.values,
        origin=shell.address.origin,
    )
    assert replay == 409

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        song = check.store.get_song(song_id)
        assert song is not None
        assert song.approved_version_id == first_id
        assert song.approved_version_id != rough_id
    finally:
        check.close()
    quit_shell(shell)


def test_approval_action_rejects_stale_current_approved_and_active_song(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, (first_id, rough_id, mix_id) = seed_history(data_root)
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9503, "approval-stale"),
        probe=Probe(),
    )
    shell.start()

    status, page = request(shell, "/song")
    assert status == 200
    stale_current = form_for_label(page, "Approve Version 1: Sketch")
    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.store.set_current_version(song_id, rough_id)
    finally:
        changer.close()
    status, _ = request(
        shell,
        "/song/version/approve",
        method="POST",
        fields=stale_current.values,
        origin=shell.address.origin,
    )
    assert status == 409
    verify = HeadquartersMemory.open(data_root, profile_id)
    try:
        song = verify.store.get_song(song_id)
        assert song is not None
        assert song.approved_version_id == rough_id
    finally:
        verify.close()

    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.store.set_current_version(song_id, mix_id)
    finally:
        changer.close()
    status, page = request(shell, "/song")
    assert status == 200
    stale_approved = form_for_label(page, "Approve Version 1: Sketch")
    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.store.approve_version(song_id, mix_id)
    finally:
        changer.close()
    status, _ = request(
        shell,
        "/song/version/approve",
        method="POST",
        fields=stale_approved.values,
        origin=shell.address.origin,
    )
    assert status == 409
    verify = HeadquartersMemory.open(data_root, profile_id)
    try:
        song = verify.store.get_song(song_id)
        assert song is not None
        assert song.approved_version_id == mix_id
    finally:
        verify.close()

    status, page = request(shell, "/song")
    assert status == 200
    stale_song = form_for_label(page, "Approve Version 1: Sketch")
    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        other = changer.store.create_song("Other Song")
    finally:
        changer.close()
    status, _ = request(
        shell,
        "/song/version/approve",
        method="POST",
        fields=stale_song.values,
        origin=shell.address.origin,
    )
    assert status == 409
    final = HeadquartersMemory.open(data_root, profile_id)
    try:
        first = final.store.get_song(song_id)
        second = final.store.get_song(other.id)
        assert first is not None and first.approved_version_id == mix_id
        assert second is not None and second.approved_version_id is None
        assert final.store.get_version(first_id) is not None
    finally:
        final.close()
    quit_shell(shell)
