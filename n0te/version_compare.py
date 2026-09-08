from __future__ import annotations

from dataclasses import dataclass

from .audio_engineering import (
    LOUDNESS_MEASURED,
    AudioEngineeringError,
    EngineeringEvidenceBinding,
    UnsupportedEngineeringMedia,
    analyze_pcm_wave,
)
from .audition import UnsupportedAuditionMedia, inspect_audition_media
from .lineage import LineageStore, Version
from .material import SongMaterialError, SongMaterialMemory

COMPARE_SIDE_STATUSES = (
    "AUDITIONABLE_MEASURED",
    "AUDITIONABLE_UNMEASURED",
    "NO_AUDITIONABLE_AUDIO",
    "INTEGRITY_BLOCKED",
)
COMPARE_STATUSES = (
    "READY",
    "PARTIAL",
    "NO_CURRENT_VERSION",
    "NO_REFERENCE_VERSION",
)


class VersionCompareError(RuntimeError):
    """An exact local Song Version comparison could not be prepared safely."""


@dataclass(frozen=True)
class VersionCompareSide:
    version_id: str
    ordinal: int
    label: str
    asset_id: str | None
    asset_sha256: str | None
    source_size_bytes: int | None
    content_type: str | None
    status: str
    sample_peak_dbfs: float | None
    rms_dbfs: float | None
    integrated_lufs: float | None
    loudness_state: str | None
    loudness_standard: str | None
    analyzer_version: str | None

    def __post_init__(self) -> None:
        if self.status not in COMPARE_SIDE_STATUSES:
            raise ValueError(f"unsupported compare side status: {self.status}")
        if self.ordinal < 1 or not self.version_id.strip() or not self.label.strip():
            raise ValueError("compare side requires exact Version identity")
        has_media = self.asset_id is not None
        if has_media != (self.asset_sha256 is not None):
            raise ValueError("compare media identity must be complete")
        if has_media != (self.source_size_bytes is not None):
            raise ValueError("compare media size binding must be complete")
        if has_media != (self.content_type is not None):
            raise ValueError("compare media type binding must be complete")
        if self.status.startswith("AUDITIONABLE") != has_media:
            raise ValueError("auditionable compare side requires exact media binding")
        if self.status == "AUDITIONABLE_MEASURED" and self.analyzer_version is None:
            raise ValueError("measured compare side requires analyzer lineage")
        if self.loudness_state is None:
            if self.integrated_lufs is not None or self.loudness_standard is not None:
                raise ValueError("loudness evidence must carry an explicit measurement state")
        else:
            if not self.loudness_state.strip() or not self.loudness_standard:
                raise ValueError("loudness state requires standards lineage")
            if (self.loudness_state == LOUDNESS_MEASURED) != (self.integrated_lufs is not None):
                raise ValueError("measured integrated loudness requires an exact LUFS value")

    @property
    def auditionable(self) -> bool:
        return self.asset_id is not None

    @property
    def measured(self) -> bool:
        return self.status == "AUDITIONABLE_MEASURED"

    @property
    def loudness_measured(self) -> bool:
        return self.loudness_state == LOUDNESS_MEASURED and self.integrated_lufs is not None


@dataclass(frozen=True)
class VersionComparison:
    song_id: str
    current_version_id: str | None
    reference_version_id: str | None
    current: VersionCompareSide | None
    reference: VersionCompareSide | None
    status: str
    rms_delta_db: float | None
    integrated_loudness_delta_lu: float | None
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in COMPARE_STATUSES:
            raise ValueError(f"unsupported comparison status: {self.status}")
        if not self.song_id.strip():
            raise ValueError("comparison requires Song identity")
        if self.current is not None and self.current.version_id != self.current_version_id:
            raise ValueError("current compare side does not match current Version binding")
        if self.reference is not None and self.reference.version_id != self.reference_version_id:
            raise ValueError("reference compare side does not match reference Version binding")
        if self.integrated_loudness_delta_lu is not None:
            if self.current is None or self.reference is None:
                raise ValueError("integrated loudness difference requires an exact pair")
            if not self.current.loudness_measured or not self.reference.loudness_measured:
                raise ValueError("integrated loudness difference requires measured loudness on both sides")

    @property
    def both_auditionable(self) -> bool:
        return bool(
            self.current is not None
            and self.reference is not None
            and self.current.auditionable
            and self.reference.auditionable
        )


