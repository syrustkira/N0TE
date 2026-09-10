from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

from .audio_engineering import EngineeringSnapshot
from .host_observation import HostObservationBinding
from .reference_calibration import (
    RankedReferenceCandidate,
    ReferenceCalibrationError,
    ReferenceCalibrationProfile,
    ReferenceDiscoveryProvider,
    discover_ranked_references,
)
from .shadow import HostShadowState

_TEMPO_FIELDS = frozenset({"BPM", "TEMPO", "TEMPO_BPM"})


class SessionReferenceCalibrationError(ReferenceCalibrationError):
    """Current DAW/audio evidence cannot safely produce a reference calibration."""


@dataclass(frozen=True)
class CalibrationFeatureEvidence:
    feature: str
    value: float
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class SessionCalibrationDerivation:
    profile: ReferenceCalibrationProfile
    evidence: tuple[CalibrationFeatureEvidence, ...]

    def evidence_map(self) -> dict[str, CalibrationFeatureEvidence]:
        return {item.feature: item for item in self.evidence}


@dataclass(frozen=True)
class SessionReferenceSearchResult:
    derivation: SessionCalibrationDerivation
    ranked: tuple[RankedReferenceCandidate, ...]

    @property
    def primary(self) -> RankedReferenceCandidate | None:
        return self.ranked[0] if self.ranked else None


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise SessionReferenceCalibrationError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SessionReferenceCalibrationError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise SessionReferenceCalibrationError(f"{field} must be finite")
    return number


def _normalized_field(value: str) -> str:
    return str(value).strip().upper().replace("-", "_").replace(" ", "_")


def _validate_shadow_binding(
    binding: HostObservationBinding,
    shadow: HostShadowState,
) -> None:
    if not isinstance(binding, HostObservationBinding):
        raise TypeError("binding must be HostObservationBinding")
    if not isinstance(shadow, HostShadowState):
        raise TypeError("shadow must be HostShadowState")
    if shadow.status != "CURRENT":
        raise SessionReferenceCalibrationError(
            "reference calibration requires a CURRENT Host Shadow"
        )
    if shadow.workspace_id != binding.workspace_id:
        raise SessionReferenceCalibrationError(
            "Host Shadow belongs to a different workspace"
        )
    if shadow.current_workspace_observation_id != binding.workspace_observation_id:
        raise SessionReferenceCalibrationError(
            "Host Shadow is stale relative to the bound workspace observation"
        )


def _tempo_evidence(shadow: HostShadowState) -> CalibrationFeatureEvidence | None:
    matches: list[tuple[float, str]] = []
    for fact in shadow.facts:
        if fact.object_kind != "TEMPO":
            continue
        if _normalized_field(fact.field) not in _TEMPO_FIELDS:
            continue
        bpm = _finite(fact.value, "tempo BPM")
        if not 20.0 <= bpm <= 400.0:
            raise SessionReferenceCalibrationError(
                "observed tempo BPM must be between 20 and 400"
            )
        source_ref = str(fact.evidence_ref).strip()
        if not source_ref:
            raise SessionReferenceCalibrationError(
                "observed tempo requires provenance"
            )
        matches.append((bpm, source_ref))

    if not matches:
        return None
    unique_values = {round(value, 9) for value, _ in matches}
    if len(unique_values) != 1:
        raise SessionReferenceCalibrationError(
            "current Host Shadow contains conflicting tempo evidence"
        )
    value = matches[0][0]
    return CalibrationFeatureEvidence(
        feature="TEMPO_BPM",
        value=value,
        source_refs=tuple(sorted({source for _, source in matches})),
    )


def _engineering_source(snapshot: EngineeringSnapshot) -> str:
    binding = snapshot.binding
    return (
        f"engineering:{binding.asset_id}:{binding.version_id}:"
        f"sha256:{binding.sha256}"
    )


