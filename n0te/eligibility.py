from __future__ import annotations

from dataclasses import dataclass

ELIGIBILITY_STATUSES = {"ALLOW", "DENY", "STALE"}
ENTITLEMENT_STATES = {"GRANTED", "DENIED", "UNKNOWN", "NOT_REQUIRED"}
PERMISSION_STATES = {"GRANTED", "DENIED", "UNKNOWN", "NOT_REQUIRED"}


class EligibilityError(ValueError):
    """Invalid execution-eligibility request or evidence."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise EligibilityError(f"{field} must not be empty")
    return text


def _strict_bool(value: bool, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field} must be a real bool")
    return value


def _nonnegative_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise EligibilityError(f"{field} must be >= 0")
    return value


def _positive_int(value: int, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result <= 0:
        raise EligibilityError(f"{field} must be > 0")
    return result


def _state(value: str, allowed: set[str], field: str) -> str:
    state = str(value).strip().upper()
    if state not in allowed:
        raise EligibilityError(f"unsupported {field}: {state}")
    return state


@dataclass(frozen=True)
class ExecutionEligibilityRequest:
    """The exact route/job/environment about to be considered for execution."""

    job_id: str
    route_id: str
    subject_id: str
    capability: str
    environment_fingerprint: str
    max_evidence_age_seconds: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _text(self.job_id, "request.job_id"))
        object.__setattr__(self, "route_id", _text(self.route_id, "request.route_id"))
        object.__setattr__(self, "subject_id", _text(self.subject_id, "request.subject_id"))
        object.__setattr__(self, "capability", _text(self.capability, "request.capability"))
        object.__setattr__(
            self,
            "environment_fingerprint",
            _text(self.environment_fingerprint, "request.environment_fingerprint"),
        )
        object.__setattr__(
            self,
            "max_evidence_age_seconds",
            _positive_int(
                self.max_evidence_age_seconds,
                "request.max_evidence_age_seconds",
            ),
        )


@dataclass(frozen=True)
class ExecutionEligibilityEvidence:
    """Opaque-reference evidence only. No credential or secret value belongs here."""

    job_id: str
    route_id: str
    subject_id: str
    capability: str
    environment_fingerprint: str
    evidence_fingerprint: str
    evidence_ref: str
    verified: bool
    entitlement_state: str
    permission_state: str
    evidence_age_seconds: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _text(self.job_id, "evidence.job_id"))
        object.__setattr__(self, "route_id", _text(self.route_id, "evidence.route_id"))
        object.__setattr__(self, "subject_id", _text(self.subject_id, "evidence.subject_id"))
        object.__setattr__(self, "capability", _text(self.capability, "evidence.capability"))
        object.__setattr__(
            self,
            "environment_fingerprint",
            _text(self.environment_fingerprint, "evidence.environment_fingerprint"),
        )
        object.__setattr__(
            self,
            "evidence_fingerprint",
            _text(self.evidence_fingerprint, "evidence.evidence_fingerprint"),
        )
        object.__setattr__(
            self,
            "evidence_ref",
            _text(self.evidence_ref, "evidence.evidence_ref"),
        )
        object.__setattr__(self, "verified", _strict_bool(self.verified, "evidence.verified"))
        object.__setattr__(
            self,
            "entitlement_state",
            _state(
                self.entitlement_state,
                ENTITLEMENT_STATES,
                "entitlement state",
            ),
        )
        object.__setattr__(
            self,
            "permission_state",
            _state(
                self.permission_state,
                PERMISSION_STATES,
                "permission state",
            ),
        )
        object.__setattr__(
            self,
            "evidence_age_seconds",
            _nonnegative_int(self.evidence_age_seconds, "evidence.evidence_age_seconds"),
        )


@dataclass(frozen=True)
class ExecutionEligibilityDecision:
    status: str
    job_id: str
    route_id: str
    subject_id: str
    capability: str
    evidence_fingerprint: str
    evidence_ref: str
    reason_codes: tuple[str, ...]
    action_authority_granted: bool = False

    def __post_init__(self) -> None:
        status = str(self.status).strip().upper()
        if status not in ELIGIBILITY_STATUSES:
            raise EligibilityError(f"unsupported eligibility status: {status}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "job_id", _text(self.job_id, "decision.job_id"))
        object.__setattr__(self, "route_id", _text(self.route_id, "decision.route_id"))
        object.__setattr__(self, "subject_id", _text(self.subject_id, "decision.subject_id"))
        object.__setattr__(self, "capability", _text(self.capability, "decision.capability"))
        object.__setattr__(
            self,
            "evidence_fingerprint",
            _text(self.evidence_fingerprint, "decision.evidence_fingerprint"),
        )
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "decision.evidence_ref"))
        reasons = tuple(sorted(dict.fromkeys(_text(x, "decision.reason_codes") for x in self.reason_codes)))
        if not reasons:
            raise EligibilityError("decision.reason_codes must not be empty")
        object.__setattr__(self, "reason_codes", reasons)
        if self.action_authority_granted is not False:
            raise EligibilityError("eligibility decision may never grant action authority")


class ExecutionEligibilityGate:
    """Pure current-usability gate for one already-selected execution route."""

    @staticmethod
    def evaluate(
        request: ExecutionEligibilityRequest,
        evidence: ExecutionEligibilityEvidence,
    ) -> ExecutionEligibilityDecision:
        if not isinstance(request, ExecutionEligibilityRequest):
            raise TypeError("request must be ExecutionEligibilityRequest")
        if not isinstance(evidence, ExecutionEligibilityEvidence):
            raise TypeError("evidence must be ExecutionEligibilityEvidence")

        deny: list[str] = []
        stale: list[str] = []

        if evidence.job_id != request.job_id:
            deny.append("JOB_MISMATCH")
        if evidence.route_id != request.route_id:
            deny.append("ROUTE_MISMATCH")
        if evidence.subject_id != request.subject_id:
            deny.append("SUBJECT_MISMATCH")
        if evidence.capability != request.capability:
            deny.append("CAPABILITY_MISMATCH")
        if not evidence.verified:
            deny.append("CAPABILITY_NOT_VERIFIED")

        if evidence.entitlement_state == "DENIED":
            deny.append("ENTITLEMENT_DENIED")
        elif evidence.entitlement_state == "UNKNOWN":
            deny.append("ENTITLEMENT_UNKNOWN")

        if evidence.permission_state == "DENIED":
            deny.append("PERMISSION_DENIED")
        elif evidence.permission_state == "UNKNOWN":
            deny.append("PERMISSION_UNKNOWN")

        if evidence.environment_fingerprint != request.environment_fingerprint:
            stale.append("ENVIRONMENT_CHANGED")
        if evidence.evidence_age_seconds > request.max_evidence_age_seconds:
            stale.append("EVIDENCE_EXPIRED")

        if deny:
            status = "DENY"
            reasons = tuple(deny + stale)
        elif stale:
            status = "STALE"
            reasons = tuple(stale)
        else:
            status = "ALLOW"
            reasons = (
                "CAPABILITY_VERIFIED",
                f"ENTITLEMENT_{evidence.entitlement_state}",
                f"PERMISSION_{evidence.permission_state}",
                "ENVIRONMENT_MATCH",
                "EVIDENCE_FRESH",
                "ELIGIBILITY_ONLY_NO_ACTION_AUTHORITY",
            )

        return ExecutionEligibilityDecision(
            status=status,
            job_id=request.job_id,
            route_id=request.route_id,
            subject_id=request.subject_id,
            capability=request.capability,
            evidence_fingerprint=evidence.evidence_fingerprint,
            evidence_ref=evidence.evidence_ref,
            reason_codes=reasons,
        )
