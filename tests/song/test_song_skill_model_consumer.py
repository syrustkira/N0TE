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


def seed_profile(data_root: Path) -> tuple[str, str]:
    hq = HeadquartersMemory.create(data_root, "Learning Artist")
    try:
        song = hq.store.create_song("Learning Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Practice compression")
        hq.sessions.close_session(
            session.id,
            debrief_summary="Completed a controlled compression pass",
            next_action="Repeat the move without prompts",
        )
        evidence = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="skill.compression",
            value="completed pass",
            source_kind="OBSERVED",
        )
        hq.skills.record_assessment(
            skill_id="Compression",
            level="PRACTICED",
            source_kind="N0TE_ASSESSED",
            source_ref="test:real-work",
            confidence=0.8,
            assistance_level=0.5,
            session_id=session.id,
            evidence_claim_ids=(evidence.id,),
        )
        return hq.store.profile_id, song.id
    finally:
        hq.close()


def declare_fields(form: Form, *, skill: str, level: str, assistance: str, confidence: str) -> dict[str, str]:
    values = dict(form.values)
    values.update(
        {
            "skill_name": skill,
            "level": level,
            "assistance": assistance,
            "confidence": confidence,
        }
    )
    return values


def correction_fields(
    form: Form,
    *,
    level: str,
    assistance: str,
    confidence: str,
    reason: str,
) -> dict[str, str]:
    values = dict(form.values)
    values.update(
        {
            "level": level,
            "assistance": assistance,
            "confidence": confidence,
            "reason": reason,
        }
    )
    return values


def skill_history(data_root: Path, profile_id: str, skill_id: str):
    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        return hq.skills.history(skill_id)
    finally:
        hq.close()


def skill_state(data_root: Path, profile_id: str, skill_id: str):
    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        return hq.skills.state(skill_id)
    finally:
        hq.close()


def song_session_snapshot(data_root: Path, profile_id: str, song_id: str):
    hq = HeadquartersMemory.open(data_root, profile_id)
    try:
        return (
            hq.store.get_song(song_id),
            hq.store.versions_for_song(song_id),
            hq.sessions.latest_for_song(song_id),
        )
    finally:
        hq.close()


def test_song_surface_shows_inspectable_skill_truth_without_internal_ids(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id = seed_profile(data_root)
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9801, "skill-visible"),
        probe=Probe(),
    )
    shell.start()
    try:
        status, page, _ = request(shell, "/song")
        assert status == 200
        assert page.count("<h2>What N0TE thinks you can do</h2>") == 1
        assert "Compression" in page
        assert "Practiced" in page
        assert "N0TE assessment" in page
        assert "Some assistance" in page
        assert "Confidence 80%" in page
        assert "1 linked evidence claim" in page
        assert 'name="confidence"' in page
        assert '<option value="MEDIUM" selected>Medium confidence</option>' in page
        assert "Independent means you can do it without guidance" in page
        assert "skillassess_" not in page
        assert "test:real-work" not in page
        assert "claim_" not in page
        assert "evidence_claim" not in page
        before = skill_history(data_root, profile_id, "Compression")
        status, again, _ = request(shell, "/song")
        assert status == 200
        assert skill_history(data_root, profile_id, "Compression") == before
        assert again.count("<h2>What N0TE thinks you can do</h2>") == 1
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert reopened.store.active_song().id == song_id
        assert reopened.skills.state("Compression").level == "PRACTICED"
    finally:
        reopened.close()


def test_artist_can_declare_then_correct_skill_without_rewriting_history(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _ = seed_profile(data_root)
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9802, "skill-declare-correct"),
        probe=Probe(),
    )
    shell.start()
    try:
        _, page, _ = request(shell, "/song")
        declaration = forms(page, "/skill/declare")
        assert len(declaration) == 1
        status, _, location = request(
            shell,
            "/skill/declare",
            method="POST",
            fields=declare_fields(
                declaration[0],
                skill="Arrangement",
                level="APPLIED",
                assistance="SOME",
                confidence="MEDIUM",
            ),
            origin=shell.address.origin,
        )
        assert status == 303 and location == "/song"
        first = skill_history(data_root, profile_id, "Arrangement")
        assert len(first) == 1
        assert first[0].source_kind == "ARTIST_DECLARED"
        assert first[0].level == "APPLIED"
        assert first[0].confidence == 0.7

        _, page, _ = request(shell, "/song")
        corrections = forms(page, "/skill/correct")
        arrangement = next(form for form in corrections if form.label == "Correct Arrangement Skill")
        status, _, location = request(
            shell,
            "/skill/correct",
            method="POST",
            fields=correction_fields(
                arrangement,
                level="PRACTICED",
                assistance="HIGH",
                confidence="LOW",
                reason="I can practice this, but I overstated how independently I apply it.",
            ),
            origin=shell.address.origin,
        )
        assert status == 303 and location == "/song"
        history = skill_history(data_root, profile_id, "Arrangement")
        assert len(history) == 2
        assert history[0].source_kind == "ARTIST_DECLARED"
        assert history[1].source_kind == "ARTIST_CORRECTION"
        assert history[1].level == "PRACTICED"
        assert history[1].confidence == 0.4
        assert "overstated" in (history[1].note or "")
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert reopened.skills.state("Arrangement").level == "PRACTICED"
        assert len(reopened.skills.history("Arrangement")) == 2
    finally:
        reopened.close()


