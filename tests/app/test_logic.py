from __future__ import annotations

from n0te import logic
from n0te.logic_midi_bridge import LogicMidiBridgeError


def test_setup_command_prints_monitor_strip_guidance(capsys):
    assert logic.main(["setup"], environment={}) == 0
    output = capsys.readouterr().out
    assert "N0TE Monitor" in output
    assert "N0TETimingProbe.js" in output
    assert "Logic Pro Virtual Out" in output
    assert "python -m n0te.logic --once" in output


def test_default_and_run_alias_delegate_without_rewriting_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        logic,
        "run_command",
        lambda argv, *, environment: calls.append((list(argv), dict(environment))) or 0,
    )
    assert logic.main(["--once"], environment={"X": "1"}) == 0
    assert logic.main(["run", "--once"], environment={"Y": "2"}) == 0
    assert calls == [(["--once"], {"X": "1"}), (["--once"], {"Y": "2"})]


def test_internet_reference_search_requires_connected_policy(capsys):
    result = logic.main(["--once"], environment={"OPENAI_API_KEY": "test-key"})
    assert result == 2
    assert "N0TE_NETWORK_MODE=CONNECTED" in capsys.readouterr().err


def test_loopback_reference_search_is_allowed_offline():
    logic.validate_network_preflight(
        {
            "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
            "N0TE_REFERENCE_SEARCH_ENDPOINT": "http://127.0.0.1:9001/search",
            "N0TE_NETWORK_MODE": "OFFLINE",
        }
    )


def test_bridge_failure_points_back_to_setup(monkeypatch, capsys):
    def fail(argv, *, environment):
        raise LogicMidiBridgeError("Logic timing frame unavailable")

    monkeypatch.setattr(logic, "run_command", fail)
    assert logic.main(["--once"], environment={}) == 2
    error = capsys.readouterr().err
    assert "Logic timing frame unavailable" in error
    assert "python -m n0te.logic setup" in error
