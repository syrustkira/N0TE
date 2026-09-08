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


def action_for_label(page: str, label: str) -> str:
    match = re.search(
        rf'<form[^>]+aria-label="{re.escape(label)}".*?'
        r'name="action" value="([^"]+)"',
        page,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def action_for_path(page: str, path: str) -> str:
    match = re.search(
        rf'<form[^>]+action="{re.escape(path)}".*?'
        r'name="action" value="([^"]+)"',
        page,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def suggestion_title(page: str) -> str:
    match = re.search(
        r'<h3>One prompt to try</h3>.*?<p><strong>([^<]+)</strong></p>',
        page,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def seed_song(data_root: Path) -> str:
    hq = HeadquartersMemory.create(data_root, "Suggestion Feedback Consumer")
    try:
        song = hq.store.create_song("Soft Signal")
        hq.sessions.start_session(
            song_id=song.id,
            objective="Explore without turning one click into a taste rule",
        )
        return hq.store.profile_id
    finally:
        hq.close()


def request_suggestion(shell: ConsumerShell, *, distance: str = "ADJACENT") -> str:
    _, page = get(shell, "/song")
    status, result = post(
        shell,
        "/suggestion/create",
        {
            "csrf": shell._csrf,
            "action": action_for_path(page, "/suggestion/create"),
            "distance": distance,
        },
    )
    assert status == 200
    assert "One prompt to try" in result
    return result


def feedback_history(data_root: Path, profile_id: str):
    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        return hq.suggestion_feedback.history()
    finally:
        hq.close()


def test_more_and_less_are_protected_soft_evidence_without_auto_weighting(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id = seed_song(data_root)

    shell = new_shell(data_root, state_root, 14101, "feedback-first")
    try:
        first = request_suggestion(shell)
        title = suggestion_title(first)
        assert "More like this" in first
        assert "Less like this" in first
        assert "does not silently become a taste rule" in first
        assert "semantic_key" not in first

        more = action_for_label(first, "More like this suggestion")
        status, remembered = post(
            shell,
            "/suggestion/feedback",
            {"csrf": shell._csrf, "action": more},
        )
        assert status == 200
        assert "More like this remembered as context" in remembered
        assert suggestion_title(remembered) == title

        status, replay = post(
            shell,
            "/suggestion/feedback",
            {"csrf": shell._csrf, "action": more},
        )
        assert status == 409
        assert "already handled or expired" in replay

        less = action_for_label(remembered, "Less like this suggestion")
        status, opposite = post(
            shell,
            "/suggestion/feedback",
            {"csrf": shell._csrf, "action": less},
        )
        assert status == 200
        assert "Less like this remembered as context" in opposite
        assert suggestion_title(opposite) == title
    finally:
        shell.stop()

    events = feedback_history(data_root, profile_id)
    assert [event.direction for event in events] == ["MORE", "LESS"]
    assert all(event.preference_promoted is False for event in events)
    assert all(event.automatic_weighting_applied is False for event in events)
    assert all(event.learning_promoted is False for event in events)
    assert all(event.song_mutation_authorized is False for event in events)
    assert all(event.external_action_authorized is False for event in events)


def test_feedback_fails_closed_on_foreign_origin_and_changed_suggestion(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id = seed_song(data_root)
    shell = new_shell(data_root, state_root, 14102, "feedback-stale")
    try:
        page = request_suggestion(shell, distance="ADJACENT")
        token = action_for_label(page, "More like this suggestion")

        status, blocked = post(
            shell,
            "/suggestion/feedback",
            {"csrf": shell._csrf, "action": token},
            origin="http://evil.invalid",
        )
        assert status == 403
        assert "did not come from this N0TE window" in blocked

        request_suggestion(shell, distance="WILDCARD")

        status, stale = post(
            shell,
            "/suggestion/feedback",
            {"csrf": shell._csrf, "action": token},
        )
        assert status == 409
        assert (
            "already handled or expired" in stale
            or "current suggestion changed" in stale
        )
    finally:
        shell.stop()

    assert feedback_history(data_root, profile_id) == ()
