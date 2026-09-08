from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from .eligibility import ENTITLEMENT_STATES, PERMISSION_STATES
from .evidence import EvidenceClaim, EvidenceMemory, SOURCE_KINDS
from .lineage import LineageCorruptionError, LineageStore, NotFoundError

ENTITLEMENT_TRUTH_SCHEMA_VERSION = 1
ACCESS_KINDS = {
    "DAW_EDITION",
    "DAW_FEATURE",
    "PLUGIN",
    "INSTRUMENT",
    "PROVIDER_PLAN",
    "PROVIDER_QUOTA",
    "TRIAL",
    "ACTIVATION",
    "LICENSE",
    "PERMISSION",
}
RESOLUTION_STATUSES = {"UNKNOWN", "RESOLVED", "CONFLICT"}
VALIDITY_STATES = {"UNKNOWN", "CURRENT", "EXPIRED"}
QUOTA_STATUSES = {"UNKNOWN", "RESOLVED", "CONFLICT"}
STRONG_ACCESS_SOURCES = {"OBSERVED", "PROVIDER_VERIFIED"}
SOURCE_TRUTH_CLASSES = {
    "USER_DECLARED": "DECLARED",
    "OBSERVED": "OBSERVED",
    "MEASURED": "MEASURED",
    "PROVIDER_VERIFIED": "PROVIDER_VERIFIED",
    "REMEMBERED": "REMEMBERED",
    "INFERRED": "INFERRED",
}
_SOURCE_STRENGTH = {
    "INFERRED": 0,
    "REMEMBERED": 1,
    "USER_DECLARED": 2,
    "MEASURED": 3,
    "OBSERVED": 4,
    "PROVIDER_VERIFIED": 5,
}


