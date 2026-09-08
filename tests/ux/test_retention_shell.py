from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=9188,
        start_token="retention-shell",
    )


def get(shell: ConsumerShell, path: str) -> tuple[int, str]:
    request = Request(shell.address.origin + path, method="GET")
    try:
        with urlopen(request, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_song_page_consults_retained_thread_without_internal_ids(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq = HeadquartersMemory.create(data_root, "Retention Artist")
    song = hq.store.create_song("Remember This Song")
    version = hq.store.create_version(song.id, label="v1")
    session = hq.sessions.start_session(
        song_id=song.id,
        version_id=version.id,
        objective="Protect the vocal while making the chorus bigger",
    )
    note = hq.sessions.append_scratch(
        session.id,
        kind="OBSERVATION",
        body="The vocal melody must stay unchanged",
    )
    hq.sessions.promote_item(
        note.id,
        scope_kind="SONG",
        key="vocal.constraint",
        source_kind="USER_DECLARED",
        twin_domain="CREATIVE",
        confidence=1.0,
    )
    episode = hq.learning.create_episode(
        session_id=session.id,
        domain="ARRANGEMENT",
        subject_ref="chorus.energy",
        change_description="Remove one support layer before the second half",
    )
    hq.learning.append_consequence(
        episode.id,
        observation="The second half feels larger by contrast",
        source_kind="USER_DECLARED",
        source_ref="artist:retention-shell:listen",
        confidence=0.75,
    )
    hq.learning.decide(
        episode.id,
        decision="KEEP",
        rationale="The contrast helps this version without changing the vocal",
        confidence=0.7,
    )
    hq.sessions.close_session(
        session.id,
        debrief_summary="Contrast improved without changing the topline",
        next_action="Check the pre-chorus transition next",
    )
    profile_id = hq.store.profile_id
    internal_ids = (session.id, note.id, episode.id)
    hq.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(),
        probe=Probe(),
    )
    shell.start()
    try:
        status, text = get(shell, "/song")
        assert status == 200
        assert "What N0TE remembers" in text
        assert "Retention active" in text
        assert "Protect the vocal while making the chorus bigger" in text
        assert "Next: Check the pre-chorus transition next" in text
        assert "vocal.constraint" in text
        assert "The vocal melody must stay unchanged" in text
        assert "read-only" in text
        assert "one kept result does not become permanent taste doctrine or a causal rule" in text
        assert profile_id not in text
        for internal_id in internal_ids:
            assert internal_id not in text
        for prefix in ("claim_", "sess_", "sitem_", "learn_", "lobs_", "ldec_"):
            assert prefix not in text
    finally:
        shell.stop(timeout=2.0)
