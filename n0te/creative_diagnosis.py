from __future__ import annotations

import re
from dataclasses import dataclass

from .audio_engineering import (
    ANALYZER_VERSION,
    AudioEngineeringError,
    EngineeringEvidenceBinding,
    EngineeringSnapshot,
    UnsupportedEngineeringMedia,
    analyze_pcm_wave,
)
from .audition import UnsupportedAuditionMedia, inspect_audition_media
from .creative_suggestions import CREATIVE_DIMENSIONS
from .lineage import LineageStore, ValidationError
from .material import SongMaterialError, SongMaterialMemory
from .session import SessionMemory

MAX_DIAGNOSIS_PROBLEM_CHARS = 800
DIAGNOSIS_TRUTH_KINDS = ("USER_DECLARED", "OBSERVED")
DIAGNOSIS_EVIDENCE_STATUSES = (
    "OBSERVED_PCM",
    "NO_CURRENT_VERSION",
    "NO_SUPPORTED_AUDIO",
    "INTEGRITY_BLOCKED",
)


class CreativeDiagnosisError(RuntimeError):
    """A truthful bounded Song diagnosis could not be prepared."""


@dataclass(frozen=True)
class DiagnosisFact:
    truth_kind: str
    label: str
    value: str
    scope: str

    def __post_init__(self) -> None:
        if self.truth_kind not in DIAGNOSIS_TRUTH_KINDS:
            raise ValueError(f"unsupported diagnosis truth kind: {self.truth_kind}")
        if not self.label.strip() or not self.value.strip() or not self.scope.strip():
            raise ValueError("diagnosis facts require label, value and scope")


@dataclass(frozen=True)
class DiagnosisHypothesis:
    label: str
    statement: str
    test_dimension: str

    def __post_init__(self) -> None:
        if self.test_dimension not in CREATIVE_DIMENSIONS:
            raise ValueError(f"unsupported hypothesis dimension: {self.test_dimension}")
        if not self.label.strip() or not self.statement.strip():
            raise ValueError("diagnosis hypotheses require label and statement")


@dataclass(frozen=True)
class InterventionPath:
    semantic_key: str
    dimension: str
    title: str
    rationale: str
    steps: tuple[str, ...]
    preserves: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.dimension not in CREATIVE_DIMENSIONS:
            raise ValueError(f"unsupported intervention dimension: {self.dimension}")
        if not self.semantic_key.strip() or not self.title.strip() or not self.rationale.strip():
            raise ValueError("intervention path identity and rationale must not be empty")
        if len(self.steps) < 2:
            raise ValueError("intervention path requires at least two bounded steps")


@dataclass(frozen=True)
class CreativeDiagnosis:
    song_id: str
    session_id: str | None
    current_version_id: str | None
    measured_asset_id: str | None
    measured_asset_sha256: str | None
    measured_source_size_bytes: int | None
    analyzer_version: str | None
    problem: str
    problem_source: str
    effective_locks: tuple[str, ...]
    evidence_status: str
    facts: tuple[DiagnosisFact, ...]
    hypotheses: tuple[DiagnosisHypothesis, ...]
    interventions: tuple[InterventionPath, ...]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.evidence_status not in DIAGNOSIS_EVIDENCE_STATUSES:
            raise ValueError(f"unsupported diagnosis evidence status: {self.evidence_status}")
        if len(self.interventions) != 2:
            raise ValueError("a bounded diagnosis must expose exactly two intervention paths")
        if self.interventions[0].dimension == self.interventions[1].dimension:
            raise ValueError("diagnosis intervention paths must be materially distinct")
        if any(path.dimension in self.effective_locks for path in self.interventions):
            raise ValueError("diagnosis intervention violated an explicit creative lock")

    @property
    def has_measured_audio(self) -> bool:
        return self.evidence_status == "OBSERVED_PCM"


