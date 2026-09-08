from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from .evidence import EvidenceClaim, EvidenceMemory, SOURCE_KINDS
from .lineage import LineageStore, NotFoundError, ValidationError
from .people import PeopleMemory, Person

FAN_JOURNEY_SCHEMA_VERSION = 1
FAN_STAGES = (
    "DISCOVER",
    "LISTEN",
    "RETURN",
    "FOLLOW",
    "ENGAGE",
    "JOIN",
    "SUPPORT",
    "ADVOCATE",
)
CONSENT_CHANNELS = ("EMAIL", "SMS", "DM", "COMMUNITY", "OTHER")
CONSENT_STATES = ("OPTED_IN", "OPTED_OUT")
FAN_JOURNEY_AUTHORITY = "EVIDENCE_ONLY"
FAN_EVIDENCE_PREFIX = "audience.fan_journey"

_OBSERVED_SOURCE_KINDS = {"OBSERVED", "MEASURED", "PROVIDER_VERIFIED"}
_DECLARED_SOURCE_KINDS = {"USER_DECLARED", "REMEMBERED"}
_INFERRED_SOURCE_KINDS = {"INFERRED"}
_SELF_RECORDABLE_STAGE_SOURCES = SOURCE_KINDS - {"PROVIDER_VERIFIED"}
_SELF_RECORDABLE_CONSENT_SOURCES = {"USER_DECLARED", "OBSERVED"}
_CONSENT_EVIDENCE_SOURCES = {"USER_DECLARED", "OBSERVED", "PROVIDER_VERIFIED"}
_SOURCE_REF_REQUIRED = {"OBSERVED", "MEASURED", "PROVIDER_VERIFIED", "INFERRED"}


class FanJourneyError(RuntimeError):
    """Fan Journey evidence could not be represented truthfully."""


class StaleFanJourneyError(FanJourneyError):
    """A Fan Journey snapshot no longer matches canonical evidence."""


def _normalize_enum(value: object, *, field: str, allowed: tuple[str, ...]) -> str:
    text = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    if text not in allowed:
        raise ValidationError(f"unsupported {field}: {text}")
    return text


