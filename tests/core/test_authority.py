import unittest
from dataclasses import replace

from n0te import (
    ActionIntent,
    ApprovalBinding,
    AuthorityService,
    AuthorityValidationError,
)


def base_intent():
    return ActionIntent(
        action_id="action:publish-preview",
        job_id="job:content.publish",
        action_class="IRREVERSIBLE",
        description="Publish the approved content asset",
        target_ref="asset:content:001",
        revision_fingerprint="sha256:revision-001",
        payload_fingerprint="sha256:payload-001",
        destination="provider:example/channel:artist",
        purpose="Publish the artist-approved content asset",
        data_categories=("content_asset", "caption"),
    )


class Core04AExactAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.service = AuthorityService()

    def test_preview_exposes_exact_material_fields_and_fingerprint(self):
        intent = base_intent()
        preview = self.service.preview(intent)
        self.assertEqual(preview.action_id, intent.action_id)
        self.assertEqual(preview.job_id, intent.job_id)
        self.assertEqual(preview.action_class, "IRREVERSIBLE")
        self.assertEqual(preview.target_ref, intent.target_ref)
        self.assertEqual(preview.revision_fingerprint, intent.revision_fingerprint)
        self.assertEqual(preview.payload_fingerprint, intent.payload_fingerprint)
        self.assertEqual(preview.destination, intent.destination)
        self.assertEqual(preview.purpose, intent.purpose)
        self.assertEqual(preview.data_categories, ("caption", "content_asset"))
        self.assertEqual(preview.intent_fingerprint, intent.intent_fingerprint)
        self.assertEqual(len(preview.intent_fingerprint), 64)

    def test_explicit_approval_binds_exact_fingerprint(self):
        intent = base_intent()
        approval = self.service.bind_approval(
            intent, source_ref="ui:approval-click:123"
        )
        self.assertEqual(approval.intent_fingerprint, intent.intent_fingerprint)
        self.assertEqual(approval.source_ref, "ui:approval-click:123")
        self.assertTrue(approval.approval_id.startswith("approval_"))
        validation = self.service.validate(intent, approval)
        self.assertEqual(validation.status, "VALID")
        self.assertEqual(
            validation.current_intent_fingerprint,
            validation.bound_intent_fingerprint,
        )

    def test_every_material_field_change_invalidates_prior_approval(self):
        intent = base_intent()
        approval = self.service.bind_approval(intent, "ui:explicit")
        variants = (
            replace(intent, action_id="action:other"),
            replace(intent, job_id="job:other"),
            replace(intent, action_class="COMPENSATABLE"),
            replace(intent, description="Publish a materially different description"),
            replace(intent, target_ref="asset:content:002"),
            replace(intent, revision_fingerprint="sha256:revision-002"),
            replace(intent, payload_fingerprint="sha256:payload-002"),
            replace(intent, destination="provider:example/channel:other"),
            replace(intent, purpose="Publish for a different bounded purpose"),
            replace(intent, data_categories=("content_asset", "caption", "analytics")),
        )
        for changed in variants:
            with self.subTest(changed=changed):
                validation = self.service.validate(changed, approval)
                self.assertEqual(validation.status, "STALE")
                self.assertNotEqual(
                    validation.current_intent_fingerprint,
                    validation.bound_intent_fingerprint,
                )

    def test_data_category_order_and_duplicates_canonicalize(self):
        left = base_intent()
        right = replace(
            left,
            data_categories=("caption", "content_asset", "caption"),
        )
        self.assertEqual(left.data_categories, right.data_categories)
        self.assertEqual(left.intent_fingerprint, right.intent_fingerprint)
        approval = self.service.bind_approval(left, "ui:explicit")
        self.assertEqual(self.service.validate(right, approval).status, "VALID")

    def test_destination_requires_explicit_purpose(self):
        with self.assertRaises(AuthorityValidationError):
            ActionIntent(
                action_id="a",
                job_id="j",
                action_class="IRREVERSIBLE",
                description="Outbound action",
                target_ref="target",
                revision_fingerprint="rev",
                payload_fingerprint="payload",
                destination="provider:remote",
                purpose=None,
            )

    def test_local_action_may_have_no_destination(self):
        intent = ActionIntent(
            action_id="action:local",
            job_id="job:local-edit",
            action_class="REVERSIBLE",
            description="Prepare one local reversible edit",
            target_ref="song:001/version:002",
            revision_fingerprint="revision:002",
            payload_fingerprint="payload:local-edit",
        )
        preview = self.service.preview(intent)
        self.assertIsNone(preview.destination)
        self.assertIsNone(preview.purpose)
        approval = self.service.bind_approval(intent, "ui:explicit")
        self.assertEqual(self.service.validate(intent, approval).status, "VALID")

    def test_blank_approval_source_is_rejected(self):
        with self.assertRaises(AuthorityValidationError):
            self.service.bind_approval(base_intent(), "   ")

    def test_action_classes_are_strict(self):
        self.assertEqual(replace(base_intent(), action_class="compensatable").action_class, "COMPENSATABLE")
        with self.assertRaises(AuthorityValidationError):
            replace(base_intent(), action_class="MAYBE_SAFE")

    def test_approval_validation_uses_fingerprint_not_approval_id_or_source(self):
        intent = base_intent()
        approval = ApprovalBinding(
            approval_id="approval_manual",
            intent_fingerprint=intent.intent_fingerprint,
            source_ref="explicit:test",
        )
        self.assertEqual(self.service.validate(intent, approval).status, "VALID")
        stale = replace(
            approval,
            intent_fingerprint="0" * 64,
        )
        self.assertEqual(self.service.validate(intent, stale).status, "STALE")

    def test_preview_and_validation_are_pure_and_deterministic(self):
        intent = base_intent()
        approval = self.service.bind_approval(intent, "ui:explicit")
        first_preview = self.service.preview(intent)
        second_preview = self.service.preview(intent)
        self.assertEqual(first_preview, second_preview)
        self.assertEqual(
            self.service.validate(intent, approval),
            self.service.validate(intent, approval),
        )
        self.assertEqual(intent, base_intent())

    def test_authority_service_structurally_has_no_execution_verbs(self):
        public_methods = {
            name
            for name in dir(AuthorityService)
            if not name.startswith("_") and callable(getattr(AuthorityService, name))
        }
        self.assertEqual(public_methods, {"preview", "bind_approval", "validate"})
        for forbidden in ("execute", "send", "post", "publish", "mutate", "charge"):
            self.assertNotIn(forbidden, public_methods)


if __name__ == "__main__":
    unittest.main()
