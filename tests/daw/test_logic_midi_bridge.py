from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.logic_midi_bridge import (
    CC_CHECKSUM,
    CC_END,
    CC_FLAGS,
    CC_METER_DENOMINATOR,
    CC_METER_NUMERATOR,
    CC_SESSION_0,
    CC_SESSION_1,
    CC_SESSION_2,
    CC_SESSION_3,
    CC_START,
    CC_TEMPO_LSB,
    CC_TEMPO_MSB,
    CC_VERSION,
    LOGIC_CC_STATUS,
    LOGIC_PROTOCOL_VERSION,
    LogicMidiBridgeError,
    LogicMidiFrameAssembler,
    LogicMidiMonitorClient,
    _checksum,
)
from n0te.memory import HeadquartersMemory


def _messages(
    *,
    sequence=9,
    session=0x0ABCDEF,
    tempo=128.4,
    playing=True,
    cycling=False,
    numerator=4,
    denominator=4,
):
    session_bytes = [
        (session >> 21) & 0x7F,
        (session >> 14) & 0x7F,
        (session >> 7) & 0x7F,
        session & 0x7F,
    ]
    tempo10 = round(tempo * 10)
    tempo_msb = (tempo10 >> 7) & 0x7F
    tempo_lsb = tempo10 & 0x7F
    flags = (1 if playing else 0) | (2 if cycling else 0)
    protected = [
        sequence,
        LOGIC_PROTOCOL_VERSION,
        *session_bytes,
        tempo_msb,
        tempo_lsb,
        flags,
        numerator,
        denominator,
    ]
    pairs = [
        (CC_START, sequence),
        (CC_VERSION, LOGIC_PROTOCOL_VERSION),
        (CC_SESSION_0, session_bytes[0]),
        (CC_SESSION_1, session_bytes[1]),
        (CC_SESSION_2, session_bytes[2]),
        (CC_SESSION_3, session_bytes[3]),
        (CC_TEMPO_MSB, tempo_msb),
        (CC_TEMPO_LSB, tempo_lsb),
        (CC_FLAGS, flags),
        (CC_METER_NUMERATOR, numerator),
        (CC_METER_DENOMINATOR, denominator),
        (CC_CHECKSUM, _checksum(protected)),
        (CC_END, sequence),
    ]
    return [[LOGIC_CC_STATUS, controller, value] for controller, value in pairs]


def test_strict_frame_assembler_roundtrips_timing_truth():
    assembler = LogicMidiFrameAssembler()
    frame = None
    for message in _messages(cycling=True, numerator=7, denominator=8):
        result = assembler.feed(message)
        if result is not None:
            frame = result

    assert frame is not None
    assert frame.bridge_session_id == "logic-scripter-0abcdef"
    assert frame.tempo_bpm == 128.4
    assert frame.is_playing is True
    assert frame.is_cycling is True
    assert frame.meter_numerator == 7
    assert frame.meter_denominator == 8


def test_assembler_ignores_unrelated_midi_but_rejects_corrupt_n0te_frames():
    assembler = LogicMidiFrameAssembler()
    assert assembler.feed([0x90, 60, 100]) is None
    assert assembler.feed([0xB0, CC_START, 1]) is None

    corrupt = _messages()
    corrupt[-2][2] ^= 1
    with pytest.raises(LogicMidiBridgeError, match="checksum"):
        for message in corrupt:
            assembler.feed(message)

    mixed = _messages()
    assembler.feed(mixed[0])
    assembler.feed(mixed[1])
    with pytest.raises(LogicMidiBridgeError, match="out of order"):
        assembler.feed(mixed[3])


def test_logic_snapshot_claims_only_timing_capabilities_and_shadow_fields():
    midi = _FakeMidiIn(["Logic Pro Virtual Out"])
    client = LogicMidiMonitorClient(
        midi_in_factory=lambda: midi,
        now_epoch_seconds=lambda: 100.0,
        monotonic_clock=lambda: 1.0,
    )
    assert client.open() == "Logic Pro Virtual Out"
    for message in _messages():
        midi.emit(message)
    snapshot = client.fetch_snapshot(timeout_seconds=0.1)

    assert snapshot.runtime.family == "LOGIC_PRO"
    assert snapshot.runtime.version == "UNOBSERVED"
    assert snapshot.runtime.platform.architecture == "UNKNOWN"
    assert snapshot.location_ref.startswith("logic-scripter-session:")
    assert snapshot.focus_dimensions() == ()
    assert {fact.capability for fact in snapshot.capabilities()} == {
        "tempo.read",
        "transport.read",
        "meter.read",
        "cycle.read",
    }
    facts = {
        (event.object_kind, event.field): event.value
        for event in snapshot.shadow().events
    }
    assert facts[("TEMPO", "bpm")] == 128.4
    assert facts[("TRANSPORT", "is_playing")] is True
    assert facts[("TRANSPORT", "is_cycling")] is False
    assert facts[("TRANSPORT", "meter_numerator")] == 4
    assert facts[("TRANSPORT", "meter_denominator")] == 4
    encoded = repr(snapshot)
    for absent in ("project_title", "project_path", "selected_track", "plugin_name"):
        assert absent not in encoded
    client.close()


