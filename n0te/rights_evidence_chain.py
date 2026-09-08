from __future__ import annotations

from dataclasses import dataclass, field

from .credits import CreditEntry, CreditsMemory
from .evidence import EvidenceClaim, EvidenceMemory
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

RIGHTS_EVIDENCE_SCHEMA_VERSION = 1
RIGHTS_TARGET_KINDS = ("CREDIT", "COMPOSITION_SPLIT")
RIGHTS_EVIDENCE_STAGES = (
    "USER_DECLARATION",
    "COMMUNICATION_CONFIRMATION",
    "SIGNED_DOCUMENT",
    "PROVIDER_RECEIPT",
)
RIGHTS_ASSERTIONS = ("SUPPORTS", "CONTRADICTS")
RIGHTS_STAGE_STATUSES = (
    "UNKNOWN",
    "UNVERIFIED",
    "SUPPORTED",
    "CONTRADICTED",
    "CONFLICT",
)

_EXTERNAL_RECORDABLE_STAGES = {
    "COMMUNICATION_CONFIRMATION",
    "SIGNED_DOCUMENT",
}
_EXTERNAL_SUPPORT_SOURCES = {"OBSERVED", "PROVIDER_VERIFIED"}


class RightsEvidenceChainError(RuntimeError):
    """Rights evidence cannot be represented without weakening its truth boundary."""


@dataclass(frozen=True)
class RightsEvidenceItem:
    claim_id: str | None
    sequence: int
    stage: str
    assertion: str
    source_kind: str
    source_ref: str | None
    note: str | None
    canonical_local_declaration: bool = False
    provider_verified: bool = False
    legal_conclusion: bool = field(default=False, init=False)
    ownership_verified: bool = field(default=False, init=False)
    registration_verified: bool = field(default=False, init=False)
    royalty_entitlement_verified: bool = field(default=False, init=False)
    action_authority_granted: bool = field(default=False, init=False)


@dataclass(frozen=True)
class RightsStageView:
    stage: str
    status: str
    items: tuple[RightsEvidenceItem, ...]


@dataclass(frozen=True)
class RightsEvidenceSnapshot:
    artist_id: str
    song_id: str
    target_kind: str
    target_id: str
    stages: tuple[RightsStageView, ...]
    highest_contiguous_supported_stage: str | None
    legal_conclusion: bool = field(default=False, init=False)
    ownership_verified: bool = field(default=False, init=False)
    registration_verified: bool = field(default=False, init=False)
    royalty_entitlement_verified: bool = field(default=False, init=False)
    payment_verified: bool = field(default=False, init=False)
    action_authority_granted: bool = field(default=False, init=False)

    def stage(self, stage: str) -> RightsStageView:
        normalized = _enum_text(stage, "rights evidence stage", RIGHTS_EVIDENCE_STAGES)
        for item in self.stages:
            if item.stage == normalized:
                return item
        raise RightsEvidenceChainError(f"rights evidence stage disappeared: {normalized}")


@dataclass(frozen=True)
class _Target:
    kind: str
    id: str
    artist_id: str
    song_id: str
    sequence: int


def _required_text(value: object, field_name: str, *, maximum: int = 1000) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text")
    text = " ".join(value.split())
    if not text:
        raise ValidationError(f"{field_name} must not be empty")
    if len(text) > maximum:
        raise ValidationError(f"{field_name} is too long")
    return text


