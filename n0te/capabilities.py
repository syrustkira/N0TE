from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

ROUTE_KINDS = {
    "HOST_NATIVE",
    "N0TE_NATIVE",
    "OWNED_TOOL",
    "PROVIDER",
    "GUIDED",
}
RESOLUTION_STATUSES = {"RESOLVED", "UNAVAILABLE"}

# Central Template-eligible vocabulary over capability identities already used by
# N0TE's resolver, Studio, Template, Recipe, tool and host-observation surfaces.
# This does not make every observed/provider capability a Template capability.
# Adding a new reusable Template role identity should extend this owner first.
SUPPORTED_TEMPLATE_CAPABILITY_KEYS = frozenset(
    {
        "arrangement.structure",
        "audio.compare",
        "audio.compress",
        "audio.master",
        "audio.process",
        "audio.repair",
        "audio.transient-edit",
        "content.generate",
        "content.publish.prepare",
        "daw.feature.comping",
        "dynamics.compress",
        "instrument.play",
        "pitch.correct",
        "session.musician",
        "track.read",
        "transport.read",
        "vocal.harmony.build",
        "vocal.tighten",
        "vocal.timing.inspect",
    }
)

_SCORE_WEIGHTS = {
    "task_fit": 0.30,
    "editability": 0.10,
    "locality": 0.10,
    "privacy": 0.10,
    "latency": 0.10,
    "reversibility": 0.10,
    "cost_efficiency": 0.10,
    "portability": 0.05,
    "user_preference": 0.05,
}


class CapabilityResolutionError(ValueError):
    """Invalid resolver input, never a provider/host execution failure."""


def canonical_template_capability_key(value: str) -> str:
    """Return one supported canonical Template capability identity.

    Capability matching elsewhere remains exact and provider-neutral. This helper
    exists specifically so immutable reusable Template meaning cannot persist a
    typo, natural-language label, casing variant or otherwise unsupported key.
    """
    if not isinstance(value, str):
        raise CapabilityResolutionError("Template capability key must be text")
    key = value.strip().casefold()
    if not key:
        raise CapabilityResolutionError("Template capability key must not be empty")
    if key not in SUPPORTED_TEMPLATE_CAPABILITY_KEYS:
        raise CapabilityResolutionError(
            f"unsupported Template capability key: {key}"
        )
    return key


def _required_text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise CapabilityResolutionError(f"{field} must not be empty")
    return text


def _unit_interval(value: float, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CapabilityResolutionError(f"{field} must be between 0 and 1") from exc
    if not 0.0 <= number <= 1.0:
        raise CapabilityResolutionError(f"{field} must be between 0 and 1")
    return number


@dataclass(frozen=True)
class N0TEableJob:
    id: str
    capability: str
    description: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_text(self.id, "job.id"))
        object.__setattr__(
            self, "capability", _required_text(self.capability, "job.capability")
        )
        object.__setattr__(
            self, "description", _required_text(self.description, "job.description")
        )


@dataclass(frozen=True)
class ResolutionConstraints:
    min_locality: float = 0.0
    min_privacy: float = 0.0
    require_reversible: bool = False
    allow_paid: bool = True
    max_evidence_age_seconds: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "min_locality",
            _unit_interval(self.min_locality, "constraints.min_locality"),
        )
        object.__setattr__(
            self,
            "min_privacy",
            _unit_interval(self.min_privacy, "constraints.min_privacy"),
        )
        if self.max_evidence_age_seconds is not None:
            try:
                age = int(self.max_evidence_age_seconds)
            except (TypeError, ValueError) as exc:
                raise CapabilityResolutionError(
                    "constraints.max_evidence_age_seconds must be >= 0"
                ) from exc
            if age < 0:
                raise CapabilityResolutionError(
                    "constraints.max_evidence_age_seconds must be >= 0"
                )
            object.__setattr__(self, "max_evidence_age_seconds", age)


