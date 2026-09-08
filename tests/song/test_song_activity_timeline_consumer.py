from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from n0te import HeadquartersMemory
from n0te.activity_timeline_shell import install_song_activity_timeline
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


def get(shell: ConsumerShell, path: str) -> tuple[int, str]:
    request = Request(shell.address.origin + path, method="GET")
    try:
        with urlopen(request, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def seed(data_root: Path) -> tuple[str, str, tuple[str, ...]]:
    hq = HeadquartersMemory.create(data_root, "Timeline Artist")
    try:
        song = hq.store.create_song("Timeline Song")
        asset = hq.store.attach_asset(
            song.id,
            name="first-pass.wav",
            sha256="a" * 64,
            source_uri="file:///external/first-pass.wav",
        )
        first = hq.store.create_version(song.id, label="First pass", asset_ids=(asset.id,))
        hq.store.approve_version(song.id, first.id)
        session = hq.sessions.start_session(song_id=song.id, objective="Choose the stronger hook")
        hq.sessions.append_scratch(session.id, kind="DECISION", body="Keep the shorter intro")
        hq.sessions.close_session(
            session.id,
            debrief_summary="Hook is clearer",
            next_action="Record the next vocal pass",
        )
        hq.attention.start_focus("FINISH", song_id=song.id)
        hq.attention.end_focus()
        ids = tuple(event.id for event in hq.activity.for_song(song.id))
        return hq.store.profile_id, song.id, ids
    finally:
        hq.close()


def test_song_page_shows_one_read_only_artist_timeline_without_internal_history_data(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, activity_ids = seed(data_root)

    install_song_activity_timeline()
    install_song_activity_timeline()

    before = HeadquartersMemory.open(data_root, profile_id)
    try:
        checkpoint = before.activity.checkpoint()
        song_before = before.store.get_song(song_id)
        versions_before = before.store.versions_for_song(song_id)
    finally:
        before.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9701, "activity-consumer"),
        probe=Probe(),
    )
    shell.start()
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert page.count("<h2>What changed?</h2>") == 1
        assert 'aria-label="Song activity timeline"' in page
        assert "Newest recorded change first" in page
        assert "does not claim wall-clock times" in page
        assert "Focus ended" in page
        assert "Focus started" in page
        assert "Work Session finished" in page
        assert "Decision captured" in page
        assert "Work Session started" in page
        assert "Version approved" in page
        assert "Version 1: First pass" in page
        assert "Song material added" in page
        assert "first-pass.wav" in page
        assert page.index("Focus ended") < page.index("Focus started")
        assert page.index("Focus started") < page.index("Work Session finished")
        assert "activity_events" not in page
        assert "payload_json" not in page
        for activity_id in activity_ids:
            assert activity_id not in page
    finally:
        shell.stop()

    after = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert after.activity.checkpoint() == checkpoint
        assert after.store.get_song(song_id) == song_before
        assert after.store.versions_for_song(song_id) == versions_before
    finally:
        after.close()


def test_song_activity_timeline_survives_quit_relaunch_and_stays_song_scoped(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, _ = seed(data_root)

    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        other = hq.store.create_song("Other Song")
        other_asset = hq.store.attach_asset(
            other.id,
            name="other-secret.wav",
            sha256="b" * 64,
            source_uri="file:///external/other-secret.wav",
        )
        hq.store.create_version(other.id, label="Other secret version", asset_ids=(other_asset.id,))
        hq.store.select_song(song_id)
    finally:
        hq.close()

    first = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9702, "activity-first"),
        probe=Probe(),
    )
    first.start()
    try:
        status, page = get(first, "/song")
        assert status == 200
        assert "Version approved" in page
        assert "other-secret.wav" not in page
        assert "Other secret version" not in page
    finally:
        first.stop()

    second = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9703, "activity-second"),
        probe=Probe(),
    )
    second.start()
    try:
        status, relaunched = get(second, "/song")
        assert status == 200
        assert relaunched.count("<h2>What changed?</h2>") == 1
        assert "Version approved" in relaunched
        assert "Decision captured" in relaunched
        assert "other-secret.wav" not in relaunched
        assert "Other secret version" not in relaunched
    finally:
        second.stop()