_EXPLICIT_LOCK_PATTERNS = {
    "MELODY": (
        r"\bwithout\s+(?:changing|altering)\b.{0,30}\b(?:vocal\s+)?melody\b",
        r"\b(?:keep|leave)\b.{0,20}\b(?:vocal\s+)?melody\b.{0,20}\b(?:unchanged|same|intact)\b",
        r"\b(?:do\s+not|don't)\s+(?:change|alter)\b.{0,20}\b(?:vocal\s+)?melody\b",
    ),
}


def _format_dbfs(value: float | None) -> str:
    return "silent" if value is None else f"{value:.2f} dBFS"


def _format_db(value: float | None) -> str:
    return "not defined for silence" if value is None else f"{value:.2f} dB"


def _clean_problem(value: str | None, fallback: str | None) -> tuple[str, str]:
    supplied = " ".join(str(value or "").split())
    source = "USER_DECLARED"
    text = supplied
    if not text:
        text = " ".join(str(fallback or "").split())
        source = "USER_DECLARED_SESSION_OBJECTIVE"
    if not text:
        raise CreativeDiagnosisError(
            "Describe the Song problem you want to test, or start a work Session with an objective first."
        )
    if len(text) > MAX_DIAGNOSIS_PROBLEM_CHARS:
        raise ValidationError(
            f"Song diagnosis problem must be {MAX_DIAGNOSIS_PROBLEM_CHARS} characters or fewer"
        )
    return text, source


def _normalize_locks(values) -> tuple[str, ...]:
    if values is None:
        return ()
    locks: set[str] = set()
    for raw in values:
        value = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
        if value not in CREATIVE_DIMENSIONS:
            raise ValidationError(f"unsupported diagnosis creative lock: {value}")
        locks.add(value)
    return tuple(sorted(locks))


def _explicit_problem_locks(problem: str) -> tuple[str, ...]:
    lowered = problem.lower()
    locks = []
    for dimension, patterns in _EXPLICIT_LOCK_PATTERNS.items():
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns):
            locks.append(dimension)
    return tuple(sorted(locks))


def _priority_dimensions(problem: str) -> tuple[str, ...]:
    text = problem.lower()
    if any(word in text for word in ("chorus", "drop", "lift", "impact", "hit harder", "weak", "energy")):
        return ("ARRANGEMENT", "DYNAMICS", "SOUND", "RHYTHM", "HARMONY", "MELODY")
    if any(word in text for word in ("muddy", "clear", "clarity", "mix", "harsh", "thin", "wide", "loud")):
        return ("SOUND", "DYNAMICS", "ARRANGEMENT", "RHYTHM", "HARMONY", "MELODY")
    if any(word in text for word in ("groove", "pocket", "timing", "bounce", "rhythm")):
        return ("RHYTHM", "ARRANGEMENT", "DYNAMICS", "SOUND", "HARMONY", "MELODY")
    if any(word in text for word in ("chord", "harmony", "harmonic", "tension", "resolve")):
        return ("HARMONY", "ARRANGEMENT", "RHYTHM", "DYNAMICS", "SOUND", "MELODY")
    return ("ARRANGEMENT", "DYNAMICS", "RHYTHM", "SOUND", "HARMONY", "MELODY")


