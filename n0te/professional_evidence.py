from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Iterable

from .career_roles import ROLE_EVIDENCE_KINDS, RoleEvidence
from .evidence import EvidenceClaim, EvidenceMemory
from .lineage import LineageStore, NotFoundError, ValidationError

PROFESSIONAL_EVIDENCE_SCHEMA_VERSION = 1
PROFESSIONAL_EVIDENCE_KINDS = {
    "CREDIT",
    "WORK_SAMPLE",
    "CASE_STUDY",
    "TESTIMONIAL",
    "REFERRAL",
    "RELIABILITY",
}
PROFESSIONAL_EVIDENCE_SOURCE_KINDS = {
    "USER_DECLARED",
    "OBSERVED",
    "PROVIDER_VERIFIED",
}
PROFESSIONAL_EVIDENCE_STATES = {"ACTIVE", "DISPUTED", "WITHDRAWN"}
PROFESSIONAL_SHARE_SCOPES = {"PRIVATE", "OPPORTUNITY", "PUBLIC"}
PROFESSIONAL_REVISION_KINDS = {
    "CREATE",
    "CORRECTION",
    "DISPUTE",
    "WITHDRAWAL",
    "RESTORE",
}
PERMISSION_REQUIRED_KINDS = {
    "WORK_SAMPLE",
    "CASE_STUDY",
    "TESTIMONIAL",
    "REFERRAL",
}
VERIFIED_PROFESSIONAL_SOURCE_KINDS = {"OBSERVED", "PROVIDER_VERIFIED"}

_KEY_PREFIX = "professional.evidence."
_ID_PATTERN = re.compile(r"^pe_[a-f0-9]{32}$")
_ROLE_PART = re.compile(r"[^A-Z0-9_]+")
_UNSET = object()

_MAX_ROLES = 16
_MAX_ROLE_CHARS = 80
_MAX_TITLE_CHARS = 240
_MAX_STATEMENT_CHARS = 8_000
_MAX_REF_CHARS = 2_048
_MAX_REASON_CHARS = 2_000

_CAREER_SOURCE_KIND = {
    "USER_DECLARED": "ARTIST_DECLARED",
    "OBSERVED": "OBSERVED",
    "PROVIDER_VERIFIED": "VERIFIED_EXTERNAL",
}


class ProfessionalEvidenceError(RuntimeError):
    """A professional-evidence operation cannot proceed truthfully."""


class ProfessionalEvidenceIntegrityError(ProfessionalEvidenceError):
    """N0TE-owned professional evidence no longer has canonical lineage."""


@dataclass(frozen=True)
class ProfessionalEvidenceRecord:
    evidence_id: str
    revision_claim_id: str
    sequence: int
    roles: tuple[str, ...]
    kind: str
    title: str
    statement: str
    evidence_source_kind: str
    evidence_source_ref: str
    share_scope: str
    permission_source_kind: str | None
    permission_source_ref: str | None
    confidential: bool
    state: str
    song_id: str | None
    version_id: str | None
    role_evidence_kind: str | None
    revision_kind: str
    revision_reason: str | None
    revision_source_kind: str
    revision_source_ref: str

    @property
    def verified(self) -> bool:
        return self.evidence_source_kind in VERIFIED_PROFESSIONAL_SOURCE_KINDS

    @property
    def permission_verified(self) -> bool:
        return self.permission_source_kind in VERIFIED_PROFESSIONAL_SOURCE_KINDS