@dataclass(frozen=True)
class CapabilityCandidate:
    candidate_id: str
    route_kind: str
    capability: str
    display_name: str
    brand: str | None
    verified: bool
    compatible: bool
    evidence_ref: str | None
    evidence_age_seconds: int | None
    task_fit: float
    editability: float
    locality: float
    privacy: float
    latency: float
    reversibility: float
    cost_efficiency: float
    portability: float
    user_preference: float = 0.5
    paid: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            _required_text(self.candidate_id, "candidate.candidate_id"),
        )
        route = str(self.route_kind).strip().upper()
        if route not in ROUTE_KINDS:
            raise CapabilityResolutionError(f"unsupported route kind: {route}")
        object.__setattr__(self, "route_kind", route)
        object.__setattr__(
            self,
            "capability",
            _required_text(self.capability, "candidate.capability"),
        )
        object.__setattr__(
            self,
            "display_name",
            _required_text(self.display_name, "candidate.display_name"),
        )
        if self.brand is not None:
            object.__setattr__(
                self, "brand", _required_text(self.brand, "candidate.brand")
            )
        if self.evidence_ref is not None:
            object.__setattr__(
                self,
                "evidence_ref",
                _required_text(self.evidence_ref, "candidate.evidence_ref"),
            )
        if self.verified and not self.evidence_ref:
            raise CapabilityResolutionError(
                "verified candidate requires an explicit evidence_ref"
            )
        if self.evidence_age_seconds is not None:
            try:
                age = int(self.evidence_age_seconds)
            except (TypeError, ValueError) as exc:
                raise CapabilityResolutionError(
                    "candidate.evidence_age_seconds must be >= 0"
                ) from exc
            if age < 0:
                raise CapabilityResolutionError(
                    "candidate.evidence_age_seconds must be >= 0"
                )
            object.__setattr__(self, "evidence_age_seconds", age)
        for field in _SCORE_WEIGHTS:
            object.__setattr__(
                self,
                field,
                _unit_interval(getattr(self, field), f"candidate.{field}"),
            )


@dataclass(frozen=True)
class ScoreContribution:
    attribute: str
    value: float
    weight: float
    contribution: float


