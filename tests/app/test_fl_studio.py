from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

from n0te import fl_studio
from n0te.fl_studio_continuous_observer import (
    _observation_fingerprint,
    _reference_fingerprint,
)
from n0te.fl_studio_host_bridge import (
    FL_STUDIO_SNAPSHOT_SCHEMA,
    FLStudioHostBridgeError,
    FLStudioObservationSnapshot,
)


def test_default_command_runs_session_with_original_arguments(monkeypatch):
    calls = []

    def fake_run(argv, *, environment):
        calls.append((list(argv), dict(environment)))
        return 7

    monkeypatch.setattr(fl_studio, "run_command", fake_run)
    result = fl_studio.main(
        ["--once", "--profile-id", "prf_" + "a" * 32],
        environment={"N0TE_NETWORK_MODE": "CONNECTED"},
    )

    assert result == 7
    assert calls == [
        (
            ["--once", "--profile-id", "prf_" + "a" * 32],
            {"N0TE_NETWORK_MODE": "CONNECTED"},
        )
    ]


def test_run_subcommand_is_only_a_readability_alias(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fl_studio,
        "run_command",
        lambda argv, *, environment: calls.append(list(argv)) or 0,
    )

    assert fl_studio.main(["run", "--once"], environment={}) == 0
    assert calls == [["--once"]]


def test_install_subcommand_delegates_to_bridge_installer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fl_studio.fl_studio_bridge_installer,
        "main",
        lambda argv: calls.append(list(argv)) or 0,
    )

    assert (
        fl_studio.main(
            ["install", "--user-data", "/custom/FL Studio"],
            environment={},
        )
        == 0
    )
    assert calls == [["--user-data", "/custom/FL Studio"]]


def test_openai_reference_search_requires_explicit_connected_policy(capsys):
    result = fl_studio.main(
        ["--once"],
        environment={"OPENAI_API_KEY": "test-key"},
    )
    assert result == 2
    assert "N0TE_NETWORK_MODE=CONNECTED" in capsys.readouterr().err


def test_remote_http_reference_search_requires_connected_policy():
    result = fl_studio.main(
        ["--once"],
        environment={
            "N0TE_REFERENCE_SEARCH_ENDPOINT": "https://references.example.test/search",
            "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
            "N0TE_NETWORK_MODE": "OFFLINE",
        },
    )
    assert result == 2


def test_loopback_http_reference_search_is_allowed_while_offline():
    environment = {
        "N0TE_REFERENCE_SEARCH_ENDPOINT": "http://127.0.0.1:9001/search",
        "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
        "N0TE_NETWORK_MODE": "OFFLINE",
    }

    fl_studio.validate_network_preflight(environment)
    assert environment["N0TE_NETWORK_MODE"] == "OFFLINE"


def test_bridge_failure_points_to_installer_and_midi_settings(monkeypatch, capsys):
    def fail(argv, *, environment):
        raise FLStudioHostBridgeError("snapshot unavailable")

    monkeypatch.setattr(fl_studio, "run_command", fail)

    assert fl_studio.main(["--once"], environment={}) == 2
    error = capsys.readouterr().err
    assert "snapshot unavailable" in error
    assert "python -m n0te.fl_studio install" in error
    assert "MIDI Settings" in error


def _device_payload(*, mixer_plugins=None, generator="Serum"):
    if mixer_plugins is None:
        mixer_plugins = [
            {"slot": 0, "name": "Fruity Parametric EQ 2"},
            {"slot": 3, "name": "ValhallaRoom"},
        ]
    return {
        "schema": FL_STUDIO_SNAPSHOT_SCHEMA,
        "adapter": {"id": "N0TEBridge", "version": "1"},
        "bridge_session_id": "fl-device-session",
        "runtime": {
            "host_family": "FL_STUDIO",
            "version": "2026.1.4.1234",
            "edition": "Producer Edition",
            "os_name": "Windows",
            "machine": "AMD64",
        },
        "observed_at_epoch_seconds": 100,
        "project": {"title": "Tool Aware", "changed_flag": 0},
        "tempo_bpm": 128.0,
        "transport": {"is_playing": True, "song_position": 0.25, "loop_mode": 1},
        "selected_mixer_track": {"index": 4, "name": "Drum Bus"},
        "selected_channel": {"index": 2, "name": "Lead"},
        "active_window": {"form_id": 7, "caption": "Serum", "plugin_name": "Serum"},
        "selected_mixer_plugins": {"complete": True, "plugins": mixer_plugins},
        "selected_channel_generator": {
            "complete": True,
            "plugin": None if generator is None else {"name": generator},
        },
    }


