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


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=172010,
        start_token="req-scope-010-compare-decide-reachability",
    )


def pcm16_mono_wav(amplitude: int) -> bytes:
    samples = (-amplitude, -(amplitude // 2), 0, amplitude // 2, amplitude)
    data = b"".join(struct.pack("<h", sample) for sample in samples)
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    chunks = (
        b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


@dataclass(frozen=True)
class DecisionForm:
    csrf: str
    action: str


class DecisionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.values: dict[str, str] = {}
        self.forms: list[DecisionForm] = []
        self.decisions: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form" and values.get("action") == "/compare/decide":
            self.inside = True
            self.values = {}
            return
        if not self.inside:
            return
        if tag == "input" and values.get("name") in {"csrf", "action"}:
            self.values[str(values["name"])] = str(values.get("value", ""))
        elif tag == "button" and values.get("name") == "decision":
            self.decisions.append(str(values.get("value", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.inside:
            self.forms.append(
                DecisionForm(
                    csrf=self.values.get("csrf", ""),
                    action=self.values.get("action", ""),
                )
            )
            self.inside = False
            self.values = {}


def get(shell: ConsumerShell, path: str) -> tuple[int, str]:
    try:
        with urlopen(Request(shell.address.origin + path, method="GET"), timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post(shell: ConsumerShell, path: str, fields: dict[str, str]) -> tuple[int, str]:
    body = urlencode(fields).encode("utf-8")
    request = Request(
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
        with urlopen(request, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_normal_customer_can_hear_compare_and_record_bounded_artist_decision(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()

    hq = HeadquartersMemory.create(data_root, "Compare Artist")
    song = hq.store.create_song("Compare Song")
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
    profile_id = hq.store.profile_id
    song_before = hq.store.get_song(song.id)
    learning_before = hq.learning.episodes_for_song(song.id)
    hq.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(),
        probe=Probe(),
    )
    shell.start()
    status, page = get(shell, "/compare")
    assert status == 200
    assert "Hear · Compare · Decide" in page
    assert "Both exact Versions are locally auditionable" in page
    assert page.count("<audio controls") == 2
    assert "Nothing has been chosen." in page

    parser = DecisionParser()
    parser.feed(page)
    assert len(parser.forms) == 1
    assert set(parser.decisions) == {"KEEP", "REVERT", "REVISE", "INCONCLUSIVE"}
    form = parser.forms[0]
    assert form.csrf and form.action

    status, decided = post(
        shell,
        "/compare/decide",
        {
            "csrf": form.csrf,
            "action": form.action,
            "decision": "REVISE",
            "rationale": "The current lift is promising, but the vocal still needs more room.",
        },
    )
    assert status == 200
    assert "A/B decision recorded: Current needs another revision" in decided
    assert "Latest judgment for this exact pair: Current needs another revision" in decided
    assert "The current lift is promising, but the vocal still needs more room." in decided
    assert "It did not change Current, Approved, audio, Learning, a provider, or a DAW" in decided
    shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        song_after = reopened.store.get_song(song.id)
        assert song_after == song_before
        assert song_after.current_version_id == current.version.id
        assert song_after.approved_version_id is None
        assert reopened.learning.episodes_for_song(song.id) == learning_before

        memory = VersionCompareDecisionMemory(reopened.store, create=False)
        decision = memory.latest_for_pair(
            song.id,
            reference.version.id,
            current.version.id,
        )
        assert decision is not None
        assert decision.decision == "REVISE"
        assert decision.rationale == (
            "The current lift is promising, but the vocal still needs more room."
        )
    finally:
        reopened.close()
