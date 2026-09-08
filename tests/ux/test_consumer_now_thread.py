from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from n0te import HeadquartersMemory
from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.now_thread_shell import install_now_thread
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


def request(shell: ConsumerShell, path: str) -> tuple[int, str]:
    req = Request(shell.address.origin + path, method="GET")
    try:
        with build_opener().open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def new_shell(data_root: Path, state_root: Path, pid: int, token: str) -> ConsumerShell:
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(pid, token),
        probe=Probe(),
    )
    shell.start()
    return shell


def event_count(data_root: Path, profile_id: str) -> int:
    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        return int(hq.store._conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0])
    finally:
        hq.close()


def test_now_thread_shows_open_session_objective_without_writing(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq = HeadquartersMemory.create(data_root, "Thread Artist")
    try:
        song = hq.store.create_song("Thread Song")
        session = hq.sessions.start_session(
            song_id=song.id,
            objective="Decide whether the chorus needs one more vocal layer",
        )
        profile_id = hq.store.profile_id
        song_id = song.id
        session_id = session.id
    finally:
        hq.close()

    before = event_count(data_root, profile_id)
    shell = new_shell(data_root, state_root, 9801, "now-open")
    try:
        status, page = request(shell, "/now")
        assert status == 200
        assert page.count("<h2>Pick up the thread</h2>") == 1
        assert "Thread Song" in page
        assert "Work Session open" in page
        assert "Decide whether the chorus needs one more vocal layer" in page
        assert "Continue this Song" in page
        assert song_id not in page
        assert session_id not in page
    finally:
        shell.stop()
    assert event_count(data_root, profile_id) == before


def test_now_thread_shows_closed_session_next_action(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq = HeadquartersMemory.create(data_root, "Resume Artist")
    try:
        song = hq.store.create_song("Resume Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Check the transition")
        hq.sessions.close_session(
            session.id,
            debrief_summary="The transition works when the last drum fill is shorter",
            next_action="Print one shorter fill and compare it at matched playback level",
        )
    finally:
        hq.close()

    shell = new_shell(data_root, state_root, 9802, "now-closed")
    try:
        status, page = request(shell, "/now")
        assert status == 200
        assert "Resume Song" in page
        assert "Last Session closed" in page
        assert "Next action" in page
        assert "Print one shorter fill and compare it at matched playback level" in page
        assert "Pick up this Song" in page
    finally:
        shell.stop()


def test_now_thread_handles_song_without_session_and_profile_without_song(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq = HeadquartersMemory.create(data_root, "Empty Artist")
    try:
        profile_id = hq.store.profile_id
    finally:
        hq.close()

    no_song = new_shell(data_root, state_root, 9803, "now-no-song")
    try:
        status, page = request(no_song, "/now")
        assert status == 200
        assert "No active Song yet" in page
        assert "Start a Song" in page
    finally:
        no_song.stop()

    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        hq.store.create_song("Fresh Song")
    finally:
        hq.close()

    no_session = new_shell(data_root, state_root, 9804, "now-no-session")
    try:
        status, page = request(no_session, "/now")
        assert status == 200
        assert "Fresh Song" in page
        assert "No work Session yet" in page
        assert "Start a work Session" in page
    finally:
        no_session.stop()


def test_now_thread_install_is_idempotent(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq = HeadquartersMemory.create(data_root, "Idempotent Artist")
    try:
        hq.store.create_song("Idempotent Song")
    finally:
        hq.close()

    install_now_thread()
    install_now_thread()
    shell = new_shell(data_root, state_root, 9805, "now-idempotent")
    try:
        status, page = request(shell, "/now")
        assert status == 200
        assert page.count("<h2>Pick up the thread</h2>") == 1
    finally:
        shell.stop()
