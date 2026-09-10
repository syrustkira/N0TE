from __future__ import annotations

from dataclasses import replace

import pytest

from integrations.ableton.N0TEBridge import N0TEBridge
from n0te import ableton
from n0te.ableton_host_bridge import AbletonHostBridgeError, AbletonRemoteScriptClient
from n0te.authority import AuthorityService
from n0te.host_session_handshake import HostSessionHandshake
from n0te.memory import HeadquartersMemory
from n0te.musical_plan import MusicalPlanError, MusicalPlanService
from n0te.musical_transactions import MusicalTransactionService
from n0te.song_transactions import SongTransactionService
from n0te.transactions import PostconditionResult


def test_default_command_runs_session_with_original_arguments(monkeypatch) -> None:
    calls = []

    def fake_run(argv, *, environment):
        calls.append((list(argv), dict(environment)))
        return 7

    monkeypatch.setattr(ableton, "run_command", fake_run)
    result = ableton.main(
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


def test_run_subcommand_is_only_a_readability_alias(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        ableton,
        "run_command",
        lambda argv, *, environment: calls.append(list(argv)) or 0,
    )

    assert ableton.main(["run", "--once"], environment={}) == 0
    assert calls == [["--once"]]


def test_install_subcommand_delegates_to_bridge_installer(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        ableton.ableton_bridge_installer,
        "main",
        lambda argv: calls.append(list(argv)) or 0,
    )

    assert (
        ableton.main(
            ["install", "--user-library", "/custom/User Library"],
            environment={},
        )
        == 0
    )
    assert calls == [["--user-library", "/custom/User Library"]]


def test_try_volume_subcommand_delegates_to_interactive_flow(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        ableton,
        "run_try_volume_command",
        lambda argv, *, environment: calls.append((list(argv), dict(environment))) or 9,
    )
    assert ableton.main(["try-volume", "0.61"], environment={"X": "1"}) == 9
    assert calls == [(["0.61"], {"X": "1"})]


def test_openai_reference_search_requires_explicit_connected_policy(capsys) -> None:
    result = ableton.main(
        ["--once"],
        environment={"OPENAI_API_KEY": "test-key"},
    )
    assert result == 2
    assert "N0TE_NETWORK_MODE=CONNECTED" in capsys.readouterr().err


def test_remote_http_reference_search_requires_connected_policy() -> None:
    result = ableton.main(
        ["--once"],
        environment={
            "N0TE_REFERENCE_SEARCH_ENDPOINT": "https://references.example.test/search",
            "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
            "N0TE_NETWORK_MODE": "OFFLINE",
        },
    )
    assert result == 2


def test_loopback_http_reference_search_is_allowed_while_offline() -> None:
    environment = {
        "N0TE_REFERENCE_SEARCH_ENDPOINT": "http://127.0.0.1:9001/search",
        "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
        "N0TE_NETWORK_MODE": "OFFLINE",
    }

    ableton.validate_network_preflight(environment)
    assert environment["N0TE_NETWORK_MODE"] == "OFFLINE"


def test_bridge_failure_points_to_installer(monkeypatch, capsys) -> None:
    def fail(argv, *, environment):
        raise AbletonHostBridgeError("bridge is unavailable")

    monkeypatch.setattr(ableton, "run_command", fail)

    assert ableton.main(["--once"], environment={}) == 2
    error = capsys.readouterr().err
    assert "bridge is unavailable" in error
    assert "python -m n0te.ableton install" in error


class _VolumeParameter:
    def __init__(self, value: float):
        self.min = 0.0
        self.max = 1.0
        self.value = float(value)
        self.is_enabled = True


class _Mixer:
    def __init__(self, value: float):
        self.volume = _VolumeParameter(value)


class _Track:
    def __init__(self, name: str, value: float):
        self.name = name
        self.mixer_device = _Mixer(value)


class _View:
    def __init__(self, selected):
        self.selected_track = selected


class _Song:
    def __init__(self, value: float = 0.5):
        self.tracks = (_Track("Drums", 0.4), _Track("Bass", value))
        self.return_tracks = ()
        self.master_track = _Track("Master", 0.7)
        self.view = _View(self.tracks[1])
        self.tempo = 128.0
        self.is_playing = False
        self.current_song_time = 0.0
        self.file_path = ""

    def get_data(self, key, default=None):
        return default


class _Application:
    def get_major_version(self):
        return 12

    def get_minor_version(self):
        return 4

    def get_bugfix_version(self):
        return 5


class _CInstance:
    def __init__(self, song):
        self._song = song

    def song(self):
        return self._song

    def application(self):
        return _Application()


class _FailingVerifyDriver(ableton.AbletonSelectedTrackVolumeDriver):
    def verify_action(self, action, execution):
        return PostconditionResult(
            action.action_id,
            action.postcondition_ref,
            "FAILED",
            f"test:evidence:forced-postcondition-failure:{action.action_id}",
        )


def _prepared_live_transaction(tmp_path, *, original=0.5, target=0.65):
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "Ableton Try Artist")
    song = headquarters.store.create_song("Ableton Try Song")
    headquarters.store.create_version(song.id, label="before-try")
    live_song = _Song(original)
    bridge = N0TEBridge(_CInstance(live_song), port=0, start_server=True)
    endpoint = f"http://127.0.0.1:{bridge.port}"
    observation_client = AbletonRemoteScriptClient(endpoint)
    snapshot = observation_client.fetch_snapshot()

    handshake = HostSessionHandshake(headquarters.host_observation).attach(
        runtime=snapshot.runtime,
        location_ref=snapshot.location_ref,
        display_name="Ableton Live • reversible transaction proof",
    )
    observed = headquarters.host_observation.observe(
        handshake.binding,
        capabilities=snapshot.capabilities(),
        focus_dimensions=snapshot.focus_dimensions(),
        focus_evidence_ref="test:ableton:selected-track-focus",
        shadow=snapshot.shadow(),
        now_epoch_seconds=snapshot.observed_at_epoch_seconds,
    )
    assert observed.shadow.status == "CURRENT"
    context = headquarters.focus.capture(
        handshake.binding.workspace_id,
        song_id=song.id,
        runtime=snapshot.runtime,
        observation_evidence_ref="test:ableton:observation",
        dimensions=snapshot.focus_dimensions(),
    )
    plans = MusicalPlanService(headquarters.focus)
    plan = plans.prepare(
        context,
        plan_id="plan:ableton-selected-track-volume-try",
        artist_intent="Try a slightly stronger selected-track level.",
        required_dimensions=("TRACK",),
        desired_change=f"Try selected track mixer level at normalized {target:.3f}.",
        editable_element_refs=(snapshot.selected_track.ref,),
        verification_refs=("verify:ableton:selected-track-volume",),
        provenance_refs=("test:ableton:try-volume",),
    )
    compiler = ableton.AbletonSelectedTrackVolumeCompiler(target)
    compilation = plans.compile(plan, context, snapshot.runtime, compiler)
    transactions = MusicalTransactionService(
        plans,
        SongTransactionService(headquarters.transactions),
    )
    intent = transactions.preview(plan, context, compilation)
    approval = AuthorityService.bind_approval(
        intent,
        "artist-confirmation:test-ableton-selected-track-volume",
    )
    prepared = transactions.prepare(
        plan,
        context,
        compilation,
        approval,
        idempotency_key="test:ableton:track-volume:1",
        claim_evidence_ref="test:ableton:execution-gate",
    )
    action_client = ableton.AbletonTrackVolumeActionClient(endpoint)
    return {
        "headquarters": headquarters,
        "live_song": live_song,
        "bridge": bridge,
        "observation_client": observation_client,
        "action_client": action_client,
        "transactions": transactions,
        "context": context,
        "plan": plan,
        "compilation": compilation,
        "prepared": prepared,
        "original": original,
        "target": target,
    }


