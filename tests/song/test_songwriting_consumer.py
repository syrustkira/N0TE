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
from n0te.platforms import PlatformEnvironment
from n0te.songwriting import SongwritingCaseHistoryService
from n0te.songwriting_shell import install_songwriting_vocal_surface


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


def post(
    shell: ConsumerShell,
    path: str,
    fields: dict[str, str],
    *,
    origin: str | None = None,
):
    return request(
        shell,
        path,
        method="POST",
        fields=fields,
        origin=shell.address.origin if origin is None else origin,
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


def seed(data_root: Path, *, close_session: bool = False):
    hq = HeadquartersMemory.create(data_root, "Writing Surface Artist")
    try:
        song = hq.store.create_song("Writing Surface Song")
        version = hq.store.create_version(song.id, label="writing pass")
        session = hq.sessions.start_session(
            song_id=song.id,
            version_id=version.id,
            objective="Shape the chorus lyric and vocal performance",
        )
        service = SongwritingCaseHistoryService(hq.store, hq.sessions)
        observation = service.capture(
            song_id=song.id,
            session_id=session.id,
            aspect="TOPLINE",
            section="Chorus",
            kind="OBSERVATION",
            text="The lower chorus entry feels calmer when I sing it.",
        )
        if close_session:
            hq.sessions.close_session(
                session.id,
                debrief_summary="Kept the lower entry as a useful option.",
                next_action="Write the second verse.",
            )
        return hq.store.profile_id, song.id, version.id, session.id, observation.item_id
    finally:
        hq.close()


def durable_counts(data_root: Path, profile_id: str, song_id: str) -> tuple[int, int]:
    """Inspect durable state on a connection owned by the calling test thread."""
    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        versions = hq.store._conn.execute(
            "SELECT COUNT(*) FROM versions WHERE song_id=?", (song_id,)
        ).fetchone()[0]
        claims = hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0]
        return int(versions), int(claims)
    finally:
        hq.close()


def capture_fields(
    form: Form,
    *,
    aspect: str = "LYRICS",
    kind: str = "DECISION",
    section: str = "Chorus",
    text: str = "Keep the first chorus line conversational and leave the thought unresolved.",
) -> dict[str, str]:
    return {
        **form.values,
        "aspect": aspect,
        "kind": kind,
        "section": section,
        "text": text,
    }


class SongwritingVocalConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data_root = (root / "data").resolve()
        self.state_root = (root / "state").resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_normal_song_surface_is_reachable_truthful_pure_and_hides_identity(self) -> None:
        profile_id, song_id, version_id, session_id, item_id = seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9981, "songwrite-visible")
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertEqual(page.count("<h2>Write &amp; Vocal</h2>"), 1)
            self.assertIn("The lower chorus entry feels calmer when I sing it.", page)
            for label in (
                "Lyrics",
                "Topline",
                "Melody",
                "Phrasing",
                "Takes / comp",
                "Lyric alignment",
                "Pitch / timing",
                "Doubles",
                "Harmonies",
                "Ad-libs",
                "Performance",
                "Vocal production",
            ):
                self.assertIn(label, page)
            self.assertIn("artist-entered case history", page)
            self.assertIn(
                "does not claim it heard, transcribed, tuned, comped, generated, cloned or edited a voice",
                page,
            )
            self.assertEqual(len(forms(page, "/songwriting/capture")), 1)
            self.assertEqual(request(shell, "/song")[0], 200)
            for forbidden in (
                song_id,
                version_id,
                session_id,
                item_id,
                "sitem_",
                "songwriting.topline",
            ):
                self.assertNotIn(forbidden, page)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            entries = SongwritingCaseHistoryService(
                reopened.store, reopened.sessions
            ).entries_for_song(song_id)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].text, "The lower chorus entry feels calmer when I sing it.")
            self.assertEqual(
                reopened.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0],
                0,
            )
        finally:
            reopened.close()

    def test_capture_then_explicit_song_decision_promotion_survives_relaunch_without_version_mutation(self) -> None:
        profile_id, song_id, _, _, _ = seed(self.data_root)
        before_versions, before_claims = durable_counts(self.data_root, profile_id, song_id)
        self.assertEqual(before_claims, 0)

        shell = new_shell(self.data_root, self.state_root, 9982, "songwrite-flow")
        promoted_page = ""
        try:
            _, page, _ = request(shell, "/song")
            capture = forms(page, "/songwriting/capture")[0]
            status, _, location = post(
                shell,
                "/songwriting/capture",
                capture_fields(capture),
            )
            self.assertEqual(status, 303)
            self.assertEqual(location, "/song")

            _, page, _ = request(shell, "/song")
            self.assertIn("Keep the first chorus line conversational", page)
            promote_forms = forms(page, "/songwriting/promote")
            self.assertEqual(len(promote_forms), 1)
            self.assertNotIn("Remembered for this Song", page)
            self.assertEqual(durable_counts(self.data_root, profile_id, song_id)[1], 0)

            status, _, location = post(
                shell,
                "/songwriting/promote",
                promote_forms[0].values,
            )
            self.assertEqual(status, 303)
            self.assertEqual(location, "/song")
            status, _, _ = post(
                shell,
                "/songwriting/promote",
                promote_forms[0].values,
            )
            self.assertEqual(status, 409)

            _, promoted_page, _ = request(shell, "/song")
            self.assertIn("Remembered for this Song", promoted_page)
            self.assertIn("not an observed fact or Version approval", promoted_page)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            self.assertEqual(
                reopened.store._conn.execute(
                    "SELECT COUNT(*) FROM versions WHERE song_id=?", (song_id,)
                ).fetchone()[0],
                before_versions,
            )
            service = SongwritingCaseHistoryService(reopened.store, reopened.sessions)
            decisions = [
                entry for entry in service.entries_for_song(song_id) if entry.kind == "DECISION"
            ]
            self.assertEqual(len(decisions), 1)
            self.assertTrue(decisions[0].promoted)
            self.assertFalse(decisions[0].provider_used)
            self.assertFalse(decisions[0].host_mutated)
            self.assertFalse(decisions[0].action_authority_granted)
            resolved = reopened.evidence.resolve_for_song(
                song_id=song_id,
                key="songwriting.lyrics.chorus",
            )
            self.assertEqual(resolved.status, "RESOLVED")
            self.assertIn("Keep the first chorus line conversational", resolved.value)
        finally:
            reopened.close()

    def test_closed_session_keeps_history_but_refuses_new_capture(self) -> None:
        _, song_id, version_id, session_id, item_id = seed(
            self.data_root, close_session=True
        )
        shell = new_shell(self.data_root, self.state_root, 9983, "songwrite-closed")
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertIn("The lower chorus entry feels calmer when I sing it.", page)
            self.assertIn("Earlier work Session", page)
            self.assertIn("Start or resume a work Session first", page)
            self.assertEqual(forms(page, "/songwriting/capture"), [])
            for forbidden in (song_id, version_id, session_id, item_id):
                self.assertNotIn(forbidden, page)
        finally:
            shell.stop()

    def test_stale_current_version_and_stale_session_capture_actions_fail_closed(self) -> None:
        profile_id, song_id, _, session_id, _ = seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9984, "songwrite-stale")
        try:
            _, page, _ = request(shell, "/song")
            stale_version_form = forms(page, "/songwriting/capture")[0]
            external = HeadquartersMemory.open(self.data_root, profile_id)
            try:
                external.store.create_version(song_id, label="new current pass")
            finally:
                external.close()

            status, body, _ = post(
                shell,
                "/songwriting/capture",
                capture_fields(stale_version_form),
            )
            self.assertEqual(status, 409)
            self.assertIn("current Version changed", body)

            _, page, _ = request(shell, "/song")
            stale_session_form = forms(page, "/songwriting/capture")[0]
            external = HeadquartersMemory.open(self.data_root, profile_id)
            try:
                external.sessions.close_session(
                    session_id,
                    debrief_summary="Move to a fresh writing pass.",
                    next_action="Try the next lyric shape.",
                )
                external.sessions.start_session(
                    song_id=song_id,
                    objective="Fresh writing pass",
                )
            finally:
                external.close()

            status, body, _ = post(
                shell,
                "/songwriting/capture",
                capture_fields(stale_session_form),
            )
            self.assertEqual(status, 409)
            self.assertIn("work Session changed", body)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            entries = SongwritingCaseHistoryService(
                reopened.store, reopened.sessions
            ).entries_for_song(song_id)
            self.assertEqual(len(entries), 1)
        finally:
            reopened.close()

    def test_stale_promotion_after_current_version_change_fails_closed(self) -> None:
        profile_id, song_id, _, _, _ = seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9987, "songwrite-promote-stale")
        try:
            _, page, _ = request(shell, "/song")
            capture = forms(page, "/songwriting/capture")[0]
            status, _, _ = post(shell, "/songwriting/capture", capture_fields(capture))
            self.assertEqual(status, 303)
            _, page, _ = request(shell, "/song")
            promote = forms(page, "/songwriting/promote")[0]

            external = HeadquartersMemory.open(self.data_root, profile_id)
            try:
                external.store.create_version(song_id, label="different current pass")
            finally:
                external.close()

            status, body, _ = post(shell, "/songwriting/promote", promote.values)
            self.assertEqual(status, 409)
            self.assertIn("current Version changed", body)
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            decisions = [
                entry
                for entry in SongwritingCaseHistoryService(
                    reopened.store, reopened.sessions
                ).entries_for_song(song_id)
                if entry.kind == "DECISION"
            ]
            self.assertEqual(len(decisions), 1)
            self.assertFalse(decisions[0].promoted)
            self.assertEqual(
                reopened.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0],
                0,
            )
        finally:
            reopened.close()

    def test_origin_csrf_replay_and_invalid_voice_cloning_aspect_fail_closed(self) -> None:
        profile_id, song_id, _, _, _ = seed(self.data_root)
        shell = new_shell(self.data_root, self.state_root, 9985, "songwrite-security")
        try:
            _, page, _ = request(shell, "/song")
            capture = forms(page, "/songwriting/capture")[0]
            fields = capture_fields(capture, kind="OBSERVATION")

            status, _, _ = post(
                shell,
                "/songwriting/capture",
                fields,
                origin="https://evil.example",
            )
            self.assertEqual(status, 403)

            bad_csrf = dict(fields)
            bad_csrf["csrf"] = "wrong"
            status, _, _ = post(shell, "/songwriting/capture", bad_csrf)
            self.assertEqual(status, 403)

            status, _, _ = post(shell, "/songwriting/capture", fields)
            self.assertEqual(status, 303)
            status, _, _ = post(shell, "/songwriting/capture", fields)
            self.assertEqual(status, 409)

            _, page, _ = request(shell, "/song")
            invalid = forms(page, "/songwriting/capture")[0]
            status, _, location = post(
                shell,
                "/songwriting/capture",
                capture_fields(
                    invalid,
                    aspect="VOICE_CLONING",
                    kind="OBSERVATION",
                    text="This must remain outside the songwriting surface.",
                ),
            )
            self.assertEqual(status, 303)
            self.assertEqual(location, "/song")
        finally:
            shell.stop()

        reopened = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            entries = SongwritingCaseHistoryService(
                reopened.store, reopened.sessions
            ).entries_for_song(song_id)
            self.assertEqual(len(entries), 2)
            self.assertNotIn("VOICE_CLONING", {entry.aspect for entry in entries})
        finally:
            reopened.close()

    def test_html_is_escaped_and_installer_is_idempotent(self) -> None:
        profile_id, song_id, _, session_id, _ = seed(self.data_root)
        hq = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            SongwritingCaseHistoryService(hq.store, hq.sessions).capture(
                song_id=song_id,
                session_id=session_id,
                aspect="LYRICS",
                kind="MARK",
                section="<chorus>",
                text="<script>not markup</script>",
            )
        finally:
            hq.close()

        install_songwriting_vocal_surface()
        install_songwriting_vocal_surface()
        shell = new_shell(self.data_root, self.state_root, 9986, "songwrite-idempotent")
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertEqual(page.count("<h2>Write &amp; Vocal</h2>"), 1)
            self.assertIn("&lt;script&gt;not markup&lt;/script&gt;", page)
            self.assertNotIn("<script>not markup</script>", page)
            self.assertIn("&lt;chorus&gt;", page)
        finally:
            shell.stop()


if __name__ == "__main__":
    unittest.main()
