from __future__ import annotations

REQUIRED_EXECUTION_SEQUENCE = (
    "COMPILE_CURRENT_STATE",
    "RESUME_CURSOR",
    "DISPATCH_REQUIRED_FUNCTIONS",
    "VALIDATE_EPISTEMIC_AND_DEPENDENCY_STATE",
    "EVALUATE_STANDING_AUTHORITY",
    "ISSUE_EXACT_ACTION_PERMIT",
    "EXECUTE",
    "VERIFY_FRESH_REALITY",
    "RECONCILE_OWNER",
    "ADVANCE_CAUSAL_DEPENDENCY",
)


def assert_single_loop_sequence(sequence) -> None:
    actual = tuple(sequence)
    if actual != REQUIRED_EXECUTION_SEQUENCE:
        raise ValueError(
            "execution path must use the single compiled coordinator loop; "
            f"expected={REQUIRED_EXECUTION_SEQUENCE} actual={actual}"
        )
