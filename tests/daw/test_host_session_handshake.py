from __future__ import annotations

import asyncio

import pytest

from n0te.focus import FocusDimension
from n0te.host_observation import CapabilityFactInput, ShadowObservationInput
from n0te.host_session_handshake import (
    HostSessionHandshake,
    HostSessionHandshakeError,
    HostSessionReferenceWorkflow,
    HostSessionReferenceWorkflowError,
)
from n0te.hosts import HostRuntimeIdentity
from n0te.memory import HeadquartersMemory
from n0te.shadow import ShadowEventInput


def _runtime(
    *,
    family: str = "ABLETON_LIVE",
    version: str = "12.1",
) -> HostRuntimeIdentity:
    return HostRuntimeIdentity.from_runtime_labels(
        host_family=family,
        version=version,
        edition="Standard",
        os_name="Darwin",
        machine="arm64",
    )


def _capability(*, observed_at: int = 100) -> CapabilityFactInput:
    return CapabilityFactInput(
        route_id="native-api",
        route_kind="HOST_NATIVE",
        capability="transport.read",
        display_name="Ableton Live API",
        availability="AVAILABLE",
        evidence_kind="RUNTIME_PROBE",
        observed_at_epoch_seconds=observed_at,
        evidence_ref="host:ableton:transport",
    )


def _focus() -> FocusDimension:
    return FocusDimension(
        dimension="TRACK",
        state="OBSERVED_EXACT",
        refs=("track:1",),
        evidence_ref="host:ableton:focus-track",
    )


def _tempo_shadow(value: float = 128.0) -> ShadowObservationInput:
    return ShadowObservationInput(
        coverage="FULL",
        actor="EXTERNAL",
        evidence_ref="host:ableton:snapshot",
        events=(
            ShadowEventInput(
                object_kind="TEMPO",
                object_ref="tempo:main",
                field="bpm",
                action="SET",
                value=value,
                evidence_ref="host:ableton:tempo",
            ),
        ),
    )


class _ReferenceClient:
    def __init__(self) -> None:
        self.calls = []

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        primary = {
            "title": "Reference A",
            "source_locator": "catalog:reference-a",
        }
        return {
            "provider_id": provider_id,
            "read_only": True,
            "action_authority_granted": False,
            "session_calibration": {
                "workspace_id": binding.workspace_id,
                "song_id": binding.song_id,
                "workspace_observation_id": binding.workspace_observation_id,
                "features": {"TEMPO_BPM": 128.0},
                "evidence": [],
            },
            "ranked": [primary],
            "primary": primary,
        }


class _FailingReferenceClient:
    def __init__(self) -> None:
        self.calls = []

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        raise RuntimeError("reference provider unavailable")


def test_first_unknown_project_attaches_to_explicitly_active_song(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        song = headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)

        result = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/active-song.als",
            display_name="Active Song.als",
            state_fingerprint="state:1",
        )

        assert result.status == "CREATED"
        assert result.binding.song_id == song.id
        assert result.workspace.workspace.song_id == song.id
        assert result.workspace.workspace.host_family == "ABLETON_LIVE"
        assert result.workspace.current_observation.location_ref == (
            "file:///projects/active-song.als"
        )
        assert result.binding.workspace_observation_id == (
            result.workspace.current_observation.id
        )
    finally:
        headquarters.close()


def test_exact_project_reuses_workspace_without_appending_history(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/active-song.als",
            display_name="Active Song.als",
            state_fingerprint="state:1",
        )
        second = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/active-song.als",
            display_name="Active Song.als",
            state_fingerprint="state:1",
        )

        assert second.status == "REUSED"
        assert second.binding.workspace_id == first.binding.workspace_id
        assert second.binding.workspace_observation_id == (
            first.binding.workspace_observation_id
        )
        assert len(headquarters.workspaces.history(first.binding.workspace_id)) == 1
    finally:
        headquarters.close()


