from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

TRUTH_PLANES = ("WHY", "WHAT", "HOW", "NOW", "PROOF")
TRUTH_TYPES = {
    "DURABLE_PRINCIPLE",
    "ACCEPTED_SCOPE",
    "CURRENT_FACT",
    "USER_INTENT",
    "PREFERENCE",
    "DECISION",
    "EXPERIMENT",
    "HYPOTHESIS",
    "HISTORICAL_EVIDENCE",
    "IMPLEMENTATION_DETAIL",
    "EXTERNAL_CONSTRAINT",
    "UNKNOWN",
}
EVIDENCE_POSTURES = {"OBSERVED", "DERIVED", "CLAIMED", "ASSUMED", "STALE", "UNKNOWN"}
ACTION_CLASSES = {"READ_ONLY", "REVERSIBLE", "COMPENSATABLE", "IRREVERSIBLE"}
OUTCOME_CLASSES = {"ARTIFACT", "SYSTEM_STATE", "EXTERNAL_STATE", "HUMAN_STATE"}
NEXT_DEPENDENCY_STATES = {"KNOWN", "BLOCKED", "NONE", "UNKNOWN"}
BLOCKING_POSTURES = {"CLAIMED", "ASSUMED", "STALE", "UNKNOWN"}


class ExecutionEnvelopeError(ValueError):
    """The coordinator attempted to act from an invalid working model."""


@dataclass(frozen=True)
class ExecutionGateResult:
    valid: bool
    execution_allowed: bool
    envelope_fingerprint: str | None
    blockers: tuple[str, ...]


def _text(value, field):
    text = str(value).strip()
    if not text:
        raise ExecutionEnvelopeError(f"{field} must not be empty")
    return text


def _string_list(value, field, *, allow_empty=False):
    if not isinstance(value, list):
        raise ExecutionEnvelopeError(f"{field} must be a list")
    result = []
    for item in value:
        text = _text(item, field)
        if text in result:
            raise ExecutionEnvelopeError(f"{field} contains duplicate value: {text}")
        result.append(text)
    if not allow_empty and not result:
        raise ExecutionEnvelopeError(f"{field} must not be empty")
    return result


def _validate_claim(claim, index):
    if not isinstance(claim, dict):
        raise ExecutionEnvelopeError(f"claims[{index}] must be an object")
    required = {"statement", "truth_type", "evidence_posture", "source_refs", "blocking"}
    missing = required - set(claim)
    if missing:
        raise ExecutionEnvelopeError(f"claims[{index}] missing {sorted(missing)}")
    _text(claim["statement"], f"claims[{index}].statement")
    truth_type = str(claim["truth_type"]).strip().upper()
    posture = str(claim["evidence_posture"]).strip().upper()
    if truth_type not in TRUTH_TYPES:
        raise ExecutionEnvelopeError(f"claims[{index}] unknown truth_type: {truth_type}")
    if posture not in EVIDENCE_POSTURES:
        raise ExecutionEnvelopeError(f"claims[{index}] unknown evidence_posture: {posture}")
    refs = _string_list(claim["source_refs"], f"claims[{index}].source_refs", allow_empty=True)
    if posture in {"OBSERVED", "DERIVED", "CLAIMED", "STALE"} and not refs:
        raise ExecutionEnvelopeError(
            f"claims[{index}] posture {posture} requires at least one source_ref"
        )
    if not isinstance(claim["blocking"], bool):
        raise ExecutionEnvelopeError(f"claims[{index}].blocking must be boolean")
    return truth_type, posture


