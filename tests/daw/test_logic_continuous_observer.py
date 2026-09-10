from __future__ import annotations

import asyncio
from pathlib import Path

from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.logic_continuous_observer import LogicContinuousObserver
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
    LogicMidiMonitorClient,
    _checksum,
)
from n0te.memory import HeadquartersMemory


def _messages(
    *,
    sequence: int = 1,
    session: int = 0x0012345,
    tempo: float = 128.0,
    playing: bool = False,
    cycling: bool = False,
    numerator: int = 4,
    denominator: int = 4,
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


class _FakeMidiIn:
    def __init__(self):
        self.callback = None

    def get_ports(self):
        return ["Logic Pro Virtual Out"]

    def open_port(self, index, name=None):
        assert index == 0

    def set_callback(self, callback):
        self.callback = callback

    def close_port(self):
        pass

    def emit(self, messages):
        assert self.callback is not None
        for message in messages:
            self.callback((message, 0.0), None)


class _ReferenceClient:
    def __init__(self):
        self.calls = []
        self.fail_next = False

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("provider unavailable")
        primary = {
            "title": f"Reference {len(self.calls)}",
            "source_locator": f"catalog:reference-{len(self.calls)}",
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


def _system(tmp_path: Path):
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "Logic Observer Artist")
    headquarters.store.create_song("Logic Observer Song")
    midi = _FakeMidiIn()
    now = {"epoch": 100.0, "mono": 10.0}
    client = LogicMidiMonitorClient(
        midi_in_factory=lambda: midi,
        now_epoch_seconds=lambda: now["epoch"],
        monotonic_clock=lambda: now["mono"],
    )
    client.open()
    references = _ReferenceClient()
    workflow = HostSessionReferenceWorkflow(
        headquarters.host_observation,
        references,
    )
    observer = LogicContinuousObserver(
        client,
        workflow,
        provider_id="session-local",
        discovery_retry_seconds=30.0,
        monotonic_clock=lambda: now["mono"],
    )
    return headquarters, midi, now, references, observer


def _emit(midi, now, *, sequence, **kwargs):
    now["epoch"] += 1.0
    midi.emit(_messages(sequence=sequence, **kwargs))


def test_transport_meter_and_cycle_changes_commit_without_reference_spam(tmp_path: Path):
    headquarters, midi, now, references, observer = _system(tmp_path)
    try:
        _emit(midi, now, sequence=1, tempo=128.0)
        first = asyncio.run(observer.poll_once())
        assert first.observation_committed is True
        assert first.discovery_performed is True
        assert first.discovery_reason == "INITIAL"
        assert len(references.calls) == 1

        unchanged = asyncio.run(observer.poll_once())
        assert unchanged.observation_committed is False
        assert unchanged.discovery_performed is False
        assert unchanged.discovery_reason is None
        assert len(references.calls) == 1

        _emit(
            midi,
            now,
            sequence=2,
            tempo=128.0,
            playing=True,
            cycling=True,
            numerator=7,
            denominator=8,
        )
        changed = asyncio.run(observer.poll_once())
        assert changed.observation_committed is True
        assert changed.discovery_performed is False
        assert changed.discovery_reason is None
        assert len(references.calls) == 1
        facts = {
            (fact.object_kind, fact.field): fact.value
            for fact in changed.observation.shadow.facts
        }
        assert facts[("TRANSPORT", "is_playing")] is True
        assert facts[("TRANSPORT", "is_cycling")] is True
        assert facts[("TRANSPORT", "meter_numerator")] == 7
        assert facts[("TRANSPORT", "meter_denominator")] == 8
    finally:
        headquarters.close()


def test_meaningful_tempo_or_monitor_session_change_triggers_discovery(tmp_path: Path):
    headquarters, midi, now, references, observer = _system(tmp_path)
    try:
        _emit(midi, now, sequence=1, session=0x1111111, tempo=128.0)
        first = asyncio.run(observer.poll_once())
        first_workspace = first.observation.binding.workspace_id
        assert first.discovery_performed is True

        _emit(midi, now, sequence=2, session=0x1111111, tempo=128.2)
        tiny = asyncio.run(observer.poll_once())
        assert tiny.observation_committed is True
        assert tiny.discovery_performed is False
        assert len(references.calls) == 1

        _emit(midi, now, sequence=3, session=0x1111111, tempo=129.0)
        tempo = asyncio.run(observer.poll_once())
        assert tempo.discovery_performed is True
        assert tempo.discovery_reason == "MUSICAL_CHANGE"
        assert len(references.calls) == 2

        _emit(midi, now, sequence=4, session=0x2222222, tempo=129.0)
        session = asyncio.run(observer.poll_once())
        assert session.discovery_performed is True
        assert session.discovery_reason == "MUSICAL_CHANGE"
        assert session.observation.binding.workspace_id != first_workspace
        assert len(references.calls) == 3
    finally:
        headquarters.close()


def test_provider_failure_keeps_truth_and_backoff_defers_retry(tmp_path: Path):
    headquarters, midi, now, references, observer = _system(tmp_path)
    try:
        references.fail_next = True
        _emit(midi, now, sequence=1, tempo=120.0)
        failed = asyncio.run(observer.poll_once())
        assert failed.observation.shadow.status == "CURRENT"
        assert failed.discovery_performed is False
        assert failed.discovery_error_class == "RuntimeError"
        assert len(references.calls) == 1

        _emit(midi, now, sequence=2, tempo=121.0)
        deferred = asyncio.run(observer.poll_once())
        assert deferred.observation_committed is True
        assert deferred.discovery_reason == "RETRY"
        assert deferred.discovery_deferred is True
        assert deferred.discovery_performed is False
        assert len(references.calls) == 1

        now["mono"] += 31.0
        retried = asyncio.run(observer.poll_once())
        assert retried.observation_committed is False
        assert retried.discovery_reason == "RETRY"
        assert retried.discovery_performed is True
        assert len(references.calls) == 2
    finally:
        headquarters.close()


def test_forced_refresh_searches_without_new_host_observation(tmp_path: Path):
    headquarters, midi, now, references, observer = _system(tmp_path)
    try:
        _emit(midi, now, sequence=1, tempo=128.0)
        asyncio.run(observer.poll_once())
        refreshed = asyncio.run(observer.poll_once(force_discovery=True))
        assert refreshed.observation_committed is False
        assert refreshed.discovery_performed is True
        assert refreshed.discovery_reason == "EXPLICIT"
        assert len(references.calls) == 2
    finally:
        headquarters.close()
