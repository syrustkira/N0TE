from __future__ import annotations

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


def seed(data_root: Path) -> tuple[str, str, str]:
    hq = HeadquartersMemory.create(data_root, "Learning Artist")
    try:
        song = hq.store.create_song("Learning Song")
        session = hq.sessions.start_session(
            song_id=song.id,
            objective="Test one deliberate creative change",
        )
        return hq.store.profile_id, song.id, session.id
    finally:
        hq.close()


def start_fields(form: Form) -> dict[str, str]:
    return {
        **form.values,
        "domain": "Mixing",
        "subject": "Vocal compression",
        "change": "Lengthened the attack to let more of the vocal transient through.",
    }


def observation_fields(form: Form, text: str = "The vocal consonants felt clearer.") -> dict[str, str]:
    return {
        **form.values,
        "observation": text,
        "confidence": "MEDIUM",
        "conditions": "Same vocal take and matched monitor level",
        "confounders": "I had also taken a short ear break",
    }


def decision_fields(form: Form, decision: str = "KEEP") -> dict[str, str]:
    return {
        **form.values,
        "decision": decision,
        "rationale": "Keep it for this vocal, but compare again in the full arrangement.",
        "confidence": "MEDIUM",
    }


def new_shell(data_root: Path, state_root: Path, pid: int, token: str) -> ConsumerShell:
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(pid, token),
        probe=Probe(),
    )
    shell.start()
    return shell


def post(shell: ConsumerShell, path: str, fields: dict[str, str], origin: str | None = None):
    return request(
        shell,
        path,
        method="POST",
        fields=fields,
        origin=shell.address.origin if origin is None else origin,
    )


