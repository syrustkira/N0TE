from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

ACTION_CLASSES = {
    "READ_ONLY",
    "REVERSIBLE",
    "COMPENSATABLE",
    "IRREVERSIBLE",
}
APPROVAL_VALIDATION_STATUSES = {"VALID", "STALE"}


class AuthorityValidationError(ValueError):
    """Invalid action-preview or approval-binding input."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise AuthorityValidationError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _categories(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (tuple, list)):
        raise TypeError("data_categories must be a tuple or list of strings")
    result: list[str] = []
    for value in values:
        text = _text(value, "data_categories")
        if text not in result:
            result.append(text)
    return tuple(sorted(result))


@dataclass(frozen=True)
class ActionIntent:
    """Exact material action representation. This object cannot execute itself."""

    action_id: str
    job_id: str
    action_class: str
    description: str
    target_ref: str
    revision_fingerprint: str
    payload_fingerprint: str
    destination: str | None = None
    purpose: str | None = None
    data_categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _text(self.action_id, "action.action_id"))
        object.__setattr__(self, "job_id", _text(self.job_id, "action.job_id"))
        action_class = str(self.action_class).strip().upper()
        if action_class not in ACTION_CLASSES:
            raise AuthorityValidationError(
                f"unsupported action class: {action_class}"
            )
        object.__setattr__(self, "action_class", action_class)
        object.__setattr__(
            self, "description", _text(self.description, "action.description")
        )
        object.__setattr__(
            self, "target_ref", _text(self.target_ref, "action.target_ref")
        )
        object.__setattr__(
            self,
            "revision_fingerprint",
            _text(self.revision_fingerprint, "action.revision_fingerprint"),
        )
        object.__setattr__(
            self,
            "payload_fingerprint",
            _text(self.payload_fingerprint, "action.payload_fingerprint"),
        )
        destination = _optional_text(self.destination, "action.destination")
        purpose = _optional_text(self.purpose, "action.purpose")
        if destination is not None and purpose is None:
            raise AuthorityValidationError(
                "outbound action destination requires an explicit purpose"
            )
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "data_categories", _categories(self.data_categories))

    def material_fields(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "job_id": self.job_id,
            "action_class": self.action_class,
            "description": self.description,
            "target_ref": self.target_ref,
            "revision_fingerprint": self.revision_fingerprint,
            "payload_fingerprint": self.payload_fingerprint,
            "destination": self.destination,
            "purpose": self.purpose,
            "data_categories": self.data_categories,
        }

    @property
    def intent_fingerprint(self) -> str:
        encoded = json.dumps(
            self.material_fields(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ActionPreview:
    action_id: str
    job_id: str
    action_class: str
    description: str
    target_ref: str
    revision_fingerprint: str
    payload_fingerprint: str
    destination: str | None
    purpose: str | None
    data_categories: tuple[str, ...]
    intent_fingerprint: str


@dataclass(frozen=True)
class ApprovalBinding:
    approval_id: str
    intent_fingerprint: str
    source_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "approval_id", _text(self.approval_id, "approval.approval_id")
        )
        object.__setattr__(
            self,
            "intent_fingerprint",
            _text(self.intent_fingerprint, "approval.intent_fingerprint"),
        )
        object.__setattr__(
            self, "source_ref", _text(self.source_ref, "approval.source_ref")
        )


@dataclass(frozen=True)
class ApprovalValidation:
    status: str
    approval_id: str
    bound_intent_fingerprint: str
    current_intent_fingerprint: str

    def __post_init__(self) -> None:
        if self.status not in APPROVAL_VALIDATION_STATUSES:
            raise AuthorityValidationError(
                f"unsupported approval validation status: {self.status}"
            )


class AuthorityService:
    """Pure exact-revision preview/approval binding.

    This service deliberately has no execute/send/post/mutate/provider/host API.
    Binding proves only that an explicit caller source approved one exact intent
    fingerprint. It grants no provider or host permission and performs no action.
    """

    @staticmethod
    def preview(intent: ActionIntent) -> ActionPreview:
        if not isinstance(intent, ActionIntent):
            raise TypeError("intent must be ActionIntent")
        return ActionPreview(
            action_id=intent.action_id,
            job_id=intent.job_id,
            action_class=intent.action_class,
            description=intent.description,
            target_ref=intent.target_ref,
            revision_fingerprint=intent.revision_fingerprint,
            payload_fingerprint=intent.payload_fingerprint,
            destination=intent.destination,
            purpose=intent.purpose,
            data_categories=intent.data_categories,
            intent_fingerprint=intent.intent_fingerprint,
        )

    @staticmethod
    def bind_approval(intent: ActionIntent, source_ref: str) -> ApprovalBinding:
        if not isinstance(intent, ActionIntent):
            raise TypeError("intent must be ActionIntent")
        source_ref = _text(source_ref, "approval.source_ref")
        return ApprovalBinding(
            approval_id=f"approval_{uuid.uuid4().hex}",
            intent_fingerprint=intent.intent_fingerprint,
            source_ref=source_ref,
        )

    @staticmethod
    def validate(
        intent: ActionIntent,
        approval: ApprovalBinding,
    ) -> ApprovalValidation:
        if not isinstance(intent, ActionIntent):
            raise TypeError("intent must be ActionIntent")
        if not isinstance(approval, ApprovalBinding):
            raise TypeError("approval must be ApprovalBinding")
        current = intent.intent_fingerprint
        status = "VALID" if current == approval.intent_fingerprint else "STALE"
        return ApprovalValidation(
            status=status,
            approval_id=approval.approval_id,
            bound_intent_fingerprint=approval.intent_fingerprint,
            current_intent_fingerprint=current,
        )
