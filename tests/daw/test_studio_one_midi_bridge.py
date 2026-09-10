from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.memory import HeadquartersMemory
from n0te.studio_one_midi_bridge import (
    DEFAULT_STUDIO_ONE_CLOCK_PORT,
    IDENTITY_SCOPE,
    MIDI_CLOCK,
    MIDI_START,
    MIDI_STOP,
    StudioOneMidiBridgeError,
    StudioOneMidiClockMonitorClient,
    StudioOneMidiClockTracker,
)


class _Clock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class _FakeMidiIn:
    def __init__(self, ports=()):
        self.ports = list(ports)
        self.callback = None
        self.opened = None
        self.virtual = None
        self.ignore = None
        self.closed = False

    def get_ports(self):
        return list(self.ports)

    def open_port(self, index, name=None):
        self.opened = (index, name)

    def open_virtual_port(self, name):
        self.virtual = name

    def ignore_types(self, **kwargs):
        self.ignore = kwargs

    def set_callback(self, callback):
        self.callback = callback

    def close_port(self):
        self.closed = True

    def emit(self, message):
        assert self.callback is not None
        self.callback((message, 0.0), None)


def _emit_tempo(midi, monotonic: _Clock, *, bpm: float, clocks: int = 25):
    interval = 60.0 / (bpm * 24.0)
    for _ in range(clocks):
        monotonic.value += interval
        midi.emit([MIDI_CLOCK])


def test_tracker_requires_stable_full_clock_window_and_resets_on_gap():
    tracker = StudioOneMidiClockTracker()
    interval = 60.0 / (120.0 * 24.0)
    now = 0.0
    for _ in range(24):
        now += interval
        tracker.feed([MIDI_CLOCK], monotonic_seconds=now)
    assert tracker.tempo_bpm is None

    now += interval
    tracker.feed([MIDI_CLOCK], monotonic_seconds=now)
    assert tracker.tempo_bpm == pytest.approx(120.0, rel=1e-6)
    assert tracker.transport_state == "UNKNOWN"

    tracker.feed([MIDI_START], monotonic_seconds=now + 0.001)
    assert tracker.transport_state == "PLAYING"
    tracker.feed([MIDI_STOP], monotonic_seconds=now + 0.002)
    assert tracker.transport_state == "STOPPED"

    now += 1.0
    tracker.feed([MIDI_CLOCK], monotonic_seconds=now)
    assert tracker.tempo_bpm is None
    assert tracker.transport_state == "STOPPED"


def test_monitor_receives_existing_port_and_does_not_invent_host_identity():
    monotonic = _Clock()
    midi = _FakeMidiIn(["Keyboard", DEFAULT_STUDIO_ONE_CLOCK_PORT])
    client = StudioOneMidiClockMonitorClient(
        midi_in_factory=lambda: midi,
        create_virtual_port=False,
        monotonic_clock=monotonic,
        now_epoch_seconds=lambda: 100.0,
        session_id="studio-one-test-session",
    )
    assert client.open() == DEFAULT_STUDIO_ONE_CLOCK_PORT
    assert midi.opened == (1, "N0TE Studio One Monitor")
    assert midi.ignore == {
        "sysex": True,
        "timing": False,
        "active_sense": True,
    }

    _emit_tempo(midi, monotonic, bpm=120.0)
    snapshot = client.fetch_snapshot(timeout_seconds=0.1)
    assert snapshot.tempo_bpm == pytest.approx(120.0, rel=1e-6)
    assert snapshot.transport_state == "UNKNOWN"
    assert snapshot.is_playing is None
    assert snapshot.identity_scope == IDENTITY_SCOPE
    assert snapshot.runtime.family == "STUDIO_ONE"
    assert snapshot.runtime.version == "UNOBSERVED"
    assert snapshot.runtime.platform.architecture == "UNKNOWN"
    assert snapshot.focus_dimensions() == ()
    assert {item.capability for item in snapshot.capabilities()} == {"tempo.read"}
    assert [(item.object_kind, item.field) for item in snapshot.shadow().events] == [
        ("TEMPO", "bpm")
    ]
    encoded = repr(snapshot)
    for forbidden in (
        "project_path",
        "project_title",
        "selected_track",
        "plugin_name",
    ):
        assert forbidden not in encoded
    client.close()
    assert midi.closed is True