def test_known_workspace_reconciles_project_move_without_new_identity(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/original.als",
            display_name="Song.als",
            state_fingerprint="state:1",
        )
        moved = handshake.attach(
            runtime=_runtime(),
            known_workspace_id=first.binding.workspace_id,
            location_ref="file:///archive/song.als",
        )

        assert moved.status == "RECONCILED"
        assert moved.binding.workspace_id == first.binding.workspace_id
        assert moved.binding.workspace_observation_id != (
            first.binding.workspace_observation_id
        )
        assert moved.workspace.current_observation.location_ref == (
            "file:///archive/song.als"
        )
        assert moved.workspace.current_observation.display_name == "Song.als"
        assert moved.workspace.current_observation.state_fingerprint == "state:1"
        assert len(headquarters.workspaces.history(first.binding.workspace_id)) == 2
    finally:
        headquarters.close()


def test_runtime_change_reconciles_same_workspace_and_preserves_optional_metadata(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(version="12.1"),
            location_ref="file:///projects/song.als",
            display_name="Song.als",
            state_fingerprint="state:1",
        )
        updated = handshake.attach(
            runtime=_runtime(version="12.2"),
            known_workspace_id=first.binding.workspace_id,
            location_ref="file:///projects/song.als",
        )

        assert updated.status == "RECONCILED"
        assert updated.binding.workspace_id == first.binding.workspace_id
        assert updated.binding.runtime.version == "12.2"
        assert updated.workspace.current_observation.display_name == "Song.als"
        assert updated.workspace.current_observation.state_fingerprint == "state:1"
    finally:
        headquarters.close()


def test_location_owned_by_another_song_fails_instead_of_rebinding(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        song_a = headquarters.store.create_song("Song A")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/shared.als",
        )
        assert first.binding.song_id == song_a.id

        song_b = headquarters.store.create_song("Song B")
        assert headquarters.store.active_song().id == song_b.id
        history_before = headquarters.workspaces.history(first.binding.workspace_id)

        with pytest.raises(HostSessionHandshakeError, match="different Song"):
            handshake.attach(
                runtime=_runtime(),
                location_ref="file:///projects/shared.als",
            )

        assert headquarters.workspaces.history(first.binding.workspace_id) == history_before
        assert headquarters.workspaces.state(first.binding.workspace_id).workspace.song_id == (
            song_a.id
        )
    finally:
        headquarters.close()


def test_known_workspace_cannot_move_onto_another_workspace_location(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/one.als",
        )
        second = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/two.als",
        )

        with pytest.raises(HostSessionHandshakeError, match="another workspace"):
            handshake.attach(
                runtime=_runtime(),
                known_workspace_id=first.binding.workspace_id,
                location_ref="file:///projects/two.als",
            )

        assert headquarters.workspaces.state(first.binding.workspace_id).current_observation.location_ref == (
            "file:///projects/one.als"
        )
        assert headquarters.workspaces.state(second.binding.workspace_id).current_observation.location_ref == (
            "file:///projects/two.als"
        )
    finally:
        headquarters.close()


def test_host_family_change_fails_closed(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/song.als",
        )
        history_before = headquarters.workspaces.history(first.binding.workspace_id)

        with pytest.raises(HostSessionHandshakeError, match="host family"):
            handshake.attach(
                runtime=_runtime(family="FL_STUDIO"),
                known_workspace_id=first.binding.workspace_id,
                location_ref="file:///projects/song.flp",
            )

        assert headquarters.workspaces.history(first.binding.workspace_id) == history_before
    finally:
        headquarters.close()


