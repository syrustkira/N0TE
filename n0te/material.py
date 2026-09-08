from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .lineage import Asset, LineageCorruptionError, LineageStore, NotFoundError, Version

MAX_MATERIAL_BYTES = 256 * 1024 * 1024
MATERIAL_URI_PREFIX = "n0te-material://sha256/"
_MATERIAL_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SongMaterialError(RuntimeError):
    """Invalid or unsafe local Song-material operation."""


class SongMaterialIntegrityError(SongMaterialError):
    """Managed material exists in lineage but its local bytes are not trustworthy."""


@dataclass(frozen=True)
class ManagedMaterial:
    sha256: str
    size_bytes: int
    source_uri: str
    path: Path


@dataclass(frozen=True)
class SongMaterialImport:
    asset: Asset
    version: Version
    material: ManagedMaterial


@dataclass(frozen=True)
class SongMaterialView:
    asset: Asset
    status: str
    size_bytes: int | None


class SongMaterialMemory:
    """Profile-scoped immutable byte ownership for canonical Song Assets.

    Bytes are installed before lineage. The content-addressed blob store never
    overwrites an existing digest path. Asset + Version + current-Version
    lineage then commits in one canonical SQLite transaction, so a failed
    consumer ingest cannot leave half-created database lineage.
    """

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("SongMaterialMemory requires the canonical LineageStore")
        self.store = store

    @property
    def profile_dir(self) -> Path:
        return self.store.root / "profiles" / self.store.profile_id

    @property
    def materials_dir(self) -> Path:
        return self.profile_dir / "materials"

    @property
    def blobs_dir(self) -> Path:
        return self.materials_dir / "sha256"

    @property
    def staging_dir(self) -> Path:
        return self.materials_dir / "staging"

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as handle:
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

    @staticmethod
    def _safe_display_name(filename: str) -> str:
        text = str(filename).replace("\\", "/").split("/")[-1]
        text = " ".join(text.replace("\x00", "").split()).strip()
        if not text or text in {".", ".."}:
            raise SongMaterialError("material filename is missing or unsafe")
        if len(text) > 240:
            raise SongMaterialError("material filename is too long")
        return text

    @staticmethod
    def _bounded_size(value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise SongMaterialError("declared material size must be an integer")
        try:
            size = int(value)
        except (TypeError, ValueError) as exc:
            raise SongMaterialError("declared material size must be an integer") from exc
        if size <= 0:
            raise SongMaterialError("material must not be empty")
        if size > MAX_MATERIAL_BYTES:
            raise SongMaterialError("material exceeds the local ingest size limit")
        return size

    def _ensure_storage_dirs(self) -> None:
        profile = self.profile_dir
        if not profile.is_dir() or profile.is_symlink():
            raise SongMaterialIntegrityError("Artist profile storage is not a safe directory")
        for path in (self.materials_dir, self.blobs_dir, self.staging_dir):
            if path.exists() and path.is_symlink():
                raise SongMaterialIntegrityError(
                    "managed material storage contains a symlink boundary"
                )
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir() or path.is_symlink():
                raise SongMaterialIntegrityError(
                    "managed material storage is not a safe directory"
                )

    @staticmethod
    def _digest_path(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _blob_path(self, digest: str) -> Path:
        digest = str(digest).strip().lower()
        if not _MATERIAL_SHA256.fullmatch(digest):
            raise SongMaterialIntegrityError("managed material digest is invalid")
        return self.blobs_dir / digest[:2] / f"{digest}.blob"

    @staticmethod
    def _source_uri(digest: str) -> str:
        return MATERIAL_URI_PREFIX + digest

    def _verify_blob(self, digest: str) -> ManagedMaterial:
        path = self._blob_path(digest)
        if path.is_symlink() or not path.is_file():
            raise SongMaterialIntegrityError("managed Song material is missing")
        size = path.stat().st_size
        if size <= 0 or size > MAX_MATERIAL_BYTES:
            raise SongMaterialIntegrityError("managed Song material has an invalid size")
        if self._digest_path(path) != digest:
            raise SongMaterialIntegrityError(
                "managed Song material no longer matches its fingerprint"
            )
        return ManagedMaterial(
            sha256=digest,
            size_bytes=size,
            source_uri=self._source_uri(digest),
            path=path,
        )

    def _install_blob(
        self,
        stream: BinaryIO,
        *,
        declared_size: int | None,
    ) -> ManagedMaterial:
        if not callable(getattr(stream, "read", None)):
            raise TypeError("material stream must provide read(size)")
        declared = self._bounded_size(declared_size)
        self._ensure_storage_dirs()
        temp_path = self.staging_dir / f".{uuid.uuid4().hex}.material.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp_path.open("xb") as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if chunk in (b"", None):
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise SongMaterialError("material stream returned non-byte data")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_MATERIAL_BYTES:
                        raise SongMaterialError(
                            "material exceeds the local ingest size limit"
                        )
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size <= 0:
                raise SongMaterialError("material must not be empty")
            if declared is not None and size != declared:
                raise SongMaterialError("material size changed during ingest")

            sha256 = digest.hexdigest()
            final_dir = self.blobs_dir / sha256[:2]
            if final_dir.exists() and final_dir.is_symlink():
                raise SongMaterialIntegrityError(
                    "managed material digest directory is unsafe"
                )
            final_dir.mkdir(parents=True, exist_ok=True)
            if final_dir.is_symlink() or not final_dir.is_dir():
                raise SongMaterialIntegrityError(
                    "managed material digest directory is unsafe"
                )
            final_path = final_dir / f"{sha256}.blob"
            if final_path.exists():
                if final_path.is_symlink() or not final_path.is_file():
                    raise SongMaterialIntegrityError(
                        "managed material digest path is unsafe"
                    )
                return self._verify_blob(sha256)

            try:
                # Staging and final paths share the profile filesystem. Hard-linking
                # installs the fully fsynced bytes without ever replacing a winner
                # that appeared after our existence check.
                os.link(temp_path, final_path)
            except FileExistsError:
                pass
            self._fsync_dir(final_dir)
            self._fsync_dir(self.blobs_dir)
            return self._verify_blob(sha256)
        except SongMaterialError:
            raise
        except OSError as exc:
            raise SongMaterialError(
                f"could not preserve local Song material safely: {exc}"
            ) from exc
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _commit_asset_version(
        self,
        song_id: str,
        *,
        name: str,
        material: ManagedMaterial,
    ) -> tuple[Asset, Version]:
        song = self.store._require_song(song_id)
        asset_id = f"asset_{uuid.uuid4().hex}"
        version_id = f"ver_{uuid.uuid4().hex}"
        label = f"Imported {name}"
        with self.store._tx():
            row = self.store._conn.execute(
                "SELECT current_version_id FROM songs WHERE id=?",
                (song.id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"Song not found in profile {self.store.profile_id}: {song.id}"
                )
            parent_version_id = (
                None
                if row["current_version_id"] is None
                else str(row["current_version_id"])
            )
            ordinal_row = self.store._conn.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 AS next_ordinal "
                "FROM versions WHERE song_id=?",
                (song.id,),
            ).fetchone()
            ordinal = int(ordinal_row["next_ordinal"])
            self.store._conn.execute(
                "INSERT INTO assets(id,song_id,name,sha256,source_uri) "
                "VALUES(?,?,?,?,?)",
                (asset_id, song.id, name, material.sha256, material.source_uri),
            )
            self.store._conn.execute(
                "INSERT INTO versions(id,song_id,ordinal,label,parent_version_id) "
                "VALUES(?,?,?,?,?)",
                (version_id, song.id, ordinal, label, parent_version_id),
            )
            self.store._conn.execute(
                "INSERT INTO version_assets(version_id,asset_id,role) "
                "VALUES(?,?,'SOURCE')",
                (version_id, asset_id),
            )
            self.store._conn.execute(
                "UPDATE songs SET current_version_id=? WHERE id=?",
                (version_id, song.id),
            )
        asset = self.store.get_asset(asset_id)
        version = self.store.get_version(version_id)
        if asset is None or version is None:
            raise LineageCorruptionError("committed Song material lineage disappeared")
        return asset, version

    def ingest_stream(
        self,
        song_id: str,
        *,
        filename: str,
        stream: BinaryIO,
        declared_size: int | None = None,
    ) -> SongMaterialImport:
        name = self._safe_display_name(filename)
        self.store._require_song(song_id)
        material = self._install_blob(stream, declared_size=declared_size)
        asset, version = self._commit_asset_version(
            song_id,
            name=name,
            material=material,
        )
        if asset.sha256 != material.sha256 or asset.source_uri != material.source_uri:
            raise LineageCorruptionError(
                "Song material lineage does not match managed bytes"
            )
        return SongMaterialImport(asset=asset, version=version, material=material)

    def _canonical_asset(self, asset: Asset) -> Asset:
        if not isinstance(asset, Asset):
            raise TypeError("asset must be Asset")
        canonical = self.store.get_asset(asset.id)
        if canonical is None:
            raise NotFoundError(
                f"asset not found in profile {self.store.profile_id}: {asset.id}"
            )
        if canonical != asset:
            raise SongMaterialIntegrityError(
                "Asset object does not match this Artist profile's canonical lineage"
            )
        return canonical

    def resolve_asset(self, asset: Asset) -> ManagedMaterial:
        asset = self._canonical_asset(asset)
        source = asset.source_uri
        if source is None or not source.startswith(MATERIAL_URI_PREFIX):
            raise SongMaterialError(
                "asset is not managed by the local Song material store"
            )
        digest = source[len(MATERIAL_URI_PREFIX) :]
        if digest != asset.sha256:
            raise SongMaterialIntegrityError(
                "managed material URI and Asset fingerprint disagree"
            )
        return self._verify_blob(digest)

    def view_asset(self, asset: Asset) -> SongMaterialView:
        asset = self._canonical_asset(asset)
        if asset.source_uri is None or not asset.source_uri.startswith(MATERIAL_URI_PREFIX):
            return SongMaterialView(
                asset=asset,
                status="EXTERNAL_REFERENCE",
                size_bytes=None,
            )
        try:
            material = self.resolve_asset(asset)
        except SongMaterialIntegrityError:
            return SongMaterialView(
                asset=asset,
                status="INTEGRITY_ERROR",
                size_bytes=None,
            )
        return SongMaterialView(
            asset=asset,
            status="VERIFIED_MANAGED",
            size_bytes=material.size_bytes,
        )

    def version_materials(self, version_id: str) -> tuple[SongMaterialView, ...]:
        asset_ids = self.store.version_asset_ids(version_id)
        views = []
        for asset_id in asset_ids:
            asset = self.store.get_asset(asset_id)
            if asset is None:
                raise LineageCorruptionError("Version references a missing Asset")
            views.append(self.view_asset(asset))
        return tuple(views)