class EntitlementTruthError(ValueError):
    """Invalid entitlement/access evidence operation."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise EntitlementTruthError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _enum(value: str, field: str, allowed: set[str]) -> str:
    text = _text(value, field).upper().replace("-", "_").replace(" ", "_")
    if text not in allowed:
        raise EntitlementTruthError(f"unsupported {field}: {text}")
    return text


def _nonnegative_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EntitlementTruthError(f"{field} must be a non-negative integer")
    if value < 0:
        raise EntitlementTruthError(f"{field} must be a non-negative integer")
    return value


def _quota(value: float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise EntitlementTruthError("quota_remaining must be a non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EntitlementTruthError(
            "quota_remaining must be a non-negative number"
        ) from exc
    if not math.isfinite(number) or number < 0.0:
        raise EntitlementTruthError("quota_remaining must be a non-negative number")
    return number


def _confidence(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EntitlementTruthError("confidence must be between 0 and 1") from exc
    if not 0.0 <= number <= 1.0:
        raise EntitlementTruthError("confidence must be between 0 and 1")
    return number


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class EntitlementFact:
    claim_id: str
    sequence: int
    profile_id: str
    route_id: str
    capability: str
    access_kind: str
    entitlement_state: str
    permission_state: str
    observed_at_epoch_seconds: int
    expires_at_epoch_seconds: int | None
    quota_remaining: float | None
    quota_unit: str | None
    environment_fingerprint: str | None
    source_kind: str
    source_ref: str | None
    confidence: float

    @property
    def source_truth_class(self) -> str:
        return SOURCE_TRUTH_CLASSES[self.source_kind]

    @property
    def provider_verified(self) -> bool:
        return self.source_kind == "PROVIDER_VERIFIED"

    @property
    def strong_access_evidence(self) -> bool:
        return self.source_kind in STRONG_ACCESS_SOURCES

    def is_expired(self, *, as_of_epoch_seconds: int) -> bool:
        as_of = _nonnegative_int(as_of_epoch_seconds, "as_of_epoch_seconds")
        return (
            self.expires_at_epoch_seconds is not None
            and self.expires_at_epoch_seconds <= as_of
        )


@dataclass(frozen=True)
class EntitlementSnapshot:
    profile_id: str
    route_id: str
    capability: str
    access_kind: str
    as_of_epoch_seconds: int
    resolution_status: str
    validity_state: str
    entitlement_state: str
    permission_state: str
    eligibility_entitlement_state: str
    eligibility_permission_state: str
    quota_status: str
    quota_remaining: float | None
    quota_unit: str | None
    strongest_source_class: str
    provider_verified: bool
    strong_access_evidence: bool
    facts: tuple[EntitlementFact, ...]
    active_fact_ids: tuple[str, ...]
    fingerprint: str
    action_authority_granted: bool = False
    execution_authority_granted: bool = False
    purchase_authority_granted: bool = False
    activation_authority_granted: bool = False
    quota_spend_authority_granted: bool = False
    provider_write_authority_granted: bool = False
    external_action_authority_granted: bool = False

    def __post_init__(self) -> None:
        if self.resolution_status not in RESOLUTION_STATUSES:
            raise EntitlementTruthError("invalid entitlement resolution status")
        if self.validity_state not in VALIDITY_STATES:
            raise EntitlementTruthError("invalid entitlement validity state")
        if self.entitlement_state not in ENTITLEMENT_STATES:
            raise EntitlementTruthError("invalid entitlement state")
        if self.permission_state not in PERMISSION_STATES:
            raise EntitlementTruthError("invalid permission state")
        if self.eligibility_entitlement_state not in ENTITLEMENT_STATES:
            raise EntitlementTruthError("invalid eligibility entitlement state")
        if self.eligibility_permission_state not in PERMISSION_STATES:
            raise EntitlementTruthError("invalid eligibility permission state")
        if self.quota_status not in QUOTA_STATUSES:
            raise EntitlementTruthError("invalid quota status")
        for field in (
            "action_authority_granted",
            "execution_authority_granted",
            "purchase_authority_granted",
            "activation_authority_granted",
            "quota_spend_authority_granted",
            "provider_write_authority_granted",
            "external_action_authority_granted",
        ):
            if getattr(self, field) is not False:
                raise EntitlementTruthError(
                    "entitlement truth may never grant action authority"
                )


class EntitlementTruthService:
    """Durable access truth without licensing or execution authority.

    Entitlement truth is profile-scoped because licensing, plans, activations and
    permissions belong to the active artist profile/environment, not to a Song.
    It is also route+capability+access-kind specific. The service never derives
    entitlement from installation, capability availability, account presence,
    pricing metadata or route ranking.

    Provider verification must be written by a canonical provider verifier into
    EvidenceMemory. This service can consume such evidence but intentionally
    refuses to self-mint PROVIDER_VERIFIED claims.
    """

    _KEY_PREFIX = "studio.entitlement.v1"

    def __init__(self, store: LineageStore, evidence: EvidenceMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("EntitlementTruthService requires the canonical LineageStore")
        if not isinstance(evidence, EvidenceMemory):
            raise TypeError("EntitlementTruthService requires EvidenceMemory")
        if evidence.store is not store:
            raise TypeError(
                "EntitlementTruthService and EvidenceMemory must share one LineageStore"
            )
        self.store = store
        self.evidence = evidence

    @classmethod
    def claim_key(cls, *, route_id: str, capability: str, access_kind: str) -> str:
        route = _text(route_id, "route_id")
        cap = _text(capability, "capability")
        kind = _enum(access_kind, "access_kind", ACCESS_KINDS)
        digest = hashlib.sha256(
            f"n0te-entitlement-key/v1\x00{route}\x00{cap}\x00{kind}".encode("utf-8")
        ).hexdigest()
        return f"{cls._KEY_PREFIX}.{digest}"

    @staticmethod
    def evidence_payload(
        *,
        route_id: str,
        capability: str,
        access_kind: str,
        entitlement_state: str,
        permission_state: str = "NOT_REQUIRED",
        observed_at_epoch_seconds: int,
        expires_at_epoch_seconds: int | None = None,
        quota_remaining: float | int | None = None,
        quota_unit: str | None = None,
        environment_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        route = _text(route_id, "route_id")
        cap = _text(capability, "capability")
        kind = _enum(access_kind, "access_kind", ACCESS_KINDS)
        entitlement = _enum(
            entitlement_state, "entitlement_state", ENTITLEMENT_STATES
        )
        permission = _enum(permission_state, "permission_state", PERMISSION_STATES)
        observed = _nonnegative_int(
            observed_at_epoch_seconds, "observed_at_epoch_seconds"
        )
        expires = None
        if expires_at_epoch_seconds is not None:
            expires = _nonnegative_int(
                expires_at_epoch_seconds, "expires_at_epoch_seconds"
            )
            if expires <= observed:
                raise EntitlementTruthError(
                    "expires_at_epoch_seconds must be after observed_at_epoch_seconds"
                )
        remaining = _quota(quota_remaining)
        unit = _optional_text(quota_unit, "quota_unit")
        if (remaining is None) != (unit is None):
            raise EntitlementTruthError(
                "quota_remaining and quota_unit must be provided together"
            )
        environment = _optional_text(
            environment_fingerprint, "environment_fingerprint"
        )
        return {
            "schema_version": ENTITLEMENT_TRUTH_SCHEMA_VERSION,
            "route_id": route,
            "capability": cap,
            "access_kind": kind,
            "entitlement_state": entitlement,
            "permission_state": permission,
            "observed_at_epoch_seconds": observed,
            "expires_at_epoch_seconds": expires,
            "quota_remaining": remaining,
            "quota_unit": unit,
            "environment_fingerprint": environment,
        }

    def _fact_from_claim(
        self,
        claim: EvidenceClaim,
        *,
        expected_route_id: str | None = None,
        expected_capability: str | None = None,
        expected_access_kind: str | None = None,
        validate_supersession_time: bool = True,
    ) -> EntitlementFact:
        if claim.scope_kind != "PROFILE" or claim.scope_id != self.store.profile_id:
            raise LineageCorruptionError(
                "entitlement evidence must be scoped to the active profile"
            )
        if claim.twin_domain != "TECHNICAL":
            raise LineageCorruptionError(
                "entitlement evidence must use the TECHNICAL Twin domain"
            )
        if claim.source_kind not in SOURCE_KINDS:
            raise LineageCorruptionError("entitlement evidence has invalid source kind")
        if claim.source_kind != "USER_DECLARED" and not claim.source_ref:
            raise LineageCorruptionError(
                "non-declared entitlement evidence requires source_ref provenance"
            )
        if not isinstance(claim.value, dict):
            raise LineageCorruptionError("entitlement evidence payload must be an object")
        payload = claim.value
        required = {
            "schema_version",
            "route_id",
            "capability",
            "access_kind",
            "entitlement_state",
            "permission_state",
            "observed_at_epoch_seconds",
            "expires_at_epoch_seconds",
            "quota_remaining",
            "quota_unit",
            "environment_fingerprint",
        }
        if set(payload) != required:
            raise LineageCorruptionError(
                "entitlement evidence payload shape does not match schema v1"
            )
        if payload.get("schema_version") != ENTITLEMENT_TRUTH_SCHEMA_VERSION:
            raise LineageCorruptionError("unsupported entitlement evidence schema version")
        try:
            canonical = self.evidence_payload(
                route_id=payload["route_id"],
                capability=payload["capability"],
                access_kind=payload["access_kind"],
                entitlement_state=payload["entitlement_state"],
                permission_state=payload["permission_state"],
                observed_at_epoch_seconds=payload["observed_at_epoch_seconds"],
                expires_at_epoch_seconds=payload["expires_at_epoch_seconds"],
                quota_remaining=payload["quota_remaining"],
                quota_unit=payload["quota_unit"],
                environment_fingerprint=payload["environment_fingerprint"],
            )
        except EntitlementTruthError as exc:
            raise LineageCorruptionError("malformed entitlement evidence payload") from exc
        if _canonical_json(canonical) != _canonical_json(payload):
            raise LineageCorruptionError("entitlement evidence payload is non-canonical")
        expected_key = self.claim_key(
            route_id=canonical["route_id"],
            capability=canonical["capability"],
            access_kind=canonical["access_kind"],
        )
        if claim.key != expected_key:
            raise LineageCorruptionError(
                "entitlement evidence key does not match its route/capability binding"
            )
        if expected_route_id is not None and canonical["route_id"] != expected_route_id:
            raise LineageCorruptionError("entitlement route binding mismatch")
        if (
            expected_capability is not None
            and canonical["capability"] != expected_capability
        ):
            raise LineageCorruptionError("entitlement capability binding mismatch")
        if (
            expected_access_kind is not None
            and canonical["access_kind"] != expected_access_kind
        ):
            raise LineageCorruptionError("entitlement access-kind binding mismatch")
        fact = EntitlementFact(
            claim_id=claim.id,
            sequence=claim.sequence,
            profile_id=claim.scope_id,
            route_id=canonical["route_id"],
            capability=canonical["capability"],
            access_kind=canonical["access_kind"],
            entitlement_state=canonical["entitlement_state"],
            permission_state=canonical["permission_state"],
            observed_at_epoch_seconds=canonical["observed_at_epoch_seconds"],
            expires_at_epoch_seconds=canonical["expires_at_epoch_seconds"],
            quota_remaining=canonical["quota_remaining"],
            quota_unit=canonical["quota_unit"],
            environment_fingerprint=canonical["environment_fingerprint"],
            source_kind=claim.source_kind,
            source_ref=claim.source_ref,
            confidence=claim.confidence,
        )
        if validate_supersession_time:
            superseded_ids = tuple(
                str(row["old_claim_id"])
                for row in self.store._conn.execute(
                    "SELECT old_claim_id FROM evidence_supersessions "
                    "WHERE new_claim_id=? ORDER BY old_claim_id",
                    (claim.id,),
                )
            )
            for old_id in superseded_ids:
                old_claim = self.evidence.get_claim(old_id)
                if old_claim is None:
                    raise LineageCorruptionError(
                        "entitlement supersession references missing evidence"
                    )
                old_fact = self._fact_from_claim(
                    old_claim,
                    expected_route_id=fact.route_id,
                    expected_capability=fact.capability,
                    expected_access_kind=fact.access_kind,
                    validate_supersession_time=False,
                )
                if fact.observed_at_epoch_seconds < old_fact.observed_at_epoch_seconds:
                    raise LineageCorruptionError(
                        "entitlement observation time regressed across supersession"
                    )
        return fact

    def consume_claim(self, claim_id: str) -> EntitlementFact:
        claim = self.evidence.get_claim(_text(claim_id, "claim_id"))
        if claim is None:
            raise NotFoundError(f"evidence claim not found: {claim_id}")
        return self._fact_from_claim(claim)

    def record_fact(
        self,
        *,
        route_id: str,
        capability: str,
        access_kind: str,
        entitlement_state: str,
        permission_state: str = "NOT_REQUIRED",
        observed_at_epoch_seconds: int,
        expires_at_epoch_seconds: int | None = None,
        quota_remaining: float | int | None = None,
        quota_unit: str | None = None,
        environment_fingerprint: str | None = None,
        source_kind: str = "USER_DECLARED",
        source_ref: str | None = None,
        confidence: float = 1.0,
        supersedes: Iterable[str] = (),
    ) -> EntitlementFact:
        source = _enum(source_kind, "source_kind", SOURCE_KINDS)
        if source == "PROVIDER_VERIFIED":
            raise EntitlementTruthError(
                "EntitlementTruthService cannot self-mint PROVIDER_VERIFIED evidence"
            )
        source_reference = _optional_text(source_ref, "source_ref")
        if source != "USER_DECLARED" and source_reference is None:
            raise EntitlementTruthError(
                "non-declared entitlement evidence requires source_ref provenance"
            )
        payload = self.evidence_payload(
            route_id=route_id,
            capability=capability,
            access_kind=access_kind,
            entitlement_state=entitlement_state,
            permission_state=permission_state,
            observed_at_epoch_seconds=observed_at_epoch_seconds,
            expires_at_epoch_seconds=expires_at_epoch_seconds,
            quota_remaining=quota_remaining,
            quota_unit=quota_unit,
            environment_fingerprint=environment_fingerprint,
        )
        old_ids = tuple(dict.fromkeys(str(item) for item in supersedes))
        for old_id in old_ids:
            old_claim = self.evidence.get_claim(old_id)
            if old_claim is None:
                raise NotFoundError(f"evidence claim not found: {old_id}")
            old_fact = self._fact_from_claim(
                old_claim,
                expected_route_id=payload["route_id"],
                expected_capability=payload["capability"],
                expected_access_kind=payload["access_kind"],
            )
            if payload["observed_at_epoch_seconds"] < old_fact.observed_at_epoch_seconds:
                raise EntitlementTruthError(
                    "entitlement observation time cannot regress when superseding evidence"
                )
        claim = self.evidence.record_claim(
            scope_kind="PROFILE",
            scope_id=self.store.profile_id,
            key=self.claim_key(
                route_id=payload["route_id"],
                capability=payload["capability"],
                access_kind=payload["access_kind"],
            ),
            value=payload,
            source_kind=source,
            source_ref=source_reference,
            confidence=_confidence(confidence),
            twin_domain="TECHNICAL",
            supersedes=old_ids,
        )
        return self._fact_from_claim(claim)

    @staticmethod
    def _source_class(facts: tuple[EntitlementFact, ...]) -> str:
        if not facts:
            return "NONE"
        strongest = max(facts, key=lambda fact: _SOURCE_STRENGTH[fact.source_kind])
        return strongest.source_truth_class

    @staticmethod
    def _state_resolution(
        facts: tuple[EntitlementFact, ...], field: str
    ) -> tuple[str, bool]:
        values = {getattr(fact, field) for fact in facts}
        if not values:
            return "UNKNOWN", False
        if len(values) != 1:
            return "UNKNOWN", True
        return next(iter(values)), False

    @staticmethod
    def _quota_resolution(
        facts: tuple[EntitlementFact, ...]
    ) -> tuple[str, float | None, str | None]:
        values = {
            (fact.quota_remaining, fact.quota_unit)
            for fact in facts
            if fact.quota_remaining is not None
        }
        if not values:
            return "UNKNOWN", None, None
        if len(values) != 1:
            return "CONFLICT", None, None
        remaining, unit = next(iter(values))
        return "RESOLVED", remaining, unit

    @staticmethod
    def _eligibility_state(
        state: str,
        *,
        resolved: bool,
        current: bool,
        strong_access_evidence: bool,
    ) -> str:
        if not resolved or not current:
            return "UNKNOWN"
        if state == "DENIED":
            return "DENIED"
        if state in {"GRANTED", "NOT_REQUIRED"}:
            return state if strong_access_evidence else "UNKNOWN"
        return "UNKNOWN"

    def snapshot(
        self,
        *,
        route_id: str,
        capability: str,
        access_kind: str,
        as_of_epoch_seconds: int,
    ) -> EntitlementSnapshot:
        route = _text(route_id, "route_id")
        cap = _text(capability, "capability")
        kind = _enum(access_kind, "access_kind", ACCESS_KINDS)
        as_of = _nonnegative_int(as_of_epoch_seconds, "as_of_epoch_seconds")
        key = self.claim_key(route_id=route, capability=cap, access_kind=kind)
        claims = self.evidence.active_claims("PROFILE", self.store.profile_id, key)
        facts = tuple(
            self._fact_from_claim(
                claim,
                expected_route_id=route,
                expected_capability=cap,
                expected_access_kind=kind,
            )
            for claim in claims
        )
        observed = tuple(
            fact for fact in facts if fact.observed_at_epoch_seconds <= as_of
        )
        current = tuple(
            fact for fact in observed if not fact.is_expired(as_of_epoch_seconds=as_of)
        )
        if not observed:
            validity = "UNKNOWN"
        elif not current:
            validity = "EXPIRED"
        else:
            validity = "CURRENT"

        entitlement_state, entitlement_conflict = self._state_resolution(
            current, "entitlement_state"
        )
        permission_state, permission_conflict = self._state_resolution(
            current, "permission_state"
        )
        quota_status, quota_remaining, quota_unit = self._quota_resolution(current)
        any_conflict = entitlement_conflict or permission_conflict or quota_status == "CONFLICT"
        if not current:
            resolution = "UNKNOWN" if not observed else "RESOLVED"
        elif any_conflict:
            resolution = "CONFLICT"
        else:
            resolution = "RESOLVED"

        strong = any(fact.strong_access_evidence for fact in current)
        provider_verified = any(fact.provider_verified for fact in current)
        eligibility_entitlement = self._eligibility_state(
            entitlement_state,
            resolved=resolution == "RESOLVED",
            current=validity == "CURRENT",
            strong_access_evidence=strong,
        )
        eligibility_permission = self._eligibility_state(
            permission_state,
            resolved=resolution == "RESOLVED",
            current=validity == "CURRENT",
            strong_access_evidence=strong,
        )

        fingerprint_payload = {
            "profile_id": self.store.profile_id,
            "route_id": route,
            "capability": cap,
            "access_kind": kind,
            "as_of_epoch_seconds": as_of,
            "active_claims": [
                {
                    "claim_id": fact.claim_id,
                    "sequence": fact.sequence,
                    "source_kind": fact.source_kind,
                    "source_ref": fact.source_ref,
                    "confidence": fact.confidence,
                    "payload": {
                        "entitlement_state": fact.entitlement_state,
                        "permission_state": fact.permission_state,
                        "observed_at_epoch_seconds": fact.observed_at_epoch_seconds,
                        "expires_at_epoch_seconds": fact.expires_at_epoch_seconds,
                        "quota_remaining": fact.quota_remaining,
                        "quota_unit": fact.quota_unit,
                        "environment_fingerprint": fact.environment_fingerprint,
                    },
                }
                for fact in facts
            ],
        }
        fingerprint = hashlib.sha256(
            _canonical_json(fingerprint_payload).encode("utf-8")
        ).hexdigest()
        return EntitlementSnapshot(
            profile_id=self.store.profile_id,
            route_id=route,
            capability=cap,
            access_kind=kind,
            as_of_epoch_seconds=as_of,
            resolution_status=resolution,
            validity_state=validity,
            entitlement_state=entitlement_state,
            permission_state=permission_state,
            eligibility_entitlement_state=eligibility_entitlement,
            eligibility_permission_state=eligibility_permission,
            quota_status=quota_status,
            quota_remaining=quota_remaining,
            quota_unit=quota_unit,
            strongest_source_class=self._source_class(current),
            provider_verified=provider_verified,
            strong_access_evidence=strong,
            facts=facts,
            active_fact_ids=tuple(fact.claim_id for fact in facts),
            fingerprint=fingerprint,
        )

    def assert_current(self, snapshot: EntitlementSnapshot) -> EntitlementSnapshot:
        if not isinstance(snapshot, EntitlementSnapshot):
            raise TypeError("snapshot must be EntitlementSnapshot")
        if snapshot.profile_id != self.store.profile_id:
            raise EntitlementTruthError("entitlement snapshot belongs to another profile")
        current = self.snapshot(
            route_id=snapshot.route_id,
            capability=snapshot.capability,
            access_kind=snapshot.access_kind,
            as_of_epoch_seconds=snapshot.as_of_epoch_seconds,
        )
        if current.fingerprint != snapshot.fingerprint:
            raise EntitlementTruthError("entitlement snapshot is stale or was modified")
        return current