class _FakeMidiIn:
    def __init__(self, ports):
        self.ports = list(ports)
        self.callback = None
        self.opened = None
        self.closed = False

    def get_ports(self):
        return list(self.ports)

    def open_port(self, index, name=None):
        self.opened = (index, name)

    def set_callback(self, callback):
        self.callback = callback

    def close_port(self):
        self.closed = True

    def emit(self, message):
        assert self.callback is not None
        self.callback((message, 0.0), None)


def test_monitor_client_selects_unique_logic_virtual_out_and_receives_frame():
    midi = _FakeMidiIn(["Keyboard", "Logic Pro Virtual Out"])
    client = LogicMidiMonitorClient(
        midi_in_factory=lambda: midi,
        now_epoch_seconds=lambda: 100.0,
        monotonic_clock=lambda: 1.0,
    )
    assert client.open() == "Logic Pro Virtual Out"
    assert midi.opened == (1, "N0TE Logic Monitor")
    for message in _messages(tempo=121.7):
        midi.emit(message)
    assert client.fetch_snapshot(timeout_seconds=0.1).tempo_bpm == 121.7
    client.close()
    assert midi.closed is True


def test_monitor_client_fails_closed_for_missing_or_ambiguous_port():
    for ports, phrase in (
        (["Keyboard"], "unavailable"),
        (["Logic Pro Virtual Out", "Logic Pro Virtual Out 2"], "ambiguous"),
    ):
        client = LogicMidiMonitorClient(
            midi_in_factory=lambda ports=ports: _FakeMidiIn(ports)
        )
        with pytest.raises(LogicMidiBridgeError, match=phrase):
            client.open()


class _ReferenceClient:
    def __init__(self):
        self.calls = []

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        primary = {
            "title": "Logic Reference",
            "source_locator": "catalog:logic-reference",
        }
        return {
            "provider_id": provider_id,
            "read_only": True,
            "action_authority_granted": False,
            "session_calibration": {
                "workspace_id": binding.workspace_id,
                "song_id": binding.song_id,
                "workspace_observation_id": binding.workspace_observation_id,
                "features": {"TEMPO_BPM": 128.4},
                "evidence": [],
            },
            "ranked": [primary],
            "primary": primary,
        }


def test_logic_timing_reaches_canonical_reference_workflow(tmp_path: Path):
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "Logic Bridge Artist")
    midi = _FakeMidiIn(["Logic Pro Virtual Out"])
    client = LogicMidiMonitorClient(
        midi_in_factory=lambda: midi,
        now_epoch_seconds=lambda: 100.0,
        monotonic_clock=lambda: 1.0,
    )
    try:
        song = headquarters.store.create_song("Logic Bridge Song")
        reference_client = _ReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        client.open()
        for message in _messages(tempo=128.4):
            midi.emit(message)

        result = asyncio.run(
            client.observe_and_discover(
                workflow,
                provider_id="session-local",
                comparison_dimensions=("tempo",),
                required_features=("tempo_bpm",),
            )
        )

        assert result.handshake.status == "CREATED"
        assert result.observation.status == "COMPLETE"
        assert result.observation.binding.song_id == song.id
        assert result.handshake.workspace.workspace.host_family == "LOGIC_PRO"
        assert result.references["primary"]["title"] == "Logic Reference"
        assert result.references["read_only"] is True
        assert result.references["action_authority_granted"] is False
        assert len(reference_client.calls) == 1
        _, binding, shadow, _ = reference_client.calls[0]
        assert binding == result.observation.binding
        assert shadow.status == "CURRENT"
        assert any(
            fact.object_kind == "TEMPO"
            and fact.field == "bpm"
            and fact.value == 128.4
            for fact in shadow.facts
        )
    finally:
        client.close()
        headquarters.close()


def test_scripter_probe_and_dependency_contract_are_explicit():
    root = Path(__file__).resolve().parents[2]
    script = (
        root / "integrations" / "logic" / "N0TETimingProbe.js"
    ).read_text(encoding="utf-8")
    assert "var NeedsTimingInfo = true" in script
    assert "GetTimingInfo()" in script
    assert "new ControlChange()" in script
    assert "N0TE_CHANNEL = 16" in script
    assert "CC_START = 119" in script
    assert "CC_END = 116" in script
    for forbidden in (
        "project_title",
        "project_path",
        "selected_track",
        "setTempo",
        "startTransport",
        "stopTransport",
    ):
        assert forbidden not in script

    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert 'python-rtmidi==1.5.7; sys_platform == "darwin"' in requirements
