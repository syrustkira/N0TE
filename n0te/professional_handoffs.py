from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Mapping

from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

PROFESSIONAL_HANDOFF_SCHEMA_VERSION = 1
HANDOFF_STATES = {"SUBMITTED", "RETURNED", "ACCEPTED", "STALE"}
VERSION_BINDING_POLICIES = {"EXACT", "CURRENT", "CURRENT_APPROVED"}
FRESHNESS_STATUSES = {"PENDING", "CURRENT", "RETURNED", "STALE"}

_MAX_ROLE_CHARS = 120
_MAX_TEXT_CHARS = 2_000
_MAX_REF_CHARS = 2_048
_MAX_INPUTS = 32
_MAX_OUTPUTS = 24
_MAX_INPUT_NAME_CHARS = 80

_ID_RE = re.compile(r"^ph_[a-f0-9]{32}$")
_SPEC_ID_RE = re.compile(r"^H[0-9]{2}$")
_INPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


def _clean_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text")
    text = " ".join(value.split())
    if not text:
        raise ValidationError(f"{field} must not be empty")
    if len(text) > maximum:
        raise ValidationError(f"{field} is too long")
    return text


def _canonical_names(
    values: tuple[str, ...], field: str, maximum_count: int
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{field} must be a collection")
    names: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValidationError(f"{field} entries must be text")
        name = raw.strip().lower().replace("-", "_").replace(" ", "_")
        name = re.sub(r"_+", "_", name).strip("_")
        if not _INPUT_NAME_RE.fullmatch(name):
            raise ValidationError(f"invalid professional handoff field name: {name}")
        names.append(name)
    canonical = tuple(dict.fromkeys(names))
    if not canonical:
        raise ValidationError(f"{field} must not be empty")
    if len(canonical) > maximum_count:
        raise ValidationError(f"{field} has too many entries")
    return canonical


class ProfessionalHandoffError(RuntimeError):
    """A professional handoff cannot proceed truthfully."""


class StaleProfessionalHandoffError(ProfessionalHandoffError):
    """The exact upstream state moved after the handoff package was prepared."""


class ProfessionalHandoffIntegrityError(ProfessionalHandoffError):
    """Durable professional handoff state no longer matches its canonical contract."""


@dataclass(frozen=True)
class HandoffSpec:
    id: str
    from_role: str
    to_role: str
    trigger: str
    required_inputs: tuple[str, ...]
    required_outputs: tuple[str, ...]
    approval_owner: str
    rights_metadata: str
    return_owner: str
    version_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise ValidationError("professional handoff spec id must be text")
        spec_id = self.id.strip().upper()
        if not _SPEC_ID_RE.fullmatch(spec_id):
            raise ValidationError("professional handoff spec id must look like H07")
        object.__setattr__(self, "id", spec_id)
        for field in (
            "from_role",
            "to_role",
            "trigger",
            "approval_owner",
            "rights_metadata",
            "return_owner",
        ):
            maximum = (
                _MAX_ROLE_CHARS
                if field in {"from_role", "to_role", "approval_owner", "return_owner"}
                else _MAX_TEXT_CHARS
            )
            object.__setattr__(
                self, field, _clean_text(getattr(self, field), field, maximum)
            )
        object.__setattr__(
            self,
            "required_inputs",
            _canonical_names(self.required_inputs, "required_inputs", _MAX_INPUTS),
        )
        object.__setattr__(
            self,
            "required_outputs",
            _canonical_names(self.required_outputs, "required_outputs", _MAX_OUTPUTS),
        )
        if not isinstance(self.version_policy, str):
            raise ValidationError("handoff Version policy must be text")
        policy = self.version_policy.strip().upper().replace("-", "_")
        if policy not in VERSION_BINDING_POLICIES:
            raise ValidationError(f"unsupported handoff Version policy: {policy}")
        object.__setattr__(self, "version_policy", policy)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "approval_owner": self.approval_owner,
            "from_role": self.from_role,
            "id": self.id,
            "required_inputs": list(self.required_inputs),
            "required_outputs": list(self.required_outputs),
            "return_owner": self.return_owner,
            "rights_metadata": self.rights_metadata,
            "to_role": self.to_role,
            "trigger": self.trigger,
            "version_policy": self.version_policy,
        }


@dataclass(frozen=True)
class ProfessionalHandoff:
    sequence: int
    id: str
    artist_id: str
    song_id: str
    upstream_version_id: str
    spec: HandoffSpec
    provided_inputs: tuple[tuple[str, str], ...]
    missing_inputs: tuple[str, ...]
    expected_current_version_id: str | None
    expected_approved_version_id: str | None
    package_fingerprint: str
    state: str
    status_reason: str | None
    acceptance_receipt: str | None
    supersedes_handoff_id: str | None

    @property
    def accepted(self) -> bool:
        return self.state == "ACCEPTED"

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def input_refs(self) -> dict[str, str]:
        return dict(self.provided_inputs)