def _optional_text(
    value: object | None,
    field_name: str,
    *,
    maximum: int = 2000,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text")
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > maximum:
        raise ValidationError(f"{field_name} is too long")
    return text


def _enum_text(value: object, field_name: str, allowed: tuple[str, ...]) -> str:
    text = (
        _required_text(value, field_name, maximum=64)
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if text not in allowed:
        raise ValidationError(f"unsupported {field_name}: {text}")
    return text


class RightsEvidenceChainService:
    """Explainable rights-evidence chain over canonical Credits and Evidence.

    Canonical Credits supply USER_DECLARATION truth. Artist-entered references to
    later evidence may be preserved as USER_DECLARED context, but they do not
    advance an externally supported stage. OBSERVED evidence may advance the
    communication/document stages only when a trusted producer actually observed
    the referenced material. Provider receipts are consumed only from canonical
    PROVIDER_VERIFIED evidence and can never be minted by manual input here.
    """

    _PREFIX = "rights.evidence_chain"

    def __init__(
        self,
        store: LineageStore,
        credits: CreditsMemory,
        evidence: EvidenceMemory,
    ) -> None:
        if not isinstance(store, LineageStore):
            raise TypeError("RightsEvidenceChainService requires LineageStore")
        if not isinstance(credits, CreditsMemory) or credits.store is not store:
            raise TypeError(
                "RightsEvidenceChainService requires CreditsMemory for the same LineageStore"
            )
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError(
                "RightsEvidenceChainService requires EvidenceMemory for the same LineageStore"
            )
        self.store = store
        self.credits = credits
        self.evidence = evidence

    @classmethod
    def evidence_key(cls, target_kind: str, target_id: str, stage: str) -> str:
        kind = _enum_text(target_kind, "rights target kind", RIGHTS_TARGET_KINDS)
        target = _required_text(target_id, "rights target id", maximum=200)
        stage_name = _enum_text(stage, "rights evidence stage", RIGHTS_EVIDENCE_STAGES)
        return f"{cls._PREFIX}.{kind.lower()}.{target}.{stage_name.lower()}"

    def _target(
        self,
        target_kind: object,
        target_id: object,
        *,
        expected_song_id: str | None = None,
    ) -> _Target:
        kind = _enum_text(target_kind, "rights target kind", RIGHTS_TARGET_KINDS)
        target = _required_text(target_id, "rights target id", maximum=200)
        if kind == "CREDIT":
            item: CreditEntry | None = self.credits.get_credit(target)
        else:
            item = self.credits.get_split_sheet(target)
        if item is None:
            raise NotFoundError(f"rights evidence target not found: {kind} {target}")
        if item.artist_id != self.store.primary_artist_id:
            raise ValidationError("rights evidence target belongs to a different Artist")
        if expected_song_id is not None and item.song_id != _required_text(
            expected_song_id,
            "Song id",
            maximum=200,
        ):
            raise ValidationError("rights evidence target belongs to a different Song")
        return _Target(
            kind=kind,
            id=item.id,
            artist_id=item.artist_id,
            song_id=item.song_id,
            sequence=item.sequence,
        )

    @staticmethod
    def _local_declaration(target: _Target) -> RightsEvidenceItem:
        return RightsEvidenceItem(
            claim_id=None,
            sequence=target.sequence,
            stage="USER_DECLARATION",
            assertion="SUPPORTS",
            source_kind="USER_DECLARED",
            source_ref=None,
            note=(
                "Canonical local Song credit declaration"
                if target.kind == "CREDIT"
                else "Canonical local composition split proposal"
            ),
            canonical_local_declaration=True,
            provider_verified=False,
        )

    @staticmethod
    def _payload(
        target: _Target,
        *,
        stage: str,
        assertion: str,
        note: str | None,
    ) -> dict[str, object]:
        return {
            "schema_version": RIGHTS_EVIDENCE_SCHEMA_VERSION,
            "target_kind": target.kind,
            "target_id": target.id,
            "stage": stage,
            "assertion": assertion,
            "note": note,
        }

    def _record_reference(
        self,
        target_kind: str,
        target_id: str,
        *,
        stage: str,
        assertion: str,
        source_ref: str,
        source_kind: str,
        note: str | None = None,
        expected_song_id: str | None = None,
    ) -> EvidenceClaim:
        target = self._target(
            target_kind,
            target_id,
            expected_song_id=expected_song_id,
        )
        stage_name = _enum_text(stage, "rights evidence stage", RIGHTS_EVIDENCE_STAGES)
        if stage_name not in _EXTERNAL_RECORDABLE_STAGES:
            if stage_name == "PROVIDER_RECEIPT":
                raise ValidationError(
                    "provider receipt evidence must come from a provider-verifying producer; it cannot be self-issued here"
                )
            raise ValidationError(
                "canonical USER_DECLARATION evidence comes from Credits and cannot be duplicated here"
            )
        assertion_name = _enum_text(
            assertion,
            "rights evidence assertion",
            RIGHTS_ASSERTIONS,
        )
        provenance = _required_text(
            source_ref,
            "rights evidence source_ref",
            maximum=1000,
        )
        normalized_note = _optional_text(
            note,
            "rights evidence note",
            maximum=2000,
        )
        return self.evidence.record_claim(
            scope_kind="SONG",
            scope_id=target.song_id,
            key=self.evidence_key(target.kind, target.id, stage_name),
            value=self._payload(
                target,
                stage=stage_name,
                assertion=assertion_name,
                note=normalized_note,
            ),
            source_kind=source_kind,
            source_ref=provenance,
            confidence=1.0,
            twin_domain="UNSPECIFIED",
        )

    def record_user_declared_reference(
        self,
        target_kind: str,
        target_id: str,
        *,
        stage: str,
        assertion: str,
        source_ref: str,
        note: str | None = None,
        expected_song_id: str | None = None,
    ) -> EvidenceClaim:
        """Preserve a manual reference without promoting it to external observation."""
        return self._record_reference(
            target_kind,
            target_id,
            stage=stage,
            assertion=assertion,
            source_ref=source_ref,
            source_kind="USER_DECLARED",
            note=note,
            expected_song_id=expected_song_id,
        )

    def record_observed(
        self,
        target_kind: str,
        target_id: str,
        *,
        stage: str,
        assertion: str,
        source_ref: str,
        note: str | None = None,
        expected_song_id: str | None = None,
    ) -> EvidenceClaim:
        """Record evidence only after a trusted producer actually observed it."""
        return self._record_reference(
            target_kind,
            target_id,
            stage=stage,
            assertion=assertion,
            source_ref=source_ref,
            source_kind="OBSERVED",
            note=note,
            expected_song_id=expected_song_id,
        )

    def _item_from_claim(
        self,
        target: _Target,
        stage: str,
        claim: EvidenceClaim,
    ) -> RightsEvidenceItem:
        expected_key = self.evidence_key(target.kind, target.id, stage)
        if (
            claim.scope_kind != "SONG"
            or claim.scope_id != target.song_id
            or claim.key != expected_key
        ):
            raise LineageCorruptionError(
                "rights evidence claim escaped its reserved target binding"
            )
        if not isinstance(claim.value, dict):
            raise LineageCorruptionError("rights evidence claim payload must be an object")
        value = claim.value
        if value.get("schema_version") != RIGHTS_EVIDENCE_SCHEMA_VERSION:
            raise LineageCorruptionError("unsupported rights evidence payload version")
        expected = {
            "target_kind": target.kind,
            "target_id": target.id,
            "stage": stage,
        }
        for key, expected_value in expected.items():
            if value.get(key) != expected_value:
                raise LineageCorruptionError(
                    "rights evidence payload does not match its reserved key"
                )
        try:
            assertion = _enum_text(
                value.get("assertion"),
                "rights evidence assertion",
                RIGHTS_ASSERTIONS,
            )
            note = _optional_text(
                value.get("note"),
                "rights evidence note",
                maximum=2000,
            )
        except ValidationError as exc:
            raise LineageCorruptionError("rights evidence payload is malformed") from exc
        source_ref = claim.source_ref
        if stage in _EXTERNAL_RECORDABLE_STAGES:
            if claim.source_kind not in {
                "USER_DECLARED",
                "OBSERVED",
                "PROVIDER_VERIFIED",
            }:
                raise LineageCorruptionError(
                    "communication/document rights evidence has an invalid source kind"
                )
            if not isinstance(source_ref, str) or not source_ref.strip():
                raise LineageCorruptionError(
                    "communication/document rights evidence is missing provenance"
                )
        elif stage == "PROVIDER_RECEIPT":
            if claim.source_kind != "PROVIDER_VERIFIED":
                raise LineageCorruptionError(
                    "provider receipt rights evidence is not provider verified"
                )
            if not isinstance(source_ref, str) or not source_ref.strip():
                raise LineageCorruptionError(
                    "provider receipt rights evidence is missing provider provenance"
                )
        else:
            raise LineageCorruptionError(
                "USER_DECLARATION must come from canonical Credits, not Evidence"
            )
        return RightsEvidenceItem(
            claim_id=claim.id,
            sequence=claim.sequence,
            stage=stage,
            assertion=assertion,
            source_kind=claim.source_kind,
            source_ref=source_ref,
            note=note,
            canonical_local_declaration=False,
            provider_verified=claim.source_kind == "PROVIDER_VERIFIED",
        )

    @staticmethod
    def _status(stage: str, items: tuple[RightsEvidenceItem, ...]) -> str:
        if not items:
            return "UNKNOWN"
        assertions = {item.assertion for item in items}
        if len(assertions) > 1:
            return "CONFLICT"
        qualifying = tuple(
            item
            for item in items
            if (
                item.source_kind in _EXTERNAL_SUPPORT_SOURCES
                if stage in _EXTERNAL_RECORDABLE_STAGES
                else item.source_kind == "PROVIDER_VERIFIED"
            )
        )
        if not qualifying:
            return "UNVERIFIED"
        qualifying_assertions = {item.assertion for item in qualifying}
        if qualifying_assertions == {"SUPPORTS"}:
            return "SUPPORTED"
        if qualifying_assertions == {"CONTRADICTS"}:
            return "CONTRADICTED"
        return "CONFLICT"

    def snapshot(
        self,
        target_kind: str,
        target_id: str,
        *,
        expected_song_id: str | None = None,
    ) -> RightsEvidenceSnapshot:
        target = self._target(
            target_kind,
            target_id,
            expected_song_id=expected_song_id,
        )
        views: list[RightsStageView] = [
            RightsStageView(
                stage="USER_DECLARATION",
                status="SUPPORTED",
                items=(self._local_declaration(target),),
            )
        ]
        for stage in RIGHTS_EVIDENCE_STAGES[1:]:
            claims = self.evidence.active_claims(
                "SONG",
                target.song_id,
                self.evidence_key(target.kind, target.id, stage),
            )
            items = tuple(
                self._item_from_claim(target, stage, claim) for claim in claims
            )
            views.append(
                RightsStageView(
                    stage=stage,
                    status=self._status(stage, items),
                    items=items,
                )
            )

        contiguous: str | None = None
        for view in views:
            if view.status != "SUPPORTED":
                break
            contiguous = view.stage

        return RightsEvidenceSnapshot(
            artist_id=target.artist_id,
            song_id=target.song_id,
            target_kind=target.kind,
            target_id=target.id,
            stages=tuple(views),
            highest_contiguous_supported_stage=contiguous,
        )
