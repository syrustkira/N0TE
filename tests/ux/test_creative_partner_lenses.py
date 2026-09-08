from __future__ import annotations

import unittest

from n0te.creative_partner_lenses import (
    CREATIVE_LENSES,
    REQUIRED_BASE_LENS_IDS,
    CallTheRoomSynthesis,
    CreativeLensDefinition,
    CreativeLensInvocation,
    CreativePartnerLensError,
    MixedLensContextError,
    PerspectiveFinding,
    StaleLensContextError,
    call_the_room,
    get_creative_lens,
)
from n0te.lineage import ValidationError
from n0te.professional_roles import ProfessionalRole
from n0te.relevance_broker import RelevanceContextBinding


EXPECTED_LENSES = (
    "PRODUCER_COPRODUCER",
    "SONGWRITER_COMPOSER",
    "ARRANGER",
    "ENGINEER",
    "TEACHER",
    "A_AND_R_FINISH_ADVISOR",
    "CREATIVE_DIRECTOR",
    "FIRST_LISTEN_AUDIENCE",
    "CHALLENGER",
    "PERFORMANCE_DIRECTOR",
    "RIGHTS_LINEAGE_STEWARD",
    "BUSINESS_MANAGER_LABEL_OPERATOR",
    "FUTURE_ARTIST_ARCHIVIST",
)


def context(*, song_id: str = "song-1", version_id: str = "version-1") -> RelevanceContextBinding:
    return RelevanceContextBinding(
        profile_id="profile-1",
        artist_id="artist-1",
        song_id=song_id,
        version_id=version_id,
        session_id="session-1",
        job_id="job-1",
        purpose_key="finish-current-song",
    )


def finding(
    ctx: RelevanceContextBinding,
    lens_id: str,
    proposition_key: str,
    stance: str,
    *,
    evidence_basis: str = "CANONICAL_EVIDENCE",
    source_refs: tuple[str, ...] = ("evidence:1",),
    audience_basis: str = "NOT_APPLICABLE",
    audience_evidence_ref: str | None = None,
) -> PerspectiveFinding:
    return PerspectiveFinding(
        lens_id=lens_id,
        context_fingerprint=ctx.fingerprint,
        proposition_key=proposition_key,
        stance=stance,
        claim=f"{lens_id} claim about {proposition_key}",
        rationale=f"{lens_id} rationale about {proposition_key}",
        source_refs=source_refs,
        evidence_basis=evidence_basis,
        audience_basis=audience_basis,
        audience_evidence_ref=audience_evidence_ref,
    )


