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


def suggestion_title(page: str) -> str:
    match = re.search(r'<h3>One prompt to try</h3>.*?<p><strong>([^<]+)</strong></p>', page, flags=re.DOTALL)
    assert match is not None
    return match.group(1)


def seed_song(data_root: Path) -> None:
    hq = HeadquartersMemory.create(data_root, "Not Now Consumer")
    try:
        song = hq.store.create_song("Signal Bloom")
        hq.sessions.start_session(
            song_id=song.id,
            objective="Keep momentum without chasing every idea",
        )
    finally:
        hq.close()


def request_suggestion(shell: ConsumerShell) -> str:
    _, page = get(shell, "/song")
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


def test_not_now_is_protected_durable_and_skips_same_key_after_relaunch(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed_song(data_root)

    shell = new_shell(data_root, state_root, 13101, "not-now-first")
    try:
        first = request_suggestion(shell)
        first_title = suggestion_title(first)
        assert "Not now · later this Song" in first
        assert "semantic_key" not in first
        not_now = action_for(first, "/suggestion/not-now")

        status, deferred = post(
            shell,
            "/suggestion/not-now",
            {"csrf": shell._csrf, "action": not_now},
        )
        assert status == 200
        assert "Not now remembered for this Song work Session" in deferred
        assert "One prompt to try" not in deferred

        status, replay = post(
            shell,
            "/suggestion/not-now",
            {"csrf": shell._csrf, "action": not_now},
        )
        assert status == 409
        assert "already handled or expired" in replay
    finally:
        shell.stop()

    relaunched = new_shell(data_root, state_root, 13102, "not-now-relaunch")
    try:
        second = request_suggestion(relaunched)
        assert suggestion_title(second) != first_title
        assert "Not now · later this Song" in second
        assert "semantic_key" not in second
    finally:
        relaunched.stop()