@dataclass(frozen=True)
class CandidateAssessment:
    candidate: CapabilityCandidate
    score: float
    score_breakdown: tuple[ScoreContribution, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CandidateRejection:
    candidate_id: str
    route_kind: str
    display_name: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityResolution:
    status: str
    job_id: str
    capability: str
    recommended: CandidateAssessment | None
    fallbacks: tuple[CandidateAssessment, ...]
    rejected: tuple[CandidateRejection, ...]
    reason_codes: tuple[str, ...]

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        recommended = (
            ()
            if self.recommended is None
            else (self.recommended.candidate.candidate_id,)
        )
        return recommended + tuple(
            item.candidate.candidate_id for item in self.fallbacks
        )


class CapabilityResolver:
    """Pure host/provider-neutral N0TEable route resolver.

    Route kind, brand and display name never contribute to score. They are
    descriptive metadata only. Legitimacy is filtered before scoring, so user
    preference cannot resurrect an unverified, incompatible or forbidden route.
    """

    @staticmethod
    def _reject_reasons(
        job: N0TEableJob,
        candidate: CapabilityCandidate,
        constraints: ResolutionConstraints,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if candidate.capability != job.capability:
            reasons.append("CAPABILITY_MISMATCH")
        if not candidate.verified:
            reasons.append("UNVERIFIED")
        elif not candidate.evidence_ref:
            reasons.append("MISSING_EVIDENCE_REF")
        if not candidate.compatible:
            reasons.append("INCOMPATIBLE")
        if (
            constraints.max_evidence_age_seconds is not None
            and (
                candidate.evidence_age_seconds is None
                or candidate.evidence_age_seconds
                > constraints.max_evidence_age_seconds
            )
        ):
            reasons.append("STALE_OR_UNKNOWN_EVIDENCE")
        if candidate.locality < constraints.min_locality:
            reasons.append("LOCALITY_BELOW_MINIMUM")
        if candidate.privacy < constraints.min_privacy:
            reasons.append("PRIVACY_BELOW_MINIMUM")
        if constraints.require_reversible and candidate.reversibility < 1.0:
            reasons.append("REVERSIBILITY_REQUIRED")
        if not constraints.allow_paid and candidate.paid:
            reasons.append("PAID_ROUTE_NOT_ALLOWED")
        return tuple(reasons)

    @staticmethod
    def _assessment(candidate: CapabilityCandidate) -> CandidateAssessment:
        breakdown = tuple(
            ScoreContribution(
                attribute=attribute,
                value=getattr(candidate, attribute),
                weight=weight,
                contribution=getattr(candidate, attribute) * weight,
            )
            for attribute, weight in _SCORE_WEIGHTS.items()
        )
        score = sum(item.contribution for item in breakdown)
        return CandidateAssessment(
            candidate=candidate,
            score=score,
            score_breakdown=breakdown,
            reason_codes=(
                "CAPABILITY_MATCH",
                "VERIFIED",
                "COMPATIBLE",
                "CONSTRAINTS_SATISFIED",
                "SCORED_FROM_EXPLICIT_ATTRIBUTES_ONLY",
            ),
        )

    def resolve(
        self,
        job: N0TEableJob,
        candidates: Iterable[CapabilityCandidate],
        constraints: ResolutionConstraints = ResolutionConstraints(),
    ) -> CapabilityResolution:
        if not isinstance(job, N0TEableJob):
            raise TypeError("job must be N0TEableJob")
        if not isinstance(constraints, ResolutionConstraints):
            raise TypeError("constraints must be ResolutionConstraints")

        candidates = tuple(candidates)
        if not all(
            isinstance(candidate, CapabilityCandidate) for candidate in candidates
        ):
            raise TypeError("all candidates must be CapabilityCandidate")
        candidate_ids = [candidate.candidate_id for candidate in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise CapabilityResolutionError("candidate_id values must be unique")

        legitimate: list[CandidateAssessment] = []
        rejected: list[CandidateRejection] = []

        for candidate in candidates:
            reasons = self._reject_reasons(job, candidate, constraints)
            if reasons:
                rejected.append(
                    CandidateRejection(
                        candidate_id=candidate.candidate_id,
                        route_kind=candidate.route_kind,
                        display_name=candidate.display_name,
                        reason_codes=reasons,
                    )
                )
            else:
                legitimate.append(self._assessment(candidate))

        rejected.sort(key=lambda item: item.candidate_id)
        legitimate.sort(key=lambda item: (-item.score, item.candidate.candidate_id))

        if not legitimate:
            gap_codes = sorted(
                {reason for item in rejected for reason in item.reason_codes}
            )
            if not rejected:
                gap_codes.append("NO_CANDIDATES")
            return CapabilityResolution(
                status="UNAVAILABLE",
                job_id=job.id,
                capability=job.capability,
                recommended=None,
                fallbacks=(),
                rejected=tuple(rejected),
                reason_codes=("NO_LEGITIMATE_ROUTE", *gap_codes),
            )

        top = legitimate[0]
        resolution_reasons = ["RECOMMENDED_HIGHEST_EXPLICIT_SCORE"]
        tied = [
            item for item in legitimate if abs(item.score - top.score) <= 1e-12
        ]
        if len(tied) > 1:
            resolution_reasons.append("SCORE_TIE_BROKEN_BY_CANDIDATE_ID")

        recommended = CandidateAssessment(
            candidate=top.candidate,
            score=top.score,
            score_breakdown=top.score_breakdown,
            reason_codes=top.reason_codes + tuple(resolution_reasons),
        )
        return CapabilityResolution(
            status="RESOLVED",
            job_id=job.id,
            capability=job.capability,
            recommended=recommended,
            fallbacks=tuple(legitimate[1:]),
            rejected=tuple(rejected),
            reason_codes=tuple(resolution_reasons),
        )
