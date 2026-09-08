from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from .evidence import EvidenceClaim, EvidenceMemory
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from .people import PeopleMemory

OPPORTUNITY_SCHEMA_VERSION = 1
OPPORTUNITY_KEY_PREFIX = "business.opportunity."
CAPTURE_OPPORTUNITY_KEY_PREFIX = "capture.opportunity."
OPPORTUNITY_KINDS = {
    "COLLABORATION",
    "COMMISSION",
    "RELEASE",
    "PITCH",
    "PERFORMANCE",
    "SYNC",
    "JOB",
    "PARTNERSHIP",
    "GRANT",
    "PRESS",
    "DISTRIBUTION",
    "OTHER",
}
PROVENANCE_REQUIRED = {"OBSERVED", "MEASURED", "PROVIDER_VERIFIED", "INFERRED"}
OPPORTUNITY_AUTHORITY = "EVIDENCE_ONLY"


@dataclass(frozen=True)
class BusinessOpportunity:
    """One immutable, source-bound business/industry Opportunity object.

    This representation preserves source/context only. It is not a fit,
    readiness, value, priority, access, recommendation, acceptance, or success
    judgment and grants no external authority.
    """

    id: str
    profile_id: str
    artist_id: str
    song_id: str | None
    person_id: str | None
    kind: str
    summary: str
    deadline_on: str | None
    source_claim_id: str
    source_scope_kind: str
    source_scope_id: str
    source_kind: str
    source_ref: str | None
    source_current: bool
    representation_claim_id: str
    representation_sequence: int
    authority: str = OPPORTUNITY_AUTHORITY
    application_authority_granted: bool = field(default=False, init=False)
    messaging_authority_granted: bool = field(default=False, init=False)
    acceptance_authority_granted: bool = field(default=False, init=False)
    contract_authority_granted: bool = field(default=False, init=False)
    payment_authority_granted: bool = field(default=False, init=False)
    purchase_authority_granted: bool = field(default=False, init=False)
    scheduling_authority_granted: bool = field(default=False, init=False)
    publication_authority_granted: bool = field(default=False, init=False)
    provider_authority_granted: bool = field(default=False, init=False)
    external_action_authority_granted: bool = field(default=False, init=False)
    obligation_created: bool = field(default=False, init=False)
    followup_created: bool = field(default=False, init=False)
    fit_score: None = field(default=None, init=False)
    readiness_score: None = field(default=None, init=False)
    value_score: None = field(default=None, init=False)
    cost_score: None = field(default=None, init=False)
    priority_score: None = field(default=None, init=False)
    predicted_success: None = field(default=None, init=False)

    @property
    def attention_state(self) -> str:
        return "AVAILABLE" if self.source_current else "NEEDS_REVALIDATION"


