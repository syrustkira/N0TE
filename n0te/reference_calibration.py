from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol

from .song_references import (
    SOURCE_EVIDENCE_KINDS,
    SOURCE_TYPES,
    SongReference,
    SongReferenceStore,
)

CALIBRATION_FEATURES = {
    "TEMPO_BPM",
    "ENERGY",
    "LOW_END",
    "BRIGHTNESS",
    "DENSITY",
    "DYNAMICS",
    "STEREO_WIDTH",
}
_FEATURE_DISTANCE_SCALE = {
    "TEMPO_BPM": 40.0,
    "ENERGY": 1.0,
    "LOW_END": 1.0,
    "BRIGHTNESS": 1.0,
    "DENSITY": 1.0,
    "DYNAMICS": 1.0,
    "STEREO_WIDTH": 1.0,
}
_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ReferenceCalibrationError(ValueError):
    """Reference discovery or calibration input is not safe to compare."""


def _token(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReferenceCalibrationError(f"{field} must be non-empty text")
    token = re.sub(r"[\s-]+", "_", value.strip().upper())
    if _TOKEN.fullmatch(token) is None:
        raise ReferenceCalibrationError(f"invalid {field}: {value}")
    return token


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ReferenceCalibrationError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReferenceCalibrationError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ReferenceCalibrationError(f"{field} must be finite")
    return number


def _confidence(value: object) -> float:
    number = _finite(value, "confidence")
    if not 0.0 <= number <= 1.0:
        raise ReferenceCalibrationError("confidence must be between 0 and 1")
    return number


def _feature_value(name: str, value: object) -> float:
    number = _finite(value, name.lower())
    if name == "TEMPO_BPM":
        if not 20.0 <= number <= 400.0:
            raise ReferenceCalibrationError("tempo_bpm must be between 20 and 400")
    elif not 0.0 <= number <= 1.0:
        raise ReferenceCalibrationError(
            f"{name.lower()} must be normalized between 0 and 1"
        )
    return number


def _canonical_features(
    values: Mapping[str, object] | Iterable[tuple[str, object]],
) -> tuple[tuple[str, float], ...]:
    items = values.items() if isinstance(values, Mapping) else values
    out: dict[str, float] = {}
    for raw_name, raw_value in items:
        name = _token(raw_name, "feature")
        if name not in CALIBRATION_FEATURES:
            raise ReferenceCalibrationError(f"unsupported calibration feature: {name}")
        if name in out:
            raise ReferenceCalibrationError(f"duplicate calibration feature: {name}")
        out[name] = _feature_value(name, raw_value)
    if not out:
        raise ReferenceCalibrationError("calibration profile requires at least one feature")
    return tuple(sorted(out.items()))


def _canonical_tokens(values: Iterable[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ReferenceCalibrationError(f"{field} must be a sequence")
    try:
        raw_values = tuple(values)
    except TypeError as exc:
        raise ReferenceCalibrationError(f"{field} must be a sequence") from exc
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        token = _token(raw, field)
        if token not in seen:
            out.append(token)
            seen.add(token)
    return tuple(out)


@dataclass(frozen=True)
class ReferenceCalibrationProfile:
    """Host-neutral measurable description used only for reference calibration.

    Values other than tempo are normalized 0..1. Semantic tags are optional
    descriptive evidence and never become artistic truth by themselves.
    """

    features: tuple[tuple[str, float], ...]
    semantic_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", _canonical_features(self.features))
        object.__setattr__(
            self,
            "semantic_tags",
            _canonical_tokens(self.semantic_tags, "semantic tag"),
        )

    @classmethod
    def create(
        cls,
        *,
        tempo_bpm: float | None = None,
        energy: float | None = None,
        low_end: float | None = None,
        brightness: float | None = None,
        density: float | None = None,
        dynamics: float | None = None,
        stereo_width: float | None = None,
        semantic_tags: Iterable[str] = (),
    ) -> "ReferenceCalibrationProfile":
        raw = {
            "TEMPO_BPM": tempo_bpm,
            "ENERGY": energy,
            "LOW_END": low_end,
            "BRIGHTNESS": brightness,
            "DENSITY": density,
            "DYNAMICS": dynamics,
            "STEREO_WIDTH": stereo_width,
        }
        return cls(
            features=tuple(
                (name, value) for name, value in raw.items() if value is not None
            ),
            semantic_tags=tuple(semantic_tags),
        )

    def feature_map(self) -> dict[str, float]:
        return dict(self.features)


@dataclass(frozen=True)
class ReferenceCandidate:
    title: str
    source_type: str
    source_locator: str
    profile: ReferenceCalibrationProfile
    comparison_dimensions: tuple[str, ...]
    source_kind: str = "PROVIDER_VERIFIED"
    source_ref: str | None = None
    confidence: float = 1.0

    def __post_init__(self) -> None:
        title = str(self.title).strip()
        locator = str(self.source_locator).strip()
        source_type = str(self.source_type).strip().upper()
        source_kind = str(self.source_kind).strip().upper()
        if not title:
            raise ReferenceCalibrationError("candidate title must not be empty")
        if not locator:
            raise ReferenceCalibrationError("candidate source_locator must not be empty")
        if source_type not in SOURCE_TYPES:
            raise ReferenceCalibrationError(f"unsupported source_type: {source_type}")
        if source_kind not in SOURCE_EVIDENCE_KINDS:
            raise ReferenceCalibrationError(f"unsupported source_kind: {source_kind}")
        source_ref = None if self.source_ref is None else str(self.source_ref).strip()
        if source_kind in {"OBSERVED", "PROVIDER_VERIFIED"} and not source_ref:
            raise ReferenceCalibrationError(
                f"{source_kind} candidate requires source_ref provenance"
            )
        if not isinstance(self.profile, ReferenceCalibrationProfile):
            raise ReferenceCalibrationError(
                "candidate profile must be ReferenceCalibrationProfile"
            )
        dimensions = _canonical_tokens(
            self.comparison_dimensions, "comparison dimension"
        )
        if not dimensions:
            raise ReferenceCalibrationError(
                "candidate comparison_dimensions must not be empty"
            )
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "source_locator", locator)
        object.__setattr__(self, "comparison_dimensions", dimensions)
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "confidence", _confidence(self.confidence))


@dataclass(frozen=True)
class RankedReferenceCandidate:
    candidate: ReferenceCandidate
    similarity: float
    matched_features: tuple[str, ...]
    feature_distances: tuple[tuple[str, float], ...]
    matched_tags: tuple[str, ...]


class ReferenceDiscoveryProvider(Protocol):
    """Provider boundary for web/catalog discovery.

    A provider may use a catalog, search engine, local library, model-backed search,
    or another legitimate route. Calibration remains deterministic after discovery.
    """

    def discover_references(
        self,
        *,
        target: ReferenceCalibrationProfile,
        comparison_dimensions: tuple[str, ...],
        limit: int,
    ) -> Iterable[ReferenceCandidate]: ...


def _distance(name: str, target: float, candidate: float) -> float:
    return min(abs(target - candidate) / _FEATURE_DISTANCE_SCALE[name], 1.0)


def rank_reference_candidates(
    target: ReferenceCalibrationProfile,
    candidates: Iterable[ReferenceCandidate],
    *,
    required_features: Iterable[str] = (),
    desired_tags: Iterable[str] = (),
    feature_weights: Mapping[str, float] | None = None,
    limit: int = 3,
) -> tuple[RankedReferenceCandidate, ...]:
    """Rank already-discovered references without making taste claims.

    Required features are strict calibration gates. If supplied, a candidate must
    expose every required feature. Without required features, overlap with the
    target profile is scored and missing target evidence reduces coverage.
    """

    if not isinstance(target, ReferenceCalibrationProfile):
        raise ReferenceCalibrationError("target must be ReferenceCalibrationProfile")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 25:
        raise ReferenceCalibrationError("limit must be an integer between 1 and 25")

    target_features = target.feature_map()
    required = _canonical_tokens(required_features, "required feature")
    unknown = set(required) - CALIBRATION_FEATURES
    if unknown:
        raise ReferenceCalibrationError(
            f"unsupported required calibration features: {sorted(unknown)}"
        )
    if set(required) - set(target_features):
        raise ReferenceCalibrationError(
            "required features must exist in the target calibration profile"
        )
    desired = set(_canonical_tokens(desired_tags, "desired tag"))

    weights: dict[str, float] = {}
    if feature_weights is not None:
        for raw_name, raw_weight in feature_weights.items():
            name = _token(raw_name, "feature weight")
            if name not in CALIBRATION_FEATURES:
                raise ReferenceCalibrationError(f"unsupported feature weight: {name}")
            weight = _finite(raw_weight, f"weight for {name}")
            if weight <= 0:
                raise ReferenceCalibrationError("feature weights must be positive")
            weights[name] = weight

    ranked: list[RankedReferenceCandidate] = []
    for candidate in candidates:
        if not isinstance(candidate, ReferenceCandidate):
            raise ReferenceCalibrationError(
                "discovery candidates must be ReferenceCandidate instances"
            )
        candidate_features = candidate.profile.feature_map()
        if required and not set(required) <= set(candidate_features):
            continue
        comparable = (
            tuple(name for name in required if name in candidate_features)
            if required
            else tuple(name for name in target_features if name in candidate_features)
        )
        if not comparable:
            continue

        distances = tuple(
            (name, _distance(name, target_features[name], candidate_features[name]))
            for name in comparable
        )
        total_weight = sum(weights.get(name, 1.0) for name in comparable)
        mean_distance = sum(
            distance * weights.get(name, 1.0) for name, distance in distances
        ) / total_weight
        denominator = len(required) if required else len(target_features)
        coverage = len(comparable) / denominator
        numeric_similarity = max(0.0, 1.0 - mean_distance) * coverage

        candidate_tags = set(candidate.profile.semantic_tags)
        matched_tags = tuple(sorted(desired & candidate_tags))
        if desired:
            tag_similarity = len(matched_tags) / len(desired)
            similarity = 0.8 * numeric_similarity + 0.2 * tag_similarity
        else:
            similarity = numeric_similarity

        ranked.append(
            RankedReferenceCandidate(
                candidate=candidate,
                similarity=round(similarity, 6),
                matched_features=comparable,
                feature_distances=tuple(
                    (name, round(distance, 6)) for name, distance in distances
                ),
                matched_tags=matched_tags,
            )
        )

    ranked.sort(
        key=lambda item: (
            -item.similarity,
            -item.candidate.confidence,
            item.candidate.title.casefold(),
            item.candidate.source_locator,
        )
    )
    return tuple(ranked[:limit])


def discover_ranked_references(
    provider: ReferenceDiscoveryProvider,
    target: ReferenceCalibrationProfile,
    *,
    comparison_dimensions: Iterable[str],
    required_features: Iterable[str] = (),
    desired_tags: Iterable[str] = (),
    feature_weights: Mapping[str, float] | None = None,
    discovery_limit: int = 12,
    result_limit: int = 3,
) -> tuple[RankedReferenceCandidate, ...]:
    """Discover externally, then calibrate locally so one provider never owns taste."""

    dimensions = _canonical_tokens(comparison_dimensions, "comparison dimension")
    if not dimensions:
        raise ReferenceCalibrationError("comparison_dimensions must not be empty")
    if (
        not isinstance(discovery_limit, int)
        or isinstance(discovery_limit, bool)
        or not 1 <= discovery_limit <= 100
    ):
        raise ReferenceCalibrationError(
            "discovery_limit must be an integer between 1 and 100"
        )
    discovered = provider.discover_references(
        target=target,
        comparison_dimensions=dimensions,
        limit=discovery_limit,
    )
    return rank_reference_candidates(
        target,
        discovered,
        required_features=required_features,
        desired_tags=desired_tags,
        feature_weights=feature_weights,
        limit=result_limit,
    )


def select_primary_reference(
    ranked: Iterable[RankedReferenceCandidate],
) -> RankedReferenceCandidate:
    ranked = tuple(ranked)
    if not ranked:
        raise ReferenceCalibrationError("no reference candidate survived calibration")
    return ranked[0]


def persist_ranked_reference(
    store: SongReferenceStore,
    *,
    song_id: str,
    ranked: RankedReferenceCandidate,
    comparison_dimensions: Iterable[str] | None = None,
    loudness_match_policy: str = "MATCH_BEFORE_COMPARISON",
    version_id: str | None = None,
    section_locator: str | None = None,
) -> SongReference:
    """Promote an explicit calibrated choice into canonical Song reference history."""

    if not isinstance(store, SongReferenceStore):
        raise TypeError("store must be SongReferenceStore")
    if not isinstance(ranked, RankedReferenceCandidate):
        raise TypeError("ranked must be RankedReferenceCandidate")
    candidate = ranked.candidate
    dimensions = (
        candidate.comparison_dimensions
        if comparison_dimensions is None
        else _canonical_tokens(comparison_dimensions, "comparison dimension")
    )
    if not dimensions:
        raise ReferenceCalibrationError("comparison_dimensions must not be empty")
    return store.create_reference(
        song_id=song_id,
        title=candidate.title,
        source_type=candidate.source_type,
        source_locator=candidate.source_locator,
        comparison_dimensions=dimensions,
        loudness_match_policy=loudness_match_policy,
        version_id=version_id,
        section_locator=section_locator,
        source_kind=candidate.source_kind,
        source_ref=candidate.source_ref,
        confidence=candidate.confidence,
    )
