from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from .evidence import SOURCE_KINDS
from .lineage import Asset, LineageCorruptionError, LineageStore, NotFoundError, ValidationError, Version

PROVENANCE_SCHEMA_VERSION = 1
OBJECT_KINDS = {"VERSION", "ASSET"}
INPUT_KINDS = {"VERSION", "ASSET", "EXTERNAL"}


@dataclass(frozen=True)
class ProvenanceRecord:
    id: str
    sequence: int
    song_id: str
    output_kind: str
    output_id: str
    input_kind: str
    input_ref: str
    operation: str
    tool_ref: str | None
    provider_ref: str | None
    model_ref: str | None
    recipe_ref: str | None
    rights_ref: str | None
    consent_ref: str | None
    cost_ref: str | None
    evidence_source_kind: str
    evidence_ref: str | None


@dataclass(frozen=True)
class ProvenanceAsset:
    id: str
    name: str
    sha256: str
    records: tuple[ProvenanceRecord, ...]


@dataclass(frozen=True)
class VersionExplanation:
    version_id: str
    label: str
    ordinal: int
    parent_version_id: str | None
    attached_assets: tuple[ProvenanceAsset, ...]
    derivations: tuple[ProvenanceRecord, ...]


class ProvenanceLedger:
    """Immutable derivation evidence inside the canonical profile database."""

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("ProvenanceLedger requires the canonical LineageStore")
        self.store = store
        self._conn = store._conn
        self._ensure_schema()
        self._validate_existing()

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _metadata_value(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _ensure_schema(self) -> None:
        exists = self._table_exists("provenance_records")
        version = self._metadata_value("provenance_schema_version")
        if exists != (version is not None):
            raise LineageCorruptionError("provenance schema metadata/table mismatch")
        if exists:
            if version != str(PROVENANCE_SCHEMA_VERSION):
                raise LineageCorruptionError(f"unsupported provenance schema version: {version}")
            return
        if not self._table_exists("activity_events"):
            raise LineageCorruptionError("ProvenanceLedger requires canonical Activity before provenance")
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE provenance_records (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        output_kind TEXT NOT NULL CHECK(output_kind IN ('VERSION','ASSET')),
                        output_id TEXT NOT NULL,
                        input_kind TEXT NOT NULL CHECK(input_kind IN ('VERSION','ASSET','EXTERNAL')),
                        input_ref TEXT NOT NULL CHECK(length(trim(input_ref)) > 0),
                        operation TEXT NOT NULL CHECK(length(trim(operation)) > 0),
                        tool_ref TEXT NULL,
                        provider_ref TEXT NULL,
                        model_ref TEXT NULL,
                        recipe_ref TEXT NULL,
                        rights_ref TEXT NULL,
                        consent_ref TEXT NULL,
                        cost_ref TEXT NULL,
                        evidence_source_kind TEXT NOT NULL CHECK(evidence_source_kind IN ('USER_DECLARED','OBSERVED','MEASURED','PROVIDER_VERIFIED','REMEMBERED','INFERRED')),
                        evidence_ref TEXT NULL
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX provenance_output_lookup ON provenance_records(output_kind,output_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX provenance_song_lookup ON provenance_records(song_id,seq)"
                )
                self._conn.execute(
                    """CREATE TRIGGER provenance_records_immutable_update
                    BEFORE UPDATE ON provenance_records BEGIN
                        SELECT RAISE(ABORT, 'provenance records are immutable');
                    END"""
                )
                self._conn.execute(
                    """CREATE TRIGGER provenance_records_immutable_delete
                    BEFORE DELETE ON provenance_records BEGIN
                        SELECT RAISE(ABORT, 'provenance records are immutable');
                    END"""
                )
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('provenance_schema_version',?)",
                    (str(PROVENANCE_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize provenance schema") from exc

    def _canonical_object(self, kind: str, object_id: str):
        if kind == "VERSION":
            return self.store.get_version(object_id)
        if kind == "ASSET":
            return self.store.get_asset(object_id)
        return None

    @staticmethod
    def _song_id_for_object(obj: Version | Asset) -> str:
        return obj.song_id

    def _validate_object(self, kind: str, object_id: str, *, corruption: bool = False):
        error = LineageCorruptionError if corruption else ValidationError
        if kind not in OBJECT_KINDS:
            raise error(f"unsupported provenance object kind: {kind}")
        obj = self._canonical_object(kind, object_id)
        if obj is None:
            raise error(f"provenance {kind.lower()} object does not exist")
        return obj

    def _validate_existing(self) -> None:
        try:
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'provenance_records_immutable_%'"
                )
            }
            if trigger_names != {
                "provenance_records_immutable_update",
                "provenance_records_immutable_delete",
            }:
                raise LineageCorruptionError("provenance immutability hooks are incomplete")
            for row in self._conn.execute(
                "SELECT id,song_id,output_kind,output_id,input_kind,input_ref,evidence_source_kind FROM provenance_records ORDER BY seq"
            ):
                output = self._validate_object(str(row["output_kind"]), str(row["output_id"]), corruption=True)
                song_id = str(row["song_id"])
                if self._song_id_for_object(output) != song_id:
                    raise LineageCorruptionError("provenance output is bound to the wrong Song")
                input_kind = str(row["input_kind"])
                input_ref = str(row["input_ref"])
                if input_kind in OBJECT_KINDS:
                    source = self._validate_object(input_kind, input_ref, corruption=True)
                    if self._song_id_for_object(source) != song_id:
                        raise LineageCorruptionError("provenance canonical input crosses Songs")
                elif input_kind != "EXTERNAL" or not input_ref.strip():
                    raise LineageCorruptionError("provenance external input is invalid")
                if str(row["evidence_source_kind"]) not in SOURCE_KINDS:
                    raise LineageCorruptionError("provenance evidence source is invalid")
        except LineageCorruptionError:
            raise
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("provenance history is unreadable") from exc

    @staticmethod
    def _clean_optional(value: str | None) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @staticmethod
    def _record(row: sqlite3.Row) -> ProvenanceRecord:
        return ProvenanceRecord(
            id=str(row["id"]),
            sequence=int(row["seq"]),
            song_id=str(row["song_id"]),
            output_kind=str(row["output_kind"]),
            output_id=str(row["output_id"]),
            input_kind=str(row["input_kind"]),
            input_ref=str(row["input_ref"]),
            operation=str(row["operation"]),
            tool_ref=None if row["tool_ref"] is None else str(row["tool_ref"]),
            provider_ref=None if row["provider_ref"] is None else str(row["provider_ref"]),
            model_ref=None if row["model_ref"] is None else str(row["model_ref"]),
            recipe_ref=None if row["recipe_ref"] is None else str(row["recipe_ref"]),
            rights_ref=None if row["rights_ref"] is None else str(row["rights_ref"]),
            consent_ref=None if row["consent_ref"] is None else str(row["consent_ref"]),
            cost_ref=None if row["cost_ref"] is None else str(row["cost_ref"]),
            evidence_source_kind=str(row["evidence_source_kind"]),
            evidence_ref=None if row["evidence_ref"] is None else str(row["evidence_ref"]),
        )

    def record(
        self,
        *,
        output_kind: str,
        output_id: str,
        input_kind: str,
        input_ref: str,
        operation: str,
        evidence_source_kind: str,
        evidence_ref: str | None = None,
        tool_ref: str | None = None,
        provider_ref: str | None = None,
        model_ref: str | None = None,
        recipe_ref: str | None = None,
        rights_ref: str | None = None,
        consent_ref: str | None = None,
        cost_ref: str | None = None,
    ) -> ProvenanceRecord:
        output_kind = str(output_kind).strip().upper()
        input_kind = str(input_kind).strip().upper()
        operation = str(operation).strip()
        evidence_source_kind = str(evidence_source_kind).strip().upper()
        input_ref = str(input_ref).strip()
        if not operation:
            raise ValidationError("provenance operation must not be empty")
        if evidence_source_kind not in SOURCE_KINDS:
            raise ValidationError(f"unsupported provenance evidence source: {evidence_source_kind}")
        output = self._validate_object(output_kind, output_id)
        song_id = self._song_id_for_object(output)
        if input_kind in OBJECT_KINDS:
            source = self._validate_object(input_kind, input_ref)
            if self._song_id_for_object(source) != song_id:
                raise ValidationError("provenance canonical input and output must belong to the same Song")
        elif input_kind == "EXTERNAL":
            if not input_ref:
                raise ValidationError("external provenance input requires an explicit reference")
        else:
            raise ValidationError(f"unsupported provenance input kind: {input_kind}")

        record_id = f"prov_{uuid.uuid4().hex}"
        payload = json.dumps(
            {"provenance_id": record_id, "operation": operation},
            sort_keys=True,
            separators=(",", ":"),
        )
        values = (
            record_id,
            song_id,
            output_kind,
            output_id,
            input_kind,
            input_ref,
            operation,
            self._clean_optional(tool_ref),
            self._clean_optional(provider_ref),
            self._clean_optional(model_ref),
            self._clean_optional(recipe_ref),
            self._clean_optional(rights_ref),
            self._clean_optional(consent_ref),
            self._clean_optional(cost_ref),
            evidence_source_kind,
            self._clean_optional(evidence_ref),
        )
        try:
            with self.store._tx():
                self._conn.execute(
                    """INSERT INTO provenance_records(
                        id,song_id,output_kind,output_id,input_kind,input_ref,operation,
                        tool_ref,provider_ref,model_ref,recipe_ref,rights_ref,consent_ref,cost_ref,
                        evidence_source_kind,evidence_ref
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
                self._conn.execute(
                    """INSERT INTO activity_events(
                        id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        f"act_{uuid.uuid4().hex}",
                        "PROVENANCE_RECORDED",
                        self.store.primary_artist_id,
                        song_id,
                        output_id if output_kind == "VERSION" else None,
                        output_kind,
                        output_id,
                        payload,
                    ),
                )
        except sqlite3.DatabaseError as exc:
            raise ValidationError(f"invalid provenance mutation: {exc}") from exc
        row = self._conn.execute(
            "SELECT * FROM provenance_records WHERE id=?", (record_id,)
        ).fetchone()
        assert row is not None
        return self._record(row)

    def for_output(self, output_kind: str, output_id: str) -> tuple[ProvenanceRecord, ...]:
        kind = str(output_kind).strip().upper()
        self._validate_object(kind, output_id)
        rows = self._conn.execute(
            "SELECT * FROM provenance_records WHERE output_kind=? AND output_id=? ORDER BY seq",
            (kind, output_id),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def explain_version(self, version_id: str) -> VersionExplanation:
        version = self.store.get_version(version_id)
        if version is None:
            raise NotFoundError(f"version not found: {version_id}")
        assets = []
        for asset_id in self.store.version_asset_ids(version_id):
            asset = self.store.get_asset(asset_id)
            if asset is None or asset.song_id != version.song_id:
                raise LineageCorruptionError("version asset reference is missing or crosses Songs")
            assets.append(
                ProvenanceAsset(
                    id=asset.id,
                    name=asset.name,
                    sha256=asset.sha256,
                    records=self.for_output("ASSET", asset.id),
                )
            )
        return VersionExplanation(
            version_id=version.id,
            label=version.label,
            ordinal=version.ordinal,
            parent_version_id=version.parent_version_id,
            attached_assets=tuple(assets),
            derivations=self.for_output("VERSION", version.id),
        )
