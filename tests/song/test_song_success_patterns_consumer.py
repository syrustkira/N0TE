from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te import HeadquartersMemory
from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.platforms import PlatformEnvironment
from n0te.success_patterns_shell import install_song_success_patterns


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


def request(shell: ConsumerShell, path: str) -> tuple[int, str]:
    req = Request(shell.address.origin + path, method="GET")
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def add_completed(
    hq: HeadquartersMemory,
    song_id: str,
    *,
    decision: str,
    observation: str,
    source_kind: str,
    confidence: float,
    conditions: tuple[str, ...] = (),
    confounders: tuple[str, ...] = (),
):
    session = hq.sessions.start_session(song_id=song_id, objective="Pattern test")
    episode = hq.learning.create_episode(
        session_id=session.id,
        domain="Mixing",
        subject_ref="Vocal compression",
        change_description="Lengthen compressor attack",
    )
    consequence = hq.learning.append_consequence(
        episode.id,
        observation=observation,
        source_kind=source_kind,
        source_ref=f"test-source:{session.id}",
        confidence=confidence,
        conditions=conditions,
        confounders=confounders,
    )
    hq.learning.decide(
        episode.id,
        decision=decision,
        rationale=f"{decision} rationale",
        confidence=confidence,
    )
    hq.sessions.close_session(
        session.id,
        debrief_summary=f"{decision} pattern test completed",
        next_action="Continue evaluating the Song",
    )
    return episode, consequence


def seed(data_root: Path) -> tuple[str, str, tuple[str, ...]]:
    hq = HeadquartersMemory.create(data_root, "Pattern Artist")
    try:
        song = hq.store.create_song("Pattern Song")
        first, c1 = add_completed(
            hq,
            song.id,
            decision="KEEP",
            observation="The vocal consonants felt clearer.",
            source_kind="USER_DECLARED",
            confidence=0.7,
            conditions=("matched monitor level",),
            confounders=("short ear break",),
        )
        second, c2 = add_completed(
            hq,
            song.id,
            decision="REVERT",
            observation="The vocal lost too much density in the verse.",
            source_kind="MEASURED",
            confidence=0.9,
            conditions=("same vocal take",),
            confounders=("different arrangement section",),
        )
        # A Learning episode can remain pending after its work Session closes. This
        # preserves pending decision evidence without violating the one-open-Session
        # invariant needed to seed unrelated Song evidence.
        session = hq.sessions.start_session(song_id=song.id, objective="Pending pattern test")
        pending = hq.learning.create_episode(
            session_id=session.id,
            domain="Mixing",
            subject_ref="Vocal compression",
            change_description="Lengthen compressor attack",
        )
        hq.sessions.close_session(
            session.id,
            debrief_summary="Pending Learning decision remains open",
            next_action="Return when there is enough evidence to decide",
        )
        foreign = hq.store.create_song("Other Song")
        add_completed(
            hq,
            foreign.id,
            decision="KEEP",
            observation="Foreign song evidence must stay out.",
            source_kind="OBSERVED",
            confidence=1.0,
        )
        hq.store.select_song(song.id)
        internal = (first.id, second.id, pending.id, c1.id, c2.id, c1.source_ref, c2.source_ref)
        return hq.store.profile_id, song.id, internal
    finally:
        hq.close()


def test_song_success_card_shows_mixed_pattern_without_internal_identity(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, internal = seed(data_root)
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9951, "success-visible"),
        probe=Probe(),
    )
    shell.start()
    try:
        status, page = request(shell, "/song")
        assert status == 200
        assert page.count("<h2>What does your past work suggest?</h2>") == 1
        assert "Mixed evidence" in page
        assert "Comparable episodes disagree" in page
        assert "Association only. This pattern is prior evidence, not a recipe or prediction." in page
        assert "2 completed · 1 pending · 1 keep · 1 revert · 0 revise · 0 inconclusive" in page
        assert "The vocal consonants felt clearer." in page
        assert "artist-reported" in page
        assert "The vocal lost too much density in the verse." in page
        assert "measured" in page
        assert "matched monitor level" in page
        assert "different arrangement section" in page
        assert "Foreign song evidence must stay out." not in page
        for value in internal:
            assert value not in page
        assert "success_" not in page

        status, again = request(shell, "/song")
        assert status == 200
        assert again.count("<h2>What does your past work suggest?</h2>") == 1
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        # Two GETs remain pure: seed created exactly three Learning episodes for the
        # active Song and the read-only success projection did not append anything.
        assert len(reopened.learning.episodes_for_song(song_id)) == 3
        assert len(reopened.success.patterns_for_song(song_id)) == 1
    finally:
        reopened.close()


def test_success_extension_is_idempotent_and_relaunch_stable(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _, _ = seed(data_root)
    install_song_success_patterns()
    install_song_success_patterns()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9952, "success-first"),
        probe=Probe(),
    )
    shell.start()
    try:
        status, page = request(shell, "/song")
        assert status == 200
        assert page.count("<h2>What does your past work suggest?</h2>") == 1
        expected_fragment = "2 completed · 1 pending · 1 keep · 1 revert"
        assert expected_fragment in page
    finally:
        shell.stop()

    shell2 = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9953, "success-second"),
        probe=Probe(),
    )
    shell2.start()
    try:
        status, page = request(shell2, "/song")
        assert status == 200
        assert page.count("<h2>What does your past work suggest?</h2>") == 1
        assert "2 completed · 1 pending · 1 keep · 1 revert" in page
    finally:
        shell2.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert len(reopened.success.patterns_for_song(reopened.store.active_song().id)) == 1
    finally:
        reopened.close()