def _optional_text(value: object | None, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > maximum:
        raise ValidationError(f"{field} is too long")
    return text


def _timestamp(value: object | None, *, field: str = "observed_at") -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    parse_value = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(parse_value)
    except ValueError as exc:
        raise ValidationError(f"{field} must be ISO-8601 date/time text") from exc
    return text


def _claim_source_ref(claim: EvidenceClaim) -> str | None:
    source_ref = None if claim.source_ref is None else str(claim.source_ref).strip()
    if claim.source_kind in _SOURCE_REF_REQUIRED and not source_ref:
        raise FanJourneyError(
            "Fan Journey observed/measured/provider/inferred evidence requires provenance"
        )
    return source_ref or None


@dataclass(frozen=True)
class FanJourneySignal:
    claim_id: str
    sequence: int
    person_id: str
    stage: str
    source_kind: str
    source_ref: str | None
    confidence: float
    observed_at: str | None
    song_id: str | None
    note: str | None

    @property
    def observed(self) -> bool:
        return self.source_kind in _OBSERVED_SOURCE_KINDS

    @property
    def declared(self) -> bool:
        return self.source_kind in _DECLARED_SOURCE_KINDS

    @property
    def inferred(self) -> bool:
        return self.source_kind in _INFERRED_SOURCE_KINDS

    @property
    def causal_claim_supported(self) -> bool:
        return False

    @property
    def contact_authority_granted(self) -> bool:
        return False


@dataclass(frozen=True)
class FanConsentEvidence:
    claim_id: str
    sequence: int
    person_id: str
    channel: str
    status: str
    source_kind: str
    source_ref: str | None
    observed_at: str | None
    note: str | None

    @property
    def external_action_authority_granted(self) -> bool:
        return False


@dataclass(frozen=True)
class FanJourneySnapshot:
    profile_id: str
    artist_id: str
    person_id: str
    person_display_name: str
    signals: tuple[FanJourneySignal, ...]
    consent_history: tuple[FanConsentEvidence, ...]
    fingerprint: str
    authority: str = FAN_JOURNEY_AUTHORITY

    @property
    def observed_stages(self) -> tuple[str, ...]:
        present = {signal.stage for signal in self.signals if signal.observed}
        return tuple(stage for stage in FAN_STAGES if stage in present)

    @property
    def declared_stages(self) -> tuple[str, ...]:
        present = {signal.stage for signal in self.signals if signal.declared}
        return tuple(stage for stage in FAN_STAGES if stage in present)

    @property
    def inferred_stages(self) -> tuple[str, ...]:
        present = {signal.stage for signal in self.signals if signal.inferred}
        return tuple(stage for stage in FAN_STAGES if stage in present)

    @property
    def furthest_observed_stage(self) -> str | None:
        stages = self.observed_stages
        return None if not stages else stages[-1]

    @property
    def status(self) -> str:
        categories = sum(
            bool(items)
            for items in (
                self.observed_stages,
                self.declared_stages,
                self.inferred_stages,
            )
        )
        if categories == 0:
            return "UNKNOWN"
        if categories > 1:
            return "MIXED"
        if self.observed_stages:
            return "OBSERVED"
        if self.declared_stages:
            return "DECLARED_ONLY"
        return "INFERRED_ONLY"

    def consent_evidence(self, channel: str) -> FanConsentEvidence | None:
        normalized = _normalize_enum(
            channel, field="consent channel", allowed=CONSENT_CHANNELS
        )
        matches = [item for item in self.consent_history if item.channel == normalized]
        return None if not matches else max(matches, key=lambda item: item.sequence)

    def consent_status(self, channel: str) -> str:
        current = self.consent_evidence(channel)
        return "UNKNOWN" if current is None else current.status

    @property
    def action_authority_granted(self) -> bool:
        return False

    @property
    def contact_authority_granted(self) -> bool:
        return False

    @property
    def marketing_permission_granted(self) -> bool:
        return False

    @property
    def causal_claim_supported(self) -> bool:
        return False

    @property
    def linear_funnel_assumed(self) -> bool:
        return False

    @property
    def relationship_score(self) -> None:
        return None


class FanJourneyService:
    """Evidence-bound direct-fan relationship model over canonical People.

    The model records relationship-stage signals and explicit channel-consent
    evidence as separate canonical Evidence claims. It never manufactures
    intermediate funnel stages, infers consent from engagement, ranks people,
    sends messages, calls providers, or grants action authority.
    """

    def __init__(
        self,
        store: LineageStore,
        people: PeopleMemory,
        evidence: EvidenceMemory,
    ) -> None:
        if not isinstance(store, LineageStore):
            raise TypeError("FanJourneyService requires LineageStore")
        if not isinstance(people, PeopleMemory) or people.store is not store:
            raise TypeError("FanJourneyService requires PeopleMemory for the same LineageStore")
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError("FanJourneyService requires EvidenceMemory for the same LineageStore")
        self.store = store
        self.people = people
        self.evidence = evidence

    def _person(self, person_id: str) -> Person:
        person = self.people.get_person(str(person_id).strip())
        if person is None:
            raise NotFoundError(f"person not found: {person_id}")
        if person.artist_id != self.store.primary_artist_id:
            raise ValidationError("Fan Journey person belongs to a different Artist")
        return person

    def _song_id(self, song_id: object | None) -> str | None:
        if song_id is None or not str(song_id).strip():
            return None
        value = str(song_id).strip()
        song = self.store.get_song(value)
        if song is None:
            raise NotFoundError(f"Song not found: {value}")
        if song.artist_id != self.store.primary_artist_id:
            raise ValidationError("Fan Journey Song belongs to a different Artist")
        return value

    @staticmethod
    def _source_kind(value: object) -> str:
        source = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        if source not in SOURCE_KINDS:
            raise ValidationError(f"unsupported evidence source: {source}")
        return source

    @staticmethod
    def _source_ref(source_kind: str, value: object | None) -> str | None:
        source_ref = _optional_text(value, field="source_ref", maximum=1000)
        if source_kind in _SOURCE_REF_REQUIRED and source_ref is None:
            raise ValidationError(f"{source_kind} Fan Journey evidence requires source_ref")
        return source_ref

    @staticmethod
    def _confidence(value: object) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("confidence must be between 0 and 1") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValidationError("confidence must be between 0 and 1")
        return confidence

    @staticmethod
    def _key(person_id: str, kind: str) -> str:
        return (
            f"{FAN_EVIDENCE_PREFIX}.{person_id}."
            f"{kind.lower()}.{uuid.uuid4().hex}"
        )

    def record_stage(
        self,
        person_id: str,
        stage: str,
        *,
        source_kind: str,
        source_ref: str | None = None,
        confidence: float = 1.0,
        observed_at: str | None = None,
        song_id: str | None = None,
        note: str | None = None,
    ) -> EvidenceClaim:
        person = self._person(person_id)
        normalized_stage = _normalize_enum(stage, field="fan stage", allowed=FAN_STAGES)
        source = self._source_kind(source_kind)
        if source not in _SELF_RECORDABLE_STAGE_SOURCES:
            raise ValidationError(
                "FanJourneyService cannot self-issue PROVIDER_VERIFIED evidence; "
                "consume verifier-backed canonical Evidence instead"
            )
        provenance = self._source_ref(source, source_ref)
        normalized_song = self._song_id(song_id)
        payload = {
            "schema_version": FAN_JOURNEY_SCHEMA_VERSION,
            "kind": "STAGE",
            "person_id": person.id,
            "stage": normalized_stage,
            "observed_at": _timestamp(observed_at),
            "song_id": normalized_song,
            "note": _optional_text(note, field="note", maximum=1200),
        }
        return self.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=self.store.primary_artist_id,
            key=self._key(person.id, "signal"),
            value=payload,
            source_kind=source,
            source_ref=provenance,
            confidence=self._confidence(confidence),
            twin_domain="UNSPECIFIED",
        )

    def record_consent(
        self,
        person_id: str,
        channel: str,
        status: str,
        *,
        source_kind: str,
        source_ref: str | None = None,
        observed_at: str | None = None,
        note: str | None = None,
    ) -> EvidenceClaim:
        person = self._person(person_id)
        normalized_channel = _normalize_enum(
            channel, field="consent channel", allowed=CONSENT_CHANNELS
        )
        normalized_status = _normalize_enum(
            status, field="consent status", allowed=CONSENT_STATES
        )
        source = self._source_kind(source_kind)
        if source == "PROVIDER_VERIFIED":
            raise ValidationError(
                "FanJourneyService cannot self-issue PROVIDER_VERIFIED consent; "
                "consume verifier-backed canonical Evidence instead"
            )
        if source not in _SELF_RECORDABLE_CONSENT_SOURCES:
            raise ValidationError(
                "consent may be recorded only from explicit USER_DECLARED or OBSERVED evidence; "
                "it may never be inferred, remembered, or measured"
            )
        provenance = self._source_ref(source, source_ref)
        payload = {
            "schema_version": FAN_JOURNEY_SCHEMA_VERSION,
            "kind": "CONSENT",
            "person_id": person.id,
            "channel": normalized_channel,
            "status": normalized_status,
            "observed_at": _timestamp(observed_at),
            "note": _optional_text(note, field="note", maximum=1200),
        }
        return self.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=self.store.primary_artist_id,
            key=self._key(person.id, "consent"),
            value=payload,
            source_kind=source,
            source_ref=provenance,
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )

    def _claims_for_person(self, person_id: str) -> tuple[EvidenceClaim, ...]:
        prefix = f"{FAN_EVIDENCE_PREFIX}.{person_id}."
        return tuple(
            claim
            for claim in self.evidence.active_claims_for_scope(
                "ARTIST", self.store.primary_artist_id
            )
            if claim.key.startswith(prefix)
        )

    def _parse_signal(self, claim: EvidenceClaim, person_id: str) -> FanJourneySignal:
        if claim.scope_kind != "ARTIST" or claim.scope_id != self.store.primary_artist_id:
            raise FanJourneyError("Fan Journey evidence crossed Artist scope")
        if claim.twin_domain != "UNSPECIFIED":
            raise FanJourneyError("Fan Journey evidence uses an unexpected Twin domain")
        value = claim.value
        if not isinstance(value, dict):
            raise FanJourneyError("Fan Journey signal payload is malformed")
        if value.get("schema_version") != FAN_JOURNEY_SCHEMA_VERSION:
            raise FanJourneyError("unsupported Fan Journey signal schema version")
        if value.get("kind") != "STAGE" or value.get("person_id") != person_id:
            raise FanJourneyError("Fan Journey signal binding is malformed")
        stage = _normalize_enum(value.get("stage"), field="fan stage", allowed=FAN_STAGES)
        source = self._source_kind(claim.source_kind)
        source_ref = _claim_source_ref(claim)
        song_id = self._song_id(value.get("song_id"))
        return FanJourneySignal(
            claim_id=claim.id,
            sequence=claim.sequence,
            person_id=person_id,
            stage=stage,
            source_kind=source,
            source_ref=source_ref,
            confidence=self._confidence(claim.confidence),
            observed_at=_timestamp(value.get("observed_at")),
            song_id=song_id,
            note=_optional_text(value.get("note"), field="note", maximum=1200),
        )

    def _parse_consent(self, claim: EvidenceClaim, person_id: str) -> FanConsentEvidence:
        if claim.scope_kind != "ARTIST" or claim.scope_id != self.store.primary_artist_id:
            raise FanJourneyError("Fan consent evidence crossed Artist scope")
        if claim.twin_domain != "UNSPECIFIED":
            raise FanJourneyError("Fan consent evidence uses an unexpected Twin domain")
        value = claim.value
        if not isinstance(value, dict):
            raise FanJourneyError("Fan consent payload is malformed")
        if value.get("schema_version") != FAN_JOURNEY_SCHEMA_VERSION:
            raise FanJourneyError("unsupported Fan consent schema version")
        if value.get("kind") != "CONSENT" or value.get("person_id") != person_id:
            raise FanJourneyError("Fan consent binding is malformed")
        source = self._source_kind(claim.source_kind)
        if source not in _CONSENT_EVIDENCE_SOURCES:
            raise FanJourneyError("Fan consent evidence may not be inferred, remembered, or measured")
        source_ref = _claim_source_ref(claim)
        return FanConsentEvidence(
            claim_id=claim.id,
            sequence=claim.sequence,
            person_id=person_id,
            channel=_normalize_enum(
                value.get("channel"), field="consent channel", allowed=CONSENT_CHANNELS
            ),
            status=_normalize_enum(
                value.get("status"), field="consent status", allowed=CONSENT_STATES
            ),
            source_kind=source,
            source_ref=source_ref,
            observed_at=_timestamp(value.get("observed_at")),
            note=_optional_text(value.get("note"), field="note", maximum=1200),
        )

    @staticmethod
    def _fingerprint_payload(
        *,
        profile_id: str,
        artist_id: str,
        person_id: str,
        person_display_name: str,
        signals: tuple[FanJourneySignal, ...],
        consent: tuple[FanConsentEvidence, ...],
    ) -> str:
        payload = {
            "schema_version": FAN_JOURNEY_SCHEMA_VERSION,
            "profile_id": profile_id,
            "artist_id": artist_id,
            "person_id": person_id,
            "person_display_name": person_display_name,
            "signals": [signal.__dict__ for signal in signals],
            "consent": [item.__dict__ for item in consent],
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def snapshot(self, person_id: str) -> FanJourneySnapshot:
        person = self._person(person_id)
        signals: list[FanJourneySignal] = []
        consent: list[FanConsentEvidence] = []
        prefix = f"{FAN_EVIDENCE_PREFIX}.{person.id}."
        for claim in self._claims_for_person(person.id):
            suffix = claim.key[len(prefix) :]
            if suffix.startswith("signal."):
                signals.append(self._parse_signal(claim, person.id))
            elif suffix.startswith("consent."):
                consent.append(self._parse_consent(claim, person.id))
            else:
                raise FanJourneyError("unknown Fan Journey evidence kind in canonical namespace")
        ordered_signals = tuple(sorted(signals, key=lambda item: item.sequence))
        ordered_consent = tuple(sorted(consent, key=lambda item: item.sequence))
        fingerprint = self._fingerprint_payload(
            profile_id=self.store.profile_id,
            artist_id=self.store.primary_artist_id,
            person_id=person.id,
            person_display_name=person.display_name,
            signals=ordered_signals,
            consent=ordered_consent,
        )
        return FanJourneySnapshot(
            profile_id=self.store.profile_id,
            artist_id=self.store.primary_artist_id,
            person_id=person.id,
            person_display_name=person.display_name,
            signals=ordered_signals,
            consent_history=ordered_consent,
            fingerprint=fingerprint,
        )

    @classmethod
    def _snapshot_fingerprint(cls, snapshot: FanJourneySnapshot) -> str:
        return cls._fingerprint_payload(
            profile_id=snapshot.profile_id,
            artist_id=snapshot.artist_id,
            person_id=snapshot.person_id,
            person_display_name=snapshot.person_display_name,
            signals=snapshot.signals,
            consent=snapshot.consent_history,
        )

    def is_current(self, snapshot: FanJourneySnapshot) -> bool:
        if not isinstance(snapshot, FanJourneySnapshot):
            return False
        if snapshot.profile_id != self.store.profile_id:
            return False
        if snapshot.artist_id != self.store.primary_artist_id:
            return False
        if snapshot.authority != FAN_JOURNEY_AUTHORITY:
            return False
        if self._snapshot_fingerprint(snapshot) != snapshot.fingerprint:
            return False
        try:
            current = self.snapshot(snapshot.person_id)
        except (NotFoundError, ValidationError, FanJourneyError):
            return False
        return current.fingerprint == snapshot.fingerprint

    def assert_current(self, snapshot: FanJourneySnapshot) -> None:
        if not self.is_current(snapshot):
            raise StaleFanJourneyError(
                "Fan Journey evidence changed; refresh the relationship context before relying on it"
            )
