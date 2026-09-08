from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA_VERSION = 1
_PROFILE_ID = re.compile(r"^prf_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LineageError(RuntimeError):
    """Base error for the N0TE2 canonical Song lineage store."""


class LineageCorruptionError(LineageError):
    """Durable state exists but cannot be trusted or interpreted safely."""


class NotFoundError(LineageError):
    """Requested object does not exist in the active profile."""


class ValidationError(LineageError):
    """Caller supplied invalid or cross-boundary lineage data."""


@dataclass(frozen=True)
class Artist:
    id: str
    display_name: str


@dataclass(frozen=True)
class Song:
    id: str
    artist_id: str
    title: str
    current_version_id: str | None
    approved_version_id: str | None


@dataclass(frozen=True)
class Asset:
    id: str
    song_id: str
    name: str
    sha256: str
    source_uri: str | None


@dataclass(frozen=True)
class Version:
    id: str
    song_id: str
    ordinal: int
    label: str
    parent_version_id: str | None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _require_text(value: str, field: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValidationError(f"{field} must not be empty")
    return value


class LineageStore:
    """
    Canonical local Artist/Song identity and exact artifact/version lineage.

    Each profile has one physical lineage database. The physical separation is
    intentional defense-in-depth for clean-profile isolation; the semantic
    model remains one canonical store abstraction.
    """

    DB_NAME = "lineage.sqlite3"

    def __init__(self, root: Path, profile_id: str, conn: sqlite3.Connection):
        self.root = Path(root)
        self.profile_id = profile_id
        self._conn = conn

    @classmethod
    def create(cls, root: str | Path, artist_name: str) -> "LineageStore":
        root = Path(root)
        artist_name = _require_text(artist_name, "artist_name")
        profile_id = _new_id("prf")
        profile_dir = root / "profiles" / profile_id
        profile_dir.mkdir(parents=True, exist_ok=False)
        db_path = profile_dir / cls.DB_NAME
        conn = cls._connect(db_path)
        try:
            cls._initialize(conn, profile_id, artist_name)
            cls._validate_existing(conn, profile_id)
        except Exception:
            conn.close()
            raise
        return cls(root, profile_id, conn)

    @classmethod
    def open(cls, root: str | Path, profile_id: str) -> "LineageStore":
        root = Path(root)
        cls._validate_profile_id(profile_id)
        db_path = root / "profiles" / profile_id / cls.DB_NAME
        if not db_path.is_file():
            raise NotFoundError(f"profile not found: {profile_id}")
        conn: sqlite3.Connection | None = None
        try:
            conn = cls._connect(db_path)
            cls._validate_existing(conn, profile_id)
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            raise LineageCorruptionError(f"lineage database is unreadable: {profile_id}") from exc
        except LineageCorruptionError:
            if conn is not None:
                conn.close()
            raise
        except Exception:
            if conn is not None:
                conn.close()
            raise
        return cls(root, profile_id, conn)

    @staticmethod
    def _validate_profile_id(profile_id: str) -> None:
        if not _PROFILE_ID.fullmatch(str(profile_id)):
            raise ValidationError("invalid profile_id")

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        except Exception:
            conn.close()
            raise
        return conn

    @classmethod
    def _initialize(cls, conn: sqlite3.Connection, profile_id: str, artist_name: str) -> None:
        artist_id = _new_id("art")
        schema = """
        BEGIN IMMEDIATE;

        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE artists (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL CHECK(length(trim(display_name)) > 0)
        );

        CREATE TABLE songs (
            id TEXT PRIMARY KEY,
            artist_id TEXT NOT NULL REFERENCES artists(id),
            title TEXT NOT NULL CHECK(length(trim(title)) > 0),
            current_version_id TEXT NULL REFERENCES versions(id),
            approved_version_id TEXT NULL REFERENCES versions(id)
        );

        CREATE TABLE assets (
            id TEXT PRIMARY KEY,
            song_id TEXT NOT NULL REFERENCES songs(id),
            name TEXT NOT NULL CHECK(length(trim(name)) > 0),
            sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
            source_uri TEXT NULL
        );

        CREATE TABLE versions (
            id TEXT PRIMARY KEY,
            song_id TEXT NOT NULL REFERENCES songs(id),
            ordinal INTEGER NOT NULL CHECK(ordinal > 0),
            label TEXT NOT NULL CHECK(length(trim(label)) > 0),
            parent_version_id TEXT NULL REFERENCES versions(id),
            UNIQUE(song_id, ordinal)
        );

        CREATE TABLE version_assets (
            version_id TEXT NOT NULL REFERENCES versions(id),
            asset_id TEXT NOT NULL REFERENCES assets(id),
            role TEXT NOT NULL CHECK(length(trim(role)) > 0),
            PRIMARY KEY(version_id, asset_id, role)
        );

        CREATE TRIGGER version_parent_same_song
        BEFORE INSERT ON versions
        WHEN NEW.parent_version_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM versions p
                 WHERE p.id = NEW.parent_version_id AND p.song_id = NEW.song_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'parent version belongs to a different song');
        END;

        CREATE TRIGGER song_current_same_song
        BEFORE UPDATE OF current_version_id ON songs
        WHEN NEW.current_version_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM versions v
                 WHERE v.id = NEW.current_version_id AND v.song_id = NEW.id
             )
        BEGIN
            SELECT RAISE(ABORT, 'current version belongs to a different song');
        END;

        CREATE TRIGGER song_approved_same_song
        BEFORE UPDATE OF approved_version_id ON songs
        WHEN NEW.approved_version_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM versions v
                 WHERE v.id = NEW.approved_version_id AND v.song_id = NEW.id
             )
        BEGIN
            SELECT RAISE(ABORT, 'approved version belongs to a different song');
        END;

        CREATE TRIGGER version_asset_same_song
        BEFORE INSERT ON version_assets
        WHEN NOT EXISTS (
            SELECT 1
            FROM versions v
            JOIN assets a ON a.song_id = v.song_id
            WHERE v.id = NEW.version_id AND a.id = NEW.asset_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'asset belongs to a different song');
        END;

        CREATE TRIGGER immutable_versions_update
        BEFORE UPDATE ON versions BEGIN
            SELECT RAISE(ABORT, 'version lineage is immutable');
        END;

        CREATE TRIGGER immutable_versions_delete
        BEFORE DELETE ON versions BEGIN
            SELECT RAISE(ABORT, 'version lineage is immutable');
        END;

        CREATE TRIGGER immutable_assets_update
        BEFORE UPDATE ON assets BEGIN
            SELECT RAISE(ABORT, 'asset identity is immutable');
        END;

        CREATE TRIGGER immutable_assets_delete
        BEFORE DELETE ON assets BEGIN
            SELECT RAISE(ABORT, 'asset identity is immutable');
        END;

        CREATE TRIGGER immutable_version_assets_update
        BEFORE UPDATE ON version_assets BEGIN
            SELECT RAISE(ABORT, 'version artifact lineage is immutable');
        END;

        CREATE TRIGGER immutable_version_assets_delete
        BEFORE DELETE ON version_assets BEGIN
            SELECT RAISE(ABORT, 'version artifact lineage is immutable');
        END;
        """
        try:
            # executescript() may commit a transaction that was opened before the
            # call. BEGIN therefore belongs inside the script so schema DDL and
            # seed rows share one all-or-nothing transaction until conn.commit().
            conn.executescript(schema)
            conn.executemany(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("profile_id", profile_id),
                    ("primary_artist_id", artist_id),
                    ("active_song_id", ""),
                ],
            )
            conn.execute(
                "INSERT INTO artists(id, display_name) VALUES(?, ?)",
                (artist_id, artist_name),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @classmethod
    def _validate_existing(cls, conn: sqlite3.Connection, expected_profile_id: str) -> None:
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise LineageCorruptionError("lineage integrity check failed")
            fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errors:
                raise LineageCorruptionError("lineage foreign-key integrity check failed")
            rows = {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key, value FROM metadata WHERE key IN "
                    "('schema_version', 'profile_id', 'primary_artist_id', 'active_song_id')"
                )
            }
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("lineage database is unreadable") from exc
        required = {"schema_version", "profile_id", "primary_artist_id", "active_song_id"}
        if set(rows) != required:
            raise LineageCorruptionError("lineage metadata is incomplete")
        if rows["schema_version"] != str(SCHEMA_VERSION):
            raise LineageCorruptionError(
                f"unsupported lineage schema version: {rows['schema_version']}"
            )
        if rows["profile_id"] != expected_profile_id:
            raise LineageCorruptionError("profile identity does not match durable state")
        artist = conn.execute(
            "SELECT 1 FROM artists WHERE id = ?", (rows["primary_artist_id"],)
        ).fetchone()
        if artist is None:
            raise LineageCorruptionError("primary artist identity is missing")
        active_song_id = rows["active_song_id"]
        if active_song_id:
            active = conn.execute(
                "SELECT 1 FROM songs WHERE id = ?", (active_song_id,)
            ).fetchone()
            if active is None:
                raise LineageCorruptionError("active Song identity is missing")

    @contextmanager
    def _tx(self) -> Iterator[None]:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @property
    def database_path(self) -> Path:
        return self.root / "profiles" / self.profile_id / self.DB_NAME

    @property
    def primary_artist_id(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = 'primary_artist_id'"
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("primary artist metadata disappeared")
        return str(row["value"])

    def artist(self) -> Artist:
        row = self._conn.execute(
            "SELECT id, display_name FROM artists WHERE id = ?",
            (self.primary_artist_id,),
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("primary artist disappeared")
        return Artist(id=row["id"], display_name=row["display_name"])

    def create_song(self, title: str) -> Song:
        title = _require_text(title, "title")
        song_id = _new_id("song")
        with self._tx():
            self._conn.execute(
                "INSERT INTO songs(id, artist_id, title) VALUES(?, ?, ?)",
                (song_id, self.primary_artist_id, title),
            )
            self._conn.execute(
                "UPDATE metadata SET value = ? WHERE key = 'active_song_id'",
                (song_id,),
            )
        song = self.get_song(song_id)
        assert song is not None
        return song

    def select_song(self, song_id: str) -> Song:
        song = self._require_song(song_id)
        with self._tx():
            self._conn.execute(
                "UPDATE metadata SET value = ? WHERE key = 'active_song_id'",
                (song.id,),
            )
        return song

    def active_song(self) -> Song | None:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = 'active_song_id'"
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("active Song metadata disappeared")
        song_id = str(row["value"])
        if not song_id:
            return None
        song = self.get_song(song_id)
        if song is None:
            raise LineageCorruptionError("active Song identity is missing")
        return song

    def get_song(self, song_id: str) -> Song | None:
        row = self._conn.execute(
            "SELECT id, artist_id, title, current_version_id, approved_version_id "
            "FROM songs WHERE id = ?",
            (song_id,),
        ).fetchone()
        if row is None:
            return None
        return Song(
            id=row["id"],
            artist_id=row["artist_id"],
            title=row["title"],
            current_version_id=row["current_version_id"],
            approved_version_id=row["approved_version_id"],
        )

    def _require_song(self, song_id: str) -> Song:
        song = self.get_song(song_id)
        if song is None:
            raise NotFoundError(f"Song not found in profile {self.profile_id}: {song_id}")
        return song

    def attach_asset(
        self,
        song_id: str,
        *,
        name: str,
        sha256: str,
        source_uri: str | None = None,
    ) -> Asset:
        self._require_song(song_id)
        name = _require_text(name, "name")
        digest = str(sha256).strip().lower()
        if not _SHA256.fullmatch(digest):
            raise ValidationError("sha256 must be a 64-character hexadecimal digest")
        asset_id = _new_id("asset")
        with self._tx():
            self._conn.execute(
                "INSERT INTO assets(id, song_id, name, sha256, source_uri) "
                "VALUES(?, ?, ?, ?, ?)",
                (asset_id, song_id, name, digest, source_uri),
            )
        return Asset(asset_id, song_id, name, digest, source_uri)

    def get_asset(self, asset_id: str) -> Asset | None:
        row = self._conn.execute(
            "SELECT id, song_id, name, sha256, source_uri FROM assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
        if row is None:
            return None
        return Asset(
            row["id"], row["song_id"], row["name"], row["sha256"], row["source_uri"]
        )

    def _require_asset_for_song(self, asset_id: str, song_id: str) -> Asset:
        asset = self.get_asset(asset_id)
        if asset is None:
            raise NotFoundError(
                f"asset not found in profile {self.profile_id}: {asset_id}"
            )
        if asset.song_id != song_id:
            raise ValidationError("asset belongs to a different Song")
        return asset

    def create_version(
        self,
        song_id: str,
        *,
        label: str,
        parent_version_id: str | None = None,
        asset_ids: Iterable[str] = (),
        make_current: bool = True,
    ) -> Version:
        self._require_song(song_id)
        label = _require_text(label, "label")
        if parent_version_id is not None:
            parent = self.get_version(parent_version_id)
            if parent is None:
                raise NotFoundError(f"parent version not found: {parent_version_id}")
            if parent.song_id != song_id:
                raise ValidationError("parent version belongs to a different Song")
        normalized_assets = tuple(dict.fromkeys(str(x) for x in asset_ids))
        for asset_id in normalized_assets:
            self._require_asset_for_song(asset_id, song_id)

        version_id = _new_id("ver")
        with self._tx():
            row = self._conn.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS next_ordinal "
                "FROM versions WHERE song_id = ?",
                (song_id,),
            ).fetchone()
            ordinal = int(row["next_ordinal"])
            self._conn.execute(
                "INSERT INTO versions(id, song_id, ordinal, label, parent_version_id) "
                "VALUES(?, ?, ?, ?, ?)",
                (version_id, song_id, ordinal, label, parent_version_id),
            )
            for asset_id in normalized_assets:
                self._conn.execute(
                    "INSERT INTO version_assets(version_id, asset_id, role) "
                    "VALUES(?, ?, 'SOURCE')",
                    (version_id, asset_id),
                )
            if make_current:
                self._conn.execute(
                    "UPDATE songs SET current_version_id = ? WHERE id = ?",
                    (version_id, song_id),
                )
        version = self.get_version(version_id)
        assert version is not None
        return version

    def get_version(self, version_id: str) -> Version | None:
        row = self._conn.execute(
            "SELECT id, song_id, ordinal, label, parent_version_id "
            "FROM versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if row is None:
            return None
        return Version(
            id=row["id"],
            song_id=row["song_id"],
            ordinal=int(row["ordinal"]),
            label=row["label"],
            parent_version_id=row["parent_version_id"],
        )

    def versions_for_song(self, song_id: str) -> tuple[Version, ...]:
        self._require_song(song_id)
        rows = self._conn.execute(
            "SELECT id, song_id, ordinal, label, parent_version_id "
            "FROM versions WHERE song_id = ? ORDER BY ordinal ASC",
            (song_id,),
        ).fetchall()
        return tuple(
            Version(
                id=row["id"],
                song_id=row["song_id"],
                ordinal=int(row["ordinal"]),
                label=row["label"],
                parent_version_id=row["parent_version_id"],
            )
            for row in rows
        )

    def set_current_version(self, song_id: str, version_id: str) -> Song:
        self._require_song(song_id)
        version = self.get_version(version_id)
        if version is None:
            raise NotFoundError(f"version not found: {version_id}")
        if version.song_id != song_id:
            raise ValidationError("version belongs to a different Song")
        with self._tx():
            self._conn.execute(
                "UPDATE songs SET current_version_id = ? WHERE id = ?",
                (version_id, song_id),
            )
        return self._require_song(song_id)

    def approve_version(self, song_id: str, version_id: str) -> Song:
        self._require_song(song_id)
        version = self.get_version(version_id)
        if version is None:
            raise NotFoundError(f"version not found: {version_id}")
        if version.song_id != song_id:
            raise ValidationError("version belongs to a different Song")
        with self._tx():
            self._conn.execute(
                "UPDATE songs SET approved_version_id = ? WHERE id = ?",
                (version_id, song_id),
            )
        return self._require_song(song_id)

    def version_asset_ids(self, version_id: str) -> tuple[str, ...]:
        version = self.get_version(version_id)
        if version is None:
            raise NotFoundError(f"version not found: {version_id}")
        rows = self._conn.execute(
            "SELECT asset_id FROM version_assets WHERE version_id = ? ORDER BY asset_id",
            (version_id,),
        )
        return tuple(str(row["asset_id"]) for row in rows)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LineageStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()