class CreativePartnerLensRegistryTests(unittest.TestCase):
    def test_required_base_perspectives_are_present_and_materially_distinct(self) -> None:
        self.assertEqual(REQUIRED_BASE_LENS_IDS, EXPECTED_LENSES)
        self.assertEqual(tuple(CREATIVE_LENSES), EXPECTED_LENSES)
        signatures = [lens.policy_signature for lens in CREATIVE_LENSES.values()]
        self.assertEqual(len(signatures), len(set(signatures)))
        for lens in CREATIVE_LENSES.values():
            with self.subTest(lens=lens.lens_id):
                self.assertGreaterEqual(len(lens.diagnostic_questions), 3)
                self.assertGreaterEqual(len(lens.evidence_emphasis), 3)
                self.assertGreaterEqual(len(lens.tradeoffs), 3)

    def test_registry_is_read_only_and_lookup_is_normalized(self) -> None:
        producer = CREATIVE_LENSES["PRODUCER_COPRODUCER"]
        self.assertIs(get_creative_lens("producer coproducer"), producer)
        self.assertIs(get_creative_lens("producer-coproducer"), producer)
        with self.assertRaises(TypeError):
            CREATIVE_LENSES["PRODUCER_COPRODUCER"] = CREATIVE_LENSES["ENGINEER"]  # type: ignore[index]
        with self.assertRaises(CreativePartnerLensError):
            get_creative_lens("Imaginary Oracle")

    def test_lens_is_not_role_agent_memory_or_authority_domain(self) -> None:
        for lens in CREATIVE_LENSES.values():
            with self.subTest(lens=lens.lens_id):
                self.assertFalse(lens.grants_identity_authority)
                self.assertFalse(lens.grants_memory_authority)
                self.assertFalse(lens.grants_mutation_authority)
                self.assertFalse(lens.grants_execution_authority)
                self.assertFalse(lens.grants_external_action_authority)
                self.assertFalse(lens.grants_spend_authority)
                self.assertFalse(lens.grants_publication_authority)
                self.assertFalse(lens.grants_rights_authority)
                self.assertFalse(lens.grants_any_authority)
                for role in lens.linked_professional_roles():
                    self.assertIsInstance(role, ProfessionalRole)
                    self.assertFalse(role.grants_any_authority)
                    self.assertIsNot(lens, role)

        self.assertEqual(
            get_creative_lens("ENGINEER").linked_professional_role_ids,
            ("R03", "R04"),
        )
        self.assertEqual(
            get_creative_lens("BUSINESS_MANAGER_LABEL_OPERATOR").linked_professional_role_ids,
            ("R07", "R19"),
        )

    def test_authority_fields_cannot_be_injected(self) -> None:
        producer = get_creative_lens("PRODUCER_COPRODUCER")
        with self.assertRaises(TypeError):
            CreativeLensDefinition(
                lens_id="TEST",
                label="Test",
                purpose="Test purpose",
                linked_professional_role_ids=(),
                diagnostic_questions=("Question one?",),
                evidence_emphasis=("Evidence",),
                tradeoffs=("A versus B",),
                explanation_posture="Bounded",
                grants_mutation_authority=True,  # type: ignore[call-arg]
            )
        self.assertFalse(producer.grants_any_authority)

    def test_linked_roles_fail_closed_on_shape_identity_and_normalized_duplicates(self) -> None:
        with self.assertRaises(CreativePartnerLensError):
            CreativeLensDefinition(
                lens_id="TEST",
                label="Test",
                purpose="Test purpose",
                linked_professional_role_ids=["R02"],  # type: ignore[arg-type]
                diagnostic_questions=("Question one?",),
                evidence_emphasis=("Evidence",),
                tradeoffs=("A versus B",),
                explanation_posture="Bounded",
            )
        with self.assertRaises(CreativePartnerLensError):
            CreativeLensDefinition(
                lens_id="TEST",
                label="Test",
                purpose="Test purpose",
                linked_professional_role_ids=("r02", "R02"),
                diagnostic_questions=("Question one?",),
                evidence_emphasis=("Evidence",),
                tradeoffs=("A versus B",),
                explanation_posture="Bounded",
            )
        with self.assertRaises(ValidationError):
            CreativeLensDefinition(
                lens_id="TEST",
                label="Test",
                purpose="Test purpose",
                linked_professional_role_ids=("R99",),
                diagnostic_questions=("Question one?",),
                evidence_emphasis=("Evidence",),
                tradeoffs=("A versus B",),
                explanation_posture="Bounded",
            )