class BusinessOpportunityService:
    """Source-bound business Opportunity substrate over canonical EvidenceMemory.

    The service normalizes independently supplied evidence into immutable objects.
    It does not discover feeds, rank opportunities, create obligations/follow-ups,
    contact people, apply, schedule, pay, contract, publish, or call providers.
    """

    _EXPECTED_PAYLOAD_FIELDS = {
        "schema_version",
        "opportunity_id",
        "song_id",
        "person_id",
        "kind",
        "summary",
        "deadline_on",
        "source_claim_id",
        "source_scope_kind",
        "source_scope_id",
        "source_kind",
        "source_ref",
    }

    def __init__(
        self,
        store: LineageStore,
        evidence: EvidenceMemory,
        people: PeopleMemory,
    ):
        if not isinstance(store, LineageStore):
            raise TypeError("BusinessOpportunityService requires canonical LineageStore")
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError(
                "BusinessOpportunityService requires EvidenceMemory on the same LineageStore"
            )
        if not isinstance(people, PeopleMemory) or people.store is not store:
            raise TypeError(
                "BusinessOpportunityService requires PeopleMemory on the same LineageStore"
            )
        self.store = store
        self.evidence = evidence
        self.people = people
        self._validate_existing()

    @staticmethod
    def _clean_text(value: str, field_name: str, *, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field_name} must be text")
        text = " ".join(value.split())
        if not text:
            raise ValidationError(f"{field_name} must not be empty")
        if len(text) > maximum:
            raise ValidationError(f"{field_name} is too long")
        return text

    @staticmethod
    def _normalize_kind(value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("business opportunity kind must be text")
        kind = value.strip().upper().replace("-", "_").replace(" ", "_")
        if kind not in OPPORTUNITY_KINDS:
            raise ValidationError(f"unsupported business opportunity kind: {kind}")
        return kind

    @staticmethod
    def _normalize_deadline(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError("deadline_on must be ISO date text or None")
        text = value.strip()
        if not text:
            raise ValidationError("deadline_on must not be blank")
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ValidationError(
                "deadline_on must be an ISO calendar date (YYYY-MM-DD)"
            ) from exc

    def _normalize_song(self, song_id: str | None) -> str | None:
        if song_id is None:
            return None
        if not isinstance(song_id, str) or not song_id.strip():
            raise ValidationError("song_id must be non-empty text or None")
        song = self.store.get_song(song_id.strip())
        if song is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        if song.artist_id != self.store.primary_artist_id:
            raise ValidationError("business opportunity Song belongs to a different Artist")
        return song.id

    def _normalize_person(self, person_id: str | None) -> str | None:
        if person_id is None:
            return None
        if not isinstance(person_id, str) or not person_id.strip():
            raise ValidationError("person_id must be non-empty text or None")
        person = self.people.get_person(person_id.strip())
        if person is None:
            raise NotFoundError(
                f"person not found in profile {self.store.profile_id}: {person_id}"
            )
        if person.artist_id != self.store.primary_artist_id:
            raise ValidationError("business opportunity person belongs to a different Artist")
        return person.id

    def _claim_is_active(self, claim: EvidenceClaim) -> bool:
        return any(
            active.id == claim.id
            for active in self.evidence.active_claims(
                claim.scope_kind,
                claim.scope_id,
                claim.key,
            )
        )

    @staticmethod
    def _require_provenance(claim: EvidenceClaim, *, purpose: str) -> None:
        if claim.source_kind in PROVENANCE_REQUIRED and not (
            isinstance(claim.source_ref, str) and claim.source_ref.strip()
        ):
            raise ValidationError(
                f"{purpose} {claim.source_kind} evidence requires source_ref provenance"
            )

    @staticmethod
    def _is_representation_key(key: str) -> bool:
        return key.startswith(OPPORTUNITY_KEY_PREFIX) or key.startswith(
            CAPTURE_OPPORTUNITY_KEY_PREFIX
        )

    def _claim_matches_target(
        self,
        claim: EvidenceClaim,
        *,
        song_id: str | None,
    ) -> bool:
        if claim.scope_kind == "PROFILE":
            return claim.scope_id == self.store.profile_id
        if claim.scope_kind == "ARTIST":
            return claim.scope_id == self.store.primary_artist_id
        if claim.scope_kind == "SONG":
            return song_id is not None and claim.scope_id == song_id
        if claim.scope_kind == "VERSION":
            if song_id is None:
                return False
            version = self.store.get_version(claim.scope_id)
            return version is not None and version.song_id == song_id
        return False

    def _require_source_claim(
        self,
        claim_id: str,
        *,
        song_id: str | None,
    ) -> EvidenceClaim:
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise ValidationError("source_claim_id must be non-empty text")
        claim = self.evidence.get_claim(claim_id.strip())
        if claim is None:
            raise NotFoundError(f"evidence claim not found: {claim_id}")
        if self._is_representation_key(claim.key):
            raise ValidationError(
                "business opportunities require independent non-opportunity source evidence"
            )
        if not self._claim_matches_target(claim, song_id=song_id):
            raise ValidationError(
                "source evidence scope does not match business opportunity context"
            )
        if not self._claim_is_active(claim):
            raise ValidationError(
                "business opportunity source must be currently active evidence"
            )
        self._require_provenance(claim, purpose="business opportunity source")
        return claim

    @staticmethod
    def _new_opportunity_id() -> str:
        return f"opportunity_{uuid.uuid4().hex}"

    @staticmethod
    def _payload(
        *,
        opportunity_id: str,
        song_id: str | None,
        person_id: str | None,
        kind: str,
        summary: str,
        deadline_on: str | None,
        source: EvidenceClaim,
    ) -> dict[str, object]:
        return {
            "schema_version": OPPORTUNITY_SCHEMA_VERSION,
            "opportunity_id": opportunity_id,
            "song_id": song_id,
            "person_id": person_id,
            "kind": kind,
            "summary": summary,
            "deadline_on": deadline_on,
            "source_claim_id": source.id,
            "source_scope_kind": source.scope_kind,
            "source_scope_id": source.scope_id,
            "source_kind": source.source_kind,
            "source_ref": source.source_ref,
        }

    def _representation_claims(self) -> tuple[EvidenceClaim, ...]:
        return tuple(
            claim
            for claim in self.evidence.active_claims_for_scope(
                "ARTIST",
                self.store.primary_artist_id,
            )
            if claim.key.startswith(OPPORTUNITY_KEY_PREFIX)
        )

    def _parse(
        self,
        claim: EvidenceClaim,
        *,
        corruption: bool = False,
    ) -> BusinessOpportunity:
        error = LineageCorruptionError if corruption else ValidationError
        value = claim.value
        if not isinstance(value, dict):
            raise error("business opportunity evidence payload must be an object")
        if set(value) != self._EXPECTED_PAYLOAD_FIELDS:
            raise error("business opportunity evidence payload shape is invalid")
        if value["schema_version"] != OPPORTUNITY_SCHEMA_VERSION:
            raise error("unsupported business opportunity schema version")

        opportunity_id = value["opportunity_id"]
        if not isinstance(opportunity_id, str) or not opportunity_id:
            raise error("business opportunity id is invalid")
        if claim.key != OPPORTUNITY_KEY_PREFIX + opportunity_id:
            raise error("business opportunity identity/key binding is invalid")
        if claim.scope_kind != "ARTIST" or claim.scope_id != self.store.primary_artist_id:
            raise error("business opportunity representation must remain Artist-scoped")
        if claim.source_kind != "INFERRED" or not claim.source_ref:
            raise error(
                "business opportunity representation must remain inferred from source evidence"
            )

        song_id = value["song_id"]
        person_id = value["person_id"]
        if song_id is not None and not isinstance(song_id, str):
            raise error("business opportunity song_id is invalid")
        if person_id is not None and not isinstance(person_id, str):
            raise error("business opportunity person_id is invalid")
        try:
            normalized_song = self._normalize_song(song_id)
            normalized_person = self._normalize_person(person_id)
            kind = self._normalize_kind(value["kind"])
            summary = self._clean_text(value["summary"], "summary", maximum=500)
            deadline_on = self._normalize_deadline(value["deadline_on"])
        except (ValidationError, NotFoundError) as exc:
            raise error(str(exc)) from exc
        if normalized_song != song_id or normalized_person != person_id:
            raise error("business opportunity context is not canonical")

        source_claim_id = value["source_claim_id"]
        if not isinstance(source_claim_id, str) or not source_claim_id:
            raise error("business opportunity source_claim_id is invalid")
        source = self.evidence.get_claim(source_claim_id)
        if source is None:
            raise error("business opportunity source evidence is missing")
        if self._is_representation_key(source.key):
            raise error("business opportunity source evidence is another opportunity representation")
        if not self._claim_matches_target(source, song_id=normalized_song):
            raise error("business opportunity source evidence scope is incompatible")
        try:
            self._require_provenance(source, purpose="business opportunity source")
        except ValidationError as exc:
            raise error(str(exc)) from exc

        if value["source_scope_kind"] != source.scope_kind:
            raise error("business opportunity source scope kind was rewritten")
        if value["source_scope_id"] != source.scope_id:
            raise error("business opportunity source scope id was rewritten")
        if value["source_kind"] != source.source_kind:
            raise error("business opportunity source kind was rewritten")
        if value["source_ref"] != source.source_ref:
            raise error("business opportunity source provenance was rewritten")
        if claim.source_ref != source.id:
            raise error("business opportunity representation source binding is invalid")

        return BusinessOpportunity(
            id=opportunity_id,
            profile_id=self.store.profile_id,
            artist_id=self.store.primary_artist_id,
            song_id=normalized_song,
            person_id=normalized_person,
            kind=kind,
            summary=summary,
            deadline_on=deadline_on,
            source_claim_id=source.id,
            source_scope_kind=source.scope_kind,
            source_scope_id=source.scope_id,
            source_kind=source.source_kind,
            source_ref=source.source_ref,
            source_current=self._claim_is_active(source),
            representation_claim_id=claim.id,
            representation_sequence=claim.sequence,
        )

    def _objects(self, *, corruption: bool = False) -> tuple[BusinessOpportunity, ...]:
        objects = tuple(
            self._parse(claim, corruption=corruption)
            for claim in self._representation_claims()
        )
        ids = [item.id for item in objects]
        if len(ids) != len(set(ids)):
            error = LineageCorruptionError if corruption else ValidationError
            raise error("duplicate business opportunity identity detected")
        return objects

    def _validate_existing(self) -> None:
        try:
            self._objects(corruption=True)
        except LineageCorruptionError:
            raise
        except Exception as exc:
            raise LineageCorruptionError(
                "business opportunity evidence is unreadable or corrupt"
            ) from exc

    def opportunities(self) -> tuple[BusinessOpportunity, ...]:
        return self._objects()

    def get(self, opportunity_id: str) -> BusinessOpportunity | None:
        if not isinstance(opportunity_id, str) or not opportunity_id.strip():
            raise ValidationError("opportunity_id must be non-empty text")
        normalized = opportunity_id.strip()
        matches = tuple(item for item in self._objects() if item.id == normalized)
        if not matches:
            return None
        if len(matches) != 1:
            raise LineageCorruptionError("duplicate business opportunity identity detected")
        return matches[0]

    def for_song(self, song_id: str) -> tuple[BusinessOpportunity, ...]:
        normalized_song = self._normalize_song(song_id)
        assert normalized_song is not None
        return tuple(item for item in self._objects() if item.song_id == normalized_song)

    def for_person(self, person_id: str) -> tuple[BusinessOpportunity, ...]:
        normalized_person = self._normalize_person(person_id)
        assert normalized_person is not None
        return tuple(item for item in self._objects() if item.person_id == normalized_person)

    def create(
        self,
        *,
        kind: str,
        summary: str,
        source_claim_id: str,
        song_id: str | None = None,
        person_id: str | None = None,
        deadline_on: str | None = None,
    ) -> BusinessOpportunity:
        normalized_kind = self._normalize_kind(kind)
        normalized_summary = self._clean_text(summary, "summary", maximum=500)
        normalized_song = self._normalize_song(song_id)
        normalized_person = self._normalize_person(person_id)
        normalized_deadline = self._normalize_deadline(deadline_on)
        source = self._require_source_claim(
            source_claim_id,
            song_id=normalized_song,
        )

        semantic_key = (
            source.id,
            normalized_song,
            normalized_person,
            normalized_kind,
            normalized_summary,
            normalized_deadline,
        )
        for existing in self._objects():
            existing_key = (
                existing.source_claim_id,
                existing.song_id,
                existing.person_id,
                existing.kind,
                existing.summary,
                existing.deadline_on,
            )
            if existing_key == semantic_key:
                raise ValidationError(
                    "semantically duplicate business opportunity already exists"
                )

        opportunity_id = self._new_opportunity_id()
        payload = self._payload(
            opportunity_id=opportunity_id,
            song_id=normalized_song,
            person_id=normalized_person,
            kind=normalized_kind,
            summary=normalized_summary,
            deadline_on=normalized_deadline,
            source=source,
        )
        representation = self.evidence.record_claim(
            scope_kind="ARTIST",
            scope_id=self.store.primary_artist_id,
            key=OPPORTUNITY_KEY_PREFIX + opportunity_id,
            value=payload,
            source_kind="INFERRED",
            source_ref=source.id,
            confidence=source.confidence,
            twin_domain="UNSPECIFIED",
        )
        result = self._parse(representation)
        if result.id != opportunity_id:
            raise LineageCorruptionError("new business opportunity identity changed on read-back")
        return result