def test_skill_actions_enforce_origin_csrf_replay_and_stale_correction(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, _ = seed_profile(data_root)
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9803, "skill-authority"),
        probe=Probe(),
    )
    shell.start()
    try:
        _, page, _ = request(shell, "/song")
        declaration = forms(page, "/skill/declare")[0]
        foreign = declare_fields(
            declaration,
            skill="EQ",
            level="PRACTICED",
            assistance="SOME",
            confidence="MEDIUM",
        )
        # The Origin gate runs before form-body consumption. A bodyless attack
        # request isolates that security boundary and avoids a Windows socket
        # reset caused by rejecting while urllib is still transmitting a body.
        status, body, _ = request(
            shell,
            "/skill/declare",
            method="POST",
            origin="https://attacker.example",
        )
        assert status == 403
        assert "That action did not come from this N0TE window." in body
        assert skill_state(data_root, profile_id, "EQ").latest_assessment is None

        bad_csrf = dict(foreign)
        bad_csrf["csrf"] = "wrong"
        status, _, _ = request(
            shell,
            "/skill/declare",
            method="POST",
            fields=bad_csrf,
            origin=shell.address.origin,
        )
        assert status == 403

        status, _, _ = request(
            shell,
            "/skill/declare",
            method="POST",
            fields=foreign,
            origin=shell.address.origin,
        )
        assert status == 303
        status, _, _ = request(
            shell,
            "/skill/declare",
            method="POST",
            fields=foreign,
            origin=shell.address.origin,
        )
        assert status == 409
        assert len(skill_history(data_root, profile_id, "EQ")) == 1

        _, page, _ = request(shell, "/song")
        correction = next(
            form for form in forms(page, "/skill/correct") if form.label == "Correct EQ Skill"
        )
        changer = HeadquartersMemory.open(data_root, profile_id)
        try:
            changer.skills.correct_skill(
                skill_id="EQ",
                level="APPLIED",
                source_ref="test:newer-assessment",
                reason="A newer correction landed after the page rendered.",
                assistance_level=0.5,
            )
        finally:
            changer.close()
        status, body, _ = request(
            shell,
            "/skill/correct",
            method="POST",
            fields=correction_fields(
                correction,
                level="INDEPENDENT",
                assistance="NONE",
                confidence="HIGH",
                reason="stale page",
            ),
            origin=shell.address.origin,
        )
        assert status == 409
        assert "Skill changed" in body
        assert skill_state(data_root, profile_id, "EQ").level == "APPLIED"
    finally:
        shell.stop()


def test_independent_requires_no_assistance_and_skill_writes_leave_song_untouched(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id = seed_profile(data_root)
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9804, "skill-independent"),
        probe=Probe(),
    )
    shell.start()
    try:
        song_before, versions_before, sessions_before = song_session_snapshot(
            data_root, profile_id, song_id
        )
        _, page, _ = request(shell, "/song")
        declaration = forms(page, "/skill/declare")[0]
        assisted = declare_fields(
            declaration,
            skill="Mastering",
            level="INDEPENDENT",
            assistance="SOME",
            confidence="MEDIUM",
        )
        status, _, _ = request(
            shell,
            "/skill/declare",
            method="POST",
            fields=assisted,
            origin=shell.address.origin,
        )
        assert status == 303
        assert skill_state(data_root, profile_id, "Mastering").latest_assessment is None

        _, page, _ = request(shell, "/song")
        declaration = forms(page, "/skill/declare")[0]
        independent = declare_fields(
            declaration,
            skill="Mastering",
            level="INDEPENDENT",
            assistance="NONE",
            confidence="MEDIUM",
        )
        status, _, _ = request(
            shell,
            "/skill/declare",
            method="POST",
            fields=independent,
            origin=shell.address.origin,
        )
        assert status == 303
        state = skill_state(data_root, profile_id, "Mastering")
        assert state.level == "INDEPENDENT"
        assert state.latest_assessment is not None
        assert state.latest_assessment.assistance_level == 0.0
        assert state.latest_assessment.confidence == 0.7
        song_after, versions_after, sessions_after = song_session_snapshot(
            data_root, profile_id, song_id
        )
        assert song_after == song_before
        assert versions_after == versions_before
        assert sessions_after == sessions_before
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert reopened.skills.state("Mastering").level == "INDEPENDENT"
    finally:
        reopened.close()
