import unittest

from n0te import (
    CapabilityCandidate,
    N0TEableJob,
    SemanticToolProfile,
    StudioCapabilityProfile,
    ToolCapabilityBinding,
    ToolEndpoint,
    ToolIdentityError,
    ToolParameterBinding,
    ToolStateBinding,
)


def owned_candidate(candidate_id, capability, **overrides):
    values = dict(
        candidate_id=candidate_id,
        route_kind="OWNED_TOOL",
        capability=capability,
        display_name=candidate_id,
        brand=None,
        verified=True,
        compatible=True,
        evidence_ref=f"evidence:{candidate_id}",
        evidence_age_seconds=10,
        task_fit=0.90,
        editability=0.90,
        locality=1.0,
        privacy=1.0,
        latency=0.90,
        reversibility=1.0,
        cost_efficiency=1.0,
        portability=0.80,
        user_preference=0.50,
        paid=False,
    )
    values.update(overrides)
    return CapabilityCandidate(**values)


def endpoint(endpoint_id, format_kind, native_identity):
    return ToolEndpoint(
        endpoint_id=endpoint_id,
        format_kind=format_kind,
        native_identity=native_identity,
        evidence_ref=f"endpoint-evidence:{endpoint_id}",
    )


def compressor_profile():
    vst3 = endpoint("endpoint:vst3", "vst3", "vendor.product.vst3")
    au = endpoint("endpoint:au", "au", "aufx:Vndr:Comp")
    return SemanticToolProfile(
        tool_id="tool:compressor:example",
        display_name="Example Compressor",
        endpoints=(au, vst3),
        capabilities=(
            ToolCapabilityBinding(
                endpoint_id=vst3.endpoint_id,
                candidate=owned_candidate(
                    "candidate:compress:vst3",
                    "dynamics.compress",
                    display_name="Example Compressor VST3",
                    brand="Example Vendor",
                ),
            ),
            ToolCapabilityBinding(
                endpoint_id=au.endpoint_id,
                candidate=owned_candidate(
                    "candidate:compress:au",
                    "dynamics.compress",
                    display_name="Example Compressor AU",
                    brand="Example Vendor",
                    task_fit=0.88,
                ),
            ),
        ),
        parameters=(
            ToolParameterBinding(
                endpoint_id=vst3.endpoint_id,
                semantic_key="mix.wet_dry",
                native_parameter_ref="param:12",
                readable=True,
                writable=True,
                evidence_ref="parameter-evidence:vst3:mix",
            ),
            ToolParameterBinding(
                endpoint_id=au.endpoint_id,
                semantic_key="mix.wet_dry",
                native_parameter_ref="kAudioUnitParameter_WetDryMix",
                readable=True,
                writable=False,
                evidence_ref="parameter-evidence:au:mix",
            ),
        ),
        state_bindings=(
            ToolStateBinding(
                endpoint_id=vst3.endpoint_id,
                readable=True,
                writable=True,
                evidence_ref="state-evidence:vst3",
            ),
            ToolStateBinding(
                endpoint_id=au.endpoint_id,
                readable=True,
                writable=False,
                evidence_ref="state-evidence:au",
            ),
        ),
    )


