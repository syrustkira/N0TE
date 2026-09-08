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
    enctype: str = ""


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self.current: Form | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form":
            self.current = Form(
                str(values.get("action", "")),
                {},
                str(values.get("enctype", "")),
            )
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and values.get("name"):
            self.current.values[str(values["name"])] = str(values.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
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
    body: bytes | None = None,
    content_type: str | None = None,
    origin: str | None = None,
) -> tuple[int, str]:
    headers: dict[str, str] = {}
    data = body
    if fields is not None:
        assert body is None
        data = urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if content_type is not None:
        headers["Content-Type"] = content_type
    if origin is not None:
        headers["Origin"] = origin
    req = Request(shell.address.origin + path, data=data, method=method, headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def multipart(values: dict[str, str], payload: bytes, filename: str) -> tuple[bytes, str]:
    boundary = "N0TEVersionHistoryBoundary43f1"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="csrf"\r\n\r\n',
            values["csrf"].encode(),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="action"\r\n\r\n',
            values["action"].encode(),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="material"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            payload,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


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


def seed_history(data_root: Path) -> tuple[str, str, str, str, str]:
    headquarters = HeadquartersMemory.create(data_root, "Version Artist")
    try:
        song = headquarters.store.create_song("Version Song")
        versions = []
        parent = None
        for ordinal, label in enumerate(("First sketch", "Approved rough", "Latest mix"), start=1):
            asset = headquarters.store.attach_asset(
                song.id,
                name=f"version-{ordinal}.wav",
                sha256=f"{ordinal:x}" * 64,
                source_uri=f"file:///external/version-{ordinal}.wav",
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
        return (
            headquarters.store.profile_id,
            song.id,
            versions[0].id,
            versions[1].id,
            versions[2].id,
        )
    finally:
        headquarters.close()


def test_song_surface_shows_full_history_and_resume_is_reversible_and_restart_safe(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, first_id, approved_id, latest_id = seed_history(data_root)
    proc = process(9301, "version-history-restart")

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    shell.start()
    status, page = request(shell, "/song")
    assert status == 200
    assert page.index("Version 3: Latest mix") < page.index("Version 2: Approved rough")
    assert page.index("Version 2: Approved rough") < page.index("Version 1: First sketch")
    assert page.count(">Current<") == 1
    assert page.count(">Approved<") == 1
    assert "Based on Version 2: Approved rough" in page
    assert "Based on Version 1: First sketch" in page
    assert "version-1.wav" in page and "version-2.wav" in page and "version-3.wav" in page
    assert "External reference" in page
    assert "prf_" not in page and "ver_" not in page and "asset_" not in page
    assert "file:///" not in page and "n0te-material://" not in page
    assert str(data_root) not in page

    resume_forms = forms(page, "/song/version/resume")
    assert len(resume_forms) == 2
    first_form = resume_forms[-1]

    blocked, _ = request(
        shell,
        "/song/version/resume",
        method="POST",
        fields=first_form.values,
        origin="https://attacker.example",
    )
    assert blocked == 403

    accepted, _ = request(
        shell,
        "/song/version/resume",
        method="POST",
        fields=first_form.values,
        origin=shell.address.origin,
    )
    assert accepted == 303
    replay, _ = request(
        shell,
        "/song/version/resume",
        method="POST",
        fields=first_form.values,
        origin=shell.address.origin,
    )
    assert replay == 409

    status, resumed = request(shell, "/song")
    assert status == 200
    assert "Resumed First sketch as the current Version. Approval was not changed." in resumed
    assert resumed.count(">Current<") == 1
    assert resumed.count(">Approved<") == 1
    assert "Version 1: First sketch" in resumed
    assert "Version 2: Approved rough" in resumed
    assert "Version 3: Latest mix" in resumed

    upload = forms(resumed, "/song/material")
    assert len(upload) == 1
    body, content_type = multipart(upload[0].values, b"branch-from-first", "branch.wav")
    imported, _ = request(
        shell,
        "/song/material",
        method="POST",
        body=body,
        content_type=content_type,
        origin=shell.address.origin,
    )
    assert imported == 303
    quit_shell(shell)

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        song = check.store.get_song(song_id)
        assert song is not None
        assert song.approved_version_id == approved_id
        assert song.current_version_id not in {None, first_id, approved_id, latest_id}
        versions = check.store.versions_for_song(song_id)
        assert [version.label for version in versions] == [
            "First sketch",
            "Approved rough",
            "Latest mix",
            "Imported branch.wav",
        ]
        assert versions[-1].parent_version_id == first_id
        assert check.store.get_version(latest_id) is not None
        assert check.store.get_version(approved_id) is not None
    finally:
        check.close()

    reopened = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    reopened.start()
    status, persistent = request(reopened, "/song")
    assert status == 200
    assert "Version 4: Imported branch.wav" in persistent
    assert "Version 3: Latest mix" in persistent
    assert "Version 2: Approved rough" in persistent
    assert "Version 1: First sketch" in persistent
    assert "Approved remains different from current" in persistent
    assert persistent.count(">Current<") == 1
    assert persistent.count(">Approved<") == 1
    quit_shell(reopened)


def test_resume_authority_rejects_stale_current_and_active_song_change(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, first_song_id, first_id, approved_id, latest_id = seed_history(data_root)

    setup = HeadquartersMemory.open(data_root, profile_id)
    try:
        second = setup.store.create_song("Other Song")
        setup.store.select_song(first_song_id)
        second_id = second.id
    finally:
        setup.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9302, "version-history-authority"),
        probe=Probe(),
    )
    shell.start()
    status, page = request(shell, "/song")
    assert status == 200
    stale_current_form = forms(page, "/song/version/resume")[-1]

    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.store.set_current_version(first_song_id, approved_id)
    finally:
        changer.close()

    stale_status, _ = request(
        shell,
        "/song/version/resume",
        method="POST",
        fields=stale_current_form.values,
        origin=shell.address.origin,
    )
    assert stale_status == 409

    verify = HeadquartersMemory.open(data_root, profile_id)
    try:
        first_song = verify.store.get_song(first_song_id)
        assert first_song is not None
        assert first_song.current_version_id == approved_id
        assert first_song.approved_version_id == approved_id
    finally:
        verify.close()

    status, refreshed = request(shell, "/song")
    assert status == 200
    song_change_form = forms(refreshed, "/song/version/resume")[-1]

    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.store.select_song(second_id)
    finally:
        changer.close()

    stale_song, _ = request(
        shell,
        "/song/version/resume",
        method="POST",
        fields=song_change_form.values,
        origin=shell.address.origin,
    )
    assert stale_song == 409
    quit_shell(shell)

    final = HeadquartersMemory.open(data_root, profile_id)
    try:
        first_song = final.store.get_song(first_song_id)
        second_song = final.store.get_song(second_id)
        assert first_song is not None and first_song.current_version_id == approved_id
        assert first_song.approved_version_id == approved_id
        assert second_song is not None and second_song.current_version_id is None
        assert final.store.get_version(first_id) is not None
        assert final.store.get_version(latest_id) is not None
    finally:
        final.close()


def test_version_history_read_is_song_scoped_and_deterministic(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    first = HeadquartersMemory.create(data_root, "First Artist")
    try:
        song_a = first.store.create_song("Song A")
        asset_a = first.store.attach_asset(
            song_a.id,
            name="a.wav",
            sha256="a" * 64,
        )
        version_a = first.store.create_version(song_a.id, label="A1", asset_ids=(asset_a.id,))
        song_b = first.store.create_song("Song B")
        asset_b = first.store.attach_asset(
            song_b.id,
            name="b.wav",
            sha256="b" * 64,
        )
        version_b = first.store.create_version(song_b.id, label="B1", asset_ids=(asset_b.id,))
        assert first.store.versions_for_song(song_a.id) == (version_a,)
        assert first.store.versions_for_song(song_b.id) == (version_b,)
    finally:
        first.close()

    second = HeadquartersMemory.create(data_root, "Second Artist")
    try:
        song_c = second.store.create_song("Song C")
        asset_c = second.store.attach_asset(
            song_c.id,
            name="c.wav",
            sha256="c" * 64,
        )
        version_c = second.store.create_version(song_c.id, label="C1", asset_ids=(asset_c.id,))
        assert second.store.versions_for_song(song_c.id) == (version_c,)
        assert second.store.get_version(version_a.id) is None
        assert second.store.get_version(version_b.id) is None
    finally:
        second.close()