def _engineering_evidence(
    binding: HostObservationBinding,
    snapshot: EngineeringSnapshot | None,
) -> tuple[CalibrationFeatureEvidence, ...]:
    if snapshot is None:
        return ()
    if not isinstance(snapshot, EngineeringSnapshot):
        raise TypeError("engineering_snapshot must be EngineeringSnapshot or None")
    if snapshot.binding.song_id != binding.song_id:
        raise SessionReferenceCalibrationError(
            "engineering evidence belongs to a different Song"
        )

    source_ref = _engineering_source(snapshot)
    out: list[CalibrationFeatureEvidence] = []

    # Crest factor is signal evidence, not an artistic quality score. Here it is
    # mapped monotonically into the calibration feature's documented 0..1 range.
    if snapshot.crest_factor_db is not None:
        crest = _finite(snapshot.crest_factor_db, "crest_factor_db")
        dynamics = min(max(crest, 0.0), 24.0) / 24.0
        out.append(
            CalibrationFeatureEvidence(
                feature="DYNAMICS",
                value=dynamics,
                source_refs=(source_ref,),
            )
        )

    # Correlation +1 is mono-identical, 0 is decorrelated, -1 is antiphase.
    # The mapping is useful only as a calibration coordinate, never a quality score.
    if snapshot.stereo_correlation is not None:
        correlation = _finite(snapshot.stereo_correlation, "stereo_correlation")
        if not -1.0 <= correlation <= 1.0:
            raise SessionReferenceCalibrationError(
                "stereo_correlation must be between -1 and 1"
            )
        stereo_width = (1.0 - correlation) / 2.0
        out.append(
            CalibrationFeatureEvidence(
                feature="STEREO_WIDTH",
                value=stereo_width,
                source_refs=(source_ref,),
            )
        )

    return tuple(out)


def derive_session_calibration(
    binding: HostObservationBinding,
    shadow: HostShadowState,
    *,
    engineering_snapshot: EngineeringSnapshot | None = None,
    semantic_tags: Iterable[str] = (),
) -> SessionCalibrationDerivation:
    """Derive a provider-neutral reference profile from current observed evidence.

    No feature is inferred from track count, plugin identity, genre labels, loudness,
    or other proxies. Tempo comes from the current Host Shadow. Dynamics and stereo
    width are admitted only when the exact Song has measured engineering evidence.
    Unsupported dimensions remain absent rather than being guessed.
    """

    _validate_shadow_binding(binding, shadow)
    evidence: list[CalibrationFeatureEvidence] = []
    tempo = _tempo_evidence(shadow)
    if tempo is not None:
        evidence.append(tempo)
    evidence.extend(_engineering_evidence(binding, engineering_snapshot))

    if not evidence:
        raise SessionReferenceCalibrationError(
            "current session has no defensible reference-calibration features"
        )
    if len({item.feature for item in evidence}) != len(evidence):
        raise SessionReferenceCalibrationError(
            "session calibration produced duplicate feature evidence"
        )

    values = {item.feature: item.value for item in evidence}
    profile = ReferenceCalibrationProfile(
        features=tuple(values.items()),
        semantic_tags=tuple(semantic_tags),
    )
    ordered_evidence = tuple(
        sorted(evidence, key=lambda item: item.feature)
    )
    return SessionCalibrationDerivation(profile=profile, evidence=ordered_evidence)


def discover_session_references(
    provider: ReferenceDiscoveryProvider,
    binding: HostObservationBinding,
    shadow: HostShadowState,
    *,
    comparison_dimensions: Iterable[str],
    engineering_snapshot: EngineeringSnapshot | None = None,
    semantic_tags: Iterable[str] = (),
    required_features: Iterable[str] = (),
    desired_tags: Iterable[str] = (),
    feature_weights: Mapping[str, float] | None = None,
    discovery_limit: int = 12,
    result_limit: int = 3,
) -> SessionReferenceSearchResult:
    """Current DAW/audio evidence -> external discovery -> deterministic ranking."""

    derivation = derive_session_calibration(
        binding,
        shadow,
        engineering_snapshot=engineering_snapshot,
        semantic_tags=semantic_tags,
    )
    ranked = discover_ranked_references(
        provider,
        derivation.profile,
        comparison_dimensions=comparison_dimensions,
        required_features=required_features,
        desired_tags=desired_tags,
        feature_weights=feature_weights,
        discovery_limit=discovery_limit,
        result_limit=result_limit,
    )
    return SessionReferenceSearchResult(derivation=derivation, ranked=ranked)
