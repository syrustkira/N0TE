from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

EVIDENCE_SCHEMA_VERSION = 2
SCOPE_KINDS = {"PROFILE", "ARTIST", "SONG", "VERSION"}
SOURCE_KINDS = {
    "USER_DECLARED",
    "OBSERVED",
    "MEASURED",
    "PROVIDER_VERIFIED",
    "REMEMBERED",
    "INFERRED",
}
TWIN_DOMAINS = {"TECHNICAL", "CREATIVE", "UNSPECIFIED"}
RESOLUTION_STATUSES = {"UNKNOWN", "RESOLVED", "CONFLICT"}


@dataclass(frozen=True)
class EvidenceClaim:
    id: str
    sequence: int
    scope_kind: str
    scope_id: str
    key: str
    value: Any
    source_kind: str
    source_ref: str | None
    confidence: float
    twin_domain: str = "UNSPECIFIED"


@dataclass(frozen=True)
class EvidenceResolution:
    status: str
    key: str
    scope_kind: str | None
    scope_id: str | None
    value: Any | None
    claims: tuple[EvidenceClaim, ...]

    @property
    def claim_ids(self) -> tuple[str, ...]:
        return tuple(claim.id for claim in self.claims)


class EvidenceMemory:
    """Append-only scoped evidence inside the canonical LineageStore database.

    Scope specificity decides *where* to look, never which contradictory belief
    is true. Twin domain is orthogonal to source kind: TECHNICAL records what
    physically/technically exists, CREATIVE records intent/meaning/taste, and
    UNSPECIFIED preserves older or not-yet-classified evidence without guessing.
    """

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("EvidenceMemory requires the canonical LineageStore")
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

    def _column_names(self, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in self._conn.execute(f"PRAGMA table_info({table})")
        }

    def _create_schema_v2(self) -> None:
        script = f"""
        BEGIN IMMEDIATE;

        CREATE TABLE evidence_claims (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            scope_kind TEXT NOT NULL CHECK(scope_kind IN ('PROFILE','ARTIST','SONG','VERSION')),
            scope_id TEXT NOT NULL,
            key TEXT NOT NULL CHECK(length(trim(key)) > 0),
            value_json TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('USER_DECLARED','OBSERVED','MEASURED','PROVIDER_VERIFIED','REMEMBERED','INFERRED')),
            source_ref TEXT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
            twin_domain TEXT NOT NULL DEFAULT 'UNSPECIFIED'
                CHECK(twin_domain IN ('TECHNICAL','CREATIVE','UNSPECIFIED'))
        );

        CREATE TABLE evidence_supersessions (
            new_claim_id TEXT NOT NULL REFERENCES evidence_claims(id),
            old_claim_id TEXT NOT NULL REFERENCES evidence_claims(id),
            PRIMARY KEY(new_claim_id, old_claim_id),
            CHECK(new_claim_id <> old_claim_id)
        );

        CREATE INDEX evidence_claim_lookup
        ON evidence_claims(scope_kind, scope_id, key, seq);

        CREATE INDEX evidence_superseded_lookup
        ON evidence_supersessions(old_claim_id);

        CREATE TRIGGER evidence_claims_immutable_update
        BEFORE UPDATE ON evidence_claims BEGIN
            SELECT RAISE(ABORT, 'evidence claims are immutable');
        END;

        CREATE TRIGGER evidence_claims_immutable_delete
        BEFORE DELETE ON evidence_claims BEGIN
            SELECT RAISE(ABORT, 'evidence claims are immutable');
        END;

        CREATE TRIGGER evidence_supersessions_immutable_update
        BEFORE UPDATE ON evidence_supersessions BEGIN
            SELECT RAISE(ABORT, 'evidence supersession is immutable');
        END;

        CREATE TRIGGER evidence_supersessions_immutable_delete
        BEFORE DELETE ON evidence_supersessions BEGIN
            SELECT RAISE(ABORT, 'evidence supersession is immutable');
        END;

        CREATE TRIGGER evidence_supersession_same_target
        BEFORE INSERT ON evidence_supersessions
        WHEN NOT EXISTS (
            SELECT 1
            FROM evidence_claims newer
            JOIN evidence_claims older ON older.id = NEW.old_claim_id
            WHERE newer.id = NEW.new_claim_id
              AND newer.scope_kind = older.scope_kind
              AND newer.scope_id = older.scope_id
              AND newer.key = older.key
              AND newer.seq > older.seq
        )
        BEGIN
            SELECT RAISE(ABORT, 'supersession must target an older claim with identical scope and key');
        END;

        INSERT INTO metadata(key, value)
        VALUES('evidence_schema_version', '{EVIDENCE_SCHEMA_VERSION}');

        COMMIT;
        """
        try:
            self._conn.executescript(script)
        except sqlite3.DatabaseError as exc:
            try:
                self._conn.rollback()
            except sqlite3.DatabaseError:
                pass
            raise LineageCorruptionError("cannot initialize scoped evidence schema") from exc

    def _migrate_v1_to_v2(self) -> None:
        try:
            with self.store._tx():
                columns = self._column_names("evidence_claims")
                if "twin_domain" not in columns:
                    self._conn.execute(
                        "ALTER TABLE evidence_claims "
                        "ADD COLUMN twin_domain TEXT NOT NULL DEFAULT 'UNSPECIFIED' "
                        "CHECK(twin_domain IN ('TECHNICAL','CREATIVE','UNSPECIFIED'))"
                    )
                self._conn.execute(
                    "UPDATE metadata SET value=? WHERE key='evidence_schema_version'",
                    (str(EVIDENCE_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot migrate scoped evidence schema v1 to v2"
            ) from exc

    def _ensure_schema(self) -> None:
        claims = self._table_exists("evidence_claims")
        supersessions = self._table_exists("evidence_supersessions")
        version = self._metadata_value("evidence_schema_version")

        if not claims and not supersessions and version is None:
            self._create_schema_v2()
            return
        if claims != supersessions or not claims or version is None:
            raise LineageCorruptionError(
                "scoped evidence schema metadata/table mismatch"
            )

        columns = self._column_names("evidence_claims")
        if version == "1":
            self._migrate_v1_to_v2()
            columns = self._column_names("evidence_claims")
            version = self._metadata_value("evidence_schema_version")
        if version != str(EVIDENCE_SCHEMA_VERSION):
            raise LineageCorruptionError(
                f"unsupported scoped evidence schema version: {version}"
            )
        if "twin_domain" not in columns:
            raise LineageCorruptionError(
                "scoped evidence schema v2 is missing twin_domain"
            )

    def _validate_existing(self) -> None:
        try:
            version = self._conn.execute(
                "SELECT value FROM metadata WHERE key = 'evidence_schema_version'"
            ).fetchone()
            if version is None or str(version["value"]) != str(EVIDENCE_SCHEMA_VERSION):
                raise LineageCorruptionError("unsupported scoped evidence schema version")

            rows = self._conn.execute(
                "SELECT seq, id, scope_kind, scope_id, key, value_json, source_kind, "
                "source_ref, confidence, twin_domain "
                "FROM evidence_claims ORDER BY seq"
            ).fetchall()
            for row in rows:
                self._validate_scope(
                    str(row["scope_kind"]), str(row["scope_id"]), corruption=True
                )
                if str(row["source_kind"]) not in SOURCE_KINDS:
                    raise LineageCorruptionError(
                        "scoped evidence contains invalid source kind"
                    )
                if str(row["twin_domain"]) not in TWIN_DOMAINS:
                    raise LineageCorruptionError(
                        "scoped evidence contains invalid Twin domain"
                    )
                confidence = float(row["confidence"])
                if not 0.0 <= confidence <= 1.0:
                    raise LineageCorruptionError(
                        "scoped evidence contains invalid confidence"
                    )
                try:
                    json.loads(str(row["value_json"]))
                except Exception as exc:
                    raise LineageCorruptionError(
                        "scoped evidence contains invalid JSON value"
                    ) from exc

            invalid_edges = self._conn.execute(
                "SELECT s.new_claim_id, s.old_claim_id "
                "FROM evidence_supersessions s "
                "LEFT JOIN evidence_claims n ON n.id = s.new_claim_id "
                "LEFT JOIN evidence_claims o ON o.id = s.old_claim_id "
                "WHERE n.id IS NULL OR o.id IS NULL "
                "OR n.scope_kind <> o.scope_kind OR n.scope_id <> o.scope_id "
                "OR n.key <> o.key OR n.seq <= o.seq"
            ).fetchall()
            if invalid_edges:
                raise LineageCorruptionError(
                    "scoped evidence supersession graph is invalid"
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "scoped evidence database is unreadable"
            ) from exc

    def _validate_scope(
        self, scope_kind: str, scope_id: str, *, corruption: bool = False
    ) -> None:
        kind = str(scope_kind).strip().upper()
        scope_id = str(scope_id).strip()
        error = LineageCorruptionError if corruption else ValidationError
        if kind not in SCOPE_KINDS:
            raise error(f"unsupported evidence scope: {kind}")
        if kind == "PROFILE":
            if scope_id != self.store.profile_id:
                raise error("evidence profile scope does not match active profile")
            return
        if kind == "ARTIST":
            if scope_id != self.store.primary_artist_id:
                raise error("evidence artist scope does not match active profile")
            return
        if kind == "SONG":
            if self.store.get_song(scope_id) is None:
                raise error("evidence Song scope does not exist in active profile")
            return
        version = self.store.get_version(scope_id)
        if version is None:
            raise error("evidence version scope does not exist in active profile")

    @staticmethod
    def _canonical_value(value: Any) -> str:
        try:
            return json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "evidence value must be valid JSON data"
            ) from exc

    @staticmethod
    def _new_claim_id() -> str:
        return f"claim_{uuid.uuid4().hex}"

    @staticmethod
    def _normalize_twin_domain(value: str) -> str:
        domain = str(value).strip().upper()
        if domain not in TWIN_DOMAINS:
            raise ValidationError(f"unsupported Twin domain: {domain}")
        return domain

    def get_claim(self, claim_id: str) -> EvidenceClaim | None:
        row = self._conn.execute(
            "SELECT seq, id, scope_kind, scope_id, key, value_json, source_kind, "
            "source_ref, confidence, twin_domain "
            "FROM evidence_claims WHERE id = ?",
            (claim_id,),
        ).fetchone()
        return None if row is None else self._claim_from_row(row)

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> EvidenceClaim:
        return EvidenceClaim(
            id=str(row["id"]),
            sequence=int(row["seq"]),
            scope_kind=str(row["scope_kind"]),
            scope_id=str(row["scope_id"]),
            key=str(row["key"]),
            value=json.loads(str(row["value_json"])),
            source_kind=str(row["source_kind"]),
            source_ref=(
                None if row["source_ref"] is None else str(row["source_ref"])
            ),
            confidence=float(row["confidence"]),
            twin_domain=str(row["twin_domain"]),
        )

    def active_claims_for_scope(
        self, scope_kind: str, scope_id: str
    ) -> tuple[EvidenceClaim, ...]:
        kind = str(scope_kind).strip().upper()
        self._validate_scope(kind, scope_id)
        rows = self._conn.execute(
            "SELECT c.seq, c.id, c.scope_kind, c.scope_id, c.key, c.value_json, "
            "c.source_kind, c.source_ref, c.confidence, c.twin_domain "
            "FROM evidence_claims c "
            "LEFT JOIN evidence_supersessions s ON s.old_claim_id = c.id "
            "WHERE c.scope_kind = ? AND c.scope_id = ? "
            "AND s.old_claim_id IS NULL ORDER BY c.seq",
            (kind, scope_id),
        ).fetchall()
        return tuple(self._claim_from_row(row) for row in rows)

    def active_claims(
        self, scope_kind: str, scope_id: str, key: str
    ) -> tuple[EvidenceClaim, ...]:
        key = str(key).strip()
        if not key:
            raise ValidationError("evidence key must not be empty")
        return tuple(
            claim
            for claim in self.active_claims_for_scope(scope_kind, scope_id)
            if claim.key == key
        )

    def record_claim(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        key: str,
        value: Any,
        source_kind: str,
        source_ref: str | None = None,
        confidence: float = 1.0,
        twin_domain: str = "UNSPECIFIED",
        supersedes: Iterable[str] = (),
    ) -> EvidenceClaim:
        kind = str(scope_kind).strip().upper()
        source = str(source_kind).strip().upper()
        domain = self._normalize_twin_domain(twin_domain)
        key = str(key).strip()
        if not key:
            raise ValidationError("evidence key must not be empty")
        self._validate_scope(kind, scope_id)
        if source not in SOURCE_KINDS:
            raise ValidationError(f"unsupported evidence source: {source}")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValidationError("confidence must be between 0 and 1") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValidationError("confidence must be between 0 and 1")
        value_json = self._canonical_value(value)
        old_ids = tuple(dict.fromkeys(str(item) for item in supersedes))
        active_by_id = {
            claim.id: claim for claim in self.active_claims(kind, scope_id, key)
        }
        for old_id in old_ids:
            old = self.get_claim(old_id)
            if old is None:
                raise NotFoundError(f"evidence claim not found: {old_id}")
            if old.scope_kind != kind or old.scope_id != scope_id or old.key != key:
                raise ValidationError(
                    "supersession cannot cross evidence scope or key"
                )
            if old_id not in active_by_id:
                raise ValidationError(
                    "supersession may target only currently active claims"
                )

        claim_id = self._new_claim_id()
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO evidence_claims("
                    "id, scope_kind, scope_id, key, value_json, source_kind, "
                    "source_ref, confidence, twin_domain"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        claim_id,
                        kind,
                        scope_id,
                        key,
                        value_json,
                        source,
                        source_ref,
                        confidence,
                        domain,
                    ),
                )
                for old_id in old_ids:
                    self._conn.execute(
                        "INSERT INTO evidence_supersessions("
                        "new_claim_id, old_claim_id"
                        ") VALUES(?, ?)",
                        (claim_id, old_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(
                f"invalid scoped evidence mutation: {exc}"
            ) from exc
        claim = self.get_claim(claim_id)
        assert claim is not None
        return claim

    def resolve_for_song(
        self,
        *,
        song_id: str,
        key: str,
        version_id: str | None = None,
    ) -> EvidenceResolution:
        song = self.store.get_song(song_id)
        if song is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        scopes: list[tuple[str, str]] = []
        if version_id is not None:
            version = self.store.get_version(version_id)
            if version is None:
                raise NotFoundError(f"version not found: {version_id}")
            if version.song_id != song_id:
                raise ValidationError("version belongs to a different Song")
            scopes.append(("VERSION", version_id))
        scopes.extend(
            [
                ("SONG", song_id),
                ("ARTIST", song.artist_id),
                ("PROFILE", self.store.profile_id),
            ]
        )
        for scope_kind, scope_id in scopes:
            claims = self.active_claims(scope_kind, scope_id, key)
            if not claims:
                continue
            canonical = {
                self._canonical_value(claim.value): claim.value
                for claim in claims
            }
            if len(canonical) == 1:
                value = next(iter(canonical.values()))
                return EvidenceResolution(
                    status="RESOLVED",
                    key=key,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    value=value,
                    claims=claims,
                )
            return EvidenceResolution(
                status="CONFLICT",
                key=key,
                scope_kind=scope_kind,
                scope_id=scope_id,
                value=None,
                claims=claims,
            )
        return EvidenceResolution(
            status="UNKNOWN",
            key=key,
            scope_kind=None,
            scope_id=None,
            value=None,
            claims=(),
        )

    def reconcile_for_song(
        self,
        *,
        song_id: str,
        key: str,
        value: Any,
        source_kind: str,
        source_ref: str | None = None,
        confidence: float = 1.0,
        twin_domain: str = "UNSPECIFIED",
        version_id: str | None = None,
    ) -> EvidenceClaim:
        resolution = self.resolve_for_song(
            song_id=song_id, key=key, version_id=version_id
        )
        if resolution.status == "UNKNOWN":
            raise ValidationError("there is no active evidence to reconcile")
        expected_kind = "VERSION" if version_id is not None else "SONG"
        expected_id = version_id if version_id is not None else song_id
        if (
            resolution.scope_kind != expected_kind
            or resolution.scope_id != expected_id
        ):
            raise ValidationError(
                "reconciliation must target the currently applicable explicit scope"
            )
        return self.record_claim(
            scope_kind=expected_kind,
            scope_id=expected_id,
            key=key,
            value=value,
            source_kind=source_kind,
            source_ref=source_ref,
            confidence=confidence,
            twin_domain=twin_domain,
            supersedes=resolution.claim_ids,
        )
