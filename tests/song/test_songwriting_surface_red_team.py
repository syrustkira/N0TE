from __future__ import annotations

import sqlite3
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
from n0te.songwriting import (
    MAX_SONGWRITING_SURFACE_TEXT_CHARS,
    SongwritingCaseHistoryService,
)


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
    values: dict[str, str]


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self.current: Form | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form":
            self.current = Form(str(values.get("action", "")), {})
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and values.get("name"):
            self.current.values[str(values["name"])] = str(values.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None


def forms(page: str, action: str) -> list[Form]:
    parser = FormParser()
    parser.feed(page)
    return [candidate for candidate in parser.forms if candidate.action == action]


def request(
    shell: ConsumerShell,
    path: str,
    *,
    method: str = "GET",
    fields: dict[str, str] | None = None,
) -> tuple[int, str, str | None]:
    headers: dict[str, str] = {}
    data = None
    if fields is not None:
        data = urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = shell.address.origin
    req = Request(shell.address.origin + path, data=data, method=method, headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8"), response.headers.get("Location")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers.get("Location")


def post(shell: ConsumerShell, path: str, fields: dict[str, str]):
    return request(shell, path, method="POST", fields=fields)


def seed(data_root: Path):
    hq = HeadquartersMemory.create(data_root, "Red Team Writing Artist")
    try:
        song = hq.store.create_song("Red Team Writing Song")
        version = hq.store.create_version(song.id, label="writing pass")
        session = hq.sessions.start_session(
            song_id=song.id,
            version_id=version.id,
            objective="Stress the writing surface without crossing authority",
        )
        return hq.store.profile_id, song.id, version.id, session.id
    finally:
        hq.close()


class _WriterRaceMixin:
    competing_version_id: str
    race_blocked: bool
    race_committed: bool

    def _attempt_competing_current_change(self, song_id: str) -> None:
        conn = sqlite3.connect(self.store.database_path, timeout=0)
        self.race_blocked = False
        self.race_committed = False
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE songs SET current_version_id=? WHERE id=?",
                    (self.competing_version_id, song_id),
                )
                conn.commit()
                self.race_committed = True
            except sqlite3.OperationalError as exc:
                self.race_blocked = "locked" in str(exc).lower()
                conn.rollback()
                if not self.race_blocked:
                    raise
        finally:
            conn.close()


class CaptureRaceService(_WriterRaceMixin, SongwritingCaseHistoryService):
    def __init__(self, store, sessions, competing_version_id: str):
        super().__init__(store, sessions)
        self.competing_version_id = competing_version_id
        self.race_blocked = False
        self.race_committed = False

    def _validate_capture_binding_locked(self, **kwargs):
        resolved = super()._validate_capture_binding_locked(**kwargs)
        self._attempt_competing_current_change(str(kwargs["song_id"]))
        return resolved


class PromotionRaceService(_WriterRaceMixin, SongwritingCaseHistoryService):
    def __init__(self, store, sessions, competing_version_id: str):
        super().__init__(store, sessions)
        self.competing_version_id = competing_version_id
        self.race_blocked = False
        self.race_committed = False

    def _validate_promotion_binding_locked(self, **kwargs):
        resolved = super()._validate_promotion_binding_locked(**kwargs)
        self._attempt_competing_current_change(str(kwargs["expected_song_id"]))
        return resolved


class SongwritingSurfaceRedTeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data_root = (root / "data").resolve()
        self.state_root = (root / "state").resolve()

    def test_bound_capture_holds_write_lock_through_freshness_and_insert(self) -> None:
        profile_id, song_id, version_id, session_id = seed(self.data_root)
        hq = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            competing = hq.store.create_version(song_id, label="competing pass", make_current=False)
            service = CaptureRaceService(hq.store, hq.sessions, competing.id)
            entry = service.capture_bound(
                song_id=song_id,
                session_id=session_id,
                expected_current_version_id=version_id,
                expected_session_version_id=version_id,
                aspect="LYRICS",
                kind="OBSERVATION",
                section="Verse 1",
                text="Keep the consonants soft on the pickup.",
            )
            self.assertTrue(service.race_blocked)
            self.assertFalse(service.race_committed)
            self.assertEqual(hq.store.get_song(song_id).current_version_id, version_id)
            self.assertEqual(entry.version_id, version_id)
            self.assertEqual(
                [item.text for item in service.entries_for_song(song_id)],
                ["Keep the consonants soft on the pickup."],
            )
        finally:
            hq.close()

    def test_bound_promotion_holds_write_lock_through_freshness_and_evidence_link(self) -> None:
        profile_id, song_id, version_id, session_id = seed(self.data_root)
        hq = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            base = SongwritingCaseHistoryService(hq.store, hq.sessions)
            decision = base.capture(
                song_id=song_id,
                session_id=session_id,
                aspect="HARMONIES",
                kind="DECISION",
                section="Final chorus",
                text="Keep the upper third only on the final word.",
            )
            competing = hq.store.create_version(song_id, label="competing pass", make_current=False)
            service = PromotionRaceService(hq.store, hq.sessions, competing.id)
            promoted = service.promote_decision_bound(
                decision.item_id,
                expected_song_id=song_id,
                expected_session_id=session_id,
                expected_entry_version_id=version_id,
                expected_current_version_id=version_id,
                scope_kind="SONG",
            )
            self.assertTrue(service.race_blocked)
            self.assertFalse(service.race_committed)
            self.assertEqual(hq.store.get_song(song_id).current_version_id, version_id)
            self.assertEqual(promoted.claim.scope_id, song_id)
            self.assertEqual(promoted.claim.source_kind, "USER_DECLARED")
            self.assertEqual(promoted.claim.twin_domain, "CREATIVE")
            link = hq.store._conn.execute(
                "SELECT claim_id FROM session_promotions WHERE item_id=?",
                (decision.item_id,),
            ).fetchone()
            self.assertIsNotNone(link)
            self.assertEqual(str(link["claim_id"]), promoted.claim.id)
        finally:
            hq.close()

    def test_multilingual_surface_limit_fits_shared_form_budget_and_persists_exact_text(self) -> None:
        profile_id, song_id, _, _ = seed(self.data_root)
        shell = ConsumerShell(
            data_root=self.data_root,
            state_root=self.state_root,
            process=process(9901, "songwrite-unicode"),
            probe=Probe(),
        )
        shell.start()
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertIn(f'maxlength="{MAX_SONGWRITING_SURFACE_TEXT_CHARS}"', page)
            capture = forms(page, "/songwriting/capture")
            self.assertEqual(len(capture), 1)
            note = "😀" * MAX_SONGWRITING_SURFACE_TEXT_CHARS
            section = "界" * 160
            fields = {
                **capture[0].values,
                "aspect": "LYRICS",
                "kind": "OBSERVATION",
                "section": section,
                "text": note,
            }
            self.assertLess(len(urlencode(fields).encode("utf-8")), 32768)
            status, _, location = post(shell, "/songwriting/capture", fields)
            self.assertEqual(status, 303)
            self.assertEqual(location, "/song")
        finally:
            shell.stop()

        hq = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            entries = SongwritingCaseHistoryService(hq.store, hq.sessions).entries_for_song(song_id)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].text, "😀" * MAX_SONGWRITING_SURFACE_TEXT_CHARS)
            self.assertEqual(entries[0].section, "界" * 160)
        finally:
            hq.close()

    def test_storage_envelope_never_leaks_through_session_or_promoted_song_views(self) -> None:
        seed(self.data_root)
        shell = ConsumerShell(
            data_root=self.data_root,
            state_root=self.state_root,
            process=process(9902, "songwrite-envelope"),
            probe=Probe(),
        )
        shell.start()
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            capture = forms(page, "/songwriting/capture")[0]
            fields = {
                **capture.values,
                "aspect": "TOPLINE",
                "kind": "DECISION",
                "section": "Chorus",
                "text": "Keep the pickup late and conversational.",
            }
            status, _, _ = post(shell, "/songwriting/capture", fields)
            self.assertEqual(status, 303)

            status, captured_page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertIn("Keep the pickup late and conversational.", captured_page)
            self.assertNotIn("[N0TE-SONGWRITE/1]", captured_page)
            self.assertNotIn("&quot;aspect&quot;", captured_page)

            promote = forms(captured_page, "/songwriting/promote")
            self.assertEqual(len(promote), 1)
            status, _, _ = post(shell, "/songwriting/promote", promote[0].values)
            self.assertEqual(status, 303)

            status, promoted_page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertIn("Remembered for this Song", promoted_page)
            self.assertIn("Keep the pickup late and conversational.", promoted_page)
            self.assertNotIn("[N0TE-SONGWRITE/1]", promoted_page)
            self.assertNotIn("&quot;aspect&quot;", promoted_page)
        finally:
            shell.stop()

    def test_malformed_owned_envelope_is_hidden_while_surface_reports_recovery(self) -> None:
        profile_id, _, _, session_id = seed(self.data_root)
        hq = HeadquartersMemory.open(self.data_root, profile_id)
        try:
            with hq.store._tx():
                hq.store._conn.execute(
                    "INSERT INTO session_items(id,session_id,kind,body) VALUES(?,?,?,?)",
                    (
                        "sitem_redteam_malformed_songwrite",
                        session_id,
                        "MARK",
                        "Do not leak me\n\n[N0TE-SONGWRITE/1] not-json",
                    ),
                )
        finally:
            hq.close()

        shell = ConsumerShell(
            data_root=self.data_root,
            state_root=self.state_root,
            process=process(9903, "songwrite-malformed-envelope"),
            probe=Probe(),
        )
        shell.start()
        try:
            status, page, _ = request(shell, "/song")
            self.assertEqual(status, 200)
            self.assertIn("Writing history needs recovery", page)
            self.assertIn("Writing history hidden pending recovery.", page)
            self.assertNotIn("[N0TE-SONGWRITE/1]", page)
            self.assertNotIn("not-json", page)
        finally:
            shell.stop()


if __name__ == "__main__":
    unittest.main()
