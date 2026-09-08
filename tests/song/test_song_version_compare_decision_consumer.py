from __future__ import annotations

import struct
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment
from n0te.version_compare_decision import VersionCompareDecisionMemory


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process(pid: int, token: str) -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=token,
    )


def pcm16_mono_wav(amplitude: int) -> bytes:
    samples = (-amplitude, -(amplitude // 2), 0, amplitude // 2, amplitude)
    data = b"".join(struct.pack("<h", sample) for sample in samples)
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000 * 2, 2, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def get_text(shell: ConsumerShell, path: str) -> tuple[int, str]:
    req = Request(shell.address.origin + path, method="GET")
    try:
        with urlopen(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post_form(shell: ConsumerShell, path: str, form: dict[str, str]) -> tuple[int, str]:
    body = urlencode(form).encode("utf-8")
    req = Request(
        shell.address.origin + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
            "Origin": shell.address.origin,
        },
    )
    try:
        with urlopen(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


@dataclass(frozen=True)
class DecisionForm:
    csrf: str
    action: str


class DecisionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_decision = False
        self._values: dict[str, str] = {}
        self.forms: list[DecisionForm] = []
        self.decision_values: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form" and values.get("action") == "/compare/decide":
            self._inside_decision = True
            self._values = {}
            return
        if not self._inside_decision:
            return
        if tag == "input" and values.get("name") in {"csrf", "action"}:
            self._values[str(values["name"])] = str(values.get("value", ""))
        elif tag == "button" and values.get("name") == "decision":
            self.decision_values.append(str(values.get("value", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag != "form" or not self._inside_decision:
            return
        self.forms.append(
            DecisionForm(
                csrf=self._values.get("csrf", ""),
                action=self._values.get("action", ""),
            )
        )
        self._inside_decision = False
        self._values = {}


def seed(data_root: Path):
    hq = HeadquartersMemory.create(data_root, "Decision Artist")
    song = hq.store.create_song("Decision Song")
    reference_payload = pcm16_mono_wav(6000)
    reference = hq.materials.ingest_stream(
        song.id,
        filename="reference.wav",
        stream=BytesIO(reference_payload),
        declared_size=len(reference_payload),
    )
    current_payload = pcm16_mono_wav(9000)
    current = hq.materials.ingest_stream(
        song.id,
        filename="current.wav",
        stream=BytesIO(current_payload),
        declared_size=len(current_payload),
    )
    return hq, song, reference, current


def shell_for(data_root: Path, state_root: Path, pid: int, token: str) -> ConsumerShell:
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(pid, token),
        probe=Probe(),
    )
    shell.start()
    return shell


def test_compare_get_stays_read_only_and_explicit_decision_persists_without_execution(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq, song, reference, current = seed(data_root)
    profile_id = hq.store.profile_id
    song_before = hq.store.get_song(song.id)
    versions_before = hq.store.versions_for_song(song.id)
    learning_before = hq.learning.episodes_for_song(song.id)
    activity_before = hq.activity.for_song(song.id)
    hq.close()

    read_shell = shell_for(data_root, state_root, 9801, "compare-decision-read")
    status, page = get_text(read_shell, "/compare")
    assert status == 200
    assert "Your decision" in page
    assert "KEEP, REVERT, REVISE, or INCONCLUSIVE remains a separate explicit decision step" in page
    assert "Decision only" in page
    parser = DecisionParser()
    parser.feed(page)
    assert len(parser.forms) == 1
    assert set(parser.decision_values) == {"KEEP", "REVERT", "REVISE", "INCONCLUSIVE"}
    assert parser.forms[0].csrf and parser.forms[0].action
    assert "song_" not in page and "ver_" not in page
    read_shell.stop()

    after_get = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert after_get.store.get_song(song.id) == song_before
        assert after_get.store.versions_for_song(song.id) == versions_before
        assert after_get.learning.episodes_for_song(song.id) == learning_before
        assert after_get.activity.for_song(song.id) == activity_before
        assert after_get.store._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='version_compare_decisions'"
        ).fetchone() is None
        assert after_get.store._conn.execute(
            "SELECT 1 FROM metadata WHERE key='version_compare_decision_schema_version'"
        ).fetchone() is None
    finally:
        after_get.close()

    write_shell = shell_for(data_root, state_root, 9802, "compare-decision-write")
    status, page = get_text(write_shell, "/compare")
    assert status == 200
    parser = DecisionParser()
    parser.feed(page)
    decision_form = parser.forms[0]

    status, decided_page = post_form(
        write_shell,
        "/compare/decide",
        {
            "csrf": decision_form.csrf,
            "action": decision_form.action,
            "decision": "REVERT",
            "rationale": "The reference leaves more room for the vocal.",
        },
    )
    assert status == 200
    assert "A/B decision recorded: Reference is the direction to return to" in decided_page
    assert "Latest judgment for this exact pair: Reference is the direction to return to" in decided_page
    assert "The reference leaves more room for the vocal." in decided_page
    assert "It did not change Current, Approved, audio, Learning, a provider, or a DAW" in decided_page

    replay_status, replay_page = post_form(
        write_shell,
        "/compare/decide",
        {
            "csrf": decision_form.csrf,
            "action": decision_form.action,
            "decision": "KEEP",
            "rationale": "Replay should not work.",
        },
    )
    assert replay_status == 409
    assert "already handled or expired" in replay_page
    write_shell.stop()

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert check.store.get_song(song.id) == song_before
        assert check.store.get_song(song.id).current_version_id == current.version.id
        assert check.store.get_song(song.id).approved_version_id is None
        assert check.store.versions_for_song(song.id) == versions_before
        assert check.learning.episodes_for_song(song.id) == learning_before
        memory = VersionCompareDecisionMemory(check.store, create=False)
        assert memory.initialized is True
        latest = memory.latest_for_pair(song.id, reference.version.id, current.version.id)
        assert latest is not None
        assert latest.decision == "REVERT"
        assert latest.rationale == "The reference leaves more room for the vocal."
        assert len(memory.decisions_for_song(song.id)) == 1
        new_events = check.activity.for_song(
            song.id,
            after_sequence=activity_before[-1].sequence,
        )
        assert len(new_events) == 1
        assert new_events[0].event_type == "VERSION_COMPARE_DECISION_RECORDED"
        assert new_events[0].payload == {"decision": "REVERT"}
    finally:
        check.close()


def test_partial_comparison_never_offers_artist_decision_write(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq, _, reference, _ = seed(data_root)
    profile_id = hq.store.profile_id
    reference.material.path.write_bytes(reference.material.path.read_bytes() + b"tampered")
    hq.close()

    shell = shell_for(data_root, state_root, 9803, "compare-decision-partial")
    status, page = get_text(shell, "/compare")
    assert status == 200
    assert "Decision recording is unavailable while both exact Versions cannot be auditioned safely" in page
    parser = DecisionParser()
    parser.feed(page)
    assert parser.forms == []
    shell.stop()

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert VersionCompareDecisionMemory(check.store, create=False).initialized is False
    finally:
        check.close()