def _close_fixture(fixture):
    try:
        fixture["bridge"].disconnect()
    finally:
        fixture["headquarters"].close()


def test_real_ableton_volume_action_completes_through_authority_transaction_and_receipt(tmp_path):
    fixture = _prepared_live_transaction(tmp_path, original=0.5, target=0.65)
    try:
        driver = ableton.AbletonSelectedTrackVolumeDriver(
            fixture["action_client"],
            observation_client=fixture["observation_client"],
        )
        result = fixture["transactions"].run(
            fixture["prepared"],
            fixture["plan"],
            fixture["context"],
            fixture["compilation"],
            driver,
        )

        assert result.status == "COMPLETE"
        assert result.operation.recorded_state == "SUCCEEDED"
        assert fixture["live_song"].tracks[1].mixer_device.volume.value == pytest.approx(0.65)
        history = fixture["transactions"].history(fixture["prepared"])
        assert history.requires_recovery_review is False
        event_types = [event.event_type for event in history.events]
        assert "SNAPSHOT_CAPTURED" in event_types
        assert "STEP_EXECUTION_STARTED" in event_types
        assert "STEP_EXECUTION_RECORDED" in event_types
        assert "POSTCONDITION_RECORDED" in event_types
        assert result.operation.receipt_ref.startswith("ableton:track-volume-receipt:sha256:")
    finally:
        _close_fixture(fixture)