def test_monitor_adds_transport_truth_only_after_start_or_stop():
    monotonic = _Clock()
    midi = _FakeMidiIn([DEFAULT_STUDIO_ONE_CLOCK_PORT])
    client = StudioOneMidiClockMonitorClient(
        midi_in_factory=lambda: midi,
        create_virtual_port=False,
        monotonic_clock=monotonic,
        now_epoch_seconds=lambda: 200.0,
        session_id="studio-one-transport-session",
    )
    client.open()
    _emit_tempo(midi, monotonic, bpm=128.0)
    midi.emit([MIDI_START])
    playing = client.fetch_snapshot(timeout_seconds=0.1)
    assert playing.transport_state == "PLAYING"
    assert playing.is_playing is True
    assert {item.capability for item in playing.capabilities()} == {
        "tempo.read",
        "transport.read",
    }
    facts = {
        (event.object_kind, event.field): event.value
        for event in playing.shadow().events
    }
    assert facts[("TRANSPORT", "is_playing")] is True

    midi.emit([MIDI_STOP])
    stopped = client.fetch_snapshot(timeout_seconds=0.1)
    assert stopped.is_playing is False
    client.close()


def test_monitor_can_expose_virtual_destination_or_fail_closed_when_unavailable():
    virtual = _FakeMidiIn([])
    client = StudioOneMidiClockMonitorClient(
        midi_in_factory=lambda: virtual,
        create_virtual_port=True,
    )
    assert client.open() == DEFAULT_STUDIO_ONE_CLOCK_PORT
    assert virtual.virtual == DEFAULT_STUDIO_ONE_CLOCK_PORT
    assert client.virtual_port is True
    client.close()

    unavailable = _FakeMidiIn([])
    client = StudioOneMidiClockMonitorClient(
        midi_in_factory=lambda: unavailable,
        create_virtual_port=False,
    )
    with pytest.raises(StudioOneMidiBridgeError, match="unavailable"):
        client.open()

    ambiguous = _FakeMidiIn([
        DEFAULT_STUDIO_ONE_CLOCK_PORT,
        DEFAULT_STUDIO_ONE_CLOCK_PORT + " 2",
    ])
    client = StudioOneMidiClockMonitorClient(
        midi_in_factory=lambda: ambiguous,
        create_virtual_port=False,
    )
    with pytest.raises(StudioOneMidiBridgeError, match="ambiguous"):
        client.open()


class _ReferenceClient:
    def __init__(self):
        self.calls = []

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        primary = {
            "title": "Studio One Reference",
            "source_locator": "catalog:studio-one-reference",
        }
        tempo = next(
            fact.value
            for fact in shadow.facts
            if fact.object_kind == "TEMPO" and fact.field == "bpm"
        )
        return {
            "provider_id": provider_id,
            "read_only": True,
            "action_authority_granted": False,
            "session_calibration": {
                "workspace_id": binding.workspace_id,
                "song_id": binding.song_id,
                "workspace_observation_id": binding.workspace_observation_id,
                "features": {"TEMPO_BPM": tempo},
                "evidence": [],
            },
            "ranked": [primary],
            "primary": primary,
        }


def test_studio_one_clock_reaches_canonical_reference_workflow(tmp_path: Path):
    headquarters = HeadquartersMemory.create(
        tmp_path / "hq",
        "Studio One Bridge Artist",
    )
    monotonic = _Clock()
    midi = _FakeMidiIn([DEFAULT_STUDIO_ONE_CLOCK_PORT])
    client = StudioOneMidiClockMonitorClient(
        midi_in_factory=lambda: midi,
        create_virtual_port=False,
        monotonic_clock=monotonic,
        now_epoch_seconds=lambda: 300.0,
        session_id="studio-one-reference-session",
    )
    try:
        song = headquarters.store.create_song("Studio One Bridge Song")
        references = _ReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            references,
        )
        client.open()
        _emit_tempo(midi, monotonic, bpm=124.0)

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
        assert result.handshake.workspace.workspace.host_family == "STUDIO_ONE"
        assert result.references["primary"]["title"] == "Studio One Reference"
        assert result.references["read_only"] is True
        assert result.references["action_authority_granted"] is False
        assert len(references.calls) == 1
        _, binding, shadow, _ = references.calls[0]
        assert binding == result.observation.binding
        assert shadow.status == "CURRENT"
        assert any(
            fact.object_kind == "TEMPO"
            and fact.field == "bpm"
            and fact.value == pytest.approx(124.0, rel=1e-6)
            for fact in shadow.facts
        )
    finally:
        client.close()
        headquarters.close()