class CreativePartnerLensBindingTests(unittest.TestCase):
    def test_switching_perspective_keeps_exact_canonical_context(self) -> None:
        ctx = context()
        producer = CreativeLensInvocation.bind("PRODUCER_COPRODUCER", ctx)
        engineer = CreativeLensInvocation.bind("ENGINEER", ctx)
        self.assertEqual(producer.context_fingerprint, ctx.fingerprint)
        self.assertEqual(engineer.context_fingerprint, ctx.fingerprint)
        self.assertNotEqual(producer.lens_id, engineer.lens_id)
        self.assertFalse(producer.grants_any_authority)
        self.assertFalse(producer.mutates_canonical_truth)
        self.assertFalse(producer.owns_memory)

    def test_invocation_context_type_and_policy_version_fail_closed(self) -> None:
        with self.assertRaises(TypeError):
            CreativeLensInvocation.bind("ENGINEER", object())  # type: ignore[arg-type]
        with self.assertRaises(CreativePartnerLensError):
            CreativeLensInvocation(
                lens_id="ENGINEER",
                context_fingerprint="fingerprint",
                schema_version=999,
            )

    def test_finding_is_read_only_reasoning_not_decision_or_authority(self) -> None:
        item = finding(context(), "ENGINEER", "translation", "SUPPORT")
        self.assertFalse(item.grants_any_authority)
        self.assertFalse(item.mutates_canonical_truth)
        self.assertFalse(item.records_artist_decision)
        with self.assertRaises(TypeError):
            PerspectiveFinding(
                lens_id="ENGINEER",
                context_fingerprint=context().fingerprint,
                proposition_key="translation",
                stance="SUPPORT",
                claim="Claim",
                rationale="Rationale",
                source_refs=("evidence:1",),
                evidence_basis="CANONICAL_EVIDENCE",
                grants_any_authority=True,  # type: ignore[call-arg]
            )

    def test_insufficient_stance_and_basis_are_one_fail_closed_state(self) -> None:
        ctx = context()
        with self.assertRaises(CreativePartnerLensError):
            finding(
                ctx,
                "ENGINEER",
                "translation",
                "SUPPORT",
                evidence_basis="INSUFFICIENT",
                source_refs=(),
            )
        with self.assertRaises(CreativePartnerLensError):
            finding(
                ctx,
                "ENGINEER",
                "translation",
                "INSUFFICIENT",
                evidence_basis="CANONICAL_EVIDENCE",
            )


class AudienceTruthTests(unittest.TestCase):
    def test_simulated_first_listen_is_explicit_bounded_inference(self) -> None:
        ctx = context()
        item = finding(
            ctx,
            "FIRST_LISTEN_AUDIENCE",
            "hook-immediacy",
            "CHALLENGE",
            evidence_basis="BOUNDED_INFERENCE",
            source_refs=("version:current",),
            audience_basis="SIMULATED",
        )
        self.assertEqual(item.audience_basis, "SIMULATED")
        self.assertEqual(item.evidence_basis, "BOUNDED_INFERENCE")
        self.assertIsNone(item.audience_evidence_ref)

    def test_first_listen_can_truthfully_report_insufficient_simulated_basis(self) -> None:
        item = finding(
            context(),
            "FIRST_LISTEN_AUDIENCE",
            "hook-immediacy",
            "INSUFFICIENT",
            evidence_basis="INSUFFICIENT",
            source_refs=(),
            audience_basis="SIMULATED",
        )
        self.assertEqual(item.stance, "INSUFFICIENT")
        self.assertEqual(item.audience_basis, "SIMULATED")

    def test_simulation_cannot_impersonate_observed_listener_evidence(self) -> None:
        ctx = context()
        with self.assertRaises(CreativePartnerLensError):
            finding(
                ctx,
                "FIRST_LISTEN_AUDIENCE",
                "hook-immediacy",
                "SUPPORT",
                evidence_basis="CANONICAL_EVIDENCE",
                audience_basis="SIMULATED",
            )
        with self.assertRaises(CreativePartnerLensError):
            finding(
                ctx,
                "FIRST_LISTEN_AUDIENCE",
                "hook-immediacy",
                "SUPPORT",
                evidence_basis="CANONICAL_EVIDENCE",
                audience_basis="OBSERVED",
            )
        with self.assertRaises(CreativePartnerLensError):
            finding(
                ctx,
                "ENGINEER",
                "hook-immediacy",
                "SUPPORT",
                evidence_basis="BOUNDED_INFERENCE",
                audience_basis="SIMULATED",
            )

    def test_observed_audience_requires_canonical_evidence_and_exact_ref(self) -> None:
        ctx = context()
        item = finding(
            ctx,
            "FIRST_LISTEN_AUDIENCE",
            "hook-immediacy",
            "SUPPORT",
            evidence_basis="CANONICAL_EVIDENCE",
            source_refs=("audience-snapshot:42",),
            audience_basis="OBSERVED",
            audience_evidence_ref="audience-snapshot:42",
        )
        self.assertEqual(item.audience_basis, "OBSERVED")
        self.assertEqual(item.audience_evidence_ref, "audience-snapshot:42")
        with self.assertRaises(CreativePartnerLensError):
            finding(
                ctx,
                "FIRST_LISTEN_AUDIENCE",
                "hook-immediacy",
                "SUPPORT",
                evidence_basis="CANONICAL_EVIDENCE",
                source_refs=("unrelated:1",),
                audience_basis="OBSERVED",
                audience_evidence_ref="audience-snapshot:42",
            )

    def test_first_listen_must_always_label_audience_truth_class(self) -> None:
        with self.assertRaises(CreativePartnerLensError):
            finding(
                context(),
                "FIRST_LISTEN_AUDIENCE",
                "hook-immediacy",
                "SUPPORT",
            )