class Core03ESemanticToolIdentityTests(unittest.TestCase):
    def test_one_tool_identity_spans_vst3_and_au_endpoints(self):
        tool = compressor_profile()
        self.assertEqual(tool.tool_id, "tool:compressor:example")
        self.assertEqual(
            tuple(item.endpoint_id for item in tool.endpoints),
            ("endpoint:au", "endpoint:vst3"),
        )
        self.assertEqual(
            {item.format_kind for item in tool.endpoints},
            {"AU", "VST3"},
        )
        self.assertNotEqual(
            tool.endpoint("endpoint:au").native_identity,
            tool.endpoint("endpoint:vst3").native_identity,
        )

    def test_same_semantic_parameter_can_map_to_different_native_refs(self):
        tool = compressor_profile()
        bindings = tool.parameter_bindings_for("mix.wet_dry")
        self.assertEqual(len(bindings), 2)
        refs = {item.endpoint_id: item.native_parameter_ref for item in bindings}
        self.assertEqual(refs["endpoint:vst3"], "param:12")
        self.assertEqual(refs["endpoint:au"], "kAudioUnitParameter_WetDryMix")
        au = next(item for item in bindings if item.endpoint_id == "endpoint:au")
        self.assertTrue(au.readable)
        self.assertFalse(au.writable)

    def test_state_support_remains_explicit_and_endpoint_specific(self):
        tool = compressor_profile()
        vst3 = tool.state_for_endpoint("endpoint:vst3")
        au = tool.state_for_endpoint("endpoint:au")
        self.assertTrue(vst3.readable and vst3.writable)
        self.assertTrue(au.readable)
        self.assertFalse(au.writable)
        self.assertIsNone(tool.state_for_endpoint("missing"))

    def test_explicit_owned_tool_candidates_feed_existing_studio_resolver(self):
        tool = compressor_profile()
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=tool.candidates(),
        )
        job = N0TEableJob(
            id="job:compress",
            capability="dynamics.compress",
            description="Compress a source with an owned tool",
        )
        resolution = studio.resolve(job)
        self.assertEqual(resolution.status, "RESOLVED")
        self.assertEqual(resolution.recommended.candidate.route_kind, "OWNED_TOOL")
        self.assertIn(
            resolution.recommended.candidate.candidate_id,
            {"candidate:compress:vst3", "candidate:compress:au"},
        )

    def test_endpoint_or_display_names_create_no_capability_by_themselves(self):
        tool = SemanticToolProfile(
            tool_id="tool:famous",
            display_name="Famous Magic Compressor",
            endpoints=(
                endpoint("endpoint:famous", "VST3", "FamousVendor.MagicCompressor"),
            ),
        )
        self.assertEqual(tool.candidates(), ())
        self.assertEqual(tool.candidates_for("dynamics.compress"), ())
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=tool.candidates(),
        )
        result = studio.resolve(
            N0TEableJob("job:compress", "dynamics.compress", "Compress source")
        )
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertIn("NO_CANDIDATES", result.reason_codes)

    def test_unverified_owned_tool_candidate_remains_rejected(self):
        ep = endpoint("endpoint:vst3", "VST3", "vendor.tool")
        unverified = owned_candidate(
            "candidate:unverified",
            "audio.repair",
            verified=False,
            evidence_ref=None,
            task_fit=1.0,
            user_preference=1.0,
        )
        tool = SemanticToolProfile(
            tool_id="tool:unverified",
            display_name="Installed Repair Tool",
            endpoints=(ep,),
            capabilities=(ToolCapabilityBinding(ep.endpoint_id, unverified),),
        )
        studio = StudioCapabilityProfile.build(
            environment_id="studio", candidates=tool.candidates()
        )
        result = studio.resolve(
            N0TEableJob("job:repair", "audio.repair", "Repair audio")
        )
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertIn("UNVERIFIED", result.reason_codes)

    def test_non_owned_tool_candidate_cannot_enter_tool_profile(self):
        ep = endpoint("endpoint:vst3", "VST3", "vendor.tool")
        foreign = CapabilityCandidate(
            candidate_id="native",
            route_kind="HOST_NATIVE",
            capability="dynamics.compress",
            display_name="Host Compressor",
            brand="Host",
            verified=True,
            compatible=True,
            evidence_ref="host:evidence",
            evidence_age_seconds=1,
            task_fit=0.9,
            editability=0.9,
            locality=1.0,
            privacy=1.0,
            latency=1.0,
            reversibility=1.0,
            cost_efficiency=1.0,
            portability=0.5,
        )
        with self.assertRaises(ToolIdentityError):
            ToolCapabilityBinding(ep.endpoint_id, foreign)

    def test_unknown_endpoint_references_are_rejected(self):
        ep = endpoint("endpoint:vst3", "VST3", "vendor.tool")
        with self.assertRaises(ToolIdentityError):
            SemanticToolProfile(
                tool_id="tool:x",
                display_name="X",
                endpoints=(ep,),
                capabilities=(
                    ToolCapabilityBinding(
                        "endpoint:missing",
                        owned_candidate("candidate:x", "cap.x"),
                    ),
                ),
            )
        with self.assertRaises(ToolIdentityError):
            SemanticToolProfile(
                tool_id="tool:x",
                display_name="X",
                endpoints=(ep,),
                parameters=(
                    ToolParameterBinding(
                        "endpoint:missing",
                        "mix",
                        "native:mix",
                        True,
                        True,
                        "parameter:evidence",
                    ),
                ),
            )
        with self.assertRaises(ToolIdentityError):
            SemanticToolProfile(
                tool_id="tool:x",
                display_name="X",
                endpoints=(ep,),
                state_bindings=(
                    ToolStateBinding(
                        "endpoint:missing", True, True, "state:evidence"
                    ),
                ),
            )

    def test_duplicate_endpoint_candidate_parameter_and_state_identity_are_rejected(self):
        ep = endpoint("endpoint:vst3", "VST3", "vendor.tool")
        with self.assertRaises(ToolIdentityError):
            SemanticToolProfile("tool:x", "X", (ep, ep))
        other_ep = endpoint("endpoint:other", "VST3", "vendor.other")
        duplicate_candidate = owned_candidate("candidate:x", "cap.x")
        with self.assertRaises(ToolIdentityError):
            SemanticToolProfile(
                "tool:x",
                "X",
                (ep, other_ep),
                capabilities=(
                    ToolCapabilityBinding(ep.endpoint_id, duplicate_candidate),
                    ToolCapabilityBinding(other_ep.endpoint_id, duplicate_candidate),
                ),
            )
        duplicate_param = ToolParameterBinding(
            ep.endpoint_id, "mix", "p1", True, False, "parameter:evidence"
        )
        with self.assertRaises(ToolIdentityError):
            SemanticToolProfile(
                "tool:x",
                "X",
                (ep,),
                parameters=(
                    duplicate_param,
                    ToolParameterBinding(
                        ep.endpoint_id,
                        "mix",
                        "p2",
                        True,
                        True,
                        "parameter:evidence:2",
                    ),
                ),
            )
        with self.assertRaises(ToolIdentityError):
            SemanticToolProfile(
                "tool:x",
                "X",
                (ep,),
                state_bindings=(
                    ToolStateBinding(ep.endpoint_id, True, False, "state:1"),
                    ToolStateBinding(ep.endpoint_id, True, True, "state:2"),
                ),
            )

    def test_parameter_and_state_flags_require_real_boolean_and_real_support(self):
        ep = endpoint("endpoint:vst3", "VST3", "vendor.tool")
        with self.assertRaises(TypeError):
            ToolParameterBinding(ep.endpoint_id, "mix", "p", "true", False, "e")
        with self.assertRaises(ToolIdentityError):
            ToolParameterBinding(ep.endpoint_id, "mix", "p", False, False, "e")
        with self.assertRaises(TypeError):
            ToolStateBinding(ep.endpoint_id, True, "false", "e")
        with self.assertRaises(ToolIdentityError):
            ToolStateBinding(ep.endpoint_id, False, False, "e")

    def test_format_validation_is_truthful_and_does_not_expand_hosting_claims(self):
        self.assertEqual(endpoint("v", "vst3", "id").format_kind, "VST3")
        self.assertEqual(endpoint("a", "aax", "id2").format_kind, "AAX")
        self.assertEqual(endpoint("l", "lv2", "id3").format_kind, "LV2")
        with self.assertRaises(ToolIdentityError):
            endpoint("x", "MAGIC_HOSTABLE_FORMAT", "id")

    def test_reads_are_pure_deterministic_and_input_order_normalized(self):
        original = compressor_profile()
        reversed_profile = SemanticToolProfile(
            tool_id=original.tool_id,
            display_name=original.display_name,
            endpoints=tuple(reversed(original.endpoints)),
            capabilities=tuple(reversed(original.capabilities)),
            parameters=tuple(reversed(original.parameters)),
            state_bindings=tuple(reversed(original.state_bindings)),
        )
        self.assertEqual(original, reversed_profile)
        before = original
        self.assertEqual(original.candidates(), original.candidates())
        self.assertEqual(
            original.parameter_bindings_for("mix.wet_dry"),
            original.parameter_bindings_for("mix.wet_dry"),
        )
        self.assertEqual(original, before)


if __name__ == "__main__":
    unittest.main()
