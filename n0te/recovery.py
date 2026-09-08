from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .lineage import LineageError, LineageStore

_PROFILE_ID = re.compile(r"^prf_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RecoveryError(LineageError):
    """Base error for explicit local canonical-memory recovery."""


class SnapshotNotFoundError(RecoveryError):
    pass


class SnapshotValidationError(RecoveryError):
    pass


class SnapshotHashMismatchError(RecoveryError):
    pass


@dataclass(frozen=True)
class SnapshotInfo:
    profile_id: str
    path: Path
    sha256: str
    size_bytes: int
    lineage_schema_version: str


@dataclass(frozen=True)
class RestoreResult:
    snapshot: SnapshotInfo
    preserved_database: Path | None
    installed_sha256: str


class RecoveryManager:
    """Explicit local snapshot/inspect/restore for the canonical profile database.

    This never participates in normal HeadquartersMemory.open(). A corrupt live
    database stays a visible failure until the caller explicitly inspects and
    restores a known snapshot by exact SHA-256.
    """

    SNAPSHOT_NAME = "lineage.snapshot.sqlite3"

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("RecoveryManager requires the canonical LineageStore")
        self.store = store

    @staticmethod
    def _validate_profile_id(profile_id: str) -> str:
        profile_id = str(profile_id)
        if not _PROFILE_ID.fullmatch(profile_id):
            raise SnapshotValidationError("invalid recovery profile_id")
        return profile_id

    @classmethod
    def _profile_dir(cls, root: str | Path, profile_id: str) -> Path:
        profile_id = cls._validate_profile_id(profile_id)
        return Path(root) / "profiles" / profile_id

    @classmethod
    def snapshot_path(cls, root: str | Path, profile_id: str) -> Path:
        return cls._profile_dir(root, profile_id) / "recovery" / cls.SNAPSHOT_NAME

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_file(path: Path) -> None:
        # Windows maps os.fsync() to a descriptor commit that requires a
        # write-capable handle. Open the already-writable file in update mode
        # instead of weakening the durability guarantee by suppressing fsync.
        with path.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @classmethod
    def _inspect_database(cls, path: Path, expected_profile_id: str) -> SnapshotInfo:
        if not path.is_file():
            raise SnapshotNotFoundError(f"snapshot/database not found: {path}")
        conn = None
        rows: dict[str, str] = {}
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            quick = conn.execute("PRAGMA quick_check").fetchone()
            if quick is None or str(quick[0]) != "ok":
                raise SnapshotValidationError("snapshot SQLite integrity check failed")
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise SnapshotValidationError("snapshot foreign-key integrity check failed")
            rows = {
                str(row["key"]): str(row["value"])
                for row in conn.execute(
                    "SELECT key,value FROM metadata WHERE key IN ('profile_id','schema_version')"
                )
            }
        except SnapshotValidationError:
            raise
        except sqlite3.DatabaseError as exc:
            raise SnapshotValidationError("snapshot is not a readable canonical SQLite database") from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if rows.get("profile_id") != expected_profile_id:
            raise SnapshotValidationError("snapshot embedded profile identity does not match target profile")
        if not rows.get("schema_version"):
            raise SnapshotValidationError("snapshot lineage schema metadata is missing")
        digest = cls._sha256(path)
        return SnapshotInfo(
            profile_id=expected_profile_id,
            path=path,
            sha256=digest,
            size_bytes=path.stat().st_size,
            lineage_schema_version=rows["schema_version"],
        )

    def create_snapshot(self) -> SnapshotInfo:
        profile_id = self.store.profile_id
        recovery_dir = self._profile_dir(self.store.root, profile_id) / "recovery"
        recovery_dir.mkdir(parents=True, exist_ok=True)
        final_path = recovery_dir / self.SNAPSHOT_NAME
        temp_path = recovery_dir / f".{self.SNAPSHOT_NAME}.{uuid.uuid4().hex}.tmp"
        dest = None
        try:
            dest = sqlite3.connect(temp_path, timeout=5.0)
            self.store._conn.backup(dest)
            dest.close()
            dest = None
            info = self._inspect_database(temp_path, profile_id)
            self._fsync_file(temp_path)
            os.replace(temp_path, final_path)
            self._fsync_dir(recovery_dir)
            return SnapshotInfo(
                profile_id=info.profile_id,
                path=final_path,
                sha256=self._sha256(final_path),
                size_bytes=final_path.stat().st_size,
                lineage_schema_version=info.lineage_schema_version,
            )
        except RecoveryError:
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            raise RecoveryError(f"could not create local recovery snapshot: {exc}") from exc
        finally:
            if dest is not None:
                try:
                    dest.close()
                except Exception:
                    pass
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def inspect_snapshot(cls, root: str | Path, profile_id: str) -> SnapshotInfo:
        profile_id = cls._validate_profile_id(profile_id)
        return cls._inspect_database(cls.snapshot_path(root, profile_id), profile_id)

    @classmethod
    def restore_snapshot(
        cls,
        root: str | Path,
        profile_id: str,
        *,
        expected_sha256: str,
    ) -> RestoreResult:
        profile_id = cls._validate_profile_id(profile_id)
        expected_sha256 = str(expected_sha256).strip().lower()
        if not _SHA256.fullmatch(expected_sha256):
            raise SnapshotHashMismatchError("restore requires an exact lowercase SHA-256")
        snapshot = cls.inspect_snapshot(root, profile_id)
        if snapshot.sha256 != expected_sha256:
            raise SnapshotHashMismatchError("snapshot SHA-256 does not match the explicitly authorized restore hash")

        profile_dir = cls._profile_dir(root, profile_id)
        recovery_dir = profile_dir / "recovery"
        live_path = profile_dir / LineageStore.DB_NAME
        temp_install = profile_dir / f".{LineageStore.DB_NAME}.restore.{uuid.uuid4().hex}.tmp"
        preserve_id = uuid.uuid4().hex
        preserved = recovery_dir / f"lineage.pre-restore.{preserve_id}.sqlite3" if live_path.exists() else None
        moved_sidecars: list[tuple[Path, Path]] = []
        recovery_dir.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copyfile(snapshot.path, temp_install)
            cls._fsync_file(temp_install)
            staged = cls._inspect_database(temp_install, profile_id)
            if staged.sha256 != expected_sha256:
                raise SnapshotHashMismatchError("staged restore copy changed before installation")

            if preserved is not None:
                shutil.copyfile(live_path, preserved)
                cls._fsync_file(preserved)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(live_path) + suffix)
                if sidecar.exists():
                    preserved_sidecar = (
                        Path(str(preserved) + suffix)
                        if preserved is not None
                        else recovery_dir / f"lineage.pre-restore.{preserve_id}.sqlite3{suffix}"
                    )
                    os.replace(sidecar, preserved_sidecar)
                    moved_sidecars.append((sidecar, preserved_sidecar))

            try:
                os.replace(temp_install, live_path)
            except Exception:
                for original, saved in reversed(moved_sidecars):
                    if saved.exists() and not original.exists():
                        os.replace(saved, original)
                raise
            cls._fsync_file(live_path)
            cls._fsync_dir(profile_dir)
            installed = cls._inspect_database(live_path, profile_id)
            if installed.sha256 != expected_sha256:
                raise SnapshotValidationError("installed restore does not match the verified snapshot")
            return RestoreResult(
                snapshot=snapshot,
                preserved_database=preserved,
                installed_sha256=installed.sha256,
            )
        except RecoveryError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise RecoveryError(f"could not restore local recovery snapshot: {exc}") from exc
        finally:
            try:
                temp_install.unlink(missing_ok=True)
            except OSError:
                pass
