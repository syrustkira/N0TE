from __future__ import annotations

import unittest

from n0te.lineage import ValidationError
from n0te.professional_roles import (
    CORE_PROFESSIONAL_ROLE_IDS,
    LENS_IDS,
    PROFESSIONAL_ROLE_ONTOLOGY_VERSION,
    PROFESSIONAL_ROLE_SOURCE,
    PROFESSIONAL_ROLES,
    REPRESENTATIVE_PROFESSIONAL_ROLE_IDS,
    ProfessionalRole,
    get_professional_role,
    linked_career_role,
    linked_runtime_handoffs,
    list_professional_roles,
    professional_lens_applicability,
)


class ProfessionalRoleOntologyTests(unittest.TestCase):
    def test_core_requirement_roles_are_independently_present(self) -> None:
        self.assertEqual(
            CORE_PROFESSIONAL_ROLE_IDS,
            ("R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08"),
        )
        labels = {
            role.id: role.label for role in list_professional_roles(scope_tier="CORE")
        }
        self.assertEqual(labels["R01"], "Artist / Featured Artist / Band")
        self.assertEqual(labels["R02"], "Producer / Co-producer / Record Producer")
        self.assertEqual(labels["R03"], "Mix Engineer")
        self.assertEqual(labels["R04"], "Mastering Engineer")
        self.assertEqual(labels["R05"], "Songwriter / Composer / Lyricist / Topliner")
        self.assertEqual(
            labels["R06"],
            "Session Musician / Session Singer / Background Vocalist",
        )
        self.assertEqual(labels["R07"], "Artist Manager")
        self.assertEqual(labels["R08"], "A&R / Artist Development")

    def test_representative_business_rights_and_live_roles_prevent_artist_flattening(
        self,
    ) -> None:
        self.assertEqual(
            REPRESENTATIVE_PROFESSIONAL_ROLE_IDS,
            ("R19", "R21", "R29"),
        )
        self.assertEqual(get_professional_role("R19").family, "Management/Finance")
        self.assertEqual(get_professional_role("R21").family, "Rights/Publishing")
        self.assertEqual(get_professional_role("R29").family, "Live/Booking")
        with self.assertRaises(ValidationError):
            get_professional_role("Music Attorney")

    def test_alias_resolution_is_deterministic_and_case_insensitive(self) -> None:
        self.assertIs(get_professional_role("artist"), PROFESSIONAL_ROLES["R01"])
        self.assertIs(get_professional_role("Featured Artist"), PROFESSIONAL_ROLES["R01"])
        self.assertIs(get_professional_role("co-PRODUCER"), PROFESSIONAL_ROLES["R02"])
        self.assertIs(get_professional_role("A&R"), PROFESSIONAL_ROLES["R08"])
        self.assertIs(
            get_professional_role("publishing administrator"),
            PROFESSIONAL_ROLES["R21"],
        )
        self.assertIs(
            get_professional_role("Artist / Featured Artist / Band"),
            PROFESSIONAL_ROLES["R01"],
        )

    def test_public_role_registry_rejects_mutation_and_keeps_indexes_consistent(self) -> None:
        artist = PROFESSIONAL_ROLES["R01"]
        with self.assertRaises(TypeError):
            PROFESSIONAL_ROLES["R01"] = PROFESSIONAL_ROLES["R02"]  # type: ignore[index]
        with self.assertRaises(TypeError):
            del PROFESSIONAL_ROLES["R01"]  # type: ignore[attr-defined]
        self.assertIs(PROFESSIONAL_ROLES["R01"], artist)
        self.assertIs(get_professional_role("Artist"), artist)
        self.assertIn(artist, list_professional_roles())

    def test_role_map_is_read_only_semantics_not_an_authority_principal(self) -> None:
        for role in PROFESSIONAL_ROLES.values():
            with self.subTest(role=role.id):
                self.assertFalse(role.grants_identity_authority)
                self.assertFalse(role.grants_action_authority)
                self.assertFalse(role.grants_execution_authority)
                self.assertFalse(role.grants_external_action_authority)
                self.assertFalse(role.grants_legal_authority)
                self.assertFalse(role.grants_spend_authority)
                self.assertFalse(role.grants_any_authority)
                self.assertEqual(role.source, PROFESSIONAL_ROLE_SOURCE)
        with self.assertRaises(TypeError):
            ProfessionalRole(
                id="R98",
                scope_tier="REPRESENTATIVE",
                family="Test",
                label="Test Role",
                aliases=("Test Role",),
                primary_outcome="Test outcome.",
                lifecycle_jobs=("test",),
                primary_lens_ids=("L01",),
                secondary_lens_ids=(),
                key_inputs=("input",),
                key_deliverables=("output",),
                rights_economics_note="No claim.",
                health_risk_note="No claim.",
                handoff_summary="No runtime handoff.",
                grants_action_authority=True,
            )

    def test_all_lens_sets_are_canonical_unique_and_disjoint(self) -> None:
        valid = set(LENS_IDS)
        self.assertEqual(len(valid), 70)
        for role in PROFESSIONAL_ROLES.values():
            with self.subTest(role=role.id):
                self.assertEqual(
                    len(role.primary_lens_ids), len(set(role.primary_lens_ids))
                )
                self.assertEqual(
                    len(role.secondary_lens_ids),
                    len(set(role.secondary_lens_ids)),
                )
                self.assertTrue(set(role.primary_lens_ids) <= valid)
                self.assertTrue(set(role.secondary_lens_ids) <= valid)
                self.assertFalse(
                    set(role.primary_lens_ids) & set(role.secondary_lens_ids)
                )

    def test_lens_applicability_preserves_primary_secondary_and_not_applicable(
        self,
    ) -> None:
        self.assertEqual(
            professional_lens_applicability("Mastering Engineer", "L20"),
            "PRIMARY",
        )
        self.assertEqual(
            professional_lens_applicability("Mastering Engineer", "L16"),
            "SECONDARY",
        )
        self.assertEqual(
            professional_lens_applicability("Mastering Engineer", "L13"),
            "NOT_APPLICABLE",
        )
        self.assertEqual(
            professional_lens_applicability("Booking Agent", "L31"),
            "PRIMARY",
        )
        self.assertEqual(
            professional_lens_applicability("Booking Agent", "L30"),
            "SECONDARY",
        )

    def test_jobs_inputs_deliverables_and_risk_context_remain_role_specific(
        self,
    ) -> None:
        producer = get_professional_role("Producer")
        mastering = get_professional_role("Mastering Engineer")
        session = get_professional_role("Session Musician")
        manager = get_professional_role("Artist Manager")

        self.assertIn("mix/master supervision", producer.lifecycle_jobs)
        self.assertIn("formats/metadata", mastering.lifecycle_jobs)
        self.assertIn("usage/terms", session.key_inputs)
        self.assertIn("opportunities", manager.key_deliverables)
        self.assertNotEqual(producer.lifecycle_jobs, mastering.lifecycle_jobs)
        self.assertNotEqual(mastering.key_inputs, session.key_inputs)
        self.assertIn("participation", mastering.rights_economics_note.casefold())
        self.assertIn("hearing", mastering.health_risk_note.casefold())
        self.assertIn("power asymmetry", manager.health_risk_note.casefold())

    def test_existing_career_ladders_are_linked_not_reimplemented(self) -> None:
        self.assertEqual(linked_career_role("Artist").id, "ARTIST")
        self.assertEqual(linked_career_role("Producer").id, "PRODUCER")
        self.assertEqual(linked_career_role("Mix Engineer").id, "MIX_ENGINEER")
        self.assertEqual(linked_career_role("Songwriter").id, "SONGWRITER")
        self.assertEqual(linked_career_role("Artist Manager").id, "MANAGER")

        # These canonical professional roles do not yet have a career ladder in
        # career_roles.py. The ontology must preserve that gap rather than
        # inventing competence or silently mapping them to a neighboring role.
        self.assertIsNone(linked_career_role("Mastering Engineer"))
        self.assertIsNone(linked_career_role("Session Musician"))
        self.assertIsNone(linked_career_role("A&R"))
        self.assertIsNone(linked_career_role("Business Manager"))

    def test_existing_runtime_handoff_specs_are_linked_without_granting_execution(
        self,
    ) -> None:
        self.assertEqual(
            tuple(spec.id for spec in linked_runtime_handoffs("Producer")),
            ("H07", "H09"),
        )
        self.assertEqual(
            tuple(spec.id for spec in linked_runtime_handoffs("Mix Engineer")),
            ("H07", "H08"),
        )
        self.assertEqual(
            tuple(spec.id for spec in linked_runtime_handoffs("Mastering Engineer")),
            ("H08", "H09"),
        )
        self.assertEqual(linked_runtime_handoffs("Songwriter"), ())
        for spec in linked_runtime_handoffs("Producer"):
            self.assertIn(spec.id, {"H07", "H09"})

    def test_ontology_has_stable_version_and_source_but_no_runtime_state(self) -> None:
        self.assertEqual(PROFESSIONAL_ROLE_ONTOLOGY_VERSION, 1)
        self.assertEqual(
            PROFESSIONAL_ROLE_SOURCE,
            "N0TE_PRODUCT_DB/MUSIC_PROFESSIONAL_MAP",
        )
        self.assertEqual(len(PROFESSIONAL_ROLES), 11)
        self.assertEqual(
            tuple(role.id for role in list_professional_roles()),
            ("R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "R19", "R21", "R29"),
        )

    def test_malformed_or_unknown_role_and_lens_inputs_fail_closed(self) -> None:
        for value in (None, True, 7, b"R01", "", "   ", "R99"):
            with self.subTest(role=value):
                with self.assertRaises(ValidationError):
                    get_professional_role(value)  # type: ignore[arg-type]

        for value in (None, True, 1, b"L01", "", "L00", "L71", "1", "L1"):
            with self.subTest(lens=value):
                with self.assertRaises(ValidationError):
                    professional_lens_applicability(
                        "Artist", value  # type: ignore[arg-type]
                    )

    def test_scope_tier_filter_rejects_semantic_coercion(self) -> None:
        self.assertEqual(
            tuple(role.id for role in list_professional_roles(scope_tier="core")),
            CORE_PROFESSIONAL_ROLE_IDS,
        )
        with self.assertRaises(ValidationError):
            list_professional_roles(scope_tier=True)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            list_professional_roles(scope_tier="ALL")

    def test_constructor_rejects_overlapping_lenses_and_fake_runtime_links(self) -> None:
        common = dict(
            id="R98",
            scope_tier="REPRESENTATIVE",
            family="Test",
            label="Test Role",
            aliases=("Test Role",),
            primary_outcome="Test outcome.",
            lifecycle_jobs=("test",),
            key_inputs=("input",),
            key_deliverables=("output",),
            rights_economics_note="No legal or economic claim.",
            health_risk_note="No health claim.",
            handoff_summary="No handoff claim.",
        )
        with self.assertRaises(ValidationError):
            ProfessionalRole(
                **common,
                primary_lens_ids=("L01",),
                secondary_lens_ids=("L01",),
            )
        with self.assertRaises(ValidationError):
            ProfessionalRole(
                **common,
                primary_lens_ids=("L01",),
                secondary_lens_ids=(),
                career_role_id="MASTERING_ENGINEER",
            )
        with self.assertRaises(ValidationError):
            ProfessionalRole(
                **common,
                primary_lens_ids=("L01",),
                secondary_lens_ids=(),
                runtime_handoff_ids=("H99",),
            )

    def test_constructor_rejects_duplicate_aliases_and_non_tuple_semantics(self) -> None:
        common = dict(
            id="R98",
            scope_tier="REPRESENTATIVE",
            family="Test",
            label="Test Role",
            primary_outcome="Test outcome.",
            primary_lens_ids=("L01",),
            secondary_lens_ids=(),
            key_inputs=("input",),
            key_deliverables=("output",),
            rights_economics_note="No legal or economic claim.",
            health_risk_note="No health claim.",
            handoff_summary="No handoff claim.",
        )
        with self.assertRaises(ValidationError):
            ProfessionalRole(
                **common,
                aliases=("Alias", "alias"),
                lifecycle_jobs=("test",),
            )
        with self.assertRaises(ValidationError):
            ProfessionalRole(
                **common,
                aliases=("Alias",),
                lifecycle_jobs=["test"],  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
