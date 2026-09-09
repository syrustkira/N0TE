from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .execution_envelope import ACTION_CLASSES, TRUTH_PLANES, validate_execution_envelope


class TrustedContextError(ValueError):
    """Canonical context is unavailable, stale, or inconsistent with the proposed action."""


def _text(value, field):
    if value is None or not isinstance(value, str) or not value.strip():
        raise TrustedContextError(f"{field} must be non-empty text")
    return value.strip()


def _text_set(value, field, *, allow_empty=False):
    if not isinstance(value, list):
        raise TrustedContextError(f"{field} must be a list")
    values = [_text(item, field) for item in value]
    if len(values) != len(set(values)):
        raise TrustedContextError(f"{field} contains duplicates")
    if not allow_empty and not values:
        raise TrustedContextError(f"{field} must not be empty")
    return frozenset(values)


def _timestamp(value, field):
    text = _text(value, field)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrustedContextError(f"{field} must be ISO-8601") from exc
    if stamp.tzinfo is None:
        raise TrustedContextError(f"{field} must include timezone")
    return stamp.astimezone(timezone.utc)


def _approval_records(value):
    if not isinstance(value, list):
        raise TrustedContextError("approvals must be a list")
    approvals = {}
    for index, item in enumerate(value):
        field = f"approvals[{index}]"
        if not isinstance(item, dict):
            raise TrustedContextError(f"{field} must be an object")
        approval_id = _text(item.get("approval_id"), f"{field}.approval_id")
        if approval_id in approvals:
            raise TrustedContextError(f"duplicate trusted approval_id: {approval_id}")
        approvals[approval_id] = {
            "intent_fingerprint": _text(
                item.get("intent_fingerprint"), f"{field}.intent_fingerprint"
            ),
            "source_ref": _text(item.get("source_ref"), f"{field}.source_ref"),
        }
    return approvals


def _authority_profiles(value, field):
    if not isinstance(value, dict) or not value:
        raise TrustedContextError(f"{field} must be a non-empty object")
    profiles = {}
    for raw_class, raw_profile in value.items():
        action_class = _text(raw_class, field).upper()
        if action_class not in ACTION_CLASSES or action_class == "READ_ONLY":
            raise TrustedContextError(
                f"{field} contains unsupported stateful action class: {action_class}"
            )
        if action_class in profiles:
            raise TrustedContextError(
                f"{field} contains duplicate normalized action class: {action_class}"
            )
        if not isinstance(raw_profile, dict):
            raise TrustedContextError(f"{field}.{action_class} must be an object")
        requires_human = raw_profile.get("requires_human")
        if type(requires_human) is not bool:
            raise TrustedContextError(
                f"{field}.{action_class}.requires_human must be bool"
            )
        source_refs = _text_set(
            raw_profile.get("source_refs"),
            f"{field}.{action_class}.source_refs",
        )
        profiles[action_class] = {
            "requires_human": requires_human,
            "source_refs": source_refs,
        }
    return profiles


@dataclass(frozen=True)
class TrustedContextSnapshot:
    snapshot_id: str
    source_fingerprint: str
    observed_at: datetime
    expires_at: datetime
    retained_scope_refs: frozenset[str]
    truth_owners: dict[str, str]
    policies: dict[str, dict]
    approvals: dict[str, dict[str, str]]
    fingerprint: str


