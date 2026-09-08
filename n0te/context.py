from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from .evidence import EvidenceClaim, EvidenceMemory
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

CONTEXT_IMPORT_SCHEMA_VERSION = 1
CONTEXT_IMPORT_SOURCES = {"IMPORTED", "SYNCED"}
CONTEXT_IMPORT_AUTHORITY = "EVIDENCE_ONLY"
CONTEXT_IMPORT_SCOPES = {"ARTIST", "SONG"}


@dataclass(frozen=True)
class ProductContext:
    context_id: str
    schema_version: int
    product_name: str
    primary_object: str
    artist_authority: str
    daw_role: str
    imported_context_authority: str

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "context_id": self.context_id,
                "schema_version": self.schema_version,
                "product_name": self.product_name,
                "primary_object": self.primary_object,
                "artist_authority": self.artist_authority,
                "daw_role": self.daw_role,
                "imported_context_authority": self.imported_context_authority,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


PRODUCT_CONTEXT = ProductContext(
    context_id="product:n0te:1",
    schema_version=1,
    product_name="N0TE",
    primary_object="SONG",
    artist_authority="ARTIST",
    daw_role="CREATIVE_WORKSPACE",
    imported_context_authority=CONTEXT_IMPORT_AUTHORITY,
)


@dataclass(frozen=True)
class ContextImport:
    id: str
    sequence: int
    scope_kind: str
    scope_id: str
    source_kind: str
    source_ref: str
    payload: Any
    authority: str = CONTEXT_IMPORT_AUTHORITY


@dataclass(frozen=True)
class ContextEnvelope:
    product: ProductContext
    profile_id: str
    artist_id: str
    artist_name: str
    song_id: str | None
    artist_claims: tuple[EvidenceClaim, ...]
    song_claims: tuple[EvidenceClaim, ...]
    imports: tuple[ContextImport, ...]


