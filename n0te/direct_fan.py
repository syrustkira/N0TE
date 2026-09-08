from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from .evidence import EvidenceClaim, EvidenceMemory, SOURCE_KINDS
from .fan_journey import CONSENT_CHANNELS, FanJourneyService
from .lineage import LineageStore, NotFoundError, ValidationError
from .people import PeopleMemory, Person

DIRECT_FAN_SCHEMA_VERSION = 1
DIRECT_FAN_EVIDENCE_PREFIX = "audience.direct_fan"
CONTACT_PURPOSES = ("RELEASE_NOTIFICATION", "PRE_SAVE_INVITE")
INTENT_STATES = (
    "REVIEWABLE",
    "NO_CONTACT_POINT",
    "NO_CURRENT_CONSENT",
    "CONSENT_REVOKED",
    "CONSENT_CHANGED",
    "CONTACT_CHANGED",
)
DIRECT_FAN_AUTHORITY = "REVIEW_ONLY"

_SELF_RECORDABLE_CONTACT_SOURCES = {"USER_DECLARED", "OBSERVED"}
_CONTACT_EVIDENCE_SOURCES = {"USER_DECLARED", "OBSERVED", "PROVIDER_VERIFIED"}
_SOURCE_REF_REQUIRED = {"OBSERVED", "PROVIDER_VERIFIED"}


class DirectFanError(RuntimeError):
    """Direct Fan evidence could not be represented truthfully."""


def _clean_text(value: object, *, field: str, maximum: int) -> str:
    if value is None:
        raise ValidationError(f"{field} must not be empty")
    text = " ".join(str(value).split())
    if not text:
        raise ValidationError(f"{field} must not be empty")
    if len(text) > maximum:
        raise ValidationError(f"{field} is too long")
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


def _normalize_enum(value: object, *, field: str, allowed: tuple[str, ...]) -> str:
    text = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    if text not in allowed:
        raise ValidationError(f"unsupported {field}: {text}")
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


@dataclass(frozen=True)
class DirectFanContactPoint:
    claim_id: str
    sequence: int
    person_id: str
    channel: str
    endpoint: str
    source_kind: str
    source_ref: str | None
    observed_at: str | None
    note: str | None

    @property
    def identity_verified(self) -> bool:
        return False

    @property
    def consent_granted(self) -> bool:
        return False

    @property
    def contact_authority_granted(self) -> bool:
        return False

    @property
    def marketing_permission_granted(self) -> bool:
        return False


@dataclass(frozen=True)
class DirectFanContactIntent:
    claim_id: str
    sequence: int
    person_id: str
    song_id: str
    channel: str
    purpose: str
    contact_claim_id: str
    consent_claim_id: str
    note: str | None

    @property
    def send_authority_granted(self) -> bool:
        return False

    @property
    def scheduling_authority_granted(self) -> bool:
        return False

    @property
    def provider_authority_granted(self) -> bool:
        return False

    @property
    def publication_authority_granted(self) -> bool:
        return False

    @property
    def spend_authority_granted(self) -> bool:
        return False


@dataclass(frozen=True)
class DirectFanIntentAssessment:
    intent: DirectFanContactIntent
    state: str
    current_contact_claim_id: str | None
    current_consent_claim_id: str | None
    current_consent_status: str
    authority: str = DIRECT_FAN_AUTHORITY

    @property
    def reviewable(self) -> bool:
        return self.state == "REVIEWABLE"

    @property
    def separate_authorization_required(self) -> bool:
        return True

    @property
    def send_authority_granted(self) -> bool:
        return False

    @property
    def scheduling_authority_granted(self) -> bool:
        return False

    @property
    def provider_authority_granted(self) -> bool:
        return False

    @property
    def delivery_verified(self) -> bool:
        return False

    @property
    def pre_save_verified(self) -> bool:
        return False