def test_failed_postcondition_restores_original_live_volume_from_transaction_snapshot(tmp_path):
    fixture = _prepared_live_transaction(tmp_path, original=0.42, target=0.68)
    try:
        driver = _FailingVerifyDriver(
            fixture["action_client"],
            observation_client=fixture["observation_client"],
        )
        result = fixture["transactions"].run(
            fixture["prepared"],
            fixture["plan"],
            fixture["context"],
            fixture["compilation"],
            driver,
        )

        assert result.status == "COMPENSATED"
        assert result.operation.recorded_state == "FAILED"
        assert result.compensated_step_ids == (
            fixture["compilation"].actions[0].action_id,
        )
        assert fixture["live_song"].tracks[1].mixer_device.volume.value == pytest.approx(0.42)
        history = fixture["transactions"].history(fixture["prepared"])
        assert history.requires_recovery_review is False
        assert any(
            event.event_type == "COMPENSATION_RECORDED" and event.status == "RESTORED"
            for event in history.events
        )
    finally:
        _close_fixture(fixture)


def test_compiler_payload_is_content_addressed_and_respects_locked_editable_targets(tmp_path):
    fixture = _prepared_live_transaction(tmp_path, original=0.5, target=0.61)
    try:
        action = fixture["compilation"].actions[0]
        payload = ableton.AbletonTrackVolumePayload.from_ref(action.payload_ref)
        assert payload.track_ref == "track:1"
        assert payload.target_normalized == pytest.approx(0.61)

        tampered = action.payload_ref.replace(":sha256:", "x:sha256:", 1)
        with pytest.raises(ableton.AbletonTrackVolumeActionError):
            ableton.AbletonTrackVolumePayload.from_ref(tampered)

        locked_plan = replace(
            fixture["plan"],
            locked_element_refs=("track:1",),
            editable_element_refs=(),
        )
        with pytest.raises(MusicalPlanError, match="locked"):
            ableton.AbletonSelectedTrackVolumeCompiler(0.7).compile(
                locked_plan,
                fixture["observation_client"].fetch_snapshot().runtime,
            )

        outside_plan = replace(
            fixture["plan"],
            editable_element_refs=("track:0",),
        )
        with pytest.raises(MusicalPlanError, match="editable_element_refs"):
            ableton.AbletonSelectedTrackVolumeCompiler(0.7).compile(
                outside_plan,
                fixture["observation_client"].fetch_snapshot().runtime,
            )
    finally:
        _close_fixture(fixture)


class _TryProbe:
    def current_process(self):
        return object()

    def status(self, process):
        return "ALIVE"


class _TryRuntime:
    def __init__(self, headquarters):
        self.headquarters = headquarters
        self.quit_calls = 0

    def launch(self, *, profile_id, process, probe):
        self.launch_args = (profile_id, process, probe)
        return type("Launch", (), {"status": "STARTED", "reason": None})()

    def quit(self):
        self.quit_calls += 1
        return type("Quit", (), {"status": "STOPPED", "reason": None})()


def _interactive_try_fixture(tmp_path, *, original=0.5, target=0.65):
    headquarters = HeadquartersMemory.create(tmp_path / "interactive-hq", "Ableton Interactive Artist")
    headquarters.store.create_song("Interactive Try Song")
    live_song = _Song(original)
    bridge = N0TEBridge(_CInstance(live_song), port=0, start_server=True)
    endpoint = f"http://127.0.0.1:{bridge.port}"
    runtime = _TryRuntime(headquarters)
    config = ableton.AbletonTryVolumeConfig(
        data_root=(tmp_path / "data").resolve(),
        state_root=(tmp_path / "state").resolve(),
        profile_id=headquarters.store.profile_id,
        bridge_endpoint=endpoint,
        target_normalized=target,
    )
    return headquarters, live_song, bridge, runtime, config