class VersionCompareService:
    """Prepare a truthful, read-only A/B view for two exact local Song Versions.

    The current Version is compared with its canonical parent when possible. If
    that relationship does not yield a peer, the nearest same-Song Version by
    ordinal is used. Managed audio may be locally auditioned. The existing exact
    PCM Engineering Snapshot supplies sample peak, RMS, and standards-based
    integrated programme loudness when supported. This service performs no gain
    processing, Song mutation, approval, Learning write, DAW action, provider
    call, or artistic decision.
    """

    def __init__(self, store: LineageStore, materials: SongMaterialMemory) -> None:
        if not isinstance(store, LineageStore):
            raise TypeError("VersionCompareService requires LineageStore")
        if not isinstance(materials, SongMaterialMemory) or materials.store is not store:
            raise TypeError("VersionCompareService requires SongMaterialMemory for the same LineageStore")
        self.store = store
        self.materials = materials

    def _reference_for(self, current: Version) -> Version | None:
        if current.parent_version_id:
            parent = self.store.get_version(current.parent_version_id)
            if parent is not None and parent.song_id == current.song_id:
                return parent
        peers = [
            version
            for version in self.store.versions_for_song(current.song_id)
            if version.id != current.id
        ]
        if not peers:
            return None
        earlier = [version for version in peers if version.ordinal < current.ordinal]
        if earlier:
            return max(earlier, key=lambda item: item.ordinal)
        later = [version for version in peers if version.ordinal > current.ordinal]
        return None if not later else min(later, key=lambda item: item.ordinal)

    def _side(self, version: Version) -> VersionCompareSide:
        integrity_blocked = False
        auditionable = None
        for view in self.materials.version_materials(version.id):
            if view.status == "INTEGRITY_ERROR":
                integrity_blocked = True
                continue
            if view.status != "VERIFIED_MANAGED":
                continue
            try:
                material = self.materials.resolve_asset(view.asset)
                media = inspect_audition_media(material.path)
            except SongMaterialError:
                integrity_blocked = True
                continue
            except UnsupportedAuditionMedia:
                continue

            if auditionable is None:
                auditionable = (view.asset, material, media)
            if media.content_type != "audio/wav":
                continue
            try:
                snapshot = analyze_pcm_wave(
                    material.path,
                    binding=EngineeringEvidenceBinding(
                        song_id=version.song_id,
                        version_id=version.id,
                        asset_id=view.asset.id,
                        sha256=view.asset.sha256,
                        source_size_bytes=material.size_bytes,
                    ),
                )
            except (UnsupportedEngineeringMedia, AudioEngineeringError):
                continue
            return VersionCompareSide(
                version_id=version.id,
                ordinal=version.ordinal,
                label=version.label,
                asset_id=view.asset.id,
                asset_sha256=view.asset.sha256,
                source_size_bytes=material.size_bytes,
                content_type=media.content_type,
                status="AUDITIONABLE_MEASURED",
                sample_peak_dbfs=snapshot.sample_peak_dbfs,
                rms_dbfs=snapshot.rms_dbfs,
                integrated_lufs=snapshot.integrated_lufs,
                loudness_state=snapshot.loudness_state,
                loudness_standard=snapshot.loudness_standard,
                analyzer_version=snapshot.analyzer_version,
            )

        if auditionable is not None:
            asset, material, media = auditionable
            return VersionCompareSide(
                version_id=version.id,
                ordinal=version.ordinal,
                label=version.label,
                asset_id=asset.id,
                asset_sha256=asset.sha256,
                source_size_bytes=material.size_bytes,
                content_type=media.content_type,
                status="AUDITIONABLE_UNMEASURED",
                sample_peak_dbfs=None,
                rms_dbfs=None,
                integrated_lufs=None,
                loudness_state=None,
                loudness_standard=None,
                analyzer_version=None,
            )
        return VersionCompareSide(
            version_id=version.id,
            ordinal=version.ordinal,
            label=version.label,
            asset_id=None,
            asset_sha256=None,
            source_size_bytes=None,
            content_type=None,
            status="INTEGRITY_BLOCKED" if integrity_blocked else "NO_AUDITIONABLE_AUDIO",
            sample_peak_dbfs=None,
            rms_dbfs=None,
            integrated_lufs=None,
            loudness_state=None,
            loudness_standard=None,
            analyzer_version=None,
        )

    def prepare(self) -> VersionComparison:
        song = self.store.active_song()
        if song is None:
            raise VersionCompareError("Start or select a Song before comparing Versions.")
        if song.current_version_id is None:
            return VersionComparison(
                song_id=song.id,
                current_version_id=None,
                reference_version_id=None,
                current=None,
                reference=None,
                status="NO_CURRENT_VERSION",
                rms_delta_db=None,
                integrated_loudness_delta_lu=None,
                limitations=(
                    "Add Song material before comparing Versions.",
                    "Preparing comparison performs no Song or external mutation.",
                ),
            )
        current_version = self.store.get_version(song.current_version_id)
        if current_version is None or current_version.song_id != song.id:
            raise VersionCompareError("The active Song current Version could not be verified.")
        current = self._side(current_version)
        reference_version = self._reference_for(current_version)
        if reference_version is None:
            return VersionComparison(
                song_id=song.id,
                current_version_id=current_version.id,
                reference_version_id=None,
                current=current,
                reference=None,
                status="NO_REFERENCE_VERSION",
                rms_delta_db=None,
                integrated_loudness_delta_lu=None,
                limitations=(
                    "A/B needs another Version of this Song; only one Version is available.",
                    "Preparing comparison performs no Song or external mutation.",
                ),
            )
        reference = self._side(reference_version)
        rms_delta = None
        if current.rms_dbfs is not None and reference.rms_dbfs is not None:
            rms_delta = current.rms_dbfs - reference.rms_dbfs
        loudness_delta = None
        if current.loudness_measured and reference.loudness_measured:
            assert current.integrated_lufs is not None and reference.integrated_lufs is not None
            loudness_delta = current.integrated_lufs - reference.integrated_lufs
        ready = current.auditionable and reference.auditionable
        limitations = [
            "Integrated programme loudness, when measured on both exact sides, is standards-based level evidence only; it does not prove artistic quality or which Version is better.",
            "Whole-render RMS remains secondary signal-level context. RMS is not perceptual loudness and is not substituted for unavailable integrated loudness.",
            "N0TE applies no gain, loudness normalization, crossfade, processing or DAW change on this comparison surface.",
            "The artist decides what sounds better. A/B evidence does not create an approval, preference, Learning outcome or new Song Version.",
        ]
        if loudness_delta is None:
            limitations.append(
                "Comparable integrated programme loudness is unavailable for this pair, so N0TE does not invent a loudness match."
            )
        if rms_delta is None:
            limitations.append(
                "A comparable whole-render RMS difference is also unavailable; no fallback level difference is fabricated."
            )
        return VersionComparison(
            song_id=song.id,
            current_version_id=current_version.id,
            reference_version_id=reference_version.id,
            current=current,
            reference=reference,
            status="READY" if ready else "PARTIAL",
            rms_delta_db=rms_delta,
            integrated_loudness_delta_lu=loudness_delta,
            limitations=tuple(limitations),
        )
