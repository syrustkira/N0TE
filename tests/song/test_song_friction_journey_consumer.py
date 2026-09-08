from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from n0te import HeadquartersMemory
from n0te.consumer_shell import ConsumerShell
from n0te.friction_journey_shell import install_song_friction_journey
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


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass
class Form:
    action: str
    label: str
    values: dict[str, str]


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self.current: Form | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form":
            self.current = Form(
                action=str(values.get("action", "")),
                label=str(values.get("aria-label", "")),
                values={},
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
    return [item for item in parser.forms if item.action == action]


def request(
    shell: ConsumerShell,
    path: str,
    *,
    method: str = "GET",
    fields: dict[str, str] | None = None,
    origin: str | None = None,
) -> tuple[int, str, str | None]:
    headers: dict[str, str] = {}
    data = None
    if fields is not None:
        data = urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if origin is not None:
        headers["Origin"] = origin
    req = Request(shell.address.origin + path, data=data, method=method, headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8"), response.headers.get("Location")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers.get("Location")


def new_shell(data_root: Path, state_root: Path, pid: int, token: str) -> ConsumerShell:
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(pid, token),
        probe=Probe(),
    )
    shell.start()
    return shell


def post(shell: ConsumerShell, fields: dict[str, str], *, origin: str | None = None):
    return request(
        shell,
        "/friction/record",
        method="POST",
        fields=fields,
        origin=shell.address.origin if origin is None else origin,
    )


def add_episode(hq: HeadquartersMemory, song_id: str, objective: str):
    session = hq.sessions.start_session(song_id=song_id, objective=objective)
    episode = hq.learning.create_episode(
        session_id=session.id,
        domain="PROCESS",
        subject_ref="creative.flow",
        change_description=f"Observe {objective.lower()} without inventing a cause",
    )
    hq.sessions.close_session(
        session.id,
        debrief_summary="Captured work honestly",
        next_action="Continue the Song",
    )
    return session, episode


def seed(data_root: Path, *, episode_count: int = 1):
    hq = HeadquartersMemory.create(data_root, "Friction Artist")
    try:
        song = hq.store.create_song("Friction Song")
        episodes = [
            add_episode(hq, song.id, f"Pass {index}")
            for index in range(1, episode_count + 1)
        ]
        return hq.store.profile_id, song.id, episodes
    finally:
        hq.close()


def friction_fields(
    form: Form,
    *,
    key: str = "context-switching",
    description: str = "Notifications broke focus",
):
    return {
        **form.values,
        "friction_key": key,
        "description": description,
        "confidence": "MEDIUM",
        "prevention_hint": "Silence notifications before focused work",
    }


class SongFrictionConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data_root = (root / "data").resolve()
        self.state_root = (root / "state").resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_song_surface_explains_incident_vs_recurrence_hides_identity_and_get_is_pure(self) -> None:
        profile_id, song_id, seeded = seed(self.data_root)
        session, episode = seeded[0]
        shell = new_shell(self.data_root, self.state_root, 9961, "friction-visible")
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertEqual(page.count("<h2>What keeps getting in the way?</h2>"), 1)
            self.assertIn("One incident remains one incident.", page)
            self.assertIn("at least two distinct work Sessions", page)
            self.assertEqual(len(forms(page, "/friction/record")), 1)
            self.assertEqual(request(shell, "/song")[0], 200)
            for forbidden in (session.id, episode.id, "fric_", "consumer-friction:"):
                self.assertNotIn(forbidden, page)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            self.assertEqual(reopened.friction.observations(song_id=song_id), ())
        finally:
            reopened.close()

    def test_full_consumer_capture_and_distinct_session_recurrence(self) -> None:
        profile_id, song_id, seeded = seed(self.data_root, episode_count=2)
        shell = new_shell(self.data_root, self.state_root, 9962, "friction-flow")
        recurrence_page = ""
        first_label = ""
        try:
            _, page, _ = request(shell, "/song")
            available = forms(page, "/friction/record")
            self.assertEqual(len(available), 2)
            self.assertNotEqual(available[0].label, available[1].label)
            first_label = available[0].label

            status, _, location = post(
                shell,
                friction_fields(
                    available[0],
                    description="Message checking broke one pass",
                ),
            )
            self.assertEqual(status, 303)
            self.assertEqual(location, "/song")

            _, page, _ = request(shell, "/song")
            self.assertIn("No recurring blocker is established for this Song yet.", page)
            remaining = forms(page, "/friction/record")
            target = next(item for item in remaining if item.label != first_label)
            status, _, _ = post(
                shell,
                friction_fields(
                    target,
                    description="Notifications interrupted the other pass",
                ),
            )
            self.assertEqual(status, 303)

            _, recurrence_page, _ = request(shell, "/song")
            self.assertIn("Recurring across 2 work Sessions", recurrence_page)
            self.assertIn("2 explicit records", recurrence_page)
            self.assertIn("Prevention ideas you recorded", recurrence_page)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            observations = reopened.friction.observations(song_id=song_id)
            self.assertEqual(len(observations), 2)
            self.assertEqual({item.source_kind for item in observations}, {"USER_DECLARED"})
            self.assertTrue(
                all(item.source_ref.startswith("consumer-friction:") for item in observations)
            )
            self.assertEqual(len({item.session_id for item in observations}), 2)
            patterns = reopened.friction.recurring_patterns(song_id=song_id)
            self.assertEqual(len(patterns), 1)
            self.assertEqual(patterns[0].session_count, 2)
            for session, episode in seeded:
                self.assertNotIn(session.id, recurrence_page)
                self.assertNotIn(episode.id, recurrence_page)
            for item in observations:
                self.assertNotIn(item.id, recurrence_page)
                self.assertNotIn(item.episode_id, recurrence_page)
                self.assertNotIn(item.session_id, recurrence_page)
                self.assertNotIn(item.source_ref, recurrence_page)
        finally:
            reopened.close()

    def test_origin_csrf_and_replay_fail_closed(self) -> None:
        profile_id, song_id, _ = seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9963, "friction-security")
        try:
            _, page, _ = request(shell, "/song")
            form = forms(page, "/friction/record")[0]
            fields = friction_fields(form)

            status, _, _ = post(shell, fields, origin="https://evil.example")
            self.assertEqual(status, 403)

            bad_csrf = dict(fields)
            bad_csrf["csrf"] = "wrong"
            status, _, _ = post(shell, bad_csrf)
            self.assertEqual(status, 403)

            status, _, _ = post(shell, fields)
            self.assertEqual(status, 303)
            status, _, _ = post(shell, fields)
            self.assertEqual(status, 409)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            self.assertEqual(len(reopened.friction.observations(song_id=song_id)), 1)
        finally:
            reopened.close()

    def test_extension_install_is_idempotent(self) -> None:
        seed(self.data_root)
        install_song_friction_journey()
        install_song_friction_journey()
        shell = new_shell(self.data_root, self.state_root, 9964, "friction-idempotent")
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertEqual(page.count("<h2>What keeps getting in the way?</h2>"), 1)
        finally:
            shell.stop()


if __name__ == "__main__":
    unittest.main()
