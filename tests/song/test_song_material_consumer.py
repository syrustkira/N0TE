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


def multipart(values: dict[str, str], payload: bytes, *, filename: str = "demo.wav") -> tuple[bytes, str]:
    boundary = "N0TEMaterialConsumerBoundary7a9b"
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


def create_profile_with_approved_version(data_root: Path) -> tuple[str, str, str]:
    headquarters = HeadquartersMemory.create(data_root, "Material Artist")
    try:
        song = headquarters.store.create_song("Material Song")
        seed = headquarters.store.attach_asset(
            song.id,
            name="approved-reference.wav",
            sha256="a" * 64,
            source_uri="file:///external/approved-reference.wav",
        )
        approved = headquarters.store.create_version(
            song.id,
            label="Approved rough",
            asset_ids=(seed.id,),
        )
        headquarters.store.approve_version(song.id, approved.id)
        return headquarters.store.profile_id, song.id, approved.id
    finally:
        headquarters.close()


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


def test_real_song_surface_ingests_verified_material_and_resumes_without_approval_drift(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, approved_id = create_profile_with_approved_version(data_root)
    proc = process(9201, "material-consumer-resume")

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    shell.start()
    status, page = request(shell, "/song")
    assert status == 200
    upload = forms(page, "/song/material")
    assert len(upload) == 1
    assert upload[0].enctype == "multipart/form-data"
    assert "Approved rough" in page
    assert "Current Version is approved" in page
    assert "prf_" not in page and "ver_" not in page and "asset_" not in page

    payload = b"RIFF-material-consumer-proof\x00\x01\x02"
    body, content_type = multipart(upload[0].values, payload, filename="../mix-02.wav")
    status, _ = request(
        shell,
        "/song/material",
        method="POST",
        body=body,
        content_type=content_type,
        origin=shell.address.origin,
    )
    assert status == 303

    status, changed = request(shell, "/song")
    assert status == 200
    assert "mix-02.wav" in changed
    assert "Verified local copy" in changed
    assert "Approved rough" in changed
    assert "Approved remains different from current" in changed
    assert "../mix-02.wav" not in changed
    assert "n0te-material://" not in changed
    assert str(data_root) not in changed

    quit_shell(shell)
    headquarters = HeadquartersMemory.open(data_root, profile_id)
    try:
        song = headquarters.store.get_song(song_id)
        assert song is not None
        assert song.approved_version_id == approved_id
        assert song.current_version_id != approved_id
        assert song.current_version_id is not None
        current = headquarters.store.get_version(song.current_version_id)
        assert current is not None
        assert current.parent_version_id == approved_id
        assert current.label == "Imported mix-02.wav"
        views = headquarters.materials.version_materials(current.id)
        assert len(views) == 1
        assert views[0].asset.name == "mix-02.wav"
        assert views[0].status == "VERIFIED_MANAGED"
        preserved = headquarters.materials.resolve_asset(views[0].asset)
        assert preserved.path.read_bytes() == payload
    finally:
        headquarters.close()

    reopened = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    reopened.start()
    status, resumed = request(reopened, "/song")
    assert status == 200
    assert "mix-02.wav" in resumed
    assert "Verified local copy" in resumed
    assert "Approved rough" in resumed
    assert "Approved remains different from current" in resumed
    quit_shell(reopened)


def test_material_action_is_origin_checked_one_shot_and_bound_to_rendered_song(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    headquarters = HeadquartersMemory.create(data_root, "Authority Artist")
    try:
        first = headquarters.store.create_song("First Song")
        second = headquarters.store.create_song("Second Song")
        headquarters.store.select_song(first.id)
        profile_id = headquarters.store.profile_id
    finally:
        headquarters.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9202, "material-consumer-authority"),
        probe=Probe(),
    )
    shell.start()
    status, page = request(shell, "/song")
    assert status == 200
    upload = forms(page, "/song/material")[0]
    body, content_type = multipart(upload.values, b"authority-bytes")

    rejected, _ = request(
        shell,
        "/song/material",
        method="POST",
        body=body,
        content_type=content_type,
        origin="https://attacker.example",
    )
    assert rejected == 403

    accepted, _ = request(
        shell,
        "/song/material",
        method="POST",
        body=body,
        content_type=content_type,
        origin=shell.address.origin,
    )
    assert accepted == 303
    replay, _ = request(
        shell,
        "/song/material",
        method="POST",
        body=body,
        content_type=content_type,
        origin=shell.address.origin,
    )
    assert replay == 409
    quit_shell(shell)

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        first_after = check.store.get_song(first.id)
        second_after = check.store.get_song(second.id)
        assert first_after is not None and first_after.current_version_id is not None
        assert second_after is not None and second_after.current_version_id is None
    finally:
        check.close()

    stale_shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9203, "material-consumer-stale-song"),
        probe=Probe(),
    )
    stale_shell.start()
    status, first_page = request(stale_shell, "/song")
    assert status == 200
    stale_upload = forms(first_page, "/song/material")[0]
    stale_body, stale_type = multipart(stale_upload.values, b"must-not-land")

    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.store.select_song(second.id)
    finally:
        changer.close()

    stale_status, _ = request(
        stale_shell,
        "/song/material",
        method="POST",
        body=stale_body,
        content_type=stale_type,
        origin=stale_shell.address.origin,
    )
    assert stale_status == 409
    quit_shell(stale_shell)

    final = HeadquartersMemory.open(data_root, profile_id)
    try:
        first_final = final.store.get_song(first.id)
        second_final = final.store.get_song(second.id)
        assert first_final is not None and first_final.current_version_id is not None
        assert second_final is not None and second_final.current_version_id is None
    finally:
        final.close()


def test_tampered_managed_blob_is_visible_as_protected_integrity_problem(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    headquarters = HeadquartersMemory.create(data_root, "Integrity Artist")
    try:
        song = headquarters.store.create_song("Integrity Song")
        profile_id = headquarters.store.profile_id
    finally:
        headquarters.close()

    proc = process(9204, "material-consumer-integrity")
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    shell.start()
    status, page = request(shell, "/song")
    assert status == 200
    upload = forms(page, "/song/material")[0]
    body, content_type = multipart(upload.values, b"trusted-before-tamper", filename="take.mid")
    status, _ = request(
        shell,
        "/song/material",
        method="POST",
        body=body,
        content_type=content_type,
        origin=shell.address.origin,
    )
    assert status == 303
    quit_shell(shell)

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        active = check.store.get_song(song.id)
        assert active is not None and active.current_version_id is not None
        views = check.materials.version_materials(active.current_version_id)
        assert len(views) == 1
        managed = check.materials.resolve_asset(views[0].asset)
        managed.path.write_bytes(b"tampered")
    finally:
        check.close()

    reopened = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=proc,
        probe=Probe(),
    )
    reopened.start()
    status, protected = request(reopened, "/song")
    assert status == 200
    assert "take.mid" in protected
    assert "Protected integrity problem" in protected
    assert "Verified local copy" not in protected
    assert str(data_root) not in protected
    quit_shell(reopened)