def _typed_token_from_prompt(prompt: str, verb: str) -> str:
    prefix = f"Type {verb} "
    assert prompt.startswith(prefix)
    return prompt[len("Type "):].split(" to ", 1)[0]


def test_try_volume_cancel_creates_no_operation_and_does_not_move_fader(tmp_path):
    headquarters, live_song, bridge, runtime, config = _interactive_try_fixture(tmp_path)
    output = []
    try:
        result = ableton.run_try_volume(
            config,
            process_probe=_TryProbe(),
            runtime_factory=lambda **kwargs: runtime,
            input_fn=lambda prompt: "CANCEL",
            output=output.append,
        )
        assert result == 0
        assert live_song.tracks[1].mixer_device.volume.value == pytest.approx(0.5)
        assert headquarters.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert any("cancelled" in line for line in output)
        assert runtime.quit_calls == 1
    finally:
        bridge.disconnect()
        headquarters.close()


def test_try_volume_apply_then_keep_requires_exact_typed_approval(tmp_path):
    headquarters, live_song, bridge, runtime, config = _interactive_try_fixture(tmp_path)
    prompts = []
    output = []

    def answer(prompt):
        prompts.append(prompt)
        if prompt.startswith("Type APPLY "):
            return _typed_token_from_prompt(prompt, "APPLY")
        if prompt.startswith("Hear it in Ableton Live"):
            return "KEEP"
        raise AssertionError(prompt)

    try:
        result = ableton.run_try_volume(
            config,
            process_probe=_TryProbe(),
            runtime_factory=lambda **kwargs: runtime,
            input_fn=answer,
            output=output.append,
        )
        assert result == 0
        assert live_song.tracks[1].mixer_device.volume.value == pytest.approx(0.65)
        assert headquarters.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert any("KEEP" in line for line in output)
        assert any("authority: REVERSIBLE" in line for line in output)
        assert runtime.quit_calls == 1
    finally:
        bridge.disconnect()
        headquarters.close()


def test_try_volume_restore_is_a_second_exact_approved_transaction(tmp_path):
    headquarters, live_song, bridge, runtime, config = _interactive_try_fixture(
        tmp_path, original=0.43, target=0.67
    )
    output = []

    def answer(prompt):
        if prompt.startswith("Type APPLY "):
            return _typed_token_from_prompt(prompt, "APPLY")
        if prompt.startswith("Hear it in Ableton Live"):
            return "RESTORE"
        if prompt.startswith("Type RESTORE "):
            return _typed_token_from_prompt(prompt, "RESTORE")
        raise AssertionError(prompt)

    try:
        result = ableton.run_try_volume(
            config,
            process_probe=_TryProbe(),
            runtime_factory=lambda **kwargs: runtime,
            input_fn=answer,
            output=output.append,
        )
        assert result == 0
        assert live_song.tracks[1].mixer_device.volume.value == pytest.approx(0.43)
        assert headquarters.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 2
        assert any("RESTORED" in line for line in output)
        states = [
            row[0]
            for row in headquarters.store._conn.execute(
                "SELECT recorded_state FROM operations ORDER BY rowid"
            )
        ]
        assert states == ["SUCCEEDED", "SUCCEEDED"]
    finally:
        bridge.disconnect()
        headquarters.close()


def test_try_volume_refuses_restore_after_manual_fader_move(tmp_path):
    headquarters, live_song, bridge, runtime, config = _interactive_try_fixture(tmp_path)

    def answer(prompt):
        if prompt.startswith("Type APPLY "):
            return _typed_token_from_prompt(prompt, "APPLY")
        if prompt.startswith("Hear it in Ableton Live"):
            live_song.tracks[1].mixer_device.volume.value = 0.73
            return "RESTORE"
        raise AssertionError(prompt)

    try:
        with pytest.raises(ableton.AbletonCommandError, match="changed during audition"):
            ableton.run_try_volume(
                config,
                process_probe=_TryProbe(),
                runtime_factory=lambda **kwargs: runtime,
                input_fn=answer,
                output=lambda message: None,
            )
        assert live_song.tracks[1].mixer_device.volume.value == pytest.approx(0.73)
        assert headquarters.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert runtime.quit_calls == 1
    finally:
        bridge.disconnect()
        headquarters.close()