def test_song_surface_is_causally_humble_and_hides_internal_identity(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, _ = seed(data_root)
    shell = new_shell(data_root, state_root, 9901, "learning-visible")
    try:
        status, page, _ = request(shell, "/song")
        assert status == 200
        assert page.count("<h2>What happened after that change?</h2>") == 1
        assert "record the change, what you observed afterward, then your decision" in page
        assert "does not prove the change caused the outcome" in page
        assert len(forms(page, "/learning/start")) == 1
        for forbidden in ("learn_", "lobs_", "ldec_", "sess_", "consumer-learning-observation:"):
            assert forbidden not in page

        before_hq = HeadquartersMemory.open(data_root, profile_id)
        try:
            before = before_hq.learning.episodes_for_song(song_id)
        finally:
            before_hq.close()
        assert request(shell, "/song")[0] == 200
        after_hq = HeadquartersMemory.open(data_root, profile_id)
        try:
            after = after_hq.learning.episodes_for_song(song_id)
        finally:
            after_hq.close()
        assert after == before == ()
    finally:
        shell.stop()


def test_full_consumer_learning_chain_preserves_source_and_session(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, session_id = seed(data_root)
    shell = new_shell(data_root, state_root, 9902, "learning-flow")
    try:
        _, page, _ = request(shell, "/song")
        status, _, location = post(shell, "/learning/start", start_fields(forms(page, "/learning/start")[0]))
        assert status == 303 and location == "/song"

        _, page, _ = request(shell, "/song")
        status, _, _ = post(
            shell,
            "/learning/observe",
            observation_fields(forms(page, "/learning/observe")[0]),
        )
        assert status == 303
        inspect = HeadquartersMemory.open(data_root, profile_id)
        try:
            episode = inspect.learning.episodes_for_song(song_id)[0]
            assert episode.session_id == session_id
            assert episode.consequences[0].source_kind == "USER_DECLARED"
            assert episode.consequences[0].source_ref.startswith("consumer-learning-observation:")
            assert episode.consequences[0].confidence == 0.7
        finally:
            inspect.close()

        _, page, _ = request(shell, "/song")
        assert "You reported this" in page
        assert "Observed after the change:" in page
        assert "Same vocal take and matched monitor level" in page
        assert "Possible confounders:" in page
        for forbidden in (episode.id, episode.consequences[0].id, episode.consequences[0].source_ref, session_id):
            assert forbidden not in page

        status, _, _ = post(
            shell,
            "/learning/decide",
            decision_fields(forms(page, "/learning/decide")[0]),
        )
        assert status == 303
        inspect = HeadquartersMemory.open(data_root, profile_id)
        try:
            episode = inspect.learning.episodes_for_song(song_id)[0]
            assert episode.decision is not None and episode.decision.decision == "KEEP"
            assert inspect.sessions.get_session(session_id).state == "OPEN"
        finally:
            inspect.close()

        _, page, _ = request(shell, "/song")
        assert "Decision: Keep this change" in page
        assert not forms(page, "/learning/observe")
        assert not forms(page, "/learning/decide")
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert reopened.learning.episodes_for_song(song_id)[0].decision.decision == "KEEP"
    finally:
        reopened.close()


def test_start_action_origin_csrf_replay_and_stale_session_fail_closed(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, session_id = seed(data_root)
    shell = new_shell(data_root, state_root, 9903, "learning-authority")
    try:
        _, page, _ = request(shell, "/song")
        fields = start_fields(forms(page, "/learning/start")[0])
        assert post(shell, "/learning/start", fields, origin="https://attacker.example")[0] == 403
        inspect = HeadquartersMemory.open(data_root, profile_id)
        try:
            assert inspect.learning.episodes_for_song(song_id) == ()
        finally:
            inspect.close()

        bad = dict(fields)
        bad["csrf"] = "wrong"
        assert post(shell, "/learning/start", bad)[0] == 403

        changer = HeadquartersMemory.open(data_root, profile_id)
        try:
            changer.sessions.close_session(
                session_id,
                debrief_summary="Closed before the prepared experiment began",
                next_action="Start a fresh Session",
            )
        finally:
            changer.close()
        status, body, _ = post(shell, "/learning/start", fields)
        assert status == 409 and "Session changed" in body
        inspect = HeadquartersMemory.open(data_root, profile_id)
        try:
            assert inspect.learning.episodes_for_song(song_id) == ()
        finally:
            inspect.close()
        assert post(shell, "/learning/start", fields)[0] == 409
    finally:
        shell.stop()


def test_decision_action_rejects_unseen_new_evidence(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, _ = seed(data_root)
    shell = new_shell(data_root, state_root, 9904, "learning-stale-decision")
    try:
        _, page, _ = request(shell, "/song")
        assert post(shell, "/learning/start", start_fields(forms(page, "/learning/start")[0]))[0] == 303
        _, page, _ = request(shell, "/song")
        assert post(
            shell,
            "/learning/observe",
            observation_fields(forms(page, "/learning/observe")[0]),
        )[0] == 303

        _, page, _ = request(shell, "/song")
        stale = forms(page, "/learning/decide")[0]
        changer = HeadquartersMemory.open(data_root, profile_id)
        try:
            episode = changer.learning.episodes_for_song(song_id)[0]
            changer.learning.append_consequence(
                episode.id,
                observation="A second observation arrived after the decision form rendered.",
                source_kind="MEASURED",
                source_ref="test:newer-measurement",
                confidence=0.8,
                confounders=("Arrangement playback position changed",),
            )
        finally:
            changer.close()
        status, body, _ = post(shell, "/learning/decide", decision_fields(stale))
        assert status == 409 and "New Learning evidence" in body
        inspect = HeadquartersMemory.open(data_root, profile_id)
        try:
            episode = inspect.learning.episodes_for_song(song_id)[0]
            assert len(episode.consequences) == 2 and episode.decision is None
        finally:
            inspect.close()

        _, page, _ = request(shell, "/song")
        assert "Measured evidence" in page
        assert "test:newer-measurement" not in page
    finally:
        shell.stop()


def test_undecided_history_survives_quit_and_can_close_after_session(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, session_id = seed(data_root)
    shell = new_shell(data_root, state_root, 9905, "learning-restart-one")
    try:
        _, page, _ = request(shell, "/song")
        assert post(shell, "/learning/start", start_fields(forms(page, "/learning/start")[0]))[0] == 303
        _, page, _ = request(shell, "/song")
        assert post(
            shell,
            "/learning/observe",
            observation_fields(forms(page, "/learning/observe")[0]),
        )[0] == 303
        changer = HeadquartersMemory.open(data_root, profile_id)
        try:
            changer.sessions.close_session(
                session_id,
                debrief_summary="Captured the observation",
                next_action="Judge the experiment after reopening",
            )
        finally:
            changer.close()
    finally:
        shell.stop()

    shell2 = new_shell(data_root, state_root, 9906, "learning-restart-two")
    try:
        status, page, _ = request(shell2, "/song")
        assert status == 200
        assert "The vocal consonants felt clearer" in page
        assert "Open a work Session on this Song to start a new Learning experiment" in page
        assert not forms(page, "/learning/start")
        status, _, _ = post(
            shell2,
            "/learning/decide",
            decision_fields(forms(page, "/learning/decide")[0], decision="INCONCLUSIVE"),
        )
        assert status == 303
        inspect = HeadquartersMemory.open(data_root, profile_id)
        try:
            episode = inspect.learning.episodes_for_song(song_id)[0]
            assert episode.decision is not None and episode.decision.decision == "INCONCLUSIVE"
            assert inspect.sessions.get_session(session_id).state == "CLOSED"
        finally:
            inspect.close()
    finally:
        shell2.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert reopened.learning.episodes_for_song(song_id)[0].decision.decision == "INCONCLUSIVE"
    finally:
        reopened.close()
