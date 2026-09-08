from __future__ import annotations

from pathlib import Path

from .activity import ActivityLog
from .attention import AttentionMemory
from .capability_evidence import CapabilityEvidenceMemory
from .context import ContextIsolationService
from .context_lifecycle import ContextProjectionService
from .evidence import EvidenceMemory
from .focus import FocusContextService
from .friction import FrictionMemory
from .graph import SongKnowledgeMapService
from .host_observation import HostObservationCoordinator
from .learning import LearningMemory
from .lineage import LineageStore
from .material import SongMaterialMemory
from .operations import OperationJournal
from .people import PeopleMemory
from .provenance import ProvenanceLedger
from .reconcile import ReconciliationService
from .recovery import RecoveryManager
from .retention import SongRetentionService
from .session import SessionMemory
from .shadow import HostShadow
from .skills import SkillMemory
from .success import SuccessMemory
from .suggestion_deferral import SuggestionDeferralMemory
from .suggestion_feedback import SuggestionFeedbackMemory
from .transactions import TransactionCoordinator
from .twins import TwinAwareSongKnowledgeMapService, TwinEvidenceService
from .workspace import WorkspaceMemory


class HeadquartersMemory:
    """Composition root for canonical local memory. Owns no persistence itself."""

    def __init__(self, store: LineageStore):
        self.store = store
        self.evidence = EvidenceMemory(store)
        self.twins = TwinEvidenceService(self.evidence)
        self.activity = ActivityLog(store)
        self.attention = AttentionMemory(store)
        self.people = PeopleMemory(store)
        self.workspaces = WorkspaceMemory(store)
        self.capability_evidence = CapabilityEvidenceMemory(store, self.workspaces)
        self.shadow = HostShadow(store, self.workspaces)
        self.reconciliation = ReconciliationService(store, self.twins, self.shadow)
        self.focus = FocusContextService(self.workspaces)
        self.host_observation = HostObservationCoordinator(
            self.workspaces,
            self.capability_evidence,
            self.focus,
            self.shadow,
        )
        self.materials = SongMaterialMemory(store)
        self.operations = OperationJournal(store, self.activity)
        self.transactions = TransactionCoordinator(self.operations)
        self.sessions = SessionMemory(store, self.evidence)
        self.suggestion_deferrals = SuggestionDeferralMemory(store, self.sessions)
        self.suggestion_feedback = SuggestionFeedbackMemory(store, self.sessions)
        self.skills = SkillMemory(store, self.evidence, self.sessions)
        self.learning = LearningMemory(store, self.sessions)
        self.success = SuccessMemory(self.learning)
        self.friction = FrictionMemory(store, self.learning)
        self.provenance = ProvenanceLedger(store)
        self.recovery = RecoveryManager(store)
        self.context = ContextIsolationService(store, self.evidence)
        self.knowledge = TwinAwareSongKnowledgeMapService(
            SongKnowledgeMapService(store), self.evidence
        )
        self.retention = SongRetentionService(
            store,
            self.evidence,
            self.context,
            self.sessions,
            self.learning,
            self.success,
            self.friction,
            self.skills,
            self.activity,
        )
        self.context_projection = ContextProjectionService(self.retention)
        # Shell installation is intentionally lazy. Headquarters instances are
        # constructed only after package import, avoiding a consumer_shell <->
        # memory import cycle while keeping artist-facing projections attached
        # to the same canonical memory composition root.
        from .creative_diagnosis_shell import install_song_creative_diagnosis
        from .creative_suggestions_shell import install_song_creative_suggestions
        from .people_shell import install_people_headquarters
        from .retention_shell import install_song_retention
        from .rights_evidence_chain_shell import install_rights_evidence_chain
        from .suggestion_feedback_shell import install_song_suggestion_feedback
        from .version_compare_shell import install_song_version_compare

        install_song_retention()
        install_song_creative_suggestions()
        install_song_suggestion_feedback()
        install_song_creative_diagnosis()
        install_people_headquarters()
        install_rights_evidence_chain()
        install_song_version_compare()

    @classmethod
    def _compose_owned_store(cls, store: LineageStore) -> "HeadquartersMemory":
        """Transfer store ownership only after the full composition root succeeds."""
        try:
            return cls(store)
        except BaseException:
            store.close()
            raise

    @classmethod
    def create(cls, root: str | Path, artist_name: str) -> "HeadquartersMemory":
        return cls._compose_owned_store(LineageStore.create(root, artist_name))

    @classmethod
    def open(cls, root: str | Path, profile_id: str) -> "HeadquartersMemory":
        return cls._compose_owned_store(LineageStore.open(root, profile_id))

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "HeadquartersMemory":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