class ProfessionalEvidenceService:
    """Portable, source-bound professional history over canonical EvidenceMemory.

    Each item owns one profile-scoped Evidence key. Revisions are immutable
    Evidence claims connected by canonical supersession. User-facing inputs are
    type-strict: non-text values never become plausible evidence by implicit
    string coercion.
    """

    _PAYLOAD_KEYS = {
        "schema_version",
        "evidence_id",
        "roles",
        "kind",
        "title",
        "statement",
        "evidence_source_kind",
        "evidence_source_ref",
        "share_scope",
        "permission_source_kind",
        "permission_source_ref",
        "confidential",
        "state",
        "song_id",
        "version_id",
        "role_evidence_kind",
        "revision_kind",
        "revision_reason",
    }

    def __init__(self, store: LineageStore, evidence: EvidenceMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("ProfessionalEvidenceService requires LineageStore")
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError(
                "ProfessionalEvidenceService requires EvidenceMemory for the same LineageStore"
            )
        self.store = store
        self.evidence = evidence

    @staticmethod
    def _new_evidence_id() -> str:
        return f"pe_{uuid.uuid4().hex}"

    @staticmethod
    def _required_text(value: object, *, field: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field} must be text")
        text = value.strip()
        if not text:
            raise ValidationError(f"{field} must not be empty")
        if len(text) > maximum:
            raise ValidationError(f"{field} exceeds the local professional-evidence limit")
        return text

    @classmethod
    def _clean_ref(cls, value: object, *, field: str) -> str:
        return cls._required_text(value, field=field, maximum=_MAX_REF_CHARS)

    @classmethod
    def _optional_ref(cls, value: object | None, *, field: str) -> str | None:
        if value is None:
            return None
        return cls._clean_ref(value, field=field)

    @staticmethod
    def _key(evidence_id: object) -> str:
        if not isinstance(evidence_id, str):
            raise ValidationError("professional evidence id must be text")
        value = evidence_id.strip()
        if not _ID_PATTERN.fullmatch(value):
            raise ValidationError("invalid professional evidence id")
        return _KEY_PREFIX + value

    @staticmethod
    def _evidence_id_from_key(key: object) -> str:
        if not isinstance(key, str):
            raise ValidationError("professional evidence key must be text")
        if not key.startswith(_KEY_PREFIX):
            raise ValidationError("evidence claim is not professional evidence")
        evidence_id = key[len(_KEY_PREFIX) :]
        if not _ID_PATTERN.fullmatch(evidence_id):
            raise ProfessionalEvidenceIntegrityError(
                "professional evidence key has an invalid identity"
            )
        return evidence_id

    @classmethod
    def _canonical_roles(cls, roles: Iterable[str]) -> tuple[str, ...]:
        if isinstance(roles, (str, bytes)):
            raise ValidationError("professional evidence roles must be a collection")
        try:
            values = tuple(roles)
        except TypeError as exc:
            raise ValidationError("professional evidence roles must be a collection") from exc
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValidationError("professional evidence role must be text")
            role = value.strip().upper().replace("-", "_").replace(" ", "_")
            role = _ROLE_PART.sub("", role)
            role = re.sub(r"_+", "_", role).strip("_")
            if not role:
                raise ValidationError("professional evidence role must not be empty")
            if len(role) > _MAX_ROLE_CHARS:
                raise ValidationError("professional evidence role is too long")
            normalized.append(role)
        canonical = tuple(sorted(set(normalized)))
        if not canonical:
            raise ValidationError("professional evidence requires at least one role")
        if len(canonical) > _MAX_ROLES:
            raise ValidationError("professional evidence has too many roles")
        return canonical

    @staticmethod
    def _token(value: object, *, field: str, allowed: set[str]) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field} must be text")
        token = value.strip().upper().replace("-", "_").replace(" ", "_")
        if token not in allowed:
            raise ValidationError(f"unsupported {field}: {token}")
        return token

    @classmethod
    def _normalize_kind(cls, value: object) -> str:
        return cls._token(
            value,
            field="professional evidence kind",
            allowed=PROFESSIONAL_EVIDENCE_KINDS,
        )

    @classmethod
    def _normalize_source_kind(cls, value: object) -> str:
        return cls._token(
            value,
            field="professional evidence source",
            allowed=PROFESSIONAL_EVIDENCE_SOURCE_KINDS,
        )

    @classmethod
    def _optional_source_kind(cls, value: object | None) -> str | None:
        if value is None:
            return None
        return cls._normalize_source_kind(value)

    @classmethod
    def _normalize_share_scope(cls, value: object) -> str:
        return cls._token(
            value,
            field="professional evidence share scope",
            allowed=PROFESSIONAL_SHARE_SCOPES,
        )

    @classmethod
    def _normalize_role_evidence_kind(cls, value: object | None) -> str | None:
        if value is None:
            return None
        return cls._token(
            value,
            field="career role evidence kind",
            allowed=ROLE_EVIDENCE_KINDS,
        )

    @classmethod
    def _normalize_state(cls, value: object) -> str:
        return cls._token(
            value,
            field="professional evidence state",
            allowed=PROFESSIONAL_EVIDENCE_STATES,
        )

    @classmethod
    def _normalize_revision_kind(cls, value: object) -> str:
        return cls._token(
            value,
            field="professional evidence revision",
            allowed=PROFESSIONAL_REVISION_KINDS,
        )

    @staticmethod
    def _optional_work_id(value: object | None, *, field: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError(f"professional evidence {field} must be text")
        return value.strip() or None

    def _validate_work_binding(
        self, song_id: object | None, version_id: object | None
    ) -> tuple[str | None, str | None]:
        song = self._optional_work_id(song_id, field="Song id")
        version = self._optional_work_id(version_id, field="Version id")
        if version is not None and song is None:
            raise ValidationError("professional evidence Version requires an exact Song")
        if song is not None and self.store.get_song(song) is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song}"
            )
        if version is not None:
            row = self.store.get_version(version)
            if row is None:
                raise NotFoundError(f"version not found: {version}")
            if row.song_id != song:
                raise ValidationError(
                    "professional evidence Version belongs to a different Song"
                )
        return song, version

    @classmethod
    def _validate_permission(
        cls,
        *,
        kind: str,
        source_kind: str,
        share_scope: str,
        permission_source_kind: str | None,
        permission_source_ref: str | None,
        confidential: object,
    ) -> None:
        if not isinstance(confidential, bool):
            raise ValidationError("professional evidence confidential flag must be boolean")
        if confidential and share_scope != "PRIVATE":
            raise ValidationError("confidential professional evidence must remain PRIVATE")
        if (permission_source_kind is None) != (permission_source_ref is None):
            raise ValidationError(
                "professional evidence permission source kind and ref must be supplied together"
            )
        if kind in PERMISSION_REQUIRED_KINDS and share_scope != "PRIVATE":
            if permission_source_ref is None:
                raise ValidationError(
                    f"{kind} requires explicit permission evidence before external reuse"
                )
            if permission_source_kind not in VERIFIED_PROFESSIONAL_SOURCE_KINDS:
                raise ValidationError(
                    f"{kind} external reuse requires observed or provider-verified permission"
                )
        if (
            kind in {"TESTIMONIAL", "REFERRAL"}
            and share_scope != "PRIVATE"
            and source_kind == "USER_DECLARED"
        ):
            raise ValidationError(
                f"artist-declared {kind.lower()} cannot be externally reused as verified evidence"
            )

    def _canonical_payload(
        self,
        *,
        evidence_id: object,
        roles: Iterable[str],
        kind: object,
        title: object,
        statement: object,
        evidence_source_kind: object,
        evidence_source_ref: object,
        share_scope: object,
        permission_source_kind: object | None,
        permission_source_ref: object | None,
        confidential: object,
        state: object,
        song_id: object | None,
        version_id: object | None,
        role_evidence_kind: object | None,
        revision_kind: object,
        revision_reason: object | None,
    ) -> dict[str, object]:
        evidence_id = self._key(evidence_id)[len(_KEY_PREFIX) :]
        canonical_roles = self._canonical_roles(roles)
        kind = self._normalize_kind(kind)
        title = self._required_text(
            title, field="professional evidence title", maximum=_MAX_TITLE_CHARS
        )
        statement = self._required_text(
            statement,
            field="professional evidence statement",
            maximum=_MAX_STATEMENT_CHARS,
        )
        source_kind = self._normalize_source_kind(evidence_source_kind)
        source_ref = self._clean_ref(
            evidence_source_ref, field="professional evidence source_ref"
        )
        share_scope = self._normalize_share_scope(share_scope)
        permission_kind = self._optional_source_kind(permission_source_kind)
        permission_ref = self._optional_ref(
            permission_source_ref,
            field="professional evidence permission_source_ref",
        )
        state = self._normalize_state(state)
        role_kind = self._normalize_role_evidence_kind(role_evidence_kind)
        revision = self._normalize_revision_kind(revision_kind)
        if revision == "CREATE":
            if revision_reason is not None:
                raise ValidationError("CREATE professional evidence cannot have a revision reason")
            if state != "ACTIVE":
                raise ValidationError("new professional evidence must start ACTIVE")
            reason = None
        else:
            reason = self._required_text(
                revision_reason,
                field="professional evidence revision reason",
                maximum=_MAX_REASON_CHARS,
            )
            expected_state = {
                "CORRECTION": "ACTIVE",
                "DISPUTE": "DISPUTED",
                "WITHDRAWAL": "WITHDRAWN",
                "RESTORE": "ACTIVE",
            }[revision]
            if state != expected_state:
                raise ValidationError(
                    f"professional evidence {revision} must produce {expected_state} state"
                )
        song, version = self._validate_work_binding(song_id, version_id)
        self._validate_permission(
            kind=kind,
            source_kind=source_kind,
            share_scope=share_scope,
            permission_source_kind=permission_kind,
            permission_source_ref=permission_ref,
            confidential=confidential,
        )
        return {
            "schema_version": PROFESSIONAL_EVIDENCE_SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "roles": list(canonical_roles),
            "kind": kind,
            "title": title,
            "statement": statement,
            "evidence_source_kind": source_kind,
            "evidence_source_ref": source_ref,
            "share_scope": share_scope,
            "permission_source_kind": permission_kind,
            "permission_source_ref": permission_ref,
            "confidential": confidential,
            "state": state,
            "song_id": song,
            "version_id": version,
            "role_evidence_kind": role_kind,
            "revision_kind": revision,
            "revision_reason": reason,
        }

    def _decode_claim(self, claim: EvidenceClaim) -> ProfessionalEvidenceRecord:
        try:
            evidence_id = self._evidence_id_from_key(claim.key)
            if claim.scope_kind != "PROFILE" or claim.scope_id != self.store.profile_id:
                raise ProfessionalEvidenceIntegrityError(
                    "professional evidence crossed its canonical profile scope"
                )
            if claim.twin_domain != "UNSPECIFIED":
                raise ProfessionalEvidenceIntegrityError(
                    "professional evidence was incorrectly flattened into a Twin domain"
                )
            revision_source = self._normalize_source_kind(claim.source_kind)
            revision_source_ref = self._clean_ref(
                claim.source_ref,
                field="professional evidence revision source_ref",
            )
            if not isinstance(claim.value, dict) or set(claim.value) != self._PAYLOAD_KEYS:
                raise ProfessionalEvidenceIntegrityError(
                    "professional evidence payload has an invalid shape"
                )
            payload = claim.value
            if payload.get("schema_version") != PROFESSIONAL_EVIDENCE_SCHEMA_VERSION:
                raise ProfessionalEvidenceIntegrityError(
                    "unsupported professional evidence payload version"
                )
            if payload.get("evidence_id") != evidence_id:
                raise ProfessionalEvidenceIntegrityError(
                    "professional evidence payload identity does not match its key"
                )
            canonical = self._canonical_payload(
                evidence_id=evidence_id,
                roles=payload["roles"],
                kind=payload["kind"],
                title=payload["title"],
                statement=payload["statement"],
                evidence_source_kind=payload["evidence_source_kind"],
                evidence_source_ref=payload["evidence_source_ref"],
                share_scope=payload["share_scope"],
                permission_source_kind=payload["permission_source_kind"],
                permission_source_ref=payload["permission_source_ref"],
                confidential=payload["confidential"],
                state=payload["state"],
                song_id=payload["song_id"],
                version_id=payload["version_id"],
                role_evidence_kind=payload["role_evidence_kind"],
                revision_kind=payload["revision_kind"],
                revision_reason=payload["revision_reason"],
            )
            if canonical != payload:
                raise ProfessionalEvidenceIntegrityError(
                    "professional evidence payload is not canonical"
                )
        except ProfessionalEvidenceIntegrityError:
            raise
        except (NotFoundError, ValidationError, TypeError, ValueError) as exc:
            raise ProfessionalEvidenceIntegrityError(
                "professional evidence payload is invalid"
            ) from exc

        return ProfessionalEvidenceRecord(
            evidence_id=evidence_id,
            revision_claim_id=claim.id,
            sequence=claim.sequence,
            roles=tuple(canonical["roles"]),
            kind=canonical["kind"],
            title=canonical["title"],
            statement=canonical["statement"],
            evidence_source_kind=canonical["evidence_source_kind"],
            evidence_source_ref=canonical["evidence_source_ref"],
            share_scope=canonical["share_scope"],
            permission_source_kind=canonical["permission_source_kind"],
            permission_source_ref=canonical["permission_source_ref"],
            confidential=canonical["confidential"],
            state=canonical["state"],
            song_id=canonical["song_id"],
            version_id=canonical["version_id"],
            role_evidence_kind=canonical["role_evidence_kind"],
            revision_kind=canonical["revision_kind"],
            revision_reason=canonical["revision_reason"],
            revision_source_kind=revision_source,
            revision_source_ref=revision_source_ref,
        )

    def _record_revision(
        self,
        *,
        payload: dict[str, object],
        revision_source_kind: object,
        revision_source_ref: object,
        supersedes: tuple[str, ...] = (),
    ) -> ProfessionalEvidenceRecord:
        revision_source = self._normalize_source_kind(revision_source_kind)
        revision_ref = self._clean_ref(
            revision_source_ref, field="professional evidence revision source_ref"
        )
        evidence_id = payload.get("evidence_id")
        key = self._key(evidence_id)
        claim = self.evidence.record_claim(
            scope_kind="PROFILE",
            scope_id=self.store.profile_id,
            key=key,
            value=payload,
            source_kind=revision_source,
            source_ref=revision_ref,
            confidence=1.0,
            twin_domain="UNSPECIFIED",
            supersedes=supersedes,
        )
        return self._decode_claim(claim)

    def record(
        self,
        *,
        roles: Iterable[str],
        kind: object,
        title: object,
        statement: object,
        evidence_source_kind: object,
        evidence_source_ref: object,
        share_scope: object = "PRIVATE",
        permission_source_kind: object | None = None,
        permission_source_ref: object | None = None,
        confidential: object = False,
        song_id: object | None = None,
        version_id: object | None = None,
        role_evidence_kind: object | None = None,
    ) -> ProfessionalEvidenceRecord:
        payload = self._canonical_payload(
            evidence_id=self._new_evidence_id(),
            roles=roles,
            kind=kind,
            title=title,
            statement=statement,
            evidence_source_kind=evidence_source_kind,
            evidence_source_ref=evidence_source_ref,
            share_scope=share_scope,
            permission_source_kind=permission_source_kind,
            permission_source_ref=permission_source_ref,
            confidential=confidential,
            state="ACTIVE",
            song_id=song_id,
            version_id=version_id,
            role_evidence_kind=role_evidence_kind,
            revision_kind="CREATE",
            revision_reason=None,
        )
        return self._record_revision(
            payload=payload,
            revision_source_kind=payload["evidence_source_kind"],
            revision_source_ref=payload["evidence_source_ref"],
        )

    def history(self, evidence_id: object) -> tuple[ProfessionalEvidenceRecord, ...]:
        key = self._key(evidence_id)
        rows = self.store._conn.execute(
            "SELECT id FROM evidence_claims "
            "WHERE scope_kind='PROFILE' AND scope_id=? AND key=? ORDER BY seq",
            (self.store.profile_id, key),
        ).fetchall()
        if not rows:
            raise NotFoundError(f"professional evidence not found: {evidence_id}")
        claims = tuple(self.evidence.get_claim(row["id"]) for row in rows)
        if any(claim is None for claim in claims):
            raise ProfessionalEvidenceIntegrityError(
                "professional evidence history references a missing claim"
            )
        records = tuple(
            self._decode_claim(claim) for claim in claims if claim is not None
        )
        if records[0].revision_kind != "CREATE":
            raise ProfessionalEvidenceIntegrityError(
                "professional evidence history does not start with CREATE"
            )
        expected_edges = {
            (records[index].revision_claim_id, records[index - 1].revision_claim_id)
            for index in range(1, len(records))
        }
        claim_ids = tuple(record.revision_claim_id for record in records)
        placeholders = ",".join("?" for _ in claim_ids)
        edge_rows = self.store._conn.execute(
            f"SELECT new_claim_id,old_claim_id FROM evidence_supersessions "
            f"WHERE new_claim_id IN ({placeholders}) OR old_claim_id IN ({placeholders})",
            (*claim_ids, *claim_ids),
        ).fetchall()
        actual_edges = {
            (row["new_claim_id"], row["old_claim_id"])
            for row in edge_rows
            if row["new_claim_id"] in claim_ids and row["old_claim_id"] in claim_ids
        }
        if actual_edges != expected_edges:
            raise ProfessionalEvidenceIntegrityError(
                "professional evidence revision lineage is not one immutable chain"
            )
        return records

    def current(self, evidence_id: object) -> ProfessionalEvidenceRecord:
        history = self.history(evidence_id)
        key = self._key(evidence_id)
        active = self.evidence.active_claims("PROFILE", self.store.profile_id, key)
        if len(active) != 1:
            raise ProfessionalEvidenceIntegrityError(
                "professional evidence has conflicting active revisions"
            )
        if active[0].id != history[-1].revision_claim_id:
            raise ProfessionalEvidenceIntegrityError(
                "professional evidence current revision does not match its lineage tail"
            )
        return history[-1]

    def list_current(
        self,
        *,
        role_id: object | None = None,
        kind: object | None = None,
    ) -> tuple[ProfessionalEvidenceRecord, ...]:
        requested_role = (
            None if role_id is None else self._canonical_roles((role_id,))[0]
        )
        requested_kind = None if kind is None else self._normalize_kind(kind)
        ids: set[str] = set()
        for claim in self.evidence.active_claims_for_scope("PROFILE", self.store.profile_id):
            if isinstance(claim.key, str) and claim.key.startswith(_KEY_PREFIX):
                ids.add(self._evidence_id_from_key(claim.key))
        records = [self.current(evidence_id) for evidence_id in ids]
        if requested_role is not None:
            records = [record for record in records if requested_role in record.roles]
        if requested_kind is not None:
            records = [record for record in records if record.kind == requested_kind]
        return tuple(sorted(records, key=lambda record: record.sequence))

    def correct(
        self,
        evidence_id: object,
        *,
        reason: object,
        revision_source_kind: object,
        revision_source_ref: object,
        roles: Iterable[str] | object = _UNSET,
        kind: object = _UNSET,
        title: object = _UNSET,
        statement: object = _UNSET,
        evidence_source_kind: object = _UNSET,
        evidence_source_ref: object = _UNSET,
        share_scope: object = _UNSET,
        permission_source_kind: object = _UNSET,
        permission_source_ref: object = _UNSET,
        confidential: object = _UNSET,
        song_id: object = _UNSET,
        version_id: object = _UNSET,
        role_evidence_kind: object = _UNSET,
    ) -> ProfessionalEvidenceRecord:
        current = self.current(evidence_id)
        if current.state == "WITHDRAWN":
            raise ValidationError(
                "withdrawn professional evidence must be explicitly restored before correction"
            )
        next_source_kind = (
            current.evidence_source_kind
            if evidence_source_kind is _UNSET
            else evidence_source_kind
        )
        next_source_ref = (
            current.evidence_source_ref
            if evidence_source_ref is _UNSET
            else evidence_source_ref
        )
        if evidence_source_kind is not _UNSET:
            normalized_next_source = self._normalize_source_kind(evidence_source_kind)
            if (
                normalized_next_source != current.evidence_source_kind
                and evidence_source_ref is _UNSET
            ):
                raise ValidationError(
                    "changing professional evidence source kind requires a new source_ref"
                )
        payload = self._canonical_payload(
            evidence_id=current.evidence_id,
            roles=current.roles if roles is _UNSET else roles,
            kind=current.kind if kind is _UNSET else kind,
            title=current.title if title is _UNSET else title,
            statement=current.statement if statement is _UNSET else statement,
            evidence_source_kind=next_source_kind,
            evidence_source_ref=next_source_ref,
            share_scope=current.share_scope if share_scope is _UNSET else share_scope,
            permission_source_kind=(
                current.permission_source_kind
                if permission_source_kind is _UNSET
                else permission_source_kind
            ),
            permission_source_ref=(
                current.permission_source_ref
                if permission_source_ref is _UNSET
                else permission_source_ref
            ),
            confidential=current.confidential if confidential is _UNSET else confidential,
            state="ACTIVE",
            song_id=current.song_id if song_id is _UNSET else song_id,
            version_id=current.version_id if version_id is _UNSET else version_id,
            role_evidence_kind=(
                current.role_evidence_kind
                if role_evidence_kind is _UNSET
                else role_evidence_kind
            ),
            revision_kind="CORRECTION",
            revision_reason=reason,
        )
        return self._record_revision(
            payload=payload,
            revision_source_kind=revision_source_kind,
            revision_source_ref=revision_source_ref,
            supersedes=(current.revision_claim_id,),
        )

    def _state_revision(
        self,
        evidence_id: object,
        *,
        target_state: object,
        revision_kind: object,
        reason: object,
        revision_source_kind: object,
        revision_source_ref: object,
    ) -> ProfessionalEvidenceRecord:
        current = self.current(evidence_id)
        revision = self._normalize_revision_kind(revision_kind)
        if revision == "DISPUTE":
            if current.state == "WITHDRAWN":
                raise ValidationError("withdrawn professional evidence cannot be disputed")
            if current.state == "DISPUTED":
                raise ValidationError("professional evidence is already disputed")
        elif revision == "WITHDRAWAL":
            if current.state == "WITHDRAWN":
                raise ValidationError("professional evidence is already withdrawn")
        elif revision == "RESTORE" and current.state == "ACTIVE":
            raise ValidationError("active professional evidence does not need restoration")
        payload = self._canonical_payload(
            evidence_id=current.evidence_id,
            roles=current.roles,
            kind=current.kind,
            title=current.title,
            statement=current.statement,
            evidence_source_kind=current.evidence_source_kind,
            evidence_source_ref=current.evidence_source_ref,
            share_scope=current.share_scope,
            permission_source_kind=current.permission_source_kind,
            permission_source_ref=current.permission_source_ref,
            confidential=current.confidential,
            state=target_state,
            song_id=current.song_id,
            version_id=current.version_id,
            role_evidence_kind=current.role_evidence_kind,
            revision_kind=revision,
            revision_reason=reason,
        )
        return self._record_revision(
            payload=payload,
            revision_source_kind=revision_source_kind,
            revision_source_ref=revision_source_ref,
            supersedes=(current.revision_claim_id,),
        )

    def dispute(
        self,
        evidence_id: object,
        *,
        reason: object,
        revision_source_kind: object,
        revision_source_ref: object,
    ) -> ProfessionalEvidenceRecord:
        return self._state_revision(
            evidence_id,
            target_state="DISPUTED",
            revision_kind="DISPUTE",
            reason=reason,
            revision_source_kind=revision_source_kind,
            revision_source_ref=revision_source_ref,
        )

    def withdraw(
        self,
        evidence_id: object,
        *,
        reason: object,
        revision_source_kind: object,
        revision_source_ref: object,
    ) -> ProfessionalEvidenceRecord:
        return self._state_revision(
            evidence_id,
            target_state="WITHDRAWN",
            revision_kind="WITHDRAWAL",
            reason=reason,
            revision_source_kind=revision_source_kind,
            revision_source_ref=revision_source_ref,
        )

    def restore(
        self,
        evidence_id: object,
        *,
        reason: object,
        revision_source_kind: object,
        revision_source_ref: object,
    ) -> ProfessionalEvidenceRecord:
        return self._state_revision(
            evidence_id,
            target_state="ACTIVE",
            revision_kind="RESTORE",
            reason=reason,
            revision_source_kind=revision_source_kind,
            revision_source_ref=revision_source_ref,
        )

    def portable_for_role(
        self,
        role_id: object,
        *,
        audience: object = "OPPORTUNITY",
        verified_only: object = True,
    ) -> tuple[ProfessionalEvidenceRecord, ...]:
        role = self._canonical_roles((role_id,))[0]
        requested_audience = self._token(
            audience,
            field="portable professional evidence audience",
            allowed={"OPPORTUNITY", "PUBLIC"},
        )
        if not isinstance(verified_only, bool):
            raise ValidationError("verified_only must be boolean")
        allowed_scopes = (
            {"OPPORTUNITY", "PUBLIC"}
            if requested_audience == "OPPORTUNITY"
            else {"PUBLIC"}
        )
        portable: list[ProfessionalEvidenceRecord] = []
        for record in self.list_current(role_id=role):
            if record.state != "ACTIVE":
                continue
            if record.confidential or record.share_scope not in allowed_scopes:
                continue
            if verified_only and not record.verified:
                continue
            if record.kind in PERMISSION_REQUIRED_KINDS and not record.permission_verified:
                raise ProfessionalEvidenceIntegrityError(
                    "shareable professional evidence lost verified permission provenance"
                )
            portable.append(record)
        return tuple(portable)

    def to_role_evidence(self, evidence_id: object) -> RoleEvidence:
        record = self.current(evidence_id)
        if record.state != "ACTIVE":
            raise ValidationError(
                "only active professional evidence can feed career-role assessment"
            )
        if record.role_evidence_kind is None:
            raise ValidationError(
                "professional evidence needs an explicit role_evidence_kind before career assessment"
            )
        return RoleEvidence(
            id=record.evidence_id,
            role_ids=record.roles,
            kind=record.role_evidence_kind,
            source_kind=_CAREER_SOURCE_KIND[record.evidence_source_kind],
            source_ref=record.evidence_source_ref,
            note=record.title,
        )