class ContextIsolationService:
    """Canonical boundary between generic product, private profile and imports.

    Product doctrine is not stored in the profile database. Imported/synced
    material is append-only EVIDENCE_ONLY context and is never inserted into
    EvidenceMemory or treated as action authority by this service.
    """

    def __init__(self, store: LineageStore, memory: EvidenceMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("ContextIsolationService requires LineageStore")
        if not isinstance(memory, EvidenceMemory) or memory.store is not store:
            raise TypeError(
                "ContextIsolationService requires EvidenceMemory for the same LineageStore"
            )
        self.store = store
        self.memory = memory
        self._conn = store._conn
        self._ensure_schema()
        self._validate_existing()

    @property
    def product(self) -> ProductContext:
        return PRODUCT_CONTEXT

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _metadata_value(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _ensure_schema(self) -> None:
        exists = self._table_exists("context_imports")
        version = self._metadata_value("context_import_schema_version")
        if exists and version == str(CONTEXT_IMPORT_SCHEMA_VERSION):
            return
        if exists or version is not None:
            raise LineageCorruptionError(
                "context import schema metadata/table mismatch"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE context_imports (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        scope_kind TEXT NOT NULL
                            CHECK(scope_kind IN ('ARTIST','SONG')),
                        scope_id TEXT NOT NULL,
                        source_kind TEXT NOT NULL
                            CHECK(source_kind IN ('IMPORTED','SYNCED')),
                        source_ref TEXT NOT NULL
                            CHECK(length(trim(source_ref)) > 0),
                        payload_json TEXT NOT NULL,
                        authority TEXT NOT NULL DEFAULT 'EVIDENCE_ONLY'
                            CHECK(authority = 'EVIDENCE_ONLY')
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX context_import_scope "
                    "ON context_imports(scope_kind,scope_id,seq)"
                )
                self._conn.execute(
                    """CREATE TRIGGER context_imports_immutable_update
                    BEFORE UPDATE ON context_imports BEGIN
                        SELECT RAISE(ABORT, 'imported context is append-only');
                    END"""
                )
                self._conn.execute(
                    """CREATE TRIGGER context_imports_immutable_delete
                    BEFORE DELETE ON context_imports BEGIN
                        SELECT RAISE(ABORT, 'imported context is append-only');
                    END"""
                )
                self._conn.execute(
                    "INSERT INTO metadata(key,value) "
                    "VALUES('context_import_schema_version',?)",
                    (str(CONTEXT_IMPORT_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot initialize context import schema"
            ) from exc

    def _validate_scope(
        self, scope_kind: str, scope_id: str, *, corruption: bool = False
    ) -> tuple[str, str]:
        kind = str(scope_kind).strip().upper()
        scope_id = str(scope_id).strip()
        error = LineageCorruptionError if corruption else ValidationError
        if kind not in CONTEXT_IMPORT_SCOPES:
            raise error(f"unsupported context import scope: {kind}")
        if kind == "ARTIST":
            if scope_id != self.store.primary_artist_id:
                raise error(
                    "context import artist scope does not match active profile"
                )
            return kind, scope_id
        song = self.store.get_song(scope_id)
        if song is None:
            raise error("context import Song scope does not exist in active profile")
        if song.artist_id != self.store.primary_artist_id:
            raise error("context import Song belongs to a different Artist")
        return kind, scope_id

    @staticmethod
    def _canonical_payload(payload: Any) -> str:
        try:
            return json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "imported context payload must be valid JSON data"
            ) from exc

    @staticmethod
    def _row(row: sqlite3.Row) -> ContextImport:
        return ContextImport(
            id=str(row["id"]),
            sequence=int(row["seq"]),
            scope_kind=str(row["scope_kind"]),
            scope_id=str(row["scope_id"]),
            source_kind=str(row["source_kind"]),
            source_ref=str(row["source_ref"]),
            payload=json.loads(str(row["payload_json"])),
            authority=str(row["authority"]),
        )

    def _validate_existing(self) -> None:
        try:
            version = self._metadata_value("context_import_schema_version")
            if version != str(CONTEXT_IMPORT_SCHEMA_VERSION):
                raise LineageCorruptionError(
                    f"unsupported context import schema version: {version}"
                )
            for row in self._conn.execute(
                "SELECT seq,id,scope_kind,scope_id,source_kind,source_ref,"
                "payload_json,authority FROM context_imports ORDER BY seq"
            ):
                self._validate_scope(
                    str(row["scope_kind"]),
                    str(row["scope_id"]),
                    corruption=True,
                )
                if str(row["source_kind"]) not in CONTEXT_IMPORT_SOURCES:
                    raise LineageCorruptionError(
                        "context import contains invalid source kind"
                    )
                if str(row["authority"]) != CONTEXT_IMPORT_AUTHORITY:
                    raise LineageCorruptionError(
                        "imported context acquired unauthorized authority"
                    )
                if not str(row["source_ref"]).strip():
                    raise LineageCorruptionError(
                        "context import source reference is empty"
                    )
                try:
                    json.loads(str(row["payload_json"]))
                except Exception as exc:
                    raise LineageCorruptionError(
                        "context import contains invalid JSON payload"
                    ) from exc
        except LineageCorruptionError:
            raise
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "context import ledger is unreadable"
            ) from exc

    def import_context(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        source_kind: str,
        source_ref: str,
        payload: Any,
    ) -> ContextImport:
        kind, scope_id = self._validate_scope(scope_kind, scope_id)
        source = str(source_kind).strip().upper()
        if source not in CONTEXT_IMPORT_SOURCES:
            raise ValidationError(f"unsupported context import source: {source}")
        source_ref = str(source_ref).strip()
        if not source_ref:
            raise ValidationError("context import source_ref must not be empty")
        payload_json = self._canonical_payload(payload)
        import_id = f"ctximp_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO context_imports("
                    "id,scope_kind,scope_id,source_kind,source_ref,payload_json,authority"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        import_id,
                        kind,
                        scope_id,
                        source,
                        source_ref,
                        payload_json,
                        CONTEXT_IMPORT_AUTHORITY,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(
                f"invalid context import mutation: {exc}"
            ) from exc
        row = self._conn.execute(
            "SELECT seq,id,scope_kind,scope_id,source_kind,source_ref,"
            "payload_json,authority FROM context_imports WHERE id=?",
            (import_id,),
        ).fetchone()
        assert row is not None
        return self._row(row)

    def imports_for(
        self, *, song_id: str | None = None
    ) -> tuple[ContextImport, ...]:
        artist_id = self.store.primary_artist_id
        if song_id is None:
            rows = self._conn.execute(
                "SELECT seq,id,scope_kind,scope_id,source_kind,source_ref,"
                "payload_json,authority FROM context_imports "
                "WHERE scope_kind='ARTIST' AND scope_id=? ORDER BY seq",
                (artist_id,),
            ).fetchall()
        else:
            self._validate_scope("SONG", song_id)
            rows = self._conn.execute(
                "SELECT seq,id,scope_kind,scope_id,source_kind,source_ref,"
                "payload_json,authority FROM context_imports "
                "WHERE (scope_kind='ARTIST' AND scope_id=?) "
                "OR (scope_kind='SONG' AND scope_id=?) ORDER BY seq",
                (artist_id, song_id),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def envelope(self, *, song_id: str | None = None) -> ContextEnvelope:
        artist = self.store.artist()
        if song_id is None:
            active = self.store.active_song()
            song_id = None if active is None else active.id
        if song_id is not None:
            self._validate_scope("SONG", song_id)

        artist_claims = self.memory.active_claims_for_scope(
            "ARTIST", artist.id
        )
        song_claims = (
            ()
            if song_id is None
            else self.memory.active_claims_for_scope("SONG", song_id)
        )
        return ContextEnvelope(
            product=PRODUCT_CONTEXT,
            profile_id=self.store.profile_id,
            artist_id=artist.id,
            artist_name=artist.display_name,
            song_id=song_id,
            artist_claims=artist_claims,
            song_claims=song_claims,
            imports=self.imports_for(song_id=song_id),
        )
