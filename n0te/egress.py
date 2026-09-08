from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .authority import (
    ActionIntent,
    ApprovalBinding,
    ApprovalValidation,
    AuthorityService,
    AuthorityValidationError,
)


class OutboundValidationError(ValueError):
    """Invalid pre-transmission outbound provenance input."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise OutboundValidationError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class OutboundMaterial:
    """One explicitly classified item that would cross a trust boundary."""

    item_id: str
    category: str
    source_ref: str
    revision_fingerprint: str
    private: bool
    rights_ref: str | None = None
    consent_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _text(self.item_id, "material.item_id"))
        object.__setattr__(self, "category", _text(self.category, "material.category"))
        object.__setattr__(
            self, "source_ref", _text(self.source_ref, "material.source_ref")
        )
        object.__setattr__(
            self,
            "revision_fingerprint",
            _text(self.revision_fingerprint, "material.revision_fingerprint"),
        )
        if type(self.private) is not bool:
            raise TypeError("material.private must be bool")
        object.__setattr__(
            self, "rights_ref", _optional_text(self.rights_ref, "material.rights_ref")
        )
        object.__setattr__(
            self,
            "consent_ref",
            _optional_text(self.consent_ref, "material.consent_ref"),
        )

    def material_fields(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "category": self.category,
            "source_ref": self.source_ref,
            "revision_fingerprint": self.revision_fingerprint,
            "private": self.private,
            "rights_ref": self.rights_ref,
            "consent_ref": self.consent_ref,
        }


@dataclass(frozen=True)
class OutboundEnvelope:
    """Canonical inspectable package that may later be handed to an executor.

    This object describes a proposed transmission only. It contains no network,
    provider, upload, model-call or send operation.
    """

    request_id: str
    job_id: str
    description: str
    destination: str
    purpose: str
    materials: tuple[OutboundMaterial, ...]
    retention_statement: str
    cost_statement: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _text(self.request_id, "envelope.request_id")
        )
        object.__setattr__(self, "job_id", _text(self.job_id, "envelope.job_id"))
        object.__setattr__(
            self, "description", _text(self.description, "envelope.description")
        )
        object.__setattr__(
            self, "destination", _text(self.destination, "envelope.destination")
        )
        object.__setattr__(self, "purpose", _text(self.purpose, "envelope.purpose"))
        object.__setattr__(
            self,
            "retention_statement",
            _text(self.retention_statement, "envelope.retention_statement"),
        )
        object.__setattr__(
            self,
            "cost_statement",
            _text(self.cost_statement, "envelope.cost_statement"),
        )
        materials = tuple(self.materials)
        if not materials:
            raise OutboundValidationError("envelope.materials must not be empty")
        if not all(isinstance(item, OutboundMaterial) for item in materials):
            raise TypeError("all envelope materials must be OutboundMaterial")
        item_ids = [item.item_id for item in materials]
        if len(item_ids) != len(set(item_ids)):
            raise OutboundValidationError("material item_id values must be unique")
        object.__setattr__(
            self, "materials", tuple(sorted(materials, key=lambda item: item.item_id))
        )

    @property
    def private_material_ids(self) -> tuple[str, ...]:
        return tuple(item.item_id for item in self.materials if item.private)

    @property
    def data_categories(self) -> tuple[str, ...]:
        return tuple(sorted({item.category for item in self.materials}))

    @property
    def material_revision_fingerprint(self) -> str:
        return _sha256(
            [
                {
                    "item_id": item.item_id,
                    "source_ref": item.source_ref,
                    "revision_fingerprint": item.revision_fingerprint,
                }
                for item in self.materials
            ]
        )

    @property
    def payload_fingerprint(self) -> str:
        return _sha256(
            {
                "materials": [item.material_fields() for item in self.materials],
                "retention_statement": self.retention_statement,
                "cost_statement": self.cost_statement,
            }
        )

    def to_action_intent(self) -> ActionIntent:
        return ActionIntent(
            action_id=f"outbound:{self.request_id}",
            job_id=self.job_id,
            action_class="IRREVERSIBLE",
            description=self.description,
            target_ref=f"outbound:{self.request_id}",
            revision_fingerprint=f"sha256:{self.material_revision_fingerprint}",
            payload_fingerprint=f"sha256:{self.payload_fingerprint}",
            destination=self.destination,
            purpose=self.purpose,
            data_categories=self.data_categories,
        )


@dataclass(frozen=True)
class OutboundPreview:
    request_id: str
    job_id: str
    description: str
    destination: str
    purpose: str
    materials: tuple[OutboundMaterial, ...]
    private_material_ids: tuple[str, ...]
    data_categories: tuple[str, ...]
    retention_statement: str
    cost_statement: str
    material_revision_fingerprint: str
    payload_fingerprint: str
    intent_fingerprint: str


class OutboundInspector:
    """Pure inspect/confirm boundary for proposed outbound material.

    Confirmation delegates to CORE-04A exact authority binding. This service has
    no transport, provider, model-call, upload or send API.
    """

    @staticmethod
    def preview(envelope: OutboundEnvelope) -> OutboundPreview:
        if not isinstance(envelope, OutboundEnvelope):
            raise TypeError("envelope must be OutboundEnvelope")
        intent = envelope.to_action_intent()
        return OutboundPreview(
            request_id=envelope.request_id,
            job_id=envelope.job_id,
            description=envelope.description,
            destination=envelope.destination,
            purpose=envelope.purpose,
            materials=envelope.materials,
            private_material_ids=envelope.private_material_ids,
            data_categories=envelope.data_categories,
            retention_statement=envelope.retention_statement,
            cost_statement=envelope.cost_statement,
            material_revision_fingerprint=envelope.material_revision_fingerprint,
            payload_fingerprint=envelope.payload_fingerprint,
            intent_fingerprint=intent.intent_fingerprint,
        )

    @staticmethod
    def bind_confirmation(
        envelope: OutboundEnvelope,
        source_ref: str,
    ) -> ApprovalBinding:
        if not isinstance(envelope, OutboundEnvelope):
            raise TypeError("envelope must be OutboundEnvelope")
        try:
            return AuthorityService.bind_approval(envelope.to_action_intent(), source_ref)
        except AuthorityValidationError as exc:
            raise OutboundValidationError(str(exc)) from exc

    @staticmethod
    def validate_confirmation(
        envelope: OutboundEnvelope,
        approval: ApprovalBinding,
    ) -> ApprovalValidation:
        if not isinstance(envelope, OutboundEnvelope):
            raise TypeError("envelope must be OutboundEnvelope")
        return AuthorityService.validate(envelope.to_action_intent(), approval)
