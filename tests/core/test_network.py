import unittest

from n0te import (
    CONNECTED_TRANSITION_CHOICES,
    OFFLINE_TRANSITION_CHOICES,
    NetworkPolicy,
    NetworkPolicyError,
    NetworkRoute,
    OfflineAccumulatedChange,
    PendingExternalChange,
)


class Core04CNetworkPolicyTests(unittest.TestCase):
    def test_offline_blocks_internet_but_preserves_localhost(self):
        policy = NetworkPolicy("OFFLINE")
        local = policy.evaluate(
            NetworkRoute("route:local", "LOCALHOST", "Local N0TE service")
        )
        internet = policy.evaluate(
            NetworkRoute("route:web", "INTERNET", "Remote provider")
        )
        self.assertEqual(local.status, "ALLOW")
        self.assertEqual(internet.status, "DENY")
        self.assertIn("OFFLINE_BLOCKS_INTERNET", internet.reason_codes)
        self.assertFalse(local.action_authority_granted)
        self.assertFalse(internet.action_authority_granted)

    def test_connected_allows_transport_class_but_not_action_authority(self):
        decision = NetworkPolicy("connected").evaluate(
            NetworkRoute("route:web", "internet", "Remote provider")
        )
        self.assertEqual(decision.status, "ALLOW")
        self.assertIn("CONNECTED_INTERNET_TRANSPORT_ELIGIBLE", decision.reason_codes)
        self.assertFalse(decision.action_authority_granted)

    def test_lan_requires_explicit_approval_in_both_modes(self):
        route = NetworkRoute("route:lan", "LAN", "Studio collaborator")
        for mode in ("OFFLINE", "CONNECTED"):
            with self.subTest(mode=mode):
                denied = NetworkPolicy(mode).evaluate(route)
                self.assertEqual(denied.status, "DENY")
                approved = NetworkPolicy(mode).evaluate(
                    NetworkRoute(
                        "route:lan",
                        "LAN",
                        "Studio collaborator",
                        lan_approval_ref="artist:lan-approval:1",
                    )
                )
                self.assertEqual(approved.status, "ALLOW")
                self.assertFalse(approved.action_authority_granted)

    def test_lan_approval_ref_cannot_be_smuggled_onto_other_route_types(self):
        with self.assertRaises(NetworkPolicyError):
            NetworkRoute(
                "route:web",
                "INTERNET",
                "Remote provider",
                lan_approval_ref="artist:approval",
            )

    def test_pending_external_changes_require_choice_before_offline(self):
        policy = NetworkPolicy("CONNECTED")
        pending = (
            PendingExternalChange(
                "change:upload",
                "UPLOAD",
                "Unsent master upload",
                "UNSENT",
            ),
            PendingExternalChange(
                "change:receipt",
                "PROVIDER_RECEIPT",
                "Publication receipt not reconciled",
                "UNRECONCILED",
            ),
        )
        plan = policy.plan_offline_transition(reversed(pending))
        self.assertEqual(plan.status, "CHOICE_REQUIRED")
        self.assertEqual(plan.change_ids, ("change:receipt", "change:upload"))
        self.assertEqual(plan.choices, OFFLINE_TRANSITION_CHOICES)

        finish = policy.resolve_offline_transition(plan, "FINISH_FIRST")
        self.assertEqual(finish.next_mode, "CONNECTED")
        self.assertTrue(finish.requires_external_work)
        self.assertEqual(finish.preserved_change_ids, plan.change_ids)
        self.assertFalse(finish.performed_external_action)

        preserve = policy.resolve_offline_transition(
            plan, "PRESERVE_AND_GO_OFFLINE"
        )
        self.assertEqual(preserve.next_mode, "OFFLINE")
        self.assertEqual(preserve.preserved_change_ids, plan.change_ids)
        self.assertFalse(preserve.performed_external_action)

        proceed = policy.resolve_offline_transition(plan, "PROCEED_WITH_PENDING")
        self.assertEqual(proceed.next_mode, "OFFLINE")
        self.assertEqual(proceed.preserved_change_ids, plan.change_ids)

    def test_no_pending_changes_can_enter_offline_without_fake_choice(self):
        policy = NetworkPolicy("CONNECTED")
        plan = policy.plan_offline_transition()
        self.assertEqual(plan.status, "READY")
        self.assertEqual(plan.change_ids, ())
        self.assertEqual(plan.choices, ())
        result = policy.resolve_offline_transition(plan)
        self.assertEqual(result.next_mode, "OFFLINE")
        self.assertFalse(result.requires_external_work)
        self.assertFalse(result.performed_external_action)

    def test_reconnect_with_offline_changes_requires_explicit_reconciliation_choice(self):
        policy = NetworkPolicy("OFFLINE")
        changes = (
            OfflineAccumulatedChange(
                "change:local-song",
                "SONG_EDIT",
                "Song changed while offline",
            ),
            OfflineAccumulatedChange(
                "change:local-draft",
                "DRAFT",
                "Provider draft changed locally while offline",
            ),
        )
        plan = policy.plan_connected_transition(reversed(changes))
        self.assertEqual(plan.status, "CHOICE_REQUIRED")
        self.assertEqual(plan.choices, CONNECTED_TRANSITION_CHOICES)
        self.assertEqual(
            plan.change_ids,
            ("change:local-draft", "change:local-song"),
        )
        for choice in CONNECTED_TRANSITION_CHOICES:
            with self.subTest(choice=choice):
                result = policy.resolve_connected_transition(plan, choice)
                self.assertEqual(result.next_mode, "CONNECTED")
                self.assertEqual(result.reconciliation_directive, choice)
                self.assertEqual(result.preserved_change_ids, plan.change_ids)
                self.assertFalse(result.performed_external_action)
                self.assertFalse(result.action_authority_granted)
                self.assertEqual(
                    result.requires_external_work,
                    choice in {"SYNC_NOW", "SYNC_SELECTIVELY"},
                )

    def test_reconnect_with_nothing_changed_reports_nothing_to_reconcile(self):
        policy = NetworkPolicy("OFFLINE")
        plan = policy.plan_connected_transition()
        self.assertEqual(plan.status, "READY")
        self.assertIn("NOTHING_TO_RECONCILE", plan.reason_codes)
        result = policy.resolve_connected_transition(plan)
        self.assertEqual(result.next_mode, "CONNECTED")
        self.assertIsNone(result.reconciliation_directive)
        self.assertFalse(result.performed_external_action)

    def test_duplicate_change_identity_is_rejected(self):
        policy = NetworkPolicy("CONNECTED")
        duplicate = PendingExternalChange(
            "change:1", "UPLOAD", "One", "PENDING"
        )
        with self.assertRaises(NetworkPolicyError):
            policy.plan_offline_transition((duplicate, duplicate))

    def test_transition_plan_must_match_current_mode_and_direction(self):
        connected = NetworkPolicy("CONNECTED")
        offline = NetworkPolicy("OFFLINE")
        offline_plan = connected.plan_offline_transition()
        connected_plan = offline.plan_connected_transition()
        with self.assertRaises(NetworkPolicyError):
            offline.resolve_offline_transition(offline_plan)
        with self.assertRaises(NetworkPolicyError):
            connected.resolve_connected_transition(connected_plan)

    def test_policy_structurally_has_no_network_or_sync_execution_verbs(self):
        public_methods = {
            name
            for name in dir(NetworkPolicy)
            if not name.startswith("_") and callable(getattr(NetworkPolicy, name))
        }
        self.assertEqual(
            public_methods,
            {
                "evaluate",
                "plan_offline_transition",
                "resolve_offline_transition",
                "plan_connected_transition",
                "resolve_connected_transition",
            },
        )
        for forbidden in (
            "connect",
            "disconnect",
            "send",
            "upload",
            "sync",
            "publish",
            "execute",
            "request",
            "call_provider",
        ):
            self.assertNotIn(forbidden, public_methods)

    def test_decisions_and_transition_resolution_are_pure_and_deterministic(self):
        policy = NetworkPolicy("OFFLINE")
        route = NetworkRoute("route:local", "LOCALHOST", "Local service")
        self.assertEqual(policy.evaluate(route), policy.evaluate(route))
        changes = (
            OfflineAccumulatedChange("change:1", "SONG_EDIT", "Local edit"),
        )
        plan = policy.plan_connected_transition(changes)
        self.assertEqual(
            policy.resolve_connected_transition(plan, "POSTPONE"),
            policy.resolve_connected_transition(plan, "POSTPONE"),
        )
        self.assertEqual(policy.mode, "OFFLINE")


if __name__ == "__main__":
    unittest.main()
