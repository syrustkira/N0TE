from __future__ import annotations

from dataclasses import dataclass


class DuplicateGovernanceError(ValueError):
    pass


@dataclass(frozen=True)
class GovernanceAddition:
    proposed_name: str
    proposed_semantic_owner: str
    searched_existing_owners: tuple[str, ...]
    equivalent_owner: str | None = None
    reason_existing_owner_cannot_be_extended: str | None = None

    def validate(self) -> None:
        if not self.proposed_name.strip() or not self.proposed_semantic_owner.strip():
            raise DuplicateGovernanceError("proposed governance addition must be named and owned")
        if not self.searched_existing_owners:
            raise DuplicateGovernanceError("existing owners must be searched before adding governance")
        if self.equivalent_owner:
            reason = (self.reason_existing_owner_cannot_be_extended or "").strip()
            if not reason:
                raise DuplicateGovernanceError(
                    f"semantic equivalent already exists at {self.equivalent_owner}; extend that owner instead"
                )


def validate_governance_addition(item: GovernanceAddition) -> GovernanceAddition:
    if not isinstance(item, GovernanceAddition):
        raise TypeError("item must be GovernanceAddition")
    item.validate()
    return item
