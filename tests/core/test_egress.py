import unittest
from dataclasses import replace

from n0te import (
    OutboundEnvelope,
    OutboundInspector,
    OutboundMaterial,
    OutboundValidationError,
)


def material(
    item_id="material:master",
    *,
    category="UNRELEASED_AUDIO",
    source_ref="song:song-1/version:v7/asset:master",
    revision_fingerprint="sha256:master-v7",
    private=True,
    rights_ref="rights:artist-owned",
    consent_ref="consent:artist:outbound-preview",
):
    return OutboundMaterial(
        item_id=item_id,
        category=category,
        source_ref=source_ref,
        revision_fingerprint=revision_fingerprint,
        private=private,
        rights_ref=rights_ref,
        consent_ref=consent_ref,
    )


def envelope(materials=None):
    return OutboundEnvelope(
        request_id="request:ai-analysis:001",
        job_id="job:analyze-master",
        description="Analyze this exact unreleased master with the selected provider",
        destination="provider:model:selected-analysis",
        purpose="Analyze this exact master for bounded mix feedback",
        materials=tuple(materials or (material(),)),
        retention_statement="Provider retention policy reviewed for this bounded request",
        cost_statement="Estimated maximum cost: $0.25",
    )


class Core04BOutboundEgressTests(unittest.TestCase):
    def setUp(self):
        self.inspector = OutboundInspector()

    def test_preview_exposes_exact_material_and_outbound_facts(self):
        env = envelope()
        preview = self.inspector.preview(env)
        self.assertEqual(preview.request_id, env.request_id)
        self.assertEqual(preview.destination, env.destination)
        self.assertEqual(preview.purpose, env.purpose)
        self.assertEqual(preview.private_material_ids, ("material:master",))
        self.assertEqual(preview.data_categories, ("UNRELEASED_AUDIO",))
        self.assertEqual(preview.materials, env.materials)
        self.assertEqual(preview.retention_statement, env.retention_statement)
        self.assertEqual(preview.cost_statement, env.cost_statement)
        self.assertEqual(len(preview.material_revision_fingerprint), 64)
        self.assertEqual(len(preview.payload_fingerprint), 64)
        self.assertEqual(len(preview.intent_fingerprint), 64)

    def test_confirmation_binds_through_exact_core04a_action_intent(self):
        env = envelope()
        confirmation = self.inspector.bind_confirmation(env, "ui:egress-confirmation:42")
        validation = self.inspector.validate_confirmation(env, confirmation)
        self.assertEqual(validation.status, "VALID")
        self.assertEqual(confirmation.source_ref, "ui:egress-confirmation:42")
        self.assertEqual(
            confirmation.intent_fingerprint,
            env.to_action_intent().intent_fingerprint,
        )

    def test_every_material_fact_change_invalidates_prior_confirmation(self):
        env = envelope()
        approval = self.inspector.bind_confirmation(env, "ui:explicit")
        base = env.materials[0]
        variants = (
            replace(base, item_id="material:other"),
            replace(base, category="PRIVATE_ARTIST_CONTEXT"),
            replace(base, source_ref="song:song-1/version:v8/asset:master"),
            replace(base, revision_fingerprint="sha256:master-v8"),
            replace(base, private=False),
            replace(base, rights_ref="rights:collaborator-controlled"),
            replace(base, consent_ref="consent:collaborator:other-scope"),
        )
        for changed_material in variants:
            with self.subTest(changed_material=changed_material):
                changed = replace(env, materials=(changed_material,))
                self.assertEqual(
                    self.inspector.validate_confirmation(changed, approval).status,
                    "STALE",
                )

    def test_envelope_changes_invalidate_prior_confirmation(self):
        env = envelope()
        approval = self.inspector.bind_confirmation(env, "ui:explicit")
        extra = material(
            "material:notes",
            category="PRIVATE_ARTIST_CONTEXT",
            source_ref="song:song-1/context:notes",
            revision_fingerprint="sha256:notes-v1",
        )
        variants = (
            replace(env, request_id="request:other"),
            replace(env, job_id="job:other"),
            replace(env, description="Different bounded outbound job"),
            replace(env, destination="provider:model:other"),
            replace(env, purpose="Different purpose"),
            replace(env, materials=(env.materials[0], extra)),
            replace(env, retention_statement="Different retention statement"),
            replace(env, cost_statement="Estimated maximum cost: $1.00"),
        )
        for changed in variants:
            with self.subTest(changed=changed):
                self.assertEqual(
                    self.inspector.validate_confirmation(changed, approval).status,
                    "STALE",
                )

    def test_material_input_order_canonicalizes(self):
        first = material()
        second = material(
            "material:notes",
            category="PRIVATE_ARTIST_CONTEXT",
            source_ref="song:song-1/context:notes",
            revision_fingerprint="sha256:notes-v1",
        )
        left = envelope((first, second))
        right = envelope((second, first))
        self.assertEqual(left, right)
        self.assertEqual(left.payload_fingerprint, right.payload_fingerprint)
        self.assertEqual(
            left.to_action_intent().intent_fingerprint,
            right.to_action_intent().intent_fingerprint,
        )

    def test_duplicate_material_identity_is_rejected(self):
        with self.assertRaises(OutboundValidationError):
            envelope((material(), replace(material(), category="OTHER")))

    def test_private_flag_requires_real_boolean(self):
        with self.assertRaises(TypeError):
            material(private="false")
        with self.assertRaises(TypeError):
            material(private=1)

    def test_required_outbound_explanation_fields_cannot_be_blank(self):
        env = envelope()
        for field in (
            "destination",
            "purpose",
            "retention_statement",
            "cost_statement",
        ):
            with self.subTest(field=field):
                with self.assertRaises(OutboundValidationError):
                    replace(env, **{field: "   "})

    def test_blank_confirmation_source_is_rejected(self):
        with self.assertRaises(OutboundValidationError):
            self.inspector.bind_confirmation(envelope(), "   ")

    def test_categories_and_private_ids_are_derived_only_from_explicit_materials(self):
        public = material(
            "material:public-caption",
            category="PUBLIC_CAPTION",
            source_ref="content:caption:1",
            revision_fingerprint="sha256:caption-1",
            private=False,
            rights_ref=None,
            consent_ref=None,
        )
        private = material()
        env = envelope((public, private))
        self.assertEqual(env.private_material_ids, ("material:master",))
        self.assertEqual(
            env.data_categories,
            ("PUBLIC_CAPTION", "UNRELEASED_AUDIO"),
        )

    def test_inspector_structurally_has_no_transport_verbs(self):
        public_methods = {
            name
            for name in dir(OutboundInspector)
            if not name.startswith("_") and callable(getattr(OutboundInspector, name))
        }
        self.assertEqual(
            public_methods,
            {"preview", "bind_confirmation", "validate_confirmation"},
        )
        for forbidden in (
            "send",
            "upload",
            "transmit",
            "request",
            "call_model",
            "execute",
            "publish",
            "post",
        ):
            self.assertNotIn(forbidden, public_methods)

    def test_preview_and_validation_are_pure_and_deterministic(self):
        env = envelope()
        approval = self.inspector.bind_confirmation(env, "ui:explicit")
        self.assertEqual(self.inspector.preview(env), self.inspector.preview(env))
        self.assertEqual(
            self.inspector.validate_confirmation(env, approval),
            self.inspector.validate_confirmation(env, approval),
        )
        self.assertEqual(env, envelope())


if __name__ == "__main__":
    unittest.main()