@dataclass(frozen=True)
class HandoffFreshness:
    status: str
    handoff: ProfessionalHandoff
    reason: str | None

    def __post_init__(self) -> None:
        if self.status not in FRESHNESS_STATUSES:
            raise ValueError(f"invalid handoff freshness status: {self.status}")

    @property
    def usable(self) -> bool:
        return self.status == "CURRENT" and self.handoff.state == "ACCEPTED"


CORE_PRODUCTION_HANDOFF_SPECS: dict[str, HandoffSpec] = {
    "H07": HandoffSpec(
        id="H07",
        from_role="Producer / Editor",
        to_role="Mix Engineer",
        trigger="Production and edits are creatively locked enough to mix.",
        required_inputs=(
            "consolidated_multitracks_or_stems",
            "rough_mix",
            "tempo",
            "notes",
            "references",
            "headroom_and_format",
            "credits",
            "unresolved_issues",
            "creative_lock_approval",
        ),
        required_outputs=(
            "mix_version",
            "alternates_or_stems",
            "mix_notes",
            "recall_archive",
        ),
        approval_owner="Artist / Producer",
        rights_metadata="Mix credit, fee/revisions, confidentiality and participation only when separately evidenced.",
        return_owner="Producer / Editor",
        version_policy="CURRENT",
    ),
    "H08": HandoffSpec(
        id="H08",
        from_role="Mix Engineer",
        to_role="Mastering Engineer",
        trigger="The mix is approved for mastering.",
        required_inputs=(
            "approved_mix",
            "sequence_or_order",
            "metadata",
            "references",
            "deliverable_requirements",
            "known_technical_notes",
        ),
        required_outputs=(
            "masters_or_sequence",
            "alternate_formats",
            "qc_notes",
            "archive",
        ),
        approval_owner="Artist / Producer / Label",
        rights_metadata="Mastering credit and fee plus release metadata; no ownership assumption.",
        return_owner="Mix Engineer",
        version_policy="CURRENT_APPROVED",
    ),
    "H09": HandoffSpec(
        id="H09",
        from_role="Mastering Engineer",
        to_role="Artist / Producer / Label",
        trigger="Masters are technically and creatively approved for final delivery review.",
        required_inputs=(
            "final_master_set",
            "sequence",
            "qc_report",
            "format_list",
            "revision_status",
        ),
        required_outputs=(
            "master_approval",
            "release_lock_asset_set",
        ),
        approval_owner="Artist / Authorized Release Owner",
        rights_metadata="Master identifiers, credits and ownership/readiness remain separately evidenced.",
        return_owner="Mastering Engineer",
        version_policy="CURRENT_APPROVED",
    ),
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ProfessionalHandoffService:
    """Durable exact-state professional handoffs inside canonical profile memory.

    This layer coordinates existing owners. It does not create new Artist/Song/
    Version identities, does not grant provider or DAW authority, and does not
    treat a complete intake package as artistic, legal or commercial approval.

    Handoffs snapshot their contract plus exact upstream Version/current/approved
    state. Policies explicitly decide which canonical pointers matter. Accepted
    work becomes permanently STALE once a watched upstream pointer moves; moving
    a pointer back later does not resurrect the old receipt. A corrected package
    is a new handoff linked to the returned/stale predecessor.
    """

    _TRIGGER_NAMES = {
        "professional_handoff_version_same_song",
        "professional_handoff_expected_current_same_song",
        "professional_handoff_expected_approved_same_song",
        "professional_handoff_supersedes_valid",
        "professional_handoff_binding_immutable",
        "professional_handoff_terminal_immutable",
        "professional_handoff_state_transition",
        "professional_handoff_delete_immutable",
        "professional_handoff_created_activity",
        "professional_handoff_state_activity",
    }

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("ProfessionalHandoffService requires LineageStore")
        self.store = store
        self._conn = store._conn
        self._ensure_schema()
        self._validate_existing()

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _metadata_value(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER professional_handoff_version_same_song
            BEFORE INSERT ON professional_handoffs
            WHEN NOT EXISTS (
                SELECT 1 FROM versions v
                WHERE v.id=NEW.upstream_version_id AND v.song_id=NEW.song_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'handoff upstream Version belongs to a different Song');
            END""",
            """CREATE TRIGGER professional_handoff_expected_current_same_song
            BEFORE INSERT ON professional_handoffs
            WHEN NEW.expected_current_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM versions v
                WHERE v.id=NEW.expected_current_version_id AND v.song_id=NEW.song_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'handoff expected current Version belongs to a different Song');
            END""",
            """CREATE TRIGGER professional_handoff_expected_approved_same_song
            BEFORE INSERT ON professional_handoffs
            WHEN NEW.expected_approved_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM versions v
                WHERE v.id=NEW.expected_approved_version_id AND v.song_id=NEW.song_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'handoff expected approved Version belongs to a different Song');
            END""",
            """CREATE TRIGGER professional_handoff_supersedes_valid
            BEFORE INSERT ON professional_handoffs
            WHEN NEW.supersedes_handoff_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM professional_handoffs h
                WHERE h.id=NEW.supersedes_handoff_id
                  AND h.artist_id=NEW.artist_id
                  AND h.song_id=NEW.song_id
                  AND h.spec_id=NEW.spec_id
                  AND h.state IN ('RETURNED','STALE')
            )
            BEGIN
                SELECT RAISE(ABORT, 'handoff may supersede only returned/stale matching work');
            END""",
            """CREATE TRIGGER professional_handoff_binding_immutable
            BEFORE UPDATE ON professional_handoffs
            WHEN NEW.id<>OLD.id OR NEW.artist_id<>OLD.artist_id
              OR NEW.song_id<>OLD.song_id OR NEW.upstream_version_id<>OLD.upstream_version_id
              OR NEW.spec_id<>OLD.spec_id OR NEW.spec_json<>OLD.spec_json
              OR NEW.provided_inputs_json<>OLD.provided_inputs_json
              OR NEW.missing_inputs_json<>OLD.missing_inputs_json
              OR NEW.expected_current_version_id IS NOT OLD.expected_current_version_id
              OR NEW.expected_approved_version_id IS NOT OLD.expected_approved_version_id
              OR NEW.package_fingerprint<>OLD.package_fingerprint
              OR NEW.supersedes_handoff_id IS NOT OLD.supersedes_handoff_id
            BEGIN
                SELECT RAISE(ABORT, 'professional handoff package binding is immutable');
            END""",
            """CREATE TRIGGER professional_handoff_terminal_immutable
            BEFORE UPDATE ON professional_handoffs
            WHEN OLD.state IN ('RETURNED','STALE')
            BEGIN
                SELECT RAISE(ABORT, 'returned/stale professional handoff is immutable');
            END""",
            """CREATE TRIGGER professional_handoff_state_transition
            BEFORE UPDATE ON professional_handoffs
            WHEN NOT (
                (OLD.state='SUBMITTED' AND NEW.state='ACCEPTED'
                    AND NEW.acceptance_receipt IS NOT NULL
                    AND NEW.status_reason IS NULL
                    AND NEW.missing_inputs_json='[]')
                OR
                (OLD.state='SUBMITTED' AND NEW.state='RETURNED'
                    AND NEW.acceptance_receipt IS NULL
                    AND NEW.status_reason IS NOT NULL
                    AND length(trim(NEW.status_reason))>0)
                OR
                (OLD.state IN ('SUBMITTED','ACCEPTED') AND NEW.state='STALE'
                    AND NEW.status_reason IS NOT NULL
                    AND length(trim(NEW.status_reason))>0
                    AND NEW.acceptance_receipt IS OLD.acceptance_receipt)
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid professional handoff state transition');
            END""",
            """CREATE TRIGGER professional_handoff_delete_immutable
            BEFORE DELETE ON professional_handoffs
            BEGIN
                SELECT RAISE(ABORT, 'professional handoff history is immutable');
            END""",
            """CREATE TRIGGER professional_handoff_created_activity
            AFTER INSERT ON professional_handoffs
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'PROFESSIONAL_HANDOFF_'||NEW.state,
                    NEW.artist_id,NEW.song_id,NEW.upstream_version_id,
                    'PROFESSIONAL_HANDOFF',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER professional_handoff_state_activity
            AFTER UPDATE OF state ON professional_handoffs
            WHEN NEW.state<>OLD.state
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'PROFESSIONAL_HANDOFF_'||NEW.state,
                    NEW.artist_id,NEW.song_id,NEW.upstream_version_id,
                    'PROFESSIONAL_HANDOFF',NEW.id,'{}'
                );
            END""",
        )

    def _ensure_schema(self) -> None:
        table_exists = self._table_exists("professional_handoffs")
        version = self._metadata_value("professional_handoff_schema_version")
        if table_exists != (version is not None):
            raise LineageCorruptionError(
                "professional handoff schema metadata/table mismatch"
            )
        if table_exists:
            if version != str(PROFESSIONAL_HANDOFF_SCHEMA_VERSION):
                raise LineageCorruptionError(
                    f"unsupported professional handoff schema version: {version}"
                )
            return
        if not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "ProfessionalHandoffService requires canonical Activity chronology first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE professional_handoffs (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        upstream_version_id TEXT NOT NULL REFERENCES versions(id),
                        spec_id TEXT NOT NULL CHECK(length(trim(spec_id))>0),
                        spec_json TEXT NOT NULL,
                        provided_inputs_json TEXT NOT NULL,
                        missing_inputs_json TEXT NOT NULL,
                        expected_current_version_id TEXT NULL REFERENCES versions(id),
                        expected_approved_version_id TEXT NULL REFERENCES versions(id),
                        package_fingerprint TEXT NOT NULL CHECK(length(package_fingerprint)=64),
                        state TEXT NOT NULL CHECK(state IN ('SUBMITTED','RETURNED','ACCEPTED','STALE')),
                        status_reason TEXT NULL CHECK(
                            status_reason IS NULL OR length(trim(status_reason))>0
                        ),
                        acceptance_receipt TEXT NULL CHECK(
                            acceptance_receipt IS NULL OR length(acceptance_receipt)=64
                        ),
                        supersedes_handoff_id TEXT NULL REFERENCES professional_handoffs(id)
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX professional_handoff_song_seq "
                    "ON professional_handoffs(song_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX professional_handoff_state "
                    "ON professional_handoffs(state,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) "
                    "VALUES('professional_handoff_schema_version',?)",
                    (str(PROFESSIONAL_HANDOFF_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot initialize professional handoff memory"
            ) from exc

    @staticmethod
    def _spec_from_payload(payload: object) -> HandoffSpec:
        if not isinstance(payload, dict):
            raise ProfessionalHandoffIntegrityError(
                "handoff spec payload is not an object"
            )
        expected = {
            "approval_owner",
            "from_role",
            "id",
            "required_inputs",
            "required_outputs",
            "return_owner",
            "rights_metadata",
            "to_role",
            "trigger",
            "version_policy",
        }
        if set(payload) != expected:
            raise ProfessionalHandoffIntegrityError(
                "handoff spec payload shape is invalid"
            )
        try:
            spec = HandoffSpec(
                id=payload["id"],
                from_role=payload["from_role"],
                to_role=payload["to_role"],
                trigger=payload["trigger"],
                required_inputs=tuple(payload["required_inputs"]),
                required_outputs=tuple(payload["required_outputs"]),
                approval_owner=payload["approval_owner"],
                rights_metadata=payload["rights_metadata"],
                return_owner=payload["return_owner"],
                version_policy=payload["version_policy"],
            )
        except (TypeError, ValidationError) as exc:
            raise ProfessionalHandoffIntegrityError(
                "handoff spec payload is invalid"
            ) from exc
        if spec.canonical_payload() != payload:
            raise ProfessionalHandoffIntegrityError(
                "handoff spec payload is not canonical"
            )
        canonical_core = CORE_PRODUCTION_HANDOFF_SPECS.get(spec.id)
        if canonical_core is not None and spec != canonical_core:
            raise ProfessionalHandoffIntegrityError(
                "reserved core professional handoff contract was overridden"
            )
        return spec

    @staticmethod
    def _canonical_inputs(
        spec: HandoffSpec, inputs: Mapping[str, str]
    ) -> tuple[tuple[str, str], ...]:
        if not isinstance(inputs, Mapping):
            raise ValidationError("professional handoff inputs must be a mapping")
        allowed = set(spec.required_inputs)
        canonical: dict[str, str] = {}
        for raw_name, raw_ref in inputs.items():
            if not isinstance(raw_name, str):
                raise ValidationError("professional handoff input names must be text")
            name = raw_name.strip().lower().replace("-", "_").replace(" ", "_")
            name = re.sub(r"_+", "_", name).strip("_")
            if name not in allowed:
                raise ValidationError(f"unexpected professional handoff input: {name}")
            ref = _clean_text(raw_ref, f"handoff input {name}", _MAX_REF_CHARS)
            canonical[name] = ref
        return tuple(sorted(canonical.items()))

    @staticmethod
    def _package_payload(
        *,
        handoff_id: str,
        artist_id: str,
        song_id: str,
        upstream_version_id: str,
        spec: HandoffSpec,
        provided_inputs: tuple[tuple[str, str], ...],
        expected_current_version_id: str | None,
        expected_approved_version_id: str | None,
        supersedes_handoff_id: str | None,
    ) -> dict[str, object]:
        return {
            "artist_id": artist_id,
            "expected_approved_version_id": expected_approved_version_id,
            "expected_current_version_id": expected_current_version_id,
            "handoff_id": handoff_id,
            "provided_inputs": dict(provided_inputs),
            "song_id": song_id,
            "spec": spec.canonical_payload(),
            "supersedes_handoff_id": supersedes_handoff_id,
            "upstream_version_id": upstream_version_id,
        }

    def _handoff(self, row: sqlite3.Row) -> ProfessionalHandoff:
        try:
            handoff_id = str(row["id"])
            if not _ID_RE.fullmatch(handoff_id):
                raise ProfessionalHandoffIntegrityError(
                    "invalid professional handoff id"
                )
            artist_id = str(row["artist_id"])
            if artist_id != self.store.primary_artist_id:
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff Artist does not match active profile"
                )
            song_id = str(row["song_id"])
            song = self.store.get_song(song_id)
            if song is None or song.artist_id != artist_id:
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff Song binding is invalid"
                )
            upstream_version_id = str(row["upstream_version_id"])
            upstream = self.store.get_version(upstream_version_id)
            if upstream is None or upstream.song_id != song_id:
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff upstream Version binding is invalid"
                )
            spec_payload = json.loads(str(row["spec_json"]))
            spec = self._spec_from_payload(spec_payload)
            if str(row["spec_id"]) != spec.id:
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff spec identity does not match its snapshot"
                )
            raw_inputs = json.loads(str(row["provided_inputs_json"]))
            if not isinstance(raw_inputs, dict):
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff inputs are not an object"
                )
            provided_inputs = self._canonical_inputs(spec, raw_inputs)
            if dict(provided_inputs) != raw_inputs:
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff inputs are not canonical"
                )
            raw_missing = json.loads(str(row["missing_inputs_json"]))
            if not isinstance(raw_missing, list) or any(
                not isinstance(item, str) for item in raw_missing
            ):
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff missing-input list is invalid"
                )
            missing = tuple(
                name for name in spec.required_inputs if name not in dict(provided_inputs)
            )
            if list(missing) != raw_missing:
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff missing-input truth no longer matches package"
                )
            expected_current = (
                None
                if row["expected_current_version_id"] is None
                else str(row["expected_current_version_id"])
            )
            expected_approved = (
                None
                if row["expected_approved_version_id"] is None
                else str(row["expected_approved_version_id"])
            )
            for candidate in (expected_current, expected_approved):
                if candidate is not None:
                    version = self.store.get_version(candidate)
                    if version is None or version.song_id != song_id:
                        raise ProfessionalHandoffIntegrityError(
                            "professional handoff observed Version pointer is invalid"
                        )
            supersedes = (
                None
                if row["supersedes_handoff_id"] is None
                else str(row["supersedes_handoff_id"])
            )
            package = self._package_payload(
                handoff_id=handoff_id,
                artist_id=artist_id,
                song_id=song_id,
                upstream_version_id=upstream_version_id,
                spec=spec,
                provided_inputs=provided_inputs,
                expected_current_version_id=expected_current,
                expected_approved_version_id=expected_approved,
                supersedes_handoff_id=supersedes,
            )
            fingerprint = _sha256(package)
            if str(row["package_fingerprint"]) != fingerprint:
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff package fingerprint does not match durable package"
                )
            state = str(row["state"])
            if state not in HANDOFF_STATES:
                raise ProfessionalHandoffIntegrityError(
                    "professional handoff has invalid durable state"
                )
            reason = (
                None if row["status_reason"] is None else str(row["status_reason"])
            )
            receipt = (
                None
                if row["acceptance_receipt"] is None
                else str(row["acceptance_receipt"])
            )
            if state == "RETURNED" and not reason:
                raise ProfessionalHandoffIntegrityError(
                    "returned professional handoff lost its reason"
                )
            if state == "ACCEPTED":
                if missing or receipt is None or reason is not None:
                    raise ProfessionalHandoffIntegrityError(
                        "accepted professional handoff has an invalid state shape"
                    )
            if receipt is not None:
                expected_receipt = _sha256(
                    {
                        "handoff_id": handoff_id,
                        "package_fingerprint": fingerprint,
                        "receipt_kind": "ACCEPTED",
                    }
                )
                if receipt != expected_receipt:
                    raise ProfessionalHandoffIntegrityError(
                        "professional handoff acceptance receipt is invalid"
                    )
        except ProfessionalHandoffIntegrityError:
            raise
        except (
            json.JSONDecodeError,
            NotFoundError,
            ValidationError,
            TypeError,
            ValueError,
        ) as exc:
            raise ProfessionalHandoffIntegrityError(
                "professional handoff durable payload is unreadable"
            ) from exc

        return ProfessionalHandoff(
            sequence=int(row["seq"]),
            id=handoff_id,
            artist_id=artist_id,
            song_id=song_id,
            upstream_version_id=upstream_version_id,
            spec=spec,
            provided_inputs=provided_inputs,
            missing_inputs=missing,
            expected_current_version_id=expected_current,
            expected_approved_version_id=expected_approved,
            package_fingerprint=fingerprint,
            state=state,
            status_reason=reason,
            acceptance_receipt=receipt,
            supersedes_handoff_id=supersedes,
        )

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("professional_handoff_schema_version") != str(
                PROFESSIONAL_HANDOFF_SCHEMA_VERSION
            ):
                raise ProfessionalHandoffIntegrityError(
                    "unsupported professional handoff schema version"
                )
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'professional_handoff_%'"
                )
            }
            missing_triggers = self._TRIGGER_NAMES - trigger_names
            if missing_triggers:
                raise ProfessionalHandoffIntegrityError(
                    f"professional handoff integrity hooks are incomplete: {sorted(missing_triggers)}"
                )
            rows = self._conn.execute(
                "SELECT seq,id,artist_id,song_id,upstream_version_id,spec_id,spec_json,"
                "provided_inputs_json,missing_inputs_json,expected_current_version_id,"
                "expected_approved_version_id,package_fingerprint,state,status_reason,"
                "acceptance_receipt,supersedes_handoff_id "
                "FROM professional_handoffs ORDER BY seq"
            ).fetchall()
            records = [self._handoff(row) for row in rows]
            by_id = {record.id: record for record in records}
            for record in records:
                if record.supersedes_handoff_id is None:
                    continue
                prior = by_id.get(record.supersedes_handoff_id)
                if prior is None:
                    raise ProfessionalHandoffIntegrityError(
                        "professional handoff supersession target is missing"
                    )
                if prior.sequence >= record.sequence:
                    raise ProfessionalHandoffIntegrityError(
                        "professional handoff supersession does not point backward"
                    )
                if prior.state not in {"RETURNED", "STALE"}:
                    raise ProfessionalHandoffIntegrityError(
                        "professional handoff superseded a nonterminal package"
                    )
                if prior.song_id != record.song_id or prior.spec.id != record.spec.id:
                    raise ProfessionalHandoffIntegrityError(
                        "professional handoff supersession crossed Song or contract"
                    )
        except ProfessionalHandoffIntegrityError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ProfessionalHandoffIntegrityError(
                "professional handoff database is unreadable"
            ) from exc

    @staticmethod
    def core_spec(spec_id: str) -> HandoffSpec:
        key = str(spec_id).strip().upper()
        try:
            return CORE_PRODUCTION_HANDOFF_SPECS[key]
        except KeyError as exc:
            raise ValidationError(
                f"unsupported core professional handoff spec: {key}"
            ) from exc

    def _require_version_for_song(self, version_id: str, song_id: str) -> str:
        version = self.store.get_version(str(version_id))
        if version is None:
            raise NotFoundError(f"version not found: {version_id}")
        if version.song_id != song_id:
            raise ValidationError(
                "professional handoff Version belongs to a different Song"
            )
        return version.id

    @staticmethod
    def _policy_submission_error(
        spec: HandoffSpec,
        *,
        upstream_version_id: str,
        current_version_id: str | None,
        approved_version_id: str | None,
    ) -> str | None:
        if spec.version_policy == "EXACT":
            return None
        if current_version_id != upstream_version_id:
            return "handoff requires the exact upstream Version to be the current Version"
        if (
            spec.version_policy == "CURRENT_APPROVED"
            and approved_version_id != upstream_version_id
        ):
            return (
                "handoff requires current and approved Version to agree on the exact upstream Version"
            )
        return None

    def submit(
        self,
        *,
        spec: HandoffSpec | str,
        song_id: str,
        upstream_version_id: str,
        inputs: Mapping[str, str],
        supersedes_handoff_id: str | None = None,
    ) -> ProfessionalHandoff:
        contract = self.core_spec(spec) if isinstance(spec, str) else spec
        if not isinstance(contract, HandoffSpec):
            raise TypeError("spec must be HandoffSpec or a supported core spec id")
        canonical_core = CORE_PRODUCTION_HANDOFF_SPECS.get(contract.id)
        if canonical_core is not None and contract != canonical_core:
            raise ValidationError(
                "reserved core professional handoff contract cannot be overridden"
            )
        provided = self._canonical_inputs(contract, inputs)
        provided_map = dict(provided)
        missing = tuple(
            name for name in contract.required_inputs if name not in provided_map
        )
        handoff_id = f"ph_{uuid.uuid4().hex}"

        try:
            with self.store._tx():
                song = self.store.get_song(str(song_id))
                if song is None:
                    raise NotFoundError(
                        f"Song not found in profile {self.store.profile_id}: {song_id}"
                    )
                upstream = self._require_version_for_song(upstream_version_id, song.id)
                policy_error = self._policy_submission_error(
                    contract,
                    upstream_version_id=upstream,
                    current_version_id=song.current_version_id,
                    approved_version_id=song.approved_version_id,
                )
                if policy_error is not None:
                    raise ValidationError(policy_error)

                prior_id = None
                if supersedes_handoff_id is not None:
                    prior = self.get(str(supersedes_handoff_id))
                    if prior.state not in {"RETURNED", "STALE"}:
                        raise ValidationError(
                            "new professional handoff may supersede only RETURNED or STALE work"
                        )
                    if prior.song_id != song.id or prior.spec.id != contract.id:
                        raise ValidationError(
                            "professional handoff supersession cannot cross Song or contract"
                        )
                    prior_id = prior.id

                package = self._package_payload(
                    handoff_id=handoff_id,
                    artist_id=song.artist_id,
                    song_id=song.id,
                    upstream_version_id=upstream,
                    spec=contract,
                    provided_inputs=provided,
                    expected_current_version_id=song.current_version_id,
                    expected_approved_version_id=song.approved_version_id,
                    supersedes_handoff_id=prior_id,
                )
                fingerprint = _sha256(package)
                state = "RETURNED" if missing else "SUBMITTED"
                reason = (
                    None
                    if not missing
                    else "Missing required inputs for "
                    + contract.return_owner
                    + ": "
                    + ", ".join(missing)
                )
                self._conn.execute(
                    "INSERT INTO professional_handoffs("
                    "id,artist_id,song_id,upstream_version_id,spec_id,spec_json,"
                    "provided_inputs_json,missing_inputs_json,expected_current_version_id,"
                    "expected_approved_version_id,package_fingerprint,state,status_reason,"
                    "acceptance_receipt,supersedes_handoff_id"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        handoff_id,
                        song.artist_id,
                        song.id,
                        upstream,
                        contract.id,
                        _canonical_json(contract.canonical_payload()),
                        _canonical_json(dict(provided)),
                        _canonical_json(list(missing)),
                        song.current_version_id,
                        song.approved_version_id,
                        fingerprint,
                        state,
                        reason,
                        None,
                        prior_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(
                f"invalid professional handoff package: {exc}"
            ) from exc
        return self.get(handoff_id)

    def get(self, handoff_id: str) -> ProfessionalHandoff:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,upstream_version_id,spec_id,spec_json,"
            "provided_inputs_json,missing_inputs_json,expected_current_version_id,"
            "expected_approved_version_id,package_fingerprint,state,status_reason,"
            "acceptance_receipt,supersedes_handoff_id "
            "FROM professional_handoffs WHERE id=?",
            (str(handoff_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"professional handoff not found: {handoff_id}")
        return self._handoff(row)

    def for_song(self, song_id: str) -> tuple[ProfessionalHandoff, ...]:
        if self.store.get_song(str(song_id)) is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        rows = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,upstream_version_id,spec_id,spec_json,"
            "provided_inputs_json,missing_inputs_json,expected_current_version_id,"
            "expected_approved_version_id,package_fingerprint,state,status_reason,"
            "acceptance_receipt,supersedes_handoff_id "
            "FROM professional_handoffs WHERE song_id=? ORDER BY seq",
            (str(song_id),),
        ).fetchall()
        return tuple(self._handoff(row) for row in rows)

    def _freshness_reason(self, handoff: ProfessionalHandoff) -> str | None:
        song = self.store.get_song(handoff.song_id)
        if song is None:
            return "handoff Song disappeared"
        upstream = self.store.get_version(handoff.upstream_version_id)
        if upstream is None or upstream.song_id != handoff.song_id:
            return "handoff upstream Version disappeared or crossed Song"
        policy = handoff.spec.version_policy
        if policy in {"CURRENT", "CURRENT_APPROVED"}:
            if song.current_version_id != handoff.expected_current_version_id:
                return "current Version changed after handoff package was prepared"
        if policy == "CURRENT_APPROVED":
            if song.approved_version_id != handoff.expected_approved_version_id:
                return "approved Version changed after handoff package was prepared"
        return None

    def _mark_stale_locked(
        self, handoff: ProfessionalHandoff, reason: str
    ) -> str:
        clean_reason = _clean_text(reason, "stale reason", _MAX_TEXT_CHARS)
        changed = self._conn.execute(
            "UPDATE professional_handoffs SET state='STALE',status_reason=? "
            "WHERE id=? AND state=?",
            (clean_reason, handoff.id, handoff.state),
        )
        if changed.rowcount != 1:
            raise StaleProfessionalHandoffError(
                "professional handoff state changed before stale mark committed"
            )
        return clean_reason

    def _mark_stale(
        self, handoff: ProfessionalHandoff, reason: str
    ) -> ProfessionalHandoff:
        if handoff.state in {"RETURNED", "STALE"}:
            return handoff
        with self.store._tx():
            current = self.get(handoff.id)
            if current.state in {"RETURNED", "STALE"}:
                return current
            self._mark_stale_locked(current, reason)
        return self.get(handoff.id)

    def verify_freshness(self, handoff_id: str) -> HandoffFreshness:
        status: str
        reason: str | None
        with self.store._tx():
            handoff = self.get(handoff_id)
            if handoff.state == "RETURNED":
                status = "RETURNED"
                reason = handoff.status_reason
            elif handoff.state == "STALE":
                status = "STALE"
                reason = handoff.status_reason
            else:
                reason = self._freshness_reason(handoff)
                if reason is not None:
                    reason = self._mark_stale_locked(handoff, reason)
                    status = "STALE"
                elif handoff.state == "ACCEPTED":
                    status = "CURRENT"
                else:
                    status = "PENDING"
        durable = self.get(handoff_id)
        return HandoffFreshness(status, durable, reason)

    def accept(self, handoff_id: str) -> ProfessionalHandoff:
        stale_reason: str | None = None
        receipt: str | None = None
        with self.store._tx():
            handoff = self.get(handoff_id)
            if handoff.state != "SUBMITTED":
                raise ValidationError(
                    "only a complete SUBMITTED professional handoff can be accepted"
                )
            if handoff.missing_inputs:
                raise ProfessionalHandoffIntegrityError(
                    "SUBMITTED professional handoff unexpectedly has missing inputs"
                )
            stale_reason = self._freshness_reason(handoff)
            if stale_reason is not None:
                stale_reason = self._mark_stale_locked(handoff, stale_reason)
            else:
                receipt = _sha256(
                    {
                        "handoff_id": handoff.id,
                        "package_fingerprint": handoff.package_fingerprint,
                        "receipt_kind": "ACCEPTED",
                    }
                )
                changed = self._conn.execute(
                    "UPDATE professional_handoffs "
                    "SET state='ACCEPTED',status_reason=NULL,acceptance_receipt=? "
                    "WHERE id=? AND state='SUBMITTED'",
                    (receipt, handoff.id),
                )
                if changed.rowcount != 1:
                    raise StaleProfessionalHandoffError(
                        "professional handoff changed before acceptance committed"
                    )

        if stale_reason is not None:
            raise StaleProfessionalHandoffError(stale_reason)
        accepted = self.get(handoff_id)
        if receipt is None or accepted.acceptance_receipt != receipt:
            raise ProfessionalHandoffIntegrityError(
                "professional handoff acceptance receipt did not persist"
            )
        return accepted

    def return_submission(
        self, handoff_id: str, *, reason: str
    ) -> ProfessionalHandoff:
        handoff = self.get(handoff_id)
        if handoff.state != "SUBMITTED":
            raise ValidationError(
                "only SUBMITTED professional handoff can be returned"
            )
        clean_reason = _clean_text(reason, "return reason", _MAX_TEXT_CHARS)
        with self.store._tx():
            changed = self._conn.execute(
                "UPDATE professional_handoffs "
                "SET state='RETURNED',status_reason=? "
                "WHERE id=? AND state='SUBMITTED'",
                (clean_reason, handoff.id),
            )
            if changed.rowcount != 1:
                raise StaleProfessionalHandoffError(
                    "professional handoff changed before return committed"
                )
        return self.get(handoff.id)

    def resubmit(
        self,
        handoff_id: str,
        *,
        upstream_version_id: str,
        inputs: Mapping[str, str],
    ) -> ProfessionalHandoff:
        prior = self.get(handoff_id)
        if prior.state not in {"RETURNED", "STALE"}:
            raise ValidationError(
                "professional handoff can be resubmitted only after RETURNED or STALE"
            )
        return self.submit(
            spec=prior.spec,
            song_id=prior.song_id,
            upstream_version_id=upstream_version_id,
            inputs=inputs,
            supersedes_handoff_id=prior.id,
        )
