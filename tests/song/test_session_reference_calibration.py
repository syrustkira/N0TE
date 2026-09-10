from __future__ import annotations

import pytest

from n0te.audio_engineering import (
    EngineeringEvidenceBinding,
    EngineeringSnapshot,
    LOUDNESS_MEASURED,
    LOUDNESS_BACKEND,
    LOUDNESS_STANDARD,
)
from n0te.host_observation import HostObservationBinding
from n0te.hosts import HostRuntimeIdentity
from n0te.reference_calibration import (
    ReferenceCalibrationProfile,
    ReferenceCandidate,
)
from n0te.session_reference_calibration import (
    SessionReferenceCalibrationError,
    build_session_reference_evidence_bundle,
    derive_session_calibration,
    discover_session_references,
)
from n0te.shadow import HostShadowState, ShadowFact


def _runtime() -> HostRuntimeIdentity:
    return HostRuntimeIdentity.from_runtime_labels(
        host_family="ABLETON_LIVE",
        version="12.1",
        edition="Standard",
        os_name="Darwin",
        machine="arm64",
    )


def _binding(*, song_id: str = "song:1") -> HostObservationBinding:
    runtime = _runtime()
    return HostObservationBinding(
        workspace_id="workspace:1",
        song_id=song_id,
        workspace_observation_id="observation:1",
        host_runtime_fingerprint=runtime.fingerprint,
        runtime=runtime,
    )


def _shadow(*facts: ShadowFact, observation_id: str = "observation:1") -> HostShadowState:
    return HostShadowState(
        status="CURRENT",
        workspace_id="workspace:1",
        current_workspace_observation_id=observation_id,
        baseline_batch_id="batch:1",
        latest_batch_id="batch:1",
        facts=tuple(facts),
    )


def _tempo(value: float, *, evidence_ref: str = "host:ableton:tempo") -> ShadowFact:
    return ShadowFact(
        object_kind="TEMPO",
        object_ref="tempo:main",
        field="bpm",
        value=value,
        batch_id="batch:1",
        actor="EXTERNAL",
        evidence_ref=evidence_ref,
    )


def _engineering(*, song_id: str = "song:1", crest: float = 12.0, correlation: float = 0.2) -> EngineeringSnapshot:
    evidence = EngineeringEvidenceBinding(
        song_id=song_id,
        version_id="version:1",
        asset_id="asset:mix",
        sha256="a" * 64,
        source_size_bytes=4096,
    )
    return EngineeringSnapshot(
        binding=evidence,
        analyzer_version="test-analyzer",
        sample_rate_hz=48000,
        channels=2,
        bits_per_sample=24,
        frame_count=48000,
        duration_seconds=1.0,
        sample_peak_dbfs=-1.0,
        rms_dbfs=-13.0,
        crest_factor_db=crest,
        dc_offset_percent=0.0,
        stereo_correlation=correlation,
        integrated_lufs=-14.0,
        loudness_state=LOUDNESS_MEASURED,
        loudness_standard=LOUDNESS_STANDARD,
        loudness_backend=LOUDNESS_BACKEND,
    )


def test_current_host_and_engineering_evidence_derive_only_defensible_features():
    derivation = derive_session_calibration(
        _binding(),
        _shadow(_tempo(128.0)),
        engineering_snapshot=_engineering(crest=12.0, correlation=0.2),
        semantic_tags=("electronic", "dark"),
    )

    features = derivation.profile.feature_map()
    assert features == {
        "DYNAMICS": 0.5,
        "STEREO_WIDTH": pytest.approx(0.4),
        "TEMPO_BPM": 128.0,
    }
    assert derivation.profile.semantic_tags == ("ELECTRONIC", "DARK")
    assert "ENERGY" not in features
    assert "LOW_END" not in features
    assert "BRIGHTNESS" not in features
    assert "DENSITY" not in features

    evidence = derivation.evidence_map()
    assert evidence["TEMPO_BPM"].source_refs == ("host:ableton:tempo",)
    engineering_ref = evidence["DYNAMICS"].source_refs[0]
    assert engineering_ref.startswith("engineering:asset:mix:version:1:sha256:")
    assert evidence["STEREO_WIDTH"].source_refs == (engineering_ref,)


def test_host_tempo_alone_is_enough_to_calibrate_without_fake_audio_features():
    derivation = derive_session_calibration(_binding(), _shadow(_tempo(124.5)))
    assert derivation.profile.feature_map() == {"TEMPO_BPM": 124.5}
    assert tuple(item.feature for item in derivation.evidence) == ("TEMPO_BPM",)


def test_stale_or_cross_song_evidence_fails_closed():
    with pytest.raises(SessionReferenceCalibrationError, match="stale"):
        derive_session_calibration(
            _binding(),
            _shadow(_tempo(128.0), observation_id="observation:old"),
        )

    with pytest.raises(SessionReferenceCalibrationError, match="different Song"):
        derive_session_calibration(
            _binding(song_id="song:1"),
            _shadow(_tempo(128.0)),
            engineering_snapshot=_engineering(song_id="song:2"),
        )


def test_conflicting_current_tempo_evidence_is_not_averaged_or_guessed():
    with pytest.raises(SessionReferenceCalibrationError, match="conflicting tempo"):
        derive_session_calibration(
            _binding(),
            _shadow(
                _tempo(128.0, evidence_ref="host:tempo:a"),
                ShadowFact(
                    object_kind="TEMPO",
                    object_ref="tempo:alternate",
                    field="tempo_bpm",
                    value=130.0,
                    batch_id="batch:1",
                    actor="EXTERNAL",
                    evidence_ref="host:tempo:b",
                ),
            ),
        )