def validate_execution_envelope(envelope):
    if not isinstance(envelope, dict):
        raise ExecutionEnvelopeError("execution envelope must be an object")
    required = {
        "intent",
        "active_object",
        "retained_scope_refs",
        "dependencies",
        "truth_owners",
        "claims",
        "required_functions",
        "invoked_functions",
        "authority",
        "acceptance",
        "next_causal_dependency",
    }
    missing = required - set(envelope)
    if missing:
        raise ExecutionEnvelopeError(f"execution envelope missing {sorted(missing)}")

    intent = envelope["intent"]
    if not isinstance(intent, dict):
        raise ExecutionEnvelopeError("intent must be an object")
    _text(intent.get("description"), "intent.description")
    outcome_class = str(intent.get("outcome_class", "")).strip().upper()
    if outcome_class not in OUTCOME_CLASSES:
        raise ExecutionEnvelopeError(f"unsupported intent outcome_class: {outcome_class}")

    _text(envelope["active_object"], "active_object")
    _string_list(envelope["retained_scope_refs"], "retained_scope_refs")

    dependencies = envelope["dependencies"]
    if not isinstance(dependencies, dict):
        raise ExecutionEnvelopeError("dependencies must be an object")
    for side in ("upstream", "downstream"):
        if side not in dependencies:
            raise ExecutionEnvelopeError(f"dependencies missing {side}")
        _string_list(dependencies[side], f"dependencies.{side}", allow_empty=True)

    owners = envelope["truth_owners"]
    if not isinstance(owners, dict):
        raise ExecutionEnvelopeError("truth_owners must be an object")
    for plane in TRUTH_PLANES:
        _text(owners.get(plane), f"truth_owners.{plane}")

    claims = envelope["claims"]
    if not isinstance(claims, list) or not claims:
        raise ExecutionEnvelopeError("claims must be a non-empty list")
    for index, claim in enumerate(claims):
        _validate_claim(claim, index)

    required_functions = set(
        _string_list(envelope["required_functions"], "required_functions")
    )
    invoked_functions = set(
        _string_list(envelope["invoked_functions"], "invoked_functions", allow_empty=True)
    )
    missing_functions = sorted(required_functions - invoked_functions)
    if missing_functions:
        raise ExecutionEnvelopeError(
            f"required functions were not invoked: {missing_functions}"
        )

    authority = envelope["authority"]
    if not isinstance(authority, dict):
        raise ExecutionEnvelopeError("authority must be an object")
    action_class = str(authority.get("action_class", "")).strip().upper()
    if action_class not in ACTION_CLASSES:
        raise ExecutionEnvelopeError(f"unsupported authority.action_class: {action_class}")
    if not isinstance(authority.get("authorized"), bool):
        raise ExecutionEnvelopeError("authority.authorized must be boolean")
    if not isinstance(authority.get("requires_human"), bool):
        raise ExecutionEnvelopeError("authority.requires_human must be boolean")
    _text(authority.get("source_ref"), "authority.source_ref")

    acceptance = envelope["acceptance"]
    if not isinstance(acceptance, dict):
        raise ExecutionEnvelopeError("acceptance must be an object")
    acceptance_class = str(acceptance.get("outcome_class", "")).strip().upper()
    if acceptance_class != outcome_class:
        raise ExecutionEnvelopeError(
            "acceptance outcome_class must match intent outcome_class"
        )
    _text(acceptance.get("observable_condition"), "acceptance.observable_condition")
    _string_list(acceptance.get("evidence_required"), "acceptance.evidence_required")
    if acceptance.get("artifact_is_not_completion") is not True and outcome_class in {
        "EXTERNAL_STATE",
        "HUMAN_STATE",
    }:
        raise ExecutionEnvelopeError(
            "external/human outcomes must explicitly reject artifact-only completion"
        )

    next_dep = envelope["next_causal_dependency"]
    if not isinstance(next_dep, dict):
        raise ExecutionEnvelopeError("next_causal_dependency must be an object")
    dep_state = str(next_dep.get("state", "")).strip().upper()
    if dep_state not in NEXT_DEPENDENCY_STATES:
        raise ExecutionEnvelopeError(
            f"unsupported next_causal_dependency.state: {dep_state}"
        )
    _text(next_dep.get("description"), "next_causal_dependency.description")
    _text(next_dep.get("source_ref"), "next_causal_dependency.source_ref")

    return True


def envelope_fingerprint(envelope):
    validate_execution_envelope(envelope)
    encoded = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_execution_envelope(envelope):
    try:
        validate_execution_envelope(envelope)
    except (ExecutionEnvelopeError, TypeError, ValueError) as exc:
        return ExecutionGateResult(False, False, None, (str(exc),))

    blockers = []
    for index, claim in enumerate(envelope["claims"]):
        posture = str(claim["evidence_posture"]).strip().upper()
        if claim["blocking"] and posture in BLOCKING_POSTURES:
            blockers.append(f"blocking epistemic claim claims[{index}]={posture}")

    authority = envelope["authority"]
    action_class = str(authority["action_class"]).strip().upper()
    if action_class != "READ_ONLY" and not authority["authorized"]:
        blockers.append("stateful action is not authorized")
    if action_class == "IRREVERSIBLE" and not authority["requires_human"]:
        blockers.append("irreversible action must require human authority")

    fingerprint = envelope_fingerprint(envelope)
    return ExecutionGateResult(True, not blockers, fingerprint, tuple(blockers))


def require_execution_permit(envelope):
    result = evaluate_execution_envelope(envelope)
    if not result.valid:
        raise ExecutionEnvelopeError(result.blockers[0])
    if not result.execution_allowed:
        raise ExecutionEnvelopeError("execution blocked: " + "; ".join(result.blockers))
    return result