def test_device_evidence_maps_into_canonical_host_shadow():
    snapshot = FLStudioObservationSnapshot.from_payload(_device_payload())

    assert snapshot.mixer_plugin_chain_observed is True
    assert [(item.slot, item.name) for item in snapshot.selected_mixer_plugins] == [
        (0, "Fruity Parametric EQ 2"),
        (3, "ValhallaRoom"),
    ]
    assert snapshot.channel_generator_observed is True
    assert snapshot.selected_channel_generator.name == "Serum"
    assert "device.chain.read" in {item.capability for item in snapshot.capabilities()}

    facts = {
        (event.object_kind, event.object_ref, event.field): event.value
        for event in snapshot.shadow().events
    }
    assert facts[("TRACK", "mixer-track:4", "device_count")] == 2
    assert facts[("DEVICE_PLUGIN", "device:mixer-track:4:slot:0", "name")] == (
        "Fruity Parametric EQ 2"
    )
    assert facts[("DEVICE_PLUGIN", "device:mixer-track:4:slot:3", "name")] == (
        "ValhallaRoom"
    )
    assert facts[("DEVICE_PLUGIN", "device:channel:2:generator", "name")] == "Serum"


def test_incomplete_plugin_probe_never_becomes_partial_chain_truth():
    payload = _device_payload()
    payload["selected_mixer_plugins"] = {"complete": False, "plugins": []}
    snapshot = FLStudioObservationSnapshot.from_payload(payload)
    assert snapshot.mixer_plugin_chain_observed is False
    assert snapshot.selected_mixer_plugins == ()
    assert not any(
        event.object_kind == "DEVICE_PLUGIN"
        and event.object_ref.startswith("device:mixer-track:4")
        for event in snapshot.shadow().events
    )

    payload["selected_mixer_plugins"] = {
        "complete": False,
        "plugins": [{"slot": 0, "name": "Do Not Trust"}],
    }
    with pytest.raises(FLStudioHostBridgeError, match="partial evidence"):
        FLStudioObservationSnapshot.from_payload(payload)


def test_device_only_change_updates_host_truth_without_changing_reference_fingerprint():
    before = FLStudioObservationSnapshot.from_payload(_device_payload())
    after = FLStudioObservationSnapshot.from_payload(
        _device_payload(
            mixer_plugins=[
                {"slot": 0, "name": "Fruity Parametric EQ 2"},
                {"slot": 3, "name": "ValhallaRoom"},
                {"slot": 8, "name": "Maximus"},
            ]
        )
    )

    assert _observation_fingerprint(before) != _observation_fingerprint(after)
    assert _reference_fingerprint(before) == _reference_fingerprint(after)


def _module(name: str, **attrs) -> ModuleType:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_real_fl_script_scans_selected_generator_and_all_mixer_slots(monkeypatch):
    calls = []

    def is_valid(index, slot=-1, use_global_index=False):
        calls.append(("valid", index, slot, use_global_index))
        if slot == -1:
            return index == 2 and use_global_index is True
        return index == 4 and slot in {0, 3}

    def plugin_name(index, slot=-1, user_name=0, use_global_index=False):
        calls.append(("name", index, slot, user_name, use_global_index))
        if slot == -1:
            return "Serum"
        return {0: "Fruity Parametric EQ 2", 3: "ValhallaRoom"}[slot]

    monkeypatch.setitem(
        sys.modules,
        "mixer",
        _module(
            "mixer",
            trackNumber=lambda: 4,
            getTrackName=lambda index: "Drum Bus",
            getCurrentTempo=lambda: 128.0,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "channels",
        _module(
            "channels",
            selectedChannel=lambda canBeNone=0, offset=0, indexGlobal=0: 2,
            getChannelName=lambda index, useGlobalIndex=False: "Lead",
        ),
    )
    monkeypatch.setitem(sys.modules, "plugins", _module("plugins", isValid=is_valid, getPluginName=plugin_name))
    monkeypatch.setitem(
        sys.modules,
        "transport",
        _module(
            "transport",
            isPlaying=lambda: 1,
            getSongPos=lambda: 0.25,
            getLoopMode=lambda: 1,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "ui",
        _module(
            "ui",
            getVersion=lambda mode=4: {0: 2026, 1: 1, 2: 4, 3: 1234, 4: "Producer", 5: "2026"}[mode],
            getFocusedFormID=lambda: 7,
            getFocusedFormCaption=lambda: "Serum",
            getFocusedPluginName=lambda: "Serum",
            setHintMsg=lambda message: None,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "general",
        _module(
            "general",
            getProjectTitle=lambda: "Tool Aware",
            getChangedFlag=lambda: 0,
        ),
    )

    script = (
        Path(__file__).resolve().parents[2]
        / "integrations"
        / "fl_studio"
        / "N0TEBridge"
        / "device_N0TEBridge.py"
    )
    namespace = runpy.run_path(str(script))
    payload = namespace["_snapshot"]()

    assert payload["selected_mixer_plugins"] == {
        "complete": True,
        "plugins": [
            {"slot": 0, "name": "Fruity Parametric EQ 2"},
            {"slot": 3, "name": "ValhallaRoom"},
        ],
    }
    assert payload["selected_channel_generator"] == {
        "complete": True,
        "plugin": {"name": "Serum"},
    }
    assert [call[2] for call in calls if call[0] == "valid" and call[2] >= 0] == list(range(10))
