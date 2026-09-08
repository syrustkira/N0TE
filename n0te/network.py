from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

NETWORK_MODES = {"OFFLINE", "CONNECTED"}
NETWORK_ROUTE_KINDS = {"LOCALHOST", "LAN", "INTERNET"}
TRANSPORT_DECISION_STATUSES = {"ALLOW", "DENY"}
TRANSITION_STATUSES = {"READY", "CHOICE_REQUIRED"}
PENDING_EXTERNAL_STATUSES = {"PENDING", "UNSENT", "UNRECONCILED"}
OFFLINE_TRANSITION_CHOICES = (
    "FINISH_FIRST",
    "PRESERVE_AND_GO_OFFLINE",
    "PROCEED_WITH_PENDING",
)
CONNECTED_TRANSITION_CHOICES = (
    "SYNC_NOW",
    "SYNC_SELECTIVELY",
    "POSTPONE",
    "REMAIN_UNSYNCED",
)


class NetworkPolicyError(ValueError):
    """Invalid connectivity-policy input or transition request."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise NetworkPolicyError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


@dataclass(frozen=True)
class NetworkRoute:
    route_id: str
    kind: str
    description: str
    lan_approval_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_id", _text(self.route_id, "route.route_id"))
        kind = str(self.kind).strip().upper()
        if kind not in NETWORK_ROUTE_KINDS:
            raise NetworkPolicyError(f"unsupported network route kind: {kind}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self, "description", _text(self.description, "route.description")
        )
        approval = _optional_text(self.lan_approval_ref, "route.lan_approval_ref")
        if kind != "LAN" and approval is not None:
            raise NetworkPolicyError(
                "lan_approval_ref is valid only for LAN routes"
            )
        object.__setattr__(self, "lan_approval_ref", approval)


@dataclass(frozen=True)
class TransportDecision:
    status: str
    mode: str
    route_id: str
    route_kind: str
    reason_codes: tuple[str, ...]
    action_authority_granted: bool = False

    def __post_init__(self) -> None:
        if self.status not in TRANSPORT_DECISION_STATUSES:
            raise NetworkPolicyError(
                f"unsupported transport decision status: {self.status}"
            )
        if self.mode not in NETWORK_MODES:
            raise NetworkPolicyError(f"unsupported network mode: {self.mode}")
        if self.route_kind not in NETWORK_ROUTE_KINDS:
            raise NetworkPolicyError(
                f"unsupported network route kind: {self.route_kind}"
            )
        if self.action_authority_granted is not False:
            raise NetworkPolicyError(
                "network policy may never grant action authority"
            )


@dataclass(frozen=True)
class PendingExternalChange:
    change_id: str
    kind: str
    description: str
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "change_id", _text(self.change_id, "pending.change_id")
        )
        object.__setattr__(self, "kind", _text(self.kind, "pending.kind"))
        object.__setattr__(
            self, "description", _text(self.description, "pending.description")
        )
        status = str(self.status).strip().upper()
        if status not in PENDING_EXTERNAL_STATUSES:
            raise NetworkPolicyError(
                f"unsupported pending external status: {status}"
            )
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class OfflineAccumulatedChange:
    change_id: str
    kind: str
    description: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "change_id", _text(self.change_id, "offline_change.change_id")
        )
        object.__setattr__(
            self, "kind", _text(self.kind, "offline_change.kind")
        )
        object.__setattr__(
            self,
            "description",
            _text(self.description, "offline_change.description"),
        )


@dataclass(frozen=True)
class NetworkTransitionPlan:
    direction: str
    status: str
    current_mode: str
    target_mode: str
    change_ids: tuple[str, ...]
    choices: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.direction not in {"TO_OFFLINE", "TO_CONNECTED"}:
            raise NetworkPolicyError(
                f"unsupported transition direction: {self.direction}"
            )
        if self.status not in TRANSITION_STATUSES:
            raise NetworkPolicyError(
                f"unsupported transition status: {self.status}"
            )
        if self.current_mode not in NETWORK_MODES or self.target_mode not in NETWORK_MODES:
            raise NetworkPolicyError("transition modes must be valid network modes")


@dataclass(frozen=True)
class NetworkTransitionResult:
    direction: str
    next_mode: str
    selected_choice: str | None
    preserved_change_ids: tuple[str, ...]
    reconciliation_directive: str | None
    requires_external_work: bool
    performed_external_action: bool = False
    action_authority_granted: bool = False

    def __post_init__(self) -> None:
        if self.direction not in {"TO_OFFLINE", "TO_CONNECTED"}:
            raise NetworkPolicyError(
                f"unsupported transition direction: {self.direction}"
            )
        if self.next_mode not in NETWORK_MODES:
            raise NetworkPolicyError(f"unsupported next network mode: {self.next_mode}")
        if self.performed_external_action is not False:
            raise NetworkPolicyError(
                "network transition planning may not perform external actions"
            )
        if self.action_authority_granted is not False:
            raise NetworkPolicyError(
                "network transition planning may not grant action authority"
            )


class NetworkPolicy:
    """Pure connectivity eligibility and transition-planning law.

    ALLOW means only that this connectivity class is permitted by the artist's
    current network policy. It never means the underlying action is approved.
    Transition resolution records an artist choice/directive but performs no
    network connection, provider call, synchronization, publication or spend.
    """

    def __init__(self, mode: str) -> None:
        normalized = str(mode).strip().upper()
        if normalized not in NETWORK_MODES:
            raise NetworkPolicyError(f"unsupported network mode: {normalized}")
        self._mode = normalized

    @property
    def mode(self) -> str:
        return self._mode

    @staticmethod
    def _unique_change_ids(changes: Iterable[object]) -> tuple[str, ...]:
        ids = tuple(sorted(item.change_id for item in changes))
        if len(ids) != len(set(ids)):
            raise NetworkPolicyError("change_id values must be unique")
        return ids

    def evaluate(self, route: NetworkRoute) -> TransportDecision:
        if not isinstance(route, NetworkRoute):
            raise TypeError("route must be NetworkRoute")

        if route.kind == "LOCALHOST":
            return TransportDecision(
                status="ALLOW",
                mode=self.mode,
                route_id=route.route_id,
                route_kind=route.kind,
                reason_codes=(
                    "LOCALHOST_PERMITTED",
                    "CONNECTIVITY_ONLY_NO_ACTION_AUTHORITY",
                ),
            )

        if route.kind == "LAN":
            if route.lan_approval_ref is None:
                return TransportDecision(
                    status="DENY",
                    mode=self.mode,
                    route_id=route.route_id,
                    route_kind=route.kind,
                    reason_codes=(
                        "LAN_EXPLICIT_APPROVAL_REQUIRED",
                        "CONNECTIVITY_ONLY_NO_ACTION_AUTHORITY",
                    ),
                )
            return TransportDecision(
                status="ALLOW",
                mode=self.mode,
                route_id=route.route_id,
                route_kind=route.kind,
                reason_codes=(
                    "LAN_EXPLICIT_APPROVAL_PRESENT",
                    "CONNECTIVITY_ONLY_NO_ACTION_AUTHORITY",
                ),
            )

        if self.mode == "OFFLINE":
            return TransportDecision(
                status="DENY",
                mode=self.mode,
                route_id=route.route_id,
                route_kind=route.kind,
                reason_codes=(
                    "OFFLINE_BLOCKS_INTERNET",
                    "CONNECTIVITY_ONLY_NO_ACTION_AUTHORITY",
                ),
            )

        return TransportDecision(
            status="ALLOW",
            mode=self.mode,
            route_id=route.route_id,
            route_kind=route.kind,
            reason_codes=(
                "CONNECTED_INTERNET_TRANSPORT_ELIGIBLE",
                "CONNECTIVITY_ONLY_NO_ACTION_AUTHORITY",
            ),
        )

    def plan_offline_transition(
        self,
        pending_changes: Iterable[PendingExternalChange] = (),
    ) -> NetworkTransitionPlan:
        if self.mode != "CONNECTED":
            raise NetworkPolicyError(
                "offline transition may be planned only from CONNECTED mode"
            )
        pending = tuple(pending_changes)
        if not all(isinstance(item, PendingExternalChange) for item in pending):
            raise TypeError("pending_changes must contain PendingExternalChange")
        change_ids = self._unique_change_ids(pending)
        if not change_ids:
            return NetworkTransitionPlan(
                direction="TO_OFFLINE",
                status="READY",
                current_mode=self.mode,
                target_mode="OFFLINE",
                change_ids=(),
                choices=(),
                reason_codes=("NO_PENDING_EXTERNAL_CHANGES",),
            )
        return NetworkTransitionPlan(
            direction="TO_OFFLINE",
            status="CHOICE_REQUIRED",
            current_mode=self.mode,
            target_mode="OFFLINE",
            change_ids=change_ids,
            choices=OFFLINE_TRANSITION_CHOICES,
            reason_codes=("PENDING_EXTERNAL_CHANGES_REQUIRE_ARTIST_CHOICE",),
        )

    def resolve_offline_transition(
        self,
        plan: NetworkTransitionPlan,
        choice: str | None = None,
    ) -> NetworkTransitionResult:
        if not isinstance(plan, NetworkTransitionPlan):
            raise TypeError("plan must be NetworkTransitionPlan")
        if self.mode != "CONNECTED" or plan.current_mode != self.mode:
            raise NetworkPolicyError("offline transition plan does not match policy mode")
        if plan.direction != "TO_OFFLINE" or plan.target_mode != "OFFLINE":
            raise NetworkPolicyError("plan is not an offline transition")

        if plan.status == "READY":
            if choice is not None:
                raise NetworkPolicyError("READY offline transition does not accept a choice")
            return NetworkTransitionResult(
                direction=plan.direction,
                next_mode="OFFLINE",
                selected_choice=None,
                preserved_change_ids=plan.change_ids,
                reconciliation_directive=None,
                requires_external_work=False,
            )

        normalized = _text(choice, "offline_transition.choice").upper()
        if normalized not in OFFLINE_TRANSITION_CHOICES:
            raise NetworkPolicyError(
                f"unsupported offline transition choice: {normalized}"
            )
        if normalized == "FINISH_FIRST":
            return NetworkTransitionResult(
                direction=plan.direction,
                next_mode="CONNECTED",
                selected_choice=normalized,
                preserved_change_ids=plan.change_ids,
                reconciliation_directive="FINISH_PENDING_REMOTE_WORK",
                requires_external_work=True,
            )
        return NetworkTransitionResult(
            direction=plan.direction,
            next_mode="OFFLINE",
            selected_choice=normalized,
            preserved_change_ids=plan.change_ids,
            reconciliation_directive=normalized,
            requires_external_work=False,
        )

    def plan_connected_transition(
        self,
        offline_changes: Iterable[OfflineAccumulatedChange] = (),
    ) -> NetworkTransitionPlan:
        if self.mode != "OFFLINE":
            raise NetworkPolicyError(
                "connected transition may be planned only from OFFLINE mode"
            )
        changes = tuple(offline_changes)
        if not all(isinstance(item, OfflineAccumulatedChange) for item in changes):
            raise TypeError(
                "offline_changes must contain OfflineAccumulatedChange"
            )
        change_ids = self._unique_change_ids(changes)
        if not change_ids:
            return NetworkTransitionPlan(
                direction="TO_CONNECTED",
                status="READY",
                current_mode=self.mode,
                target_mode="CONNECTED",
                change_ids=(),
                choices=(),
                reason_codes=("NOTHING_TO_RECONCILE",),
            )
        return NetworkTransitionPlan(
            direction="TO_CONNECTED",
            status="CHOICE_REQUIRED",
            current_mode=self.mode,
            target_mode="CONNECTED",
            change_ids=change_ids,
            choices=CONNECTED_TRANSITION_CHOICES,
            reason_codes=("OFFLINE_CHANGES_REQUIRE_RECONCILIATION_CHOICE",),
        )

    def resolve_connected_transition(
        self,
        plan: NetworkTransitionPlan,
        choice: str | None = None,
    ) -> NetworkTransitionResult:
        if not isinstance(plan, NetworkTransitionPlan):
            raise TypeError("plan must be NetworkTransitionPlan")
        if self.mode != "OFFLINE" or plan.current_mode != self.mode:
            raise NetworkPolicyError(
                "connected transition plan does not match policy mode"
            )
        if plan.direction != "TO_CONNECTED" or plan.target_mode != "CONNECTED":
            raise NetworkPolicyError("plan is not a connected transition")

        if plan.status == "READY":
            if choice is not None:
                raise NetworkPolicyError(
                    "READY connected transition does not accept a choice"
                )
            return NetworkTransitionResult(
                direction=plan.direction,
                next_mode="CONNECTED",
                selected_choice=None,
                preserved_change_ids=plan.change_ids,
                reconciliation_directive=None,
                requires_external_work=False,
            )

        normalized = _text(choice, "connected_transition.choice").upper()
        if normalized not in CONNECTED_TRANSITION_CHOICES:
            raise NetworkPolicyError(
                f"unsupported connected transition choice: {normalized}"
            )
        return NetworkTransitionResult(
            direction=plan.direction,
            next_mode="CONNECTED",
            selected_choice=normalized,
            preserved_change_ids=plan.change_ids,
            reconciliation_directive=normalized,
            requires_external_work=normalized in {"SYNC_NOW", "SYNC_SELECTIVELY"},
        )