class CreativeDiagnosisService:
    """Connect artist-declared Song problems to verified evidence and bounded hypotheses.

    The service is intentionally read-only and ephemeral. It may measure an exact
    current managed PCM WAV, but it never claims that whole-file signal evidence
    proves a section-level artistic problem. It creates no preference, Learning
    outcome, Song artifact, provider call, DAW mutation or action authority.
    """

    def __init__(
        self,
        store: LineageStore,
        sessions: SessionMemory,
        materials: SongMaterialMemory,
    ) -> None:
        if not isinstance(store, LineageStore):
            raise TypeError("CreativeDiagnosisService requires LineageStore")
        if not isinstance(sessions, SessionMemory) or sessions.store is not store:
            raise TypeError("CreativeDiagnosisService requires SessionMemory for the same LineageStore")
        if not isinstance(materials, SongMaterialMemory) or materials.store is not store:
            raise TypeError("CreativeDiagnosisService requires SongMaterialMemory for the same LineageStore")
        self.store = store
        self.sessions = sessions
        self.materials = materials

    def _engineering_snapshot(
        self, song_id: str, version_id: str | None
    ) -> tuple[EngineeringSnapshot | None, str, bool]:
        if version_id is None:
            return None, "NO_CURRENT_VERSION", False
        version = self.store.get_version(version_id)
        if version is None or version.song_id != song_id:
            raise CreativeDiagnosisError(
                "The current Song Version changed while N0TE was preparing diagnosis evidence. Reload and try again."
            )

        integrity_blocked = False
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
            if media.content_type != "audio/wav":
                continue
            try:
                snapshot = analyze_pcm_wave(
                    material.path,
                    binding=EngineeringEvidenceBinding(
                        song_id=song_id,
                        version_id=version.id,
                        asset_id=view.asset.id,
                        sha256=view.asset.sha256,
                        source_size_bytes=material.size_bytes,
                    ),
                )
            except UnsupportedEngineeringMedia:
                continue
            except AudioEngineeringError:
                integrity_blocked = True
                continue
            return snapshot, "OBSERVED_PCM", integrity_blocked

        return None, ("INTEGRITY_BLOCKED" if integrity_blocked else "NO_SUPPORTED_AUDIO"), integrity_blocked

    @staticmethod
    def _facts(
        *,
        problem: str,
        problem_source: str,
        session_objective: str | None,
        snapshot: EngineeringSnapshot | None,
    ) -> tuple[DiagnosisFact, ...]:
        facts = [
            DiagnosisFact(
                truth_kind="USER_DECLARED",
                label="Problem to test",
                value=problem,
                scope="Artist statement for this diagnosis",
            )
        ]
        objective = " ".join(str(session_objective or "").split())
        if objective and objective != problem:
            facts.append(
                DiagnosisFact(
                    truth_kind="USER_DECLARED",
                    label="Current work objective",
                    value=objective,
                    scope="Latest Song work Session",
                )
            )
        if snapshot is not None:
            facts.extend(
                (
                    DiagnosisFact(
                        truth_kind="OBSERVED",
                        label="Current render format",
                        value=(
                            f"{snapshot.sample_rate_hz} Hz · {snapshot.channels} channel"
                            f"{'s' if snapshot.channels != 1 else ''} · {snapshot.bits_per_sample}-bit integer PCM"
                        ),
                        scope="Exact verified current-Version WAV; whole render, not a detected Song section",
                    ),
                    DiagnosisFact(
                        truth_kind="OBSERVED",
                        label="Sample peak",
                        value=_format_dbfs(snapshot.sample_peak_dbfs),
                        scope="Exact verified current-Version WAV; whole render",
                    ),
                    DiagnosisFact(
                        truth_kind="OBSERVED",
                        label="RMS",
                        value=_format_dbfs(snapshot.rms_dbfs),
                        scope="Exact verified current-Version WAV; whole render; RMS is not LUFS",
                    ),
                    DiagnosisFact(
                        truth_kind="OBSERVED",
                        label="Crest factor",
                        value=_format_db(snapshot.crest_factor_db),
                        scope="Exact verified current-Version WAV; whole render",
                    ),
                )
            )
            if snapshot.channels == 2:
                correlation = (
                    "not defined for this signal"
                    if snapshot.stereo_correlation is None
                    else f"{snapshot.stereo_correlation:+.3f}"
                )
                facts.append(
                    DiagnosisFact(
                        truth_kind="OBSERVED",
                        label="Stereo correlation",
                        value=correlation,
                        scope="Exact verified current-Version WAV; whole render",
                    )
                )
        return tuple(facts)

    @staticmethod
    def _path_for(
        dimension: str,
        *,
        effective_locks: tuple[str, ...],
        snapshot: EngineeringSnapshot | None,
    ) -> InterventionPath:
        preserves = tuple(sorted(effective_locks))
        locked_note = (
            " Preserve " + ", ".join(item.title() for item in preserves) + " exactly as requested."
            if preserves
            else ""
        )
        if dimension == "ARRANGEMENT":
            return InterventionPath(
                semantic_key="diagnosis:arrangement-arrival-contrast",
                dimension=dimension,
                title="Test the arrival, not the melody",
                rationale=(
                    "This tests whether the perceived weakness is really a contrast problem between sections rather than a need for more notes."
                    + locked_note
                ),
                steps=(
                    "Create one short contrast window immediately before the target section by thinning or removing one supporting layer.",
                    "Restore or expand support at the target arrival while keeping the core musical idea intact, then compare the transition in context.",
                    "Judge the transition at a matched listening level before keeping the change.",
                ),
                preserves=preserves,
            )
        if dimension == "DYNAMICS":
            baseline = (
                " Use the measured whole-render peak/RMS/crest only as a before/after baseline, never as an artistic target."
                if snapshot is not None
                else " No supported exact PCM measurement is available, so judge this path from a new render rather than invented numbers."
            )
            return InterventionPath(
                semantic_key="diagnosis:dynamics-energy-delivery",
                dimension=dimension,
                title="Test energy delivery",
                rationale=(
                    "This keeps the composition intact and tests whether supporting transients, level shape or density are delivering enough contrast."
                    + locked_note
                    + baseline
                ),
                steps=(
                    "Keep notes and the target section structure fixed; change only the energy delivery of supporting drums, bass or accompaniment.",
                    "Make one bounded transient or dynamic contrast move rather than raising the whole section indiscriminately.",
                    "Render and compare at matched loudness so a louder result cannot win by volume alone.",
                ),
                preserves=preserves,
            )
        if dimension == "SOUND":
            return InterventionPath(
                semantic_key="diagnosis:sound-role-preserving-contrast",
                dimension=dimension,
                title="Test timbral contrast",
                rationale=(
                    "This tests whether the same musical roles need clearer timbral separation rather than different composition."
                    + locked_note
                ),
                steps=(
                    "Keep the notes and rhythm of one supporting role fixed and audition a more contrasting timbre for that role only.",
                    "Avoid stacking new parts; compare whether the changed tone makes the target section read more clearly.",
                    "Level-match the alternative before deciding whether the timbre itself helped.",
                ),
                preserves=preserves,
            )
        if dimension == "RHYTHM":
            return InterventionPath(
                semantic_key="diagnosis:rhythm-support-pocket",
                dimension=dimension,
                title="Test the supporting pocket",
                rationale=(
                    "This tests whether impact is being weakened by the supporting groove rather than by the topline or harmony."
                    + locked_note
                ),
                steps=(
                    "Keep the main musical idea fixed and change one supporting accent, subdivision or recurring hit around the target section.",
                    "Use one version with more space and one with stronger arrival emphasis rather than rewriting the full groove.",
                    "Compare the pocket in context and keep only the version that improves the intended movement.",
                ),
                preserves=preserves,
            )
        if dimension == "HARMONY":
            return InterventionPath(
                semantic_key="diagnosis:harmony-arrival-pressure-test",
                dimension=dimension,
                title="Test harmonic arrival pressure",
                rationale=(
                    "This tests whether the section needs a stronger harmonic sense of arrival rather than additional production weight."
                    + locked_note
                ),
                steps=(
                    "Keep the phrase rhythm intact and pressure-test one voicing, inversion or color at the arrival point.",
                    "Change only that harmonic moment so the comparison isolates harmonic arrival rather than a full rewrite.",
                    "Compare against the original before promoting any new harmony into the Song.",
                ),
                preserves=preserves,
            )
        return InterventionPath(
            semantic_key="diagnosis:melody-motif-pressure-test",
            dimension="MELODY",
            title="Test one motif edge",
            rationale="This tests whether one motif boundary carries the issue while leaving the rest of the topline intact.",
            steps=(
                "Change only one motif ending or response while keeping its rhythmic identity recognizable.",
                "Compare that single variation against the original in the full section.",
                "Do not generalize the result into a new melody rule until the artist chooses it.",
            ),
            preserves=preserves,
        )

    def diagnose(
        self,
        *,
        problem: str | None = None,
        locked_dimensions=(),
    ) -> CreativeDiagnosis:
        song = self.store.active_song()
        if song is None:
            raise CreativeDiagnosisError("Start or select a Song before asking N0TE to diagnose a creative problem.")
        latest = self.sessions.latest_for_song(song.id)
        objective = None if latest is None else latest.objective
        text, problem_source = _clean_problem(problem, objective)
        locks = set(_normalize_locks(locked_dimensions))
        locks.update(_explicit_problem_locks(text))
        effective_locks = tuple(sorted(locks))

        available = [dimension for dimension in _priority_dimensions(text) if dimension not in locks]
        if len(available) < 2:
            raise CreativeDiagnosisError(
                "N0TE needs at least two unlocked creative dimensions to give you two materially different ways to test this problem."
            )

        snapshot, evidence_status, _ = self._engineering_snapshot(song.id, song.current_version_id)
        facts = self._facts(
            problem=text,
            problem_source=problem_source,
            session_objective=objective,
            snapshot=snapshot,
        )
        first, second = available[:2]
        hypotheses = (
            DiagnosisHypothesis(
                label="Path A hypothesis",
                statement=(
                    "The artist-described problem may be driven by how contrast is created around the target moment. "
                    "N0TE has not observed a section-level cause; this is a testable interpretation of the stated problem."
                ),
                test_dimension=first,
            ),
            DiagnosisHypothesis(
                label="Path B hypothesis",
                statement=(
                    "A different explanation may be that the existing musical material is acceptable but its supporting energy, pocket or timbre is not delivering the intended effect. "
                    "This remains a hypothesis until the resulting version is heard and compared."
                ),
                test_dimension=second,
            ),
        )
        interventions = (
            self._path_for(first, effective_locks=effective_locks, snapshot=snapshot),
            self._path_for(second, effective_locks=effective_locks, snapshot=snapshot),
        )
        limitations = [
            "N0TE has not heard a subjective weakness; the problem statement came from the artist.",
            "Current signal measurements, when available, describe the whole verified render and do not prove what happens specifically in a chorus, verse, drop or other section.",
            "These are reversible tests, not an artistic verdict. Nothing has been changed yet.",
        ]
        if snapshot is None:
            limitations.append(
                "No supported exact current-Version PCM measurement was available, so N0TE did not invent signal evidence."
            )

        return CreativeDiagnosis(
            song_id=song.id,
            session_id=None if latest is None else latest.id,
            current_version_id=song.current_version_id,
            measured_asset_id=None if snapshot is None else snapshot.binding.asset_id,
            measured_asset_sha256=None if snapshot is None else snapshot.binding.sha256,
            measured_source_size_bytes=None if snapshot is None else snapshot.binding.source_size_bytes,
            analyzer_version=None if snapshot is None else snapshot.analyzer_version,
            problem=text,
            problem_source=problem_source,
            effective_locks=effective_locks,
            evidence_status=evidence_status,
            facts=facts,
            hypotheses=hypotheses,
            interventions=interventions,
            limitations=tuple(limitations),
        )

    def is_current(self, diagnosis: CreativeDiagnosis) -> bool:
        if not isinstance(diagnosis, CreativeDiagnosis):
            return False
        song = self.store.active_song()
        if song is None or song.id != diagnosis.song_id:
            return False
        latest = self.sessions.latest_for_song(song.id)
        latest_id = None if latest is None else latest.id
        if latest_id != diagnosis.session_id:
            return False
        if song.current_version_id != diagnosis.current_version_id:
            return False
        if diagnosis.measured_asset_id is None:
            return True
        if diagnosis.analyzer_version != ANALYZER_VERSION:
            return False
        asset = self.store.get_asset(diagnosis.measured_asset_id)
        if asset is None or asset.song_id != song.id:
            return False
        if asset.sha256 != diagnosis.measured_asset_sha256:
            return False
        if song.current_version_id is None:
            return False
        if asset.id not in self.store.version_asset_ids(song.current_version_id):
            return False
        try:
            material = self.materials.resolve_asset(asset)
        except SongMaterialError:
            return False
        return material.size_bytes == diagnosis.measured_source_size_bytes
