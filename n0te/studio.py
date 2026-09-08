from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .capabilities import (
    ROUTE_KINDS,
    CapabilityCandidate,
    CapabilityResolution,
    CapabilityResolutionError,
    CapabilityResolver,
    N0TEableJob,
    ResolutionConstraints,
)


@dataclass(frozen=True)
class RouteCapabilitySummary:
    route_kind: str
    candidate_ids: tuple[str, ...]
    capabilities: tuple[str, ...]
    verified_candidate_ids: tuple[str, ...]
    unverified_candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class StudioCapabilityGap:
    job_id: str
    capability: str
    reason_codes: tuple[str, ...]
    rejected_candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class StudioCapabilityProfile:
    """Pure per-studio truth surface over explicit CapabilityCandidate facts.

    The profile does not discover tools, infer capability from installed names, score
    candidates itself, persist environment state, or execute a route. Host/brand
    metadata remains descriptive only. CORE-03A CapabilityResolver remains the one
    owner of legitimacy filtering and route scoring.
    """

    environment_id: str
    candidates: tuple[CapabilityCandidate, ...]
    host_label: str | None = None

    def __post_init__(self) -> None:
        environment_id = str(self.environment_id).strip()
        if not environment_id:
            raise CapabilityResolutionError("environment_id must not be empty")
        object.__setattr__(self, "environment_id", environment_id)
        if self.host_label is not None:
            host_label = str(self.host_label).strip()
            if not host_label:
                raise CapabilityResolutionError("host_label must not be empty")
            object.__setattr__(self, "host_label", host_label)

        facts = tuple(self.candidates)
        if not all(isinstance(item, CapabilityCandidate) for item in facts):
            raise TypeError("all studio capability facts must be CapabilityCandidate")
        candidate_ids = [item.candidate_id for item in facts]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise CapabilityResolutionError(
                "studio capability candidate_id values must be unique"
            )
        object.__setattr__(
            self,
            "candidates",
            tuple(sorted(facts, key=lambda item: item.candidate_id)),
        )

    @classmethod
    def build(
        cls,
        *,
        environment_id: str,
        candidates: Iterable[CapabilityCandidate],
        host_label: str | None = None,
    ) -> "StudioCapabilityProfile":
        return cls(
            environment_id=environment_id,
            candidates=tuple(candidates),
            host_label=host_label,
        )

    def candidates_for(self, capability: str) -> tuple[CapabilityCandidate, ...]:
        capability = str(capability).strip()
        if not capability:
            raise CapabilityResolutionError("capability must not be empty")
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.capability == capability
        )

    def route_summary(self) -> tuple[RouteCapabilitySummary, ...]:
        summaries: list[RouteCapabilitySummary] = []
        for route_kind in sorted(ROUTE_KINDS):
            facts = tuple(
                candidate
                for candidate in self.candidates
                if candidate.route_kind == route_kind
            )
            if not facts:
                continue
            summaries.append(
                RouteCapabilitySummary(
                    route_kind=route_kind,
                    candidate_ids=tuple(item.candidate_id for item in facts),
                    capabilities=tuple(
                        sorted({item.capability for item in facts})
                    ),
                    verified_candidate_ids=tuple(
                        item.candidate_id for item in facts if item.verified
                    ),
                    unverified_candidate_ids=tuple(
                        item.candidate_id for item in facts if not item.verified
                    ),
                )
            )
        return tuple(summaries)

    def resolve(
        self,
        job: N0TEableJob,
        constraints: ResolutionConstraints = ResolutionConstraints(),
    ) -> CapabilityResolution:
        return CapabilityResolver().resolve(
            job,
            self.candidates_for(job.capability),
            constraints,
        )

    def resolve_many(
        self,
        jobs: Iterable[N0TEableJob],
        constraints: ResolutionConstraints = ResolutionConstraints(),
    ) -> tuple[CapabilityResolution, ...]:
        jobs = tuple(jobs)
        if not all(isinstance(job, N0TEableJob) for job in jobs):
            raise TypeError("all jobs must be N0TEableJob")
        job_ids = [job.id for job in jobs]
        if len(job_ids) != len(set(job_ids)):
            raise CapabilityResolutionError("job.id values must be unique")
        return tuple(
            self.resolve(job, constraints)
            for job in sorted(jobs, key=lambda item: item.id)
        )

    def gaps(
        self,
        jobs: Iterable[N0TEableJob],
        constraints: ResolutionConstraints = ResolutionConstraints(),
    ) -> tuple[StudioCapabilityGap, ...]:
        gaps: list[StudioCapabilityGap] = []
        for resolution in self.resolve_many(jobs, constraints):
            if resolution.status != "UNAVAILABLE":
                continue
            gaps.append(
                StudioCapabilityGap(
                    job_id=resolution.job_id,
                    capability=resolution.capability,
                    reason_codes=resolution.reason_codes,
                    rejected_candidate_ids=tuple(
                        item.candidate_id for item in resolution.rejected
                    ),
                )
            )
        return tuple(gaps)
