import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment
from n0te.suggestion_deferral import NEVER_SUGGEST_AGAIN, NEXT_SONG


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process(pid: int, token: str) -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=token,
    )


def new_shell(data_root: Path, state_root: Path, pid: int, token: str) -> ConsumerShell:
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


def post(shell: ConsumerShell, path: str, fields: dict[str, str]) -> tuple[int, str]:
    payload = urlencode(fields).encode("utf-8")
    req = Request(shell.address.origin + path, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Origin", shell.address.origin)
    try:
        with build_opener().open(req, timeout=3.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def action_for(page: str, path: str) -> str:
    match = re.search(
        rf'<form[^>]+action="{re.escape(path)}".*?name="action" value="([^"]+)"',
        page,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def action_for_aria(page: str, aria_label: str) -> str:
    match = re.search(
        rf'<form[^>]+aria-label="{re.escape(aria_label)}"[^>]*>.*?name="action" value="([^"]+)"',
        page,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def request_suggestion(shell: ConsumerShell) -> str:
    status, page = get(shell, "/song")
    assert status == 200
    status, result = post(
        shell,
        "/suggestion/create",
        {
            "csrf": shell._csrf,
            "action": action_for(page, "/suggestion/create"),
            "distance": "ADJACENT",
        },
    )
    assert status == 200
    assert "One prompt to try" in result
    return result


def seed(data_root: Path, *, session: bool) -> tuple[str, str]:
    hq = HeadquartersMemory.create(data_root, "Horizon Consumer")
    try:
        song = hq.store.create_song("Source Song")
        if session:
            hq.sessions.start_session(
                song_id=song.id,
                objective="Protect attention while exploring one idea",
            )
        return hq.store.profile_id, song.id
    finally:
        hq.close()


def test_next_song_choice_is_token_bound_and_crossing_horizon_ends_suppression(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, source_song_id = seed(data_root, session=True)
    shell = new_shell(data_root, state_root, 13201, "horizon-next")
    try:
        result = request_suggestion(shell)
        assert "Not now · later this Song" in result
        assert "Save for next Song" in result
        assert "Never suggest this again" in result
        assert "After release" not in result
        assert "Someday" not in result
        assert "These choices change suggestion visibility only and grant no action authority." in result

        next_action = action_for_aria(
            result,
            "Defer this suggestion until the Artist moves to another Song",
        )
        status, deferred = post(
            shell,
            "/suggestion/not-now",
            {
                "csrf": shell._csrf,
                "action": next_action,
                # Hostile client input must not upgrade the server-bound horizon.
                "scope": NEVER_SUGGEST_AGAIN,
            },
        )
        assert status == 200
        assert "Saved until the next Song" in deferred

        status, replay = post(
            shell,
            "/suggestion/not-now",
            {"csrf": shell._csrf, "action": next_action},
        )
        assert status == 409
        assert "already handled or expired" in replay
    finally:
        shell.stop()

    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        history = hq.suggestion_deferrals.history()
        assert len(history) == 1
        record = history[0]
        assert record.scope == NEXT_SONG
        assert record.song_id == source_song_id
        assert hq.suggestion_deferrals.is_deferred_now(record.semantic_key)

        other = hq.store.create_song("Actually Next Song")
        hq.sessions.start_session(
            song_id=other.id,
            objective="Cross the deferred horizon",
        )
        assert not hq.suggestion_deferrals.is_deferred_now(record.semantic_key)
        hq.store.select_song(source_song_id)
        assert not hq.suggestion_deferrals.is_deferred_now(record.semantic_key)
    finally:
        hq.close()


def test_no_session_offers_next_song_and_never_but_not_session_horizon(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _ = seed(data_root, session=False)
    shell = new_shell(data_root, state_root, 13202, "horizon-no-session")
    try:
        result = request_suggestion(shell)
        assert "Not now · later this Song" not in result
        assert "Save for next Song" in result
        assert "Never suggest this again" in result
        assert "After release" not in result
        assert "Someday" not in result

        never_action = action_for_aria(
            result,
            "Suppress this exact suggestion pattern across this Artist profile",
        )
        status, deferred = post(
            shell,
            "/suggestion/not-now",
            {"csrf": shell._csrf, "action": never_action},
        )
        assert status == 200
        assert "Never suggest again remembered for this Artist" in deferred
    finally:
        shell.stop()

    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        history = hq.suggestion_deferrals.history()
        assert len(history) == 1
        record = history[0]
        assert record.scope == NEVER_SUGGEST_AGAIN
        assert record.session_id is None

        other = hq.store.create_song("Other Song")
        assert other.id != record.song_id
        assert hq.suggestion_deferrals.is_deferred_now(record.semantic_key)
    finally:
        hq.close()


def test_stale_never_action_cannot_promote_old_session_suggestion_to_artist_preference(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _ = seed(data_root, session=True)
    shell = new_shell(data_root, state_root, 13203, "horizon-stale")
    try:
        result = request_suggestion(shell)
        never_action = action_for_aria(
            result,
            "Suppress this exact suggestion pattern across this Artist profile",
        )

        finish_action = action_for(result, "/session/finish")
        status, finished = post(
            shell,
            "/session/finish",
            {
                "csrf": shell._csrf,
                "action": finish_action,
                "debrief": "Changed the work context before using the old suggestion action",
                "next_action": "Start a fresh Session",
            },
        )
        assert status == 200
        start_action = action_for(finished, "/session/start")
        status, restarted = post(
            shell,
            "/session/start",
            {
                "csrf": shell._csrf,
                "action": start_action,
                "objective": "Fresh work context",
            },
        )
        assert status == 200
        assert "Work Session started" in restarted

        status, rejected = post(
            shell,
            "/suggestion/not-now",
            {"csrf": shell._csrf, "action": never_action},
        )
        assert status == 409
        assert "already handled or expired" in rejected
    finally:
        shell.stop()

    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert hq.suggestion_deferrals.history() == ()
    finally:
        hq.close()
