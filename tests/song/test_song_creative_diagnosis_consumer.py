from __future__ import annotations

import re
import struct
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

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


def pcm16_stereo_wav() -> bytes:
    frames = [
        (-16384, -16384),
        (-8192, -4096),
        (0, 0),
        (8192, 4096),
        (16384, 16384),
    ]
    data = b"".join(struct.pack("<hh", *frame) for frame in frames)
    fmt = struct.pack("<HHIIHH", 1, 2, 8000, 8000 * 4, 4, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def seed(data_root: Path) -> tuple[str, str, str, str]:
    hq = HeadquartersMemory.create(data_root, "Diagnosis Consumer")
    try:
        song = hq.store.create_song("Signal Bloom")
        session = hq.sessions.start_session(
            song_id=song.id,
            objective="Make the chorus hit harder without changing the vocal melody",
        )
        audio = pcm16_stereo_wav()
        imported = hq.materials.ingest_stream(
            song.id,
            filename="current-mix.wav",
            stream=BytesIO(audio),
            declared_size=len(audio),
        )
        return hq.store.profile_id, song.id, session.id, imported.asset.sha256
    finally:
        hq.close()


def shell_for(data_root: Path, state_root: Path, pid: int, token: str) -> ConsumerShell:
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(pid, token),
        probe=Probe(),
    )
    shell.start()
    return shell


def get(shell: ConsumerShell, path: str) -> tuple[int, str]:
    req = Request(shell.address.origin + path, method="GET")
    try:
        with build_opener().open(req, timeout=3.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post(
    shell: ConsumerShell,
    path: str,
    fields: dict[str, str],
    *,
    origin: str | None = None,
) -> tuple[int, str]:
    payload = urlencode(fields).encode("utf-8")
    req = Request(shell.address.origin + path, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Origin", shell.address.origin if origin is None else origin)
    try:
        with build_opener().open(req, timeout=3.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def diagnosis_action(page: str) -> str:
    match = re.search(
        r'<form[^>]+action="/diagnosis/create".*?name="action" value="([^"]+)"',
        page,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_real_song_problem_reaches_evidence_hypothesis_and_two_distinct_paths(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, session_id, digest = seed(data_root)

    before = HeadquartersMemory.open(data_root, profile_id)
    try:
        before_versions = before.store.versions_for_song(song_id)
        before_learning = before.learning.episodes_for_song(song_id)
    finally:
        before.close()

    shell = shell_for(data_root, state_root, 13101, "diagnosis-consumer")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert page.count("<h2>Diagnose a Song problem</h2>") == 1
        assert "Give me two ways to test it" in page
        token = diagnosis_action(page)

        status, result = post(
            shell,
            "/diagnosis/create",
            {
                "csrf": shell._csrf,
                "action": token,
                "problem": "My chorus feels weak. Give me two ways to make it hit harder without changing the vocal melody.",
                "diagnosis_lock_melody": "1",
            },
        )
        assert status == 200
        assert "What I know" in result
        assert "You said" in result
        assert "My chorus feels weak" in result
        assert "Observed" in result
        assert "Sample peak" in result
        assert "RMS" in result
        assert "whole render" in result
        assert "What I’m inferring" in result
        assert result.count("Hypothesis, not observation.") == 2
        assert "Two ways to test it" in result
        assert "Path 1 · Arrangement" in result
        assert "Path 2 · Dynamics" in result
        assert "Preserve: Melody" in result
        assert "Nothing changed yet." in result
        assert "has not heard a subjective weakness" in result
        assert "No provider call" not in result

        for private in (profile_id, song_id, session_id, digest, str(data_root), "n0te-material://"):
            assert private not in result

        inspect = HeadquartersMemory.open(data_root, profile_id)
        try:
            assert inspect.store.versions_for_song(song_id) == before_versions
            assert inspect.learning.episodes_for_song(song_id) == before_learning
        finally:
            inspect.close()
    finally:
        shell.stop()


def test_diagnosis_action_rejects_foreign_origin_and_replay(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, _, _ = seed(data_root)
    shell = shell_for(data_root, state_root, 13102, "diagnosis-authority")
    try:
        _, page = get(shell, "/song")
        fields = {
            "csrf": shell._csrf,
            "action": diagnosis_action(page),
            "problem": "Test the chorus impact without changing melody.",
        }
        status, _ = post(
            shell,
            "/diagnosis/create",
            fields,
            origin="https://attacker.example",
        )
        assert status == 403

        inspect = HeadquartersMemory.open(data_root, profile_id)
        try:
            assert inspect.learning.episodes_for_song(song_id) == ()
        finally:
            inspect.close()

        status, result = post(shell, "/diagnosis/create", fields)
        assert status == 200
        assert "Two ways to test it" in result
        status, replay = post(shell, "/diagnosis/create", fields)
        assert status == 409
        assert "already handled or expired" in replay
    finally:
        shell.stop()


def test_diagnosis_result_is_ephemeral_across_relaunch(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed(data_root)
    shell = shell_for(data_root, state_root, 13103, "diagnosis-one")
    try:
        _, page = get(shell, "/song")
        status, result = post(
            shell,
            "/diagnosis/create",
            {
                "csrf": shell._csrf,
                "action": diagnosis_action(page),
                "problem": "Why does the chorus feel weak?",
            },
        )
        assert status == 200
        assert "What I know" in result
    finally:
        shell.stop()

    relaunched = shell_for(data_root, state_root, 13104, "diagnosis-two")
    try:
        status, page = get(relaunched, "/song")
        assert status == 200
        assert "<h2>Diagnose a Song problem</h2>" in page
        assert "<h3>What I know</h3>" not in page
        assert "<h3>Two ways to test it</h3>" not in page
    finally:
        relaunched.stop()


def test_new_session_invalidates_prepared_diagnosis(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, session_id, _ = seed(data_root)
    shell = shell_for(data_root, state_root, 13105, "diagnosis-stale")
    try:
        _, page = get(shell, "/song")
        status, result = post(
            shell,
            "/diagnosis/create",
            {
                "csrf": shell._csrf,
                "action": diagnosis_action(page),
                "problem": "Test one chorus problem.",
            },
        )
        assert status == 200 and "What I know" in result

        changer = HeadquartersMemory.open(data_root, profile_id)
        try:
            changer.sessions.close_session(
                session_id,
                debrief_summary="Close the prior Session before changing diagnosis context.",
                next_action="Start the newer Session objective.",
            )
            changer.sessions.start_session(song_id=song_id, objective="A newer Session objective")
        finally:
            changer.close()

        status, page = get(shell, "/song")
        assert status == 200
        assert "<h3>What I know</h3>" not in page
        assert "A newer Session objective" in page
    finally:
        shell.stop()