class DirectFanService:
    """Consent-bound Direct Fan contact intent over canonical People + Evidence.

    Contact points, channel consent and outbound intent remain three different
    facts. This service can make an intent reviewable; it never sends a message,
    schedules a notification, publishes a smart link, performs a pre-save,
    calls a provider, spends money, or turns consent into execution authority.
    """

    def __init__(
        self,
        store: LineageStore,
        people: PeopleMemory,
        evidence: EvidenceMemory,
        fan_journey: FanJourneyService | None = None,
    ) -> None:
        if not isinstance(store, LineageStore):
            raise TypeError("DirectFanService requires LineageStore")
        if not isinstance(people, PeopleMemory) or people.store is not store:
            raise TypeError("DirectFanService requires PeopleMemory for the same LineageStore")
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError("DirectFanService requires EvidenceMemory for the same LineageStore")
        journey = fan_journey or FanJourneyService(store, people, evidence)
        if not isinstance(journey, FanJourneyService) or journey.store is not store:
            raise TypeError("DirectFanService requires FanJourneyService for the same LineageStore")
        self.store = store
        self.people = people
        self.evidence = evidence
        self.fan_journey = journey

    def _person(self, person_id: str) -> Person:
        person = self.people.get_person(str(person_id).strip())
        if person is None:
            raise NotFoundError(f"person not found: {person_id}")
        if person.artist_id != self.store.primary_artist_id:
            raise ValidationError("Direct Fan person belongs to a different Artist")
        return person

    def _song_id(self, song_id: str) -> str:
        value = str(song_id).strip()
        song = self.store.get_song(value)
        if song is None:
            raise NotFoundError(f"Song not found: {value}")
        if song.artist_id != self.store.primary_artist_id:
            raise ValidationError("Direct Fan Song belongs to a different Artist")
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
            raise ValidationError(f"{source_kind} Direct Fan evidence requires source_ref")
        return source_ref

    @staticmethod
    def _event_key(person_id: str, kind: str) -> str:
        return f"{DIRECT_FAN_EVIDENCE_PREFIX}.{person_id}.{kind}.{uuid.uuid4().hex}"

    def record_contact_point(
        self,
        person_id: str,
        channel: str,
        endpoint: str,
        *,
        source_kind: str,
        source_ref: str | None = None,
        observed_at: str | None = None,
        note: str | None = None,
    ) -> EvidenceClaim:
        person = self._person(person_id)
        normalized_channel = _normalize_enum(
            channel, field="contact channel", allowed=CONSENT_CHANNELS
        )
        source = self._source_kind(source_kind)
        if source == "PROVIDER_VERIFIED":
            raise ValidationError(
                "DirectFanService cannot self-issue PROVIDER_VERIFIED contact evidence; "
                "consume verifier-backed canonical Evidence instead"
            )
        if source not in _SELF_RECORDABLE_CONTACT_SOURCES:
            raise ValidationError(
                "contact points may be recorded only from explicit USER_DECLARED or OBSERVED evidence; "
                "they may never be inferred, remembered, or measured"
            )
        payload = {
            "schema_version": DIRECT_FAN_SCHEMA_VERSION,
            "kind": "CONTACT_POINT",
            "person_id": person.id,
            "channel": normalized_channel,
            "endpoint": _clean_text(endpoint, field="endpoint", maximum=500),
            "observed_at": _timestamp(observed_at),
            "note": _optional_text(note, field="note", maximum=1200),
        }
        return self.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=self.store.primary_artist_id,
            key=self._event_key(person.id, "contact"),
            value=payload,
            source_kind=source,
            source_ref=self._source_ref(source, source_ref),
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )

    def _claims_for_person(self, person_id: str) -> tuple[EvidenceClaim, ...]:
        prefix = f"{DIRECT_FAN_EVIDENCE_PREFIX}.{person_id}."
        return tuple(
            claim
            for claim in self.evidence.active_claims_for_scope(
                "ARTIST", self.store.primary_artist_id
            )
            if claim.key.startswith(prefix)
        )

    def _parse_contact(self, claim: EvidenceClaim, person_id: str) -> DirectFanContactPoint:
        expected_prefix = f"{DIRECT_FAN_EVIDENCE_PREFIX}.{person_id}.contact."
        if not claim.key.startswith(expected_prefix):
            raise DirectFanError("Direct Fan contact key binding is malformed")
        if claim.scope_kind != "ARTIST" or claim.scope_id != self.store.primary_artist_id:
            raise DirectFanError("Direct Fan contact evidence crossed Artist scope")
        if claim.twin_domain != "UNSPECIFIED":
            raise DirectFanError("Direct Fan contact evidence uses an unexpected Twin domain")
        value = claim.value
        if not isinstance(value, dict):
            raise DirectFanError("Direct Fan contact payload is malformed")
        if value.get("schema_version") != DIRECT_FAN_SCHEMA_VERSION:
            raise DirectFanError("unsupported Direct Fan contact schema version")
        if value.get("kind") != "CONTACT_POINT" or value.get("person_id") != person_id:
            raise DirectFanError("Direct Fan contact binding is malformed")
        source = self._source_kind(claim.source_kind)
        if source not in _CONTACT_EVIDENCE_SOURCES:
            raise DirectFanError("Direct Fan contact evidence may not be inferred, remembered, or measured")
        source_ref = None if claim.source_ref is None else str(claim.source_ref).strip() or None
        if source in _SOURCE_REF_REQUIRED and source_ref is None:
            raise DirectFanError("Direct Fan observed/provider contact evidence requires provenance")
        return DirectFanContactPoint(
            claim_id=claim.id,
            sequence=claim.sequence,
            person_id=person_id,
            channel=_normalize_enum(
                value.get("channel"), field="contact channel", allowed=CONSENT_CHANNELS
            ),
            endpoint=_clean_text(value.get("endpoint"), field="endpoint", maximum=500),
            source_kind=source,
            source_ref=source_ref,
            observed_at=_timestamp(value.get("observed_at")),
            note=_optional_text(value.get("note"), field="note", maximum=1200),
        )

    def contact_history(self, person_id: str) -> tuple[DirectFanContactPoint, ...]:
        person = self._person(person_id)
        contacts: list[DirectFanContactPoint] = []
        for claim in self._claims_for_person(person.id):
            value = claim.value
            if isinstance(value, dict) and value.get("kind") == "CONTACT_POINT":
                contacts.append(self._parse_contact(claim, person.id))
            elif isinstance(value, dict) and value.get("kind") == "CONTACT_INTENT":
                self._parse_intent(claim, person.id)
            else:
                raise DirectFanError("Direct Fan owned namespace contains malformed evidence")
        return tuple(sorted(contacts, key=lambda item: item.sequence))

    def current_contact_point(
        self, person_id: str, channel: str
    ) -> DirectFanContactPoint | None:
        normalized = _normalize_enum(
            channel, field="contact channel", allowed=CONSENT_CHANNELS
        )
        matches = [
            item for item in self.contact_history(person_id) if item.channel == normalized
        ]
        return None if not matches else max(matches, key=lambda item: item.sequence)

    def record_contact_intent(
        self,
        person_id: str,
        song_id: str,
        channel: str,
        purpose: str,
        *,
        note: str | None = None,
    ) -> EvidenceClaim:
        person = self._person(person_id)
        normalized_song = self._song_id(song_id)
        normalized_channel = _normalize_enum(
            channel, field="contact channel", allowed=CONSENT_CHANNELS
        )
        normalized_purpose = _normalize_enum(
            purpose, field="contact purpose", allowed=CONTACT_PURPOSES
        )
        contact = self.current_contact_point(person.id, normalized_channel)
        if contact is None:
            raise ValidationError(
                "Direct Fan contact intent requires an explicit current contact point"
            )
        fan = self.fan_journey.snapshot(person.id)
        consent = fan.consent_evidence(normalized_channel)
        if consent is None or consent.status != "OPTED_IN":
            raise ValidationError(
                "Direct Fan contact intent requires explicit current OPTED_IN consent for the channel"
            )
        duplicate = [
            intent
            for intent in self.intents_for_person(person.id)
            if intent.song_id == normalized_song
            and intent.channel == normalized_channel
            and intent.purpose == normalized_purpose
            and intent.contact_claim_id == contact.claim_id
            and intent.consent_claim_id == consent.claim_id
        ]
        if duplicate:
            raise ValidationError(
                "an equivalent Direct Fan contact intent already exists for this exact contact and consent evidence"
            )
        payload = {
            "schema_version": DIRECT_FAN_SCHEMA_VERSION,
            "kind": "CONTACT_INTENT",
            "person_id": person.id,
            "song_id": normalized_song,
            "channel": normalized_channel,
            "purpose": normalized_purpose,
            "contact_claim_id": contact.claim_id,
            "consent_claim_id": consent.claim_id,
            "note": _optional_text(note, field="note", maximum=1200),
        }
        return self.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=self.store.primary_artist_id,
            key=self._event_key(person.id, "intent"),
            value=payload,
            source_kind="USER_DECLARED",
            source_ref=None,
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )

    def _parse_intent(self, claim: EvidenceClaim, person_id: str) -> DirectFanContactIntent:
        expected_prefix = f"{DIRECT_FAN_EVIDENCE_PREFIX}.{person_id}.intent."
        if not claim.key.startswith(expected_prefix):
            raise DirectFanError("Direct Fan intent key binding is malformed")
        if claim.scope_kind != "ARTIST" or claim.scope_id != self.store.primary_artist_id:
            raise DirectFanError("Direct Fan intent evidence crossed Artist scope")
        if claim.twin_domain != "UNSPECIFIED":
            raise DirectFanError("Direct Fan intent evidence uses an unexpected Twin domain")
        if claim.source_kind != "USER_DECLARED":
            raise DirectFanError("Direct Fan contact intent must remain explicit USER_DECLARED evidence")
        value = claim.value
        if not isinstance(value, dict):
            raise DirectFanError("Direct Fan intent payload is malformed")
        if value.get("schema_version") != DIRECT_FAN_SCHEMA_VERSION:
            raise DirectFanError("unsupported Direct Fan intent schema version")
        if value.get("kind") != "CONTACT_INTENT" or value.get("person_id") != person_id:
            raise DirectFanError("Direct Fan intent binding is malformed")
        song_id = self._song_id(
            _clean_text(value.get("song_id"), field="song_id", maximum=200)
        )
        channel = _normalize_enum(
            value.get("channel"), field="contact channel", allowed=CONSENT_CHANNELS
        )
        purpose = _normalize_enum(
            value.get("purpose"), field="contact purpose", allowed=CONTACT_PURPOSES
        )
        contact_claim_id = _clean_text(
            value.get("contact_claim_id"), field="contact_claim_id", maximum=200
        )
        consent_claim_id = _clean_text(
            value.get("consent_claim_id"), field="consent_claim_id", maximum=200
        )
        contact_claim = self.evidence.get_claim(contact_claim_id)
        if contact_claim is None:
            raise DirectFanError("Direct Fan intent points to missing contact evidence")
        parsed_contact = self._parse_contact(contact_claim, person_id)
        if parsed_contact.channel != channel:
            raise DirectFanError("Direct Fan intent contact channel binding is malformed")
        consent_claim = self.evidence.get_claim(consent_claim_id)
        if consent_claim is None:
            raise DirectFanError("Direct Fan intent points to missing consent evidence")
        fan = self.fan_journey.snapshot(person_id)
        matching_consent = [
            item for item in fan.consent_history if item.claim_id == consent_claim_id
        ]
        if not matching_consent or matching_consent[0].channel != channel:
            raise DirectFanError("Direct Fan intent consent binding is malformed")
        if matching_consent[0].status != "OPTED_IN":
            raise DirectFanError("Direct Fan intent was bound to non-opt-in consent evidence")
        return DirectFanContactIntent(
            claim_id=claim.id,
            sequence=claim.sequence,
            person_id=person_id,
            song_id=song_id,
            channel=channel,
            purpose=purpose,
            contact_claim_id=contact_claim_id,
            consent_claim_id=consent_claim_id,
            note=_optional_text(value.get("note"), field="note", maximum=1200),
        )

    def intents_for_person(self, person_id: str) -> tuple[DirectFanContactIntent, ...]:
        person = self._person(person_id)
        intents: list[DirectFanContactIntent] = []
        for claim in self._claims_for_person(person.id):
            value = claim.value
            if isinstance(value, dict) and value.get("kind") == "CONTACT_INTENT":
                intents.append(self._parse_intent(claim, person.id))
            elif isinstance(value, dict) and value.get("kind") == "CONTACT_POINT":
                self._parse_contact(claim, person.id)
            else:
                raise DirectFanError("Direct Fan owned namespace contains malformed evidence")
        return tuple(sorted(intents, key=lambda item: item.sequence))

    def get_intent(self, claim_id: str) -> DirectFanContactIntent:
        claim = self.evidence.get_claim(str(claim_id).strip())
        if claim is None:
            raise NotFoundError(f"Direct Fan contact intent not found: {claim_id}")
        value = claim.value
        if not isinstance(value, dict) or value.get("kind") != "CONTACT_INTENT":
            raise ValidationError("evidence claim is not a Direct Fan contact intent")
        person_id = _clean_text(value.get("person_id"), field="person_id", maximum=200)
        self._person(person_id)
        return self._parse_intent(claim, person_id)

    def assess_intent(self, claim_id: str) -> DirectFanIntentAssessment:
        intent = self.get_intent(claim_id)
        current_contact = self.current_contact_point(intent.person_id, intent.channel)
        fan = self.fan_journey.snapshot(intent.person_id)
        current_consent = fan.consent_evidence(intent.channel)
        consent_status = "UNKNOWN" if current_consent is None else current_consent.status

        if current_consent is None:
            state = "NO_CURRENT_CONSENT"
        elif current_consent.status == "OPTED_OUT":
            state = "CONSENT_REVOKED"
        elif current_consent.claim_id != intent.consent_claim_id:
            state = "CONSENT_CHANGED"
        elif current_contact is None:
            state = "NO_CONTACT_POINT"
        elif current_contact.claim_id != intent.contact_claim_id:
            state = "CONTACT_CHANGED"
        else:
            state = "REVIEWABLE"
        if state not in INTENT_STATES:
            raise DirectFanError(f"unsupported Direct Fan intent state: {state}")
        return DirectFanIntentAssessment(
            intent=intent,
            state=state,
            current_contact_claim_id=(
                None if current_contact is None else current_contact.claim_id
            ),
            current_consent_claim_id=(
                None if current_consent is None else current_consent.claim_id
            ),
            current_consent_status=consent_status,
        )

    def reviewable_intents(
        self, person_id: str | None = None
    ) -> tuple[DirectFanIntentAssessment, ...]:
        people = (self._person(person_id),) if person_id is not None else self.people.people()
        assessments: list[DirectFanIntentAssessment] = []
        for person in people:
            for intent in self.intents_for_person(person.id):
                assessment = self.assess_intent(intent.claim_id)
                if assessment.reviewable:
                    assessments.append(assessment)
        return tuple(assessments)