def test_no_defensible_features_fails_instead_of_filling_profile_with_proxies():
    shadow = _shadow(
        ShadowFact(
            object_kind="TRACK",
            object_ref="track:1",
            field="name",
            value="Lead Vocal",
            batch_id="batch:1",
            actor="EXTERNAL",
            evidence_ref="host:track:name",
        )
    )
    with pytest.raises(SessionReferenceCalibrationError, match="no defensible"):
        derive_session_calibration(_binding(), shadow)


def test_host_observation_serializes_one_canonical_reference_evidence_bundle():
    binding = _binding()
    engineering = _engineering(crest=12.0, correlation=0.2)
    bundle = build_session_reference_evidence_bundle(
        binding,
        _shadow(_tempo(128.0)),
        engineering_snapshot=engineering,
    )

    assert set(bundle) == {"binding", "shadow", "engineering_snapshot"}
    assert "calibration" not in bundle
    assert bundle["binding"] == {
        "workspace_id": "workspace:1",
        "song_id": "song:1",
        "workspace_observation_id": "observation:1",
        "host_runtime_fingerprint": binding.runtime.fingerprint,
        "runtime": {
            "host_family": "ABLETON_LIVE",
            "version": "12.1",
            "edition": "Standard",
            "os_name": "Darwin",
            "machine": "arm64",
            "translation_mode": "NATIVE",
            "display_name": None,
            "generic_host_label": None,
            "fingerprint": binding.runtime.fingerprint,
        },
    }
    assert bundle["shadow"] == {
        "status": "CURRENT",
        "workspace_id": "workspace:1",
        "current_workspace_observation_id": "observation:1",
        "baseline_batch_id": "batch:1",
        "latest_batch_id": "batch:1",
        "facts": [
            {
                "object_kind": "TEMPO",
                "object_ref": "tempo:main",
                "field": "bpm",
                "value": 128.0,
                "batch_id": "batch:1",
                "actor": "EXTERNAL",
                "evidence_ref": "host:ableton:tempo",
            }
        ],
    }
    assert bundle["engineering_snapshot"]["binding"] == {
        "song_id": "song:1",
        "version_id": "version:1",
        "asset_id": "asset:mix",
        "sha256": "a" * 64,
        "source_size_bytes": 4096,
    }
    assert bundle["engineering_snapshot"]["crest_factor_db"] == 12.0
    assert bundle["engineering_snapshot"]["stereo_correlation"] == 0.2


def test_evidence_bundle_refuses_stale_and_cross_song_state_before_transport():
    with pytest.raises(SessionReferenceCalibrationError, match="stale"):
        build_session_reference_evidence_bundle(
            _binding(),
            _shadow(_tempo(128.0), observation_id="observation:old"),
        )

    with pytest.raises(SessionReferenceCalibrationError, match="different Song"):
        build_session_reference_evidence_bundle(
            _binding(song_id="song:1"),
            _shadow(_tempo(128.0)),
            engineering_snapshot=_engineering(song_id="song:2"),
        )


def test_evidence_bundle_refuses_non_json_shadow_values():
    shadow = _shadow(
        _tempo(128.0),
        ShadowFact(
            object_kind="TRACK",
            object_ref="track:1",
            field="opaque_state",
            value=object(),
            batch_id="batch:1",
            actor="EXTERNAL",
            evidence_ref="host:track:opaque",
        ),
    )
    with pytest.raises(SessionReferenceCalibrationError, match="JSON-transportable"):
        build_session_reference_evidence_bundle(_binding(), shadow)


class _Provider:
    def __init__(self):
        self.calls = []

    def discover_references(self, *, target, comparison_dimensions, limit):
        self.calls.append((target, comparison_dimensions, limit))
        return (
            ReferenceCandidate(
                title="Near Tempo",
                source_type="CATALOG_RECORDING",
                source_locator="catalog:near-tempo",
                profile=ReferenceCalibrationProfile.create(
                    tempo_bpm=128.5,
                    dynamics=0.48,
                    stereo_width=0.42,
                    semantic_tags=("electronic",),
                ),
                comparison_dimensions=("tempo", "dynamics", "stereo-width"),
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider:near-tempo",
            ),
            ReferenceCandidate(
                title="Far Tempo",
                source_type="CATALOG_RECORDING",
                source_locator="catalog:far-tempo",
                profile=ReferenceCalibrationProfile.create(
                    tempo_bpm=92.0,
                    dynamics=0.8,
                    stereo_width=0.8,
                    semantic_tags=("electronic",),
                ),
                comparison_dimensions=("tempo", "dynamics", "stereo-width"),
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider:far-tempo",
            ),
        )


def test_session_evidence_flows_directly_into_provider_discovery_and_ranking():
    provider = _Provider()
    result = discover_session_references(
        provider,
        _binding(),
        _shadow(_tempo(128.0)),
        engineering_snapshot=_engineering(crest=12.0, correlation=0.2),
        semantic_tags=("electronic",),
        comparison_dimensions=("tempo", "dynamics", "stereo-width"),
        required_features=("tempo_bpm", "dynamics", "stereo_width"),
        desired_tags=("electronic",),
        result_limit=2,
    )

    assert provider.calls[0][0] == result.derivation.profile
    assert provider.calls[0][1] == ("TEMPO", "DYNAMICS", "STEREO_WIDTH")
    assert [item.candidate.title for item in result.ranked] == [
        "Near Tempo",
        "Far Tempo",
    ]
    assert result.primary is result.ranked[0]
    assert result.primary.candidate.title == "Near Tempo"
