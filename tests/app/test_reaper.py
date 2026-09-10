from __future__ import annotations

from n0te import reaper
from n0te.reaper_host_bridge import ReaperHostBridgeError


def test_install_subcommand_delegates_to_bridge_installer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        reaper.reaper_bridge_installer,
        "main",
        lambda argv: calls.append(list(argv)) or 0,
    )
    assert reaper.main(["install", "--resource-path", "/custom/REAPER"], environment={}) == 0
    assert calls == [["--resource-path", "/custom/REAPER"]]


def test_default_and_run_alias_delegate_without_rewriting_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        reaper,
        "run_command",
        lambda argv, *, environment: calls.append((list(argv), dict(environment))) or 0,
    )
    assert reaper.main(["--once"], environment={"X": "1"}) == 0
    assert reaper.main(["run", "--once"], environment={"Y": "2"}) == 0
    assert calls == [(["--once"], {"X": "1"}), (["--once"], {"Y": "2"})]


def test_internet_reference_search_requires_connected_policy(capsys):
    result = reaper.main(["--once"], environment={"OPENAI_API_KEY": "test-key"})
    assert result == 2
    assert "N0TE_NETWORK_MODE=CONNECTED" in capsys.readouterr().err


def test_loopback_reference_search_is_allowed_offline():
    reaper.validate_network_preflight(
        {
            "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
            "N0TE_REFERENCE_SEARCH_ENDPOINT": "http://127.0.0.1:9001/search",
            "N0TE_NETWORK_MODE": "OFFLINE",
        }
    )


def test_bridge_failure_points_to_install_and_actions_registration(monkeypatch, capsys):
    def fail(argv, *, environment):
        raise ReaperHostBridgeError("snapshot unavailable")

    monkeypatch.setattr(reaper, "run_command", fail)
    assert reaper.main(["--once"], environment={}) == 2
    error = capsys.readouterr().err
    assert "snapshot unavailable" in error
    assert "python -m n0te.reaper install" in error
    assert "ReaScript: Load" in error
