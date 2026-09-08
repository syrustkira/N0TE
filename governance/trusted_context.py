from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .execution_envelope import TRUTH_PLANES, validate_execution_envelope


class TrustedContextError(ValueError):
    """Canonical context is unavailable, stale, or inconsistent with the proposed action."""


def _text(value, field):
    if value is None or not isinstance(value, str) or not value.strip():
        raise TrustedContextError(f"{field} must be non-empty text")
    return value.strip()


def _text_set(value, field):
    if not isinstance(value, list):
        raise TrustedContextError(f"{field} must be a list")
    values = [_text(item, field) for item in value]
    if len(values) != len(set(values)):
        raise TrustedContextError(f"{field} contains duplicates")
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


@dataclass(frozen=True)
class TrustedContextSnapshot:
    snapshot_id: str
    source_fingerprint: str
    observed_at: datetime
    expires_at: datetime
    retained_scope_refs: frozenset[str]
    truth_owners: dict[str, str]
    policies: dict[str, dict]
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
    if not retained_scope_refs:
        raise TrustedContextError("retained_scope_refs must not be empty")

    owners = raw["truth_owners"]
    if not isinstance(owners, dict):
        raise TrustedContextError("truth_owners must be an object")
    truth_owners = {plane: _text(owners.get(plane), f"truth_owners.{plane}") for plane in TRUTH_PLANES}

    policies = raw["policies"]
    if not isinstance(policies, dict) or not policies:
        raise TrustedContextError("policies must be a non-empty object")
    normalized_policies = {}
    for active_object, policy in policies.items():
        active_object = _text(active_object, "policies.active_object")
        if not isinstance(policy, dict):
            raise TrustedContextError(f"policy for {active_object} must be an object")
        required_functions = _text_set(policy.get("required_functions"), f"policies.{active_object}.required_functions")
        deps = policy.get("required_dependencies")
        if not isinstance(deps, dict):
            raise TrustedContextError(f"policies.{active_object}.required_dependencies must be an object")
        upstream = _text_set(deps.get("upstream", []), f"policies.{active_object}.required_dependencies.upstream")
        downstream = _text_set(deps.get("downstream", []), f"policies.{active_object}.required_dependencies.downstream")
        outcomes = _text_set(policy.get("allowed_outcome_classes"), f"policies.{active_object}.allowed_outcome_classes")
        normalized_policies[active_object] = {
            "required_functions": required_functions,
            "required_dependencies": {"upstream": upstream, "downstream": downstream},
            "allowed_outcome_classes": frozenset(item.upper() for item in outcomes),
        }

    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return TrustedContextSnapshot(
        snapshot_id=snapshot_id,
        source_fingerprint=source_fingerprint,
        observed_at=observed_at,
        expires_at=expires_at,
        retained_scope_refs=retained_scope_refs,
        truth_owners=truth_owners,
        policies=normalized_policies,
        fingerprint=hashlib.sha256(canonical).hexdigest(),
    )


def crosscheck_execution_envelope(envelope, snapshot: TrustedContextSnapshot, *, now=None):
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
        raise TrustedContextError(f"active_object has no trusted policy: {active_object}")

    envelope_scope = frozenset(str(item).strip() for item in envelope["retained_scope_refs"])
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

    required_functions = frozenset(str(item).strip() for item in envelope["required_functions"])
    missing_functions = sorted(policy["required_functions"] - required_functions)
    if missing_functions:
        raise TrustedContextError(
            f"envelope omitted canonically required functions: {missing_functions}"
        )

    for side in ("upstream", "downstream"):
        actual = frozenset(str(item).strip() for item in envelope["dependencies"][side])
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
    return snapshot


class FileTrustedContextProvider:
    """Read-only provider for snapshots produced by a trusted sync process.

    The MCP/model-facing surface deliberately has no method that creates or edits
    these snapshots. If the canonical sync has not produced one, permit issuance
    fails closed instead of asking the model to invent its own context authority.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def get(self, snapshot_id: str) -> TrustedContextSnapshot:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        if not self.path.is_file():
            raise TrustedContextError("trusted context snapshot store is unavailable")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TrustedContextError("trusted context snapshot store is unreadable") from exc
        snapshots = raw.get("snapshots") if isinstance(raw, dict) else None
        if not isinstance(snapshots, list):
            raise TrustedContextError("trusted context snapshot store must contain snapshots list")
        for item in snapshots:
            if isinstance(item, dict) and item.get("snapshot_id") == snapshot_id:
                return validate_trusted_context_snapshot(item)
        raise TrustedContextError(f"trusted context snapshot not found: {snapshot_id}")