def validate_trusted_context_snapshot(raw):
    if not isinstance(raw, dict):
        raise TrustedContextError("trusted context snapshot must be an object")
    required = {
        "snapshot_id",
        "source_fingerprint",
        "observed_at",
        "expires_at",
        "retained_scope_refs",
        "truth_owners",
        "policies",
        "approvals",
    }
    missing = required - set(raw)
    if missing:
        raise TrustedContextError(f"trusted context snapshot missing {sorted(missing)}")

    snapshot_id = _text(raw["snapshot_id"], "snapshot_id")
    source_fingerprint = _text(raw["source_fingerprint"], "source_fingerprint")
    observed_at = _timestamp(raw["observed_at"], "observed_at")
    expires_at = _timestamp(raw["expires_at"], "expires_at")
    if expires_at <= observed_at:
        raise TrustedContextError("expires_at must be after observed_at")
    retained_scope_refs = _text_set(raw["retained_scope_refs"], "retained_scope_refs")

    owners = raw["truth_owners"]
    if not isinstance(owners, dict):
        raise TrustedContextError("truth_owners must be an object")
    truth_owners = {
        plane: _text(owners.get(plane), f"truth_owners.{plane}")
        for plane in TRUTH_PLANES
    }

    policies = raw["policies"]
    if not isinstance(policies, dict) or not policies:
        raise TrustedContextError("policies must be a non-empty object")
    normalized_policies = {}
    for active_object, policy in policies.items():
        active_object = _text(active_object, "policies.active_object")
        if not isinstance(policy, dict):
            raise TrustedContextError(f"policy for {active_object} must be an object")
        required_functions = _text_set(
            policy.get("required_functions"),
            f"policies.{active_object}.required_functions",
        )
        deps = policy.get("required_dependencies")
        if not isinstance(deps, dict):
            raise TrustedContextError(
                f"policies.{active_object}.required_dependencies must be an object"
            )
        upstream = _text_set(
            deps.get("upstream", []),
            f"policies.{active_object}.required_dependencies.upstream",
            allow_empty=True,
        )
        downstream = _text_set(
            deps.get("downstream", []),
            f"policies.{active_object}.required_dependencies.downstream",
            allow_empty=True,
        )
        outcomes = _text_set(
            policy.get("allowed_outcome_classes"),
            f"policies.{active_object}.allowed_outcome_classes",
        )
        authority_profiles = _authority_profiles(
            policy.get("authority_by_action_class"),
            f"policies.{active_object}.authority_by_action_class",
        )
        normalized_policies[active_object] = {
            "required_functions": required_functions,
            "required_dependencies": {
                "upstream": upstream,
                "downstream": downstream,
            },
            "allowed_outcome_classes": frozenset(item.upper() for item in outcomes),
            "authority_by_action_class": authority_profiles,
        }

    approvals = _approval_records(raw["approvals"])
    canonical = json.dumps(
        raw,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return TrustedContextSnapshot(
        snapshot_id=snapshot_id,
        source_fingerprint=source_fingerprint,
        observed_at=observed_at,
        expires_at=expires_at,
        retained_scope_refs=retained_scope_refs,
        truth_owners=truth_owners,
        policies=normalized_policies,
        approvals=approvals,
        fingerprint=hashlib.sha256(canonical).hexdigest(),
    )


def crosscheck_execution_envelope(
    envelope,
    snapshot: TrustedContextSnapshot,
    *,
    now=None,
):
    validate_execution_envelope(envelope)
    if not isinstance(snapshot, TrustedContextSnapshot):
        raise TypeError("snapshot must be TrustedContextSnapshot")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise TrustedContextError("now must include timezone")
    now = now.astimezone(timezone.utc)
    if now > snapshot.expires_at:
        raise TrustedContextError("trusted context snapshot is expired")

    active_object = str(envelope["active_object"]).strip()
    policy = snapshot.policies.get(active_object)
    if policy is None:
        raise TrustedContextError(
            f"active_object has no trusted policy: {active_object}"
        )

    envelope_scope = frozenset(
        str(item).strip() for item in envelope["retained_scope_refs"]
    )
    if envelope_scope != snapshot.retained_scope_refs:
        missing = sorted(snapshot.retained_scope_refs - envelope_scope)
        extra = sorted(envelope_scope - snapshot.retained_scope_refs)
        raise TrustedContextError(
            f"retained scope does not match trusted snapshot missing={missing} extra={extra}"
        )

    envelope_owners = envelope["truth_owners"]
    for plane in TRUTH_PLANES:
        if str(envelope_owners[plane]).strip() != snapshot.truth_owners[plane]:
            raise TrustedContextError(f"truth owner mismatch for {plane}")

    required_functions = frozenset(
        str(item).strip() for item in envelope["required_functions"]
    )
    missing_functions = sorted(policy["required_functions"] - required_functions)
    if missing_functions:
        raise TrustedContextError(
            f"envelope omitted canonically required functions: {missing_functions}"
        )

    for side in ("upstream", "downstream"):
        actual = frozenset(
            str(item).strip() for item in envelope["dependencies"][side]
        )
        missing = sorted(policy["required_dependencies"][side] - actual)
        if missing:
            raise TrustedContextError(
                f"envelope omitted required {side} dependencies: {missing}"
            )

    outcome = str(envelope["intent"]["outcome_class"]).strip().upper()
    if outcome not in policy["allowed_outcome_classes"]:
        raise TrustedContextError(
            f"outcome class {outcome} is not allowed for active_object {active_object}"
        )

    envelope_authority = envelope["authority"]
    action_class = str(envelope_authority["action_class"]).strip().upper()
    authority_profile = policy["authority_by_action_class"].get(action_class)
    if authority_profile is None:
        raise TrustedContextError(
            f"action class {action_class} has no trusted authority for active_object {active_object}"
        )
    if (
        envelope_authority["requires_human"]
        is not authority_profile["requires_human"]
    ):
        raise TrustedContextError(
            f"human approval requirement mismatch for action class {action_class}"
        )
    source_ref = str(envelope_authority["source_ref"]).strip()
    if source_ref not in authority_profile["source_refs"]:
        raise TrustedContextError(
            f"authority source is not trusted for action class {action_class}: {source_ref}"
        )
    return snapshot


def require_trusted_approval(
    snapshot: TrustedContextSnapshot,
    *,
    approval_id: str,
    intent_fingerprint: str,
    source_ref: str,
):
    if not isinstance(snapshot, TrustedContextSnapshot):
        raise TypeError("snapshot must be TrustedContextSnapshot")
    approval_id = _text(approval_id, "approval.approval_id")
    intent_fingerprint = _text(
        intent_fingerprint,
        "approval.intent_fingerprint",
    )
    source_ref = _text(source_ref, "approval.source_ref")
    trusted = snapshot.approvals.get(approval_id)
    if trusted is None:
        raise TrustedContextError(
            f"approval is not present in trusted context: {approval_id}"
        )
    if trusted["intent_fingerprint"] != intent_fingerprint:
        raise TrustedContextError(
            f"trusted approval intent mismatch: {approval_id}"
        )
    if trusted["source_ref"] != source_ref:
        raise TrustedContextError(
            f"trusted approval source mismatch: {approval_id}"
        )
    return trusted


class FileTrustedContextProvider:
    """Read-only provider for snapshots produced by a trusted sync process.

    The MCP/model-facing surface deliberately has no method that creates or edits
    these snapshots. If the canonical sync has not produced one, permit issuance
    fails closed instead of asking the model to invent its own context authority.
    Multi-snapshot stores must also name exactly one current snapshot so callers
    cannot revive superseded approvals or authority profiles with an older ID.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def get(self, snapshot_id: str) -> TrustedContextSnapshot:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        if not self.path.is_file():
            raise TrustedContextError(
                "trusted context snapshot store is unavailable"
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TrustedContextError(
                "trusted context snapshot store is unreadable"
            ) from exc
        snapshots = raw.get("snapshots") if isinstance(raw, dict) else None
        if not isinstance(snapshots, list):
            raise TrustedContextError(
                "trusted context snapshot store must contain snapshots list"
            )

        raw_current_snapshot_id = raw.get("current_snapshot_id")
        if raw_current_snapshot_id is None:
            if len(snapshots) != 1:
                raise TrustedContextError(
                    "multi-snapshot trusted context store requires current_snapshot_id"
                )
            only = snapshots[0]
            if not isinstance(only, dict):
                raise TrustedContextError(
                    "trusted context snapshot store contains invalid snapshot"
                )
            current_snapshot_id = _text(
                only.get("snapshot_id"),
                "snapshots[0].snapshot_id",
            )
        else:
            current_snapshot_id = _text(
                raw_current_snapshot_id,
                "current_snapshot_id",
            )

        if snapshot_id != current_snapshot_id:
            raise TrustedContextError(
                f"trusted context snapshot is not current: {snapshot_id}"
            )
        matches = [
            item
            for item in snapshots
            if isinstance(item, dict)
            and item.get("snapshot_id") == current_snapshot_id
        ]
        if not matches:
            raise TrustedContextError(
                f"current trusted context snapshot not found: {current_snapshot_id}"
            )
        if len(matches) != 1:
            raise TrustedContextError(
                f"duplicate current trusted context snapshot: {current_snapshot_id}"
            )
        return validate_trusted_context_snapshot(matches[0])
