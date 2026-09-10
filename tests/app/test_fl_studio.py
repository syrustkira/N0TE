from __future__ import annotations

from n0te import fl_studio
from n0te.fl_studio_host_bridge import FLStudioHostBridgeError


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
