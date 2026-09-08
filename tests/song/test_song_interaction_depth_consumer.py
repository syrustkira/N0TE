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
from n0te.instance import ProcessIdentity
from n0te.interaction_depth_shell import install_song_interaction_depth
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


def form_with_label(page: str, action: str, phrase: str) -> Form:
    matches = [item for item in forms(page, action) if phrase in item.label]
    if len(matches) != 1:
        raise AssertionError(f"expected one {action} form containing {phrase!r}, got {len(matches)}")
    return matches[0]


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


def new_shell(
    data_root: Path,
    state_root: Path,
    pid: int,
    token: str,
) -> ConsumerShell:
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(pid, token),
        probe=Probe(),
    )
    shell.start()
    return shell


def post(
    shell: ConsumerShell,
    path: str,
    fields: dict[str, str],
    *,
    origin: str | None = None,
) -> tuple[int, str, str | None]:
    return request(
        shell,
        path,
        method="POST",
        fields=fields,
        origin=shell.address.origin if origin is None else origin,
    )


def seed(data_root: Path):
    hq = HeadquartersMemory.create(data_root, "Interaction Artist")
    try:
        song = hq.store.create_song("Interaction Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Improve the chorus")
        episode = hq.learning.create_episode(
            session_id=session.id,
            domain="ARRANGEMENT",
            subject_ref="chorus impact",
            change_description="Mute the pre-chorus kick for one bar before the chorus",
        )
        return hq.store.profile_id, song.id, session.id, episode.id
    finally:
        hq.close()


class SongInteractionDepthConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data_root = (root / "data").resolve()
        self.state_root = (root / "state").resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_song_exposes_five_modes_without_internal_lineage_or_get_mutation(self) -> None:
        profile_id, song_id, session_id, episode_id = seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9971, "interaction-visible")
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertEqual(page.count("<h2>How should N0TE work with you?</h2>"), 1)
            self.assertEqual(len(forms(page, "/interaction/depth")), 5)
            for label in ("DO IT", "WITH ME", "SHOW ME", "EXPLAIN WHY", "LET ME TRY"):
                self.assertIn(label, page)
            self.assertIn("separate from ASK / ADVISE / TRY / DO authority", page)
            self.assertEqual(request(shell, "/song")[0], 200)
            for forbidden in (song_id, session_id, episode_id):
                self.assertNotIn(forbidden, page)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            episode = reopened.learning.get_episode(episode_id)
            self.assertIsNotNone(episode)
            self.assertEqual(episode.consequences, ())
            self.assertIsNone(episode.decision)
            self.assertEqual(
                reopened.store._conn.execute("SELECT COUNT(*) FROM skill_assessments").fetchone()[0],
                0,
            )
        finally:
            reopened.close()

    def test_do_it_and_let_me_try_have_opposite_agency_and_usable_steps(self) -> None:
        seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9972, "interaction-agency")
        try:
            _, page, _ = request(shell, "/song")
            do_it = form_with_label(page, "/interaction/depth", "DO IT")
            status, _, location = post(shell, "/interaction/depth", do_it.values)
            self.assertEqual(status, 303)
            self.assertEqual(location, "/song")

            _, page, _ = request(shell, "/song")
            self.assertIn("Working style: DO IT", page)
            self.assertIn("Work it this way", page)
            self.assertIn("Do not silently broaden the job", page)
            self.assertIn("Resolve whether a verified executor exists", page)
            self.assertIn("separate exact authority", page)
            self.assertIn("Execution requested, but not granted here", page)
            self.assertIn("does not grant capability, approval, eligibility, or mutation authority", page)
            self.assertIn("will not claim the project changed", page)

            let_me_try = form_with_label(page, "/interaction/depth", "LET ME TRY")
            status, _, _ = post(shell, "/interaction/depth", let_me_try.values)
            self.assertEqual(status, 303)
            _, page, _ = request(shell, "/song")
            self.assertIn("Working style: LET ME TRY", page)
            self.assertIn("Try only this bounded change yourself", page)
            self.assertIn("Stand back while you act", page)
            self.assertIn("No execution requested by this interaction mode", page)
            self.assertIn("never approves a mutation", page)
        finally:
            shell.stop()

    def test_show_me_is_visible_as_read_only_before_change_walkthrough(self) -> None:
        seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9978, "interaction-show")
        try:
            _, page, _ = request(shell, "/song")
            show = form_with_label(page, "/interaction/depth", "SHOW ME")
            status, _, _ = post(shell, "/interaction/depth", show.values)
            self.assertEqual(status, 303)
            _, page, _ = request(shell, "/song")
            self.assertIn("Working style: SHOW ME", page)
            self.assertIn("read-only walkthrough", page)
            self.assertIn("BEFORE", page)
            self.assertIn("AFTER", page)
            self.assertIn("does not claim the project was modified", page)
            self.assertIn("No consequence has been recorded yet", page)
        finally:
            shell.stop()

    def test_origin_csrf_and_replay_fail_closed(self) -> None:
        profile_id, song_id, _, episode_id = seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9973, "interaction-security")
        try:
            _, page, _ = request(shell, "/song")
            form = form_with_label(page, "/interaction/depth", "SHOW ME")
            fields = dict(form.values)

            status, _, _ = post(
                shell,
                "/interaction/depth",
                fields,
                origin="https://evil.example",
            )
            self.assertEqual(status, 403)

            bad_csrf = dict(fields)
            bad_csrf["csrf"] = "wrong"
            status, _, _ = post(shell, "/interaction/depth", bad_csrf)
            self.assertEqual(status, 403)

            status, _, _ = post(shell, "/interaction/depth", fields)
            self.assertEqual(status, 303)
            status, _, _ = post(shell, "/interaction/depth", fields)
            self.assertEqual(status, 409)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            episode = reopened.learning.get_episode(episode_id)
            self.assertIsNotNone(episode)
            self.assertEqual(episode.song_id, song_id)
            self.assertEqual(episode.consequences, ())
            self.assertIsNone(episode.decision)
        finally:
            reopened.close()

    def test_learning_evidence_change_invalidates_old_binding_preserves_uncertainty_and_closed_job_clears_selection(self) -> None:
        profile_id, _, _, episode_id = seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9974, "interaction-stale")
        try:
            _, page, _ = request(shell, "/song")
            stale_mode = form_with_label(page, "/interaction/depth", "WITH ME")
            observe = form_with_label(page, "/learning/observe", "Record observation")
            observe_fields = {
                **observe.values,
                "observation": "The chorus entrance felt larger",
                "confidence": "MEDIUM",
                "conditions": "Same playback level",
                "confounders": "Arrangement contrast may also matter",
            }
            status, _, _ = post(shell, "/learning/observe", observe_fields)
            self.assertEqual(status, 303)

            status, body, _ = post(shell, "/interaction/depth", stale_mode.values)
            self.assertEqual(status, 409)
            self.assertIn("changed after this interaction choice was prepared", body)

            _, page, _ = request(shell, "/song")
            explain = form_with_label(page, "/interaction/depth", "EXPLAIN WHY")
            status, _, _ = post(shell, "/interaction/depth", explain.values)
            self.assertEqual(status, 303)
            _, page, _ = request(shell, "/song")
            self.assertIn("Working style: EXPLAIN WHY", page)
            self.assertIn("The chorus entrance felt larger", page)
            self.assertIn("artist-reported, 70% confidence", page)
            self.assertIn("Conditions: Same playback level", page)
            self.assertIn("Possible confounders: Arrangement contrast may also matter", page)
            self.assertIn("question, not established causation", page)
            self.assertIn("changing fewer variables", page)

            decide = form_with_label(page, "/learning/decide", "Decide")
            decide_fields = {
                **decide.values,
                "decision": "INCONCLUSIVE",
                "rationale": "Arrangement contrast is still a plausible alternative explanation",
                "confidence": "MEDIUM",
            }
            status, _, _ = post(shell, "/learning/decide", decide_fields)
            self.assertEqual(status, 303)
            _, closed, _ = request(shell, "/song")
            self.assertNotIn("Working style: EXPLAIN WHY", closed)
            self.assertIn("That working style ended with the previous Learning job", closed)
            self.assertIn("Start a new Learning experiment", closed)
            self.assertEqual(forms(closed, "/interaction/depth"), [])
            self.assertNotIn("Choose a mode again from the current job below", closed)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            episode = reopened.learning.get_episode(episode_id)
            self.assertIsNotNone(episode)
            self.assertEqual(len(episode.consequences), 1)
            self.assertIsNotNone(episode.decision)
            self.assertEqual(episode.decision.decision, "INCONCLUSIVE")
        finally:
            reopened.close()

    def test_mode_selection_is_ephemeral_across_relaunch(self) -> None:
        profile_id, _, _, episode_id = seed(self.data_root)
        first = new_shell(self.data_root, self.state_root, 9975, "interaction-first")
        try:
            _, page, _ = request(first, "/song")
            explain = form_with_label(page, "/interaction/depth", "EXPLAIN WHY")
            status, _, _ = post(first, "/interaction/depth", explain.values)
            self.assertEqual(status, 303)
            _, page, _ = request(first, "/song")
            self.assertIn("Working style: EXPLAIN WHY", page)
        finally:
            first.stop()

        second = new_shell(self.data_root, self.state_root, 9976, "interaction-second")
        try:
            status, page, _ = request(second, "/song")
            self.assertEqual(status, 200)
            self.assertNotIn("Working style: EXPLAIN WHY", page)
            self.assertEqual(len(forms(page, "/interaction/depth")), 5)
        finally:
            second.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            episode = reopened.learning.get_episode(episode_id)
            self.assertIsNotNone(episode)
            self.assertEqual(episode.consequences, ())
            self.assertIsNone(episode.decision)
            self.assertEqual(
                reopened.store._conn.execute("SELECT COUNT(*) FROM skill_assessments").fetchone()[0],
                0,
            )
        finally:
            reopened.close()

    def test_extension_install_is_idempotent(self) -> None:
        seed(self.data_root)
        install_song_interaction_depth()
        install_song_interaction_depth()
        shell = new_shell(self.data_root, self.state_root, 9977, "interaction-idempotent")
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertEqual(page.count("<h2>How should N0TE work with you?</h2>"), 1)
        finally:
            shell.stop()


if __name__ == "__main__":
    unittest.main()
