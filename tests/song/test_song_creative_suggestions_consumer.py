import re
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


def post(shell: ConsumerShell, path: str, fields: dict[str, str], *, origin: str | None = None) -> tuple[int, str]:
    payload = urlencode(fields).encode("utf-8")
    req = Request(shell.address.origin + path, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if origin is not None:
        req.add_header("Origin", origin)
    try:
        with build_opener().open(req, timeout=3.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def suggestion_action(page: str) -> str:
    match = re.search(
        r'<form[^>]+action="/suggestion/create".*?name="action" value="([^"]+)"',
        page,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def seed_song(data_root: Path) -> None:
    hq = HeadquartersMemory.create(data_root, "Suggestion Consumer")
    try:
        song = hq.store.create_song("Signal Bloom")
        hq.sessions.start_session(
            song_id=song.id,
            objective="Find a stronger chorus lift without losing the intimate verse",
        )
    finally:
        hq.close()


def test_song_page_can_prepare_local_bounded_suggestion(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed_song(data_root)

    shell = new_shell(data_root, state_root, 12101, "suggest-local")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert page.count("<h2>Suggest something</h2>") == 1
        assert "Familiar · small move" in page
        assert "Adjacent · change one dimension" in page
        assert "Wildcard · deliberate contrast" in page
        assert "Keep Melody unchanged" in page
        token = suggestion_action(page)

        status, result = post(
            shell,
            "/suggestion/create",
            {
                "csrf": shell._csrf,
                "action": token,
                "distance": "ADJACENT",
                "lock_melody": "1",
            },
            origin=shell.address.origin,
        )
        assert status == 200
        assert "One prompt to try" in result
        assert "Adjacent" in result
        assert "Generated locally and deterministically" in result
        assert "No AI provider was called" in result
        assert "no project was changed" in result
        assert "not a claim about what your Song needs" in result
        assert "Find a stronger chorus lift" in result
        assert "semantic_key" not in result
        assert "arrangement:contrast-window" not in result
        assert "rhythm:single-groove-variable" not in result
        assert "melody:motif-variation" not in result
    finally:
        shell.stop()


def test_foreign_origin_is_rejected_before_action_consumption_and_replay_is_rejected(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed_song(data_root)

    shell = new_shell(data_root, state_root, 12102, "suggest-auth")
    try:
        _, page = get(shell, "/song")
        token = suggestion_action(page)
        fields = {
            "csrf": shell._csrf,
            "action": token,
            "distance": "FAMILIAR",
        }
        status, _ = post(shell, "/suggestion/create", fields, origin="https://example.invalid")
        assert status == 403

        status, result = post(shell, "/suggestion/create", fields, origin=shell.address.origin)
        assert status == 200
        assert "One prompt to try" in result

        status, replay = post(shell, "/suggestion/create", fields, origin=shell.address.origin)
        assert status == 409
        assert "already handled or expired" in replay
    finally:
        shell.stop()


def test_all_locks_fail_truthfully_and_ephemeral_result_does_not_survive_relaunch(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed_song(data_root)

    shell = new_shell(data_root, state_root, 12103, "suggest-locks")
    try:
        _, page = get(shell, "/song")
        fields = {
            "csrf": shell._csrf,
            "action": suggestion_action(page),
            "distance": "WILDCARD",
            "lock_arrangement": "1",
            "lock_rhythm": "1",
            "lock_harmony": "1",
            "lock_melody": "1",
            "lock_sound": "1",
            "lock_dynamics": "1",
        }
        status, blocked = post(shell, "/suggestion/create", fields, origin=shell.address.origin)
        assert status == 200
        assert "Every creative dimension is locked" in blocked

        token = suggestion_action(blocked)
        status, result = post(
            shell,
            "/suggestion/create",
            {"csrf": shell._csrf, "action": token, "distance": "WILDCARD"},
            origin=shell.address.origin,
        )
        assert status == 200
        assert "One prompt to try" in result
    finally:
        shell.stop()

    relaunched = new_shell(data_root, state_root, 12104, "suggest-relaunch")
    try:
        status, page = get(relaunched, "/song")
        assert status == 200
        assert "<h2>Suggest something</h2>" in page
        assert "One prompt to try" not in page
    finally:
        relaunched.stop()


def test_ephemeral_suggestion_is_dropped_when_active_song_changes(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed_song(data_root)

    shell = new_shell(data_root, state_root, 12105, "suggest-song-binding")
    try:
        _, page = get(shell, "/song")
        status, result = post(
            shell,
            "/suggestion/create",
            {
                "csrf": shell._csrf,
                "action": suggestion_action(page),
                "distance": "ADJACENT",
            },
            origin=shell.address.origin,
        )
        assert status == 200
        assert "One prompt to try" in result
        assert "Find a stronger chorus lift" in result

        # Change Song through the normal protected consumer path rather than
        # touching the shell runtime SQLite connection from the test thread.
        start_action = shell._new_action("song-start")
        status, new_song_page = post(
            shell,
            "/song/start",
            {
                "csrf": shell._csrf,
                "action": start_action,
                "song_title": "Second Signal",
            },
            origin=shell.address.origin,
        )
        assert status == 200
        assert "Second Signal" in new_song_page
        assert "One prompt to try" not in new_song_page
        assert "Find a stronger chorus lift" not in new_song_page
        assert getattr(shell, "_creative_suggestion_result", None) is None
    finally:
        shell.stop()
