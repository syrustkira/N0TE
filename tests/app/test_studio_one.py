from __future__ import annotations

from n0te import studio_one
from n0te.studio_one_midi_bridge import StudioOneMidiBridgeError


def test_setup_command_prints_supported_midi_clock_guidance(capsys):
    assert studio_one.main(["setup"], environment={}) == 0
    output = capsys.readouterr().out
    assert "N0TE Studio One Clock" in output
    assert "External Devices" in output
    assert "MIDI Clock" in output
    assert "MIDI Clock Start" in output
    assert "python -m n0te.studio_one --once" in output
    assert "switching Studio One Songs" in output


def test_default_and_run_alias_delegate_without_rewriting_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        studio_one,
        "run_command",
        lambda argv, *, environment: calls.append((list(argv), dict(environment))) or 0,
    )
    assert studio_one.main(["--once"], environment={"X": "1"}) == 0
    assert studio_one.main(["run", "--once"], environment={"Y": "2"}) == 0
    assert calls == [(["--once"], {"X": "1"}), (["--once"], {"Y": "2"})]


def test_internet_reference_search_requires_connected_policy(capsys):
    result = studio_one.main(["--once"], environment={"OPENAI_API_KEY": "test-key"})
    assert result == 2
    assert "N0TE_NETWORK_MODE=CONNECTED" in capsys.readouterr().err


def test_loopback_reference_search_is_allowed_offline():
    studio_one.validate_network_preflight(
        {
            "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
            "N0TE_REFERENCE_SEARCH_ENDPOINT": "http://127.0.0.1:9001/search",
            "N0TE_NETWORK_MODE": "OFFLINE",
        }
    )


def test_bridge_failure_points_back_to_setup(monkeypatch, capsys):
    def fail(argv, *, environment):
        raise StudioOneMidiBridgeError("stable Studio One clock unavailable")

    monkeypatch.setattr(studio_one, "run_command", fail)
    assert studio_one.main(["--once"], environment={}) == 2
    error = capsys.readouterr().err
    assert "stable Studio One clock unavailable" in error
    assert "python -m n0te.studio_one setup" in error