def test_attachment_requires_active_song(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        handshake = HostSessionHandshake(headquarters.host_observation)
        with pytest.raises(HostSessionHandshakeError, match="active Song"):
            handshake.attach(
                runtime=_runtime(),
                location_ref="file:///projects/unbound.als",
            )
        assert headquarters.workspaces.current_candidates_at_location(
            "file:///projects/unbound.als"
        ) == ()
    finally:
        headquarters.close()


def test_reference_workflow_attaches_observes_and_discovers_from_canonical_state(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Workflow Artist")
    try:
        song = headquarters.store.create_song("Reference Song")
        reference_client = _ReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )

        result = asyncio.run(
            workflow.observe_and_discover(
                runtime=_runtime(),
                location_ref="file:///projects/reference-song.als",
                display_name="Reference Song.als",
                state_fingerprint="state:reference-1",
                provider_id="session-local",
                capabilities=(_capability(),),
                focus_dimensions=(_focus(),),
                focus_evidence_ref="host:ableton:focus-snapshot",
                shadow=_tempo_shadow(128.0),
                now_epoch_seconds=101,
                comparison_dimensions=("tempo",),
                semantic_tags=("electronic",),
                required_features=("tempo_bpm",),
                desired_tags=("electronic",),
                result_limit=2,
            )
        )

        assert result.handshake.status == "CREATED"
        assert result.observation.status == "COMPLETE"
        assert result.observation.binding.song_id == song.id
        assert result.references["read_only"] is True
        assert result.references["action_authority_granted"] is False
        assert result.references["primary"]["title"] == "Reference A"
        assert len(reference_client.calls) == 1
        provider_id, binding, shadow, kwargs = reference_client.calls[0]
        assert provider_id == "session-local"
        assert binding == result.observation.binding
        assert shadow == result.observation.shadow
        assert shadow.status == "CURRENT"
        assert {fact.field: fact.value for fact in shadow.facts} == {"bpm": 128.0}
        assert kwargs["comparison_dimensions"] == ("tempo",)
        assert kwargs["semantic_tags"] == ("electronic",)
        assert kwargs["required_features"] == ("tempo_bpm",)
        assert kwargs["desired_tags"] == ("electronic",)
        assert kwargs["result_limit"] == 2
    finally:
        headquarters.close()


def test_reference_workflow_requires_current_shadow_without_erasing_observation(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Workflow Artist")
    try:
        headquarters.store.create_song("Reference Song")
        reference_client = _ReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        location = "file:///projects/no-shadow.als"

        with pytest.raises(HostSessionReferenceWorkflowError, match="CURRENT Host Shadow"):
            asyncio.run(
                workflow.observe_and_discover(
                    runtime=_runtime(),
                    location_ref=location,
                    provider_id="session-local",
                    capabilities=(_capability(),),
                    focus_evidence_ref="host:ableton:focus-snapshot",
                    shadow=None,
                    now_epoch_seconds=101,
                    comparison_dimensions=("tempo",),
                )
            )

        assert reference_client.calls == []
        workspaces = headquarters.workspaces.current_candidates_at_location(location)
        assert len(workspaces) == 1
        workspace = workspaces[0]
        assert len(headquarters.capability_evidence.history(workspace.id)) == 1
        assert headquarters.shadow.state(workspace.id).status == "EMPTY"
    finally:
        headquarters.close()


def test_reference_provider_failure_preserves_truthful_host_shadow(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Workflow Artist")
    try:
        headquarters.store.create_song("Reference Song")
        reference_client = _FailingReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        location = "file:///projects/provider-failure.als"

        with pytest.raises(RuntimeError, match="reference provider unavailable"):
            asyncio.run(
                workflow.observe_and_discover(
                    runtime=_runtime(),
                    location_ref=location,
                    provider_id="session-local",
                    focus_evidence_ref="host:ableton:focus-snapshot",
                    shadow=_tempo_shadow(126.0),
                    now_epoch_seconds=101,
                    comparison_dimensions=("tempo",),
                )
            )

        assert len(reference_client.calls) == 1
        workspaces = headquarters.workspaces.current_candidates_at_location(location)
        assert len(workspaces) == 1
        shadow = headquarters.shadow.state(workspaces[0].id)
        assert shadow.status == "CURRENT"
        assert {fact.field: fact.value for fact in shadow.facts} == {"bpm": 126.0}
        assert len(headquarters.shadow.history(workspaces[0].id)) == 1
    finally:
        headquarters.close()
