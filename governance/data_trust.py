from __future__ import annotations

from dataclasses import dataclass


GOVERNANCE_FIELDS = frozenset({
    "truth_owners",
    "retained_scope_refs",
    "required_functions",
    "acceptance",
    "authority",
    "dependency_graph",
    "lens_dispatch",
})


class DataTrustError(ValueError):
    pass


@dataclass(frozen=True)
class ExternalEvidence:
    source_ref: str
    content: str


def assert_external_data_cannot_rewrite_governance(candidate: dict) -> None:
    if not isinstance(candidate, dict):
        raise DataTrustError("external candidate must be an object")
    attempted = sorted(GOVERNANCE_FIELDS.intersection(candidate))
    if attempted:
        raise DataTrustError(
            f"external evidence cannot redefine governance fields: {attempted}"
        )