class CallTheRoomTests(unittest.TestCase):
    def test_same_direction_evidence_bound_views_are_descriptive_agreement(self) -> None:
        ctx = context()
        result = call_the_room(
            ctx,
            (
                finding(ctx, "PRODUCER_COPRODUCER", "chorus-arrives", "SUPPORT", source_refs=("song-map:1",)),
                finding(ctx, "ARRANGER", "chorus-arrives", "SUPPORT", source_refs=("arrangement:1",)),
            ),
        )
        self.assertIsInstance(result, CallTheRoomSynthesis)
        self.assertEqual(result.topics[0].status, "AGREEMENT")
        self.assertEqual(
            result.topics[0].source_refs,
            ("arrangement:1", "song-map:1"),
        )
        self.assertFalse(result.grants_any_authority)
        self.assertFalse(result.mutates_canonical_truth)
        self.assertFalse(result.records_artist_decision)
        self.assertFalse(result.creates_memory)
        self.assertFalse(hasattr(result, "winner"))
        self.assertFalse(hasattr(result.topics[0], "score"))

    def test_material_opposition_remains_disagreement_not_average(self) -> None:
        ctx = context()
        result = call_the_room(
            ctx,
            (
                finding(ctx, "PRODUCER_COPRODUCER", "bridge-needed", "SUPPORT"),
                finding(ctx, "SONGWRITER_COMPOSER", "bridge-needed", "CHALLENGE", source_refs=("composition:1",)),
                finding(ctx, "ENGINEER", "bridge-needed", "NEUTRAL", source_refs=("engineering:1",)),
            ),
        )
        topic = result.topics[0]
        self.assertEqual(topic.status, "DISAGREEMENT")
        self.assertEqual(topic.support_lens_ids, ("PRODUCER_COPRODUCER",))
        self.assertEqual(topic.challenge_lens_ids, ("SONGWRITER_COMPOSER",))
        self.assertEqual(topic.neutral_lens_ids, ("ENGINEER",))

    def test_lone_dissent_survives_as_unique_concern(self) -> None:
        ctx = context()
        result = call_the_room(
            ctx,
            (
                finding(ctx, "RIGHTS_LINEAGE_STEWARD", "sample-clearance", "CHALLENGE", source_refs=("rights:gap",)),
                finding(
                    ctx,
                    "CREATIVE_DIRECTOR",
                    "sample-clearance",
                    "INSUFFICIENT",
                    evidence_basis="INSUFFICIENT",
                    source_refs=(),
                ),
            ),
        )
        topic = result.topics[0]
        self.assertEqual(topic.status, "UNIQUE_CONCERN")
        self.assertEqual(topic.challenge_lens_ids, ("RIGHTS_LINEAGE_STEWARD",))
        self.assertEqual(topic.insufficient_lens_ids, ("CREATIVE_DIRECTOR",))
        self.assertEqual(result.unique_concerns, (topic,))

    def test_insufficient_view_prevents_false_consensus(self) -> None:
        ctx = context()
        result = call_the_room(
            ctx,
            (
                finding(ctx, "PRODUCER_COPRODUCER", "mix-ready", "SUPPORT"),
                finding(ctx, "ENGINEER", "mix-ready", "SUPPORT", source_refs=("engineering:1",)),
                finding(
                    ctx,
                    "A_AND_R_FINISH_ADVISOR",
                    "mix-ready",
                    "INSUFFICIENT",
                    evidence_basis="INSUFFICIENT",
                    source_refs=(),
                ),
            ),
        )
        self.assertEqual(result.topics[0].status, "UNRESOLVED")
        self.assertEqual(result.agreement_topics, ())

    def test_all_insufficient_remains_insufficient(self) -> None:
        ctx = context()
        result = call_the_room(
            ctx,
            (
                finding(
                    ctx,
                    "ENGINEER",
                    "translation",
                    "INSUFFICIENT",
                    evidence_basis="INSUFFICIENT",
                    source_refs=(),
                ),
                finding(
                    ctx,
                    "PRODUCER_COPRODUCER",
                    "translation",
                    "INSUFFICIENT",
                    evidence_basis="INSUFFICIENT",
                    source_refs=(),
                ),
            ),
        )
        self.assertEqual(result.topics[0].status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result.unresolved_topics, result.topics)

    def test_single_support_is_unresolved_not_consensus(self) -> None:
        ctx = context()
        result = call_the_room(
            ctx,
            (finding(ctx, "CHALLENGER", "strip-verse", "SUPPORT"),),
        )
        self.assertEqual(result.topics[0].status, "UNRESOLVED")

    def test_observed_audience_ref_is_preserved_separately_from_simulation(self) -> None:
        ctx = context()
        result = call_the_room(
            ctx,
            (
                finding(
                    ctx,
                    "FIRST_LISTEN_AUDIENCE",
                    "hook-immediacy",
                    "SUPPORT",
                    evidence_basis="CANONICAL_EVIDENCE",
                    source_refs=("audience-snapshot:42",),
                    audience_basis="OBSERVED",
                    audience_evidence_ref="audience-snapshot:42",
                ),
                finding(ctx, "A_AND_R_FINISH_ADVISOR", "hook-immediacy", "SUPPORT", source_refs=("catalog:1",)),
            ),
        )
        self.assertEqual(result.topics[0].status, "AGREEMENT")
        self.assertEqual(
            result.topics[0].observed_audience_refs,
            ("audience-snapshot:42",),
        )

    def test_mixed_context_findings_fail_closed(self) -> None:
        current = context()
        other = context(song_id="song-2", version_id="version-2")
        with self.assertRaises(MixedLensContextError):
            call_the_room(
                current,
                (
                    finding(current, "ENGINEER", "translation", "SUPPORT"),
                    finding(other, "PRODUCER_COPRODUCER", "translation", "SUPPORT"),
                ),
            )

    def test_stale_context_findings_fail_closed_even_when_internally_consistent(self) -> None:
        old = context(version_id="version-1")
        current = context(version_id="version-2")
        with self.assertRaises(StaleLensContextError):
            call_the_room(
                current,
                (
                    finding(old, "ENGINEER", "translation", "SUPPORT"),
                    finding(old, "PRODUCER_COPRODUCER", "translation", "SUPPORT"),
                ),
            )

    def test_one_lens_cannot_double_vote_on_same_proposition(self) -> None:
        ctx = context()
        with self.assertRaises(CreativePartnerLensError):
            call_the_room(
                ctx,
                (
                    finding(ctx, "ENGINEER", "translation", "SUPPORT", source_refs=("evidence:1",)),
                    finding(ctx, "ENGINEER", "translation", "CHALLENGE", source_refs=("evidence:2",)),
                ),
            )

    def test_empty_room_and_wrong_types_fail_closed(self) -> None:
        with self.assertRaises(CreativePartnerLensError):
            call_the_room(context(), ())
        with self.assertRaises(TypeError):
            call_the_room(object(), ())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            call_the_room(context(), (object(),))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
