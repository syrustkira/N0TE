from __future__ import annotations

import sqlite3

import pytest

from n0te.capabilities import CapabilityCandidate
from n0te.memory import HeadquartersMemory
from n0te.tool_inventory import ToolInventoryError
from n0te.tools import (
    SemanticToolProfile,
    ToolCapabilityBinding,
    ToolEndpoint,
    ToolParameterBinding,
    ToolStateBinding,
)


def _candidate(candidate_id: str, display_name: str) -> CapabilityCandidate:
    return CapabilityCandidate(
        candidate_id=candidate_id,
        route_kind="OWNED_TOOL",
        capability="dynamics.compress",
        display_name=display_name,
        brand="Test Vendor",
        verified=True,
        compatible=True,
        evidence_ref="artist:declared:capability",
        evidence_age_seconds=0,
        task_fit=0.9,
        editability=0.9,
        locality=1.0,
        privacy=1.0,
        latency=0.9,
        reversibility=1.0,
        cost_efficiency=1.0,
        portability=0.8,
        user_preference=0.5,
        paid=False,
    )


def _profile(
    tool_id: str = "tool:test-compressor",
    *,
    endpoint_id: str = "endpoint:test:vst3",
    native_identity: str = "TestVendor.TestCompressor",
    candidate_id: str = "candidate:test:compress",
    display_name: str = "Test Compressor",
) -> SemanticToolProfile:
    endpoint = ToolEndpoint(
        endpoint_id=endpoint_id,
        format_kind="VST3",
        native_identity=native_identity,
        evidence_ref="artist:declared:endpoint",
    )
    return SemanticToolProfile(
        tool_id=tool_id,
        display_name=display_name,
        endpoints=(endpoint,),
        capabilities=(
            ToolCapabilityBinding(
                endpoint_id=endpoint.endpoint_id,
                candidate=_candidate(candidate_id, display_name),
            ),
        ),
        parameters=(
            ToolParameterBinding(
                endpoint_id=endpoint.endpoint_id,
                semantic_key="mix.wet_dry",
                native_parameter_ref="param:12",
                readable=True,
                writable=True,
                evidence_ref="artist:declared:parameter",
            ),
        ),
        state_bindings=(
            ToolStateBinding(
                endpoint_id=endpoint.endpoint_id,
                readable=True,
                writable=True,
                evidence_ref="artist:declared:state",
            ),
        ),
    )


def test_semantic_tool_inventory_round_trips_through_headquarters_relaunch(tmp_path):
    root = tmp_path / "hq"
    hq = HeadquartersMemory.create(root, "Tool Artist")
    profile_id = hq.store.profile_id
    try:
        revision = hq.tool_inventory.register(
            _profile(),
            source_kind="ARTIST_DECLARED",
            source_ref="artist:setup:tool-1",
        )
        assert revision.state == "ACTIVE"
        assert revision.source_kind == "ARTIST_DECLARED"
        assert hq.tool_inventory.active_profiles() == (_profile(),)
        exact = hq.tool_inventory.by_native_identity(
            "vst3", "TestVendor.TestCompressor"
        )
        assert exact is not None
        assert exact[0].tool_id == "tool:test-compressor"
        assert exact[1].endpoint_id == "endpoint:test:vst3"
        assert tuple(item.candidate_id for item in hq.tool_inventory.candidates()) == (
            "candidate:test:compress",
        )
    finally:
        hq.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        state = reopened.tool_inventory.state("tool:test-compressor")
        assert state.state == "ACTIVE"
        assert state.profile == _profile()
        assert len(reopened.tool_inventory.history("tool:test-compressor")) == 1
    finally:
        reopened.close()


def test_correction_retirement_and_reactivation_are_append_only_revisions(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Tool Artist")
    try:
        original = _profile()
        corrected = _profile(display_name="Test Compressor Corrected")
        hq.tool_inventory.register(
            original,
            source_kind="EXPLICIT_IMPORT",
            source_ref="explicit:manifest:v1",
        )
        correction = hq.tool_inventory.correct(
            corrected,
            source_ref="artist:correction:1",
            reason="Correct the semantic display label after checking the product.",
        )
        assert correction.source_kind == "ARTIST_CORRECTION"
        assert hq.tool_inventory.state(original.tool_id).profile == corrected

        retired = hq.tool_inventory.retire(
            original.tool_id,
            source_ref="artist:retire:1",
            reason="No longer part of the owned production setup.",
        )
        assert retired.state == "RETIRED"
        assert hq.tool_inventory.active_profiles() == ()
        assert hq.tool_inventory.by_native_identity(
            "VST3", "TestVendor.TestCompressor"
        ) is None

        reactivated = hq.tool_inventory.correct(
            corrected,
            source_ref="artist:reactivate:1",
            reason="Tool is part of the setup again after entitlement was restored.",
        )
        assert reactivated.state == "ACTIVE"
        history = hq.tool_inventory.history(original.tool_id)
        assert [item.state for item in history] == ["ACTIVE", "ACTIVE", "RETIRED", "ACTIVE"]
        assert [item.sequence for item in history] == sorted(item.sequence for item in history)
    finally:
        hq.close()


def test_daw_and_discovery_observations_cannot_create_semantic_ownership(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Tool Artist")
    try:
        for source_kind in ("DAW_OBSERVED", "DISCOVERED_PACKAGE", "FILESYSTEM_DISCOVERY"):
            with pytest.raises(ToolInventoryError, match="unsupported"):
                hq.tool_inventory.register(
                    _profile(),
                    source_kind=source_kind,
                    source_ref="observation:not-authority",
                )
        assert hq.tool_inventory.active_profiles() == ()
    finally:
        hq.close()


def test_active_semantic_tools_cannot_claim_the_same_endpoint_or_candidate_identity(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Tool Artist")
    try:
        first = _profile()
        hq.tool_inventory.register(
            first,
            source_kind="ARTIST_DECLARED",
            source_ref="artist:first",
        )
        with pytest.raises(ToolInventoryError, match="endpoint_id collision"):
            hq.tool_inventory.register(
                _profile(
                    "tool:other",
                    native_identity="OtherVendor.OtherProduct",
                    candidate_id="candidate:other",
                ),
                source_kind="ARTIST_DECLARED",
                source_ref="artist:other",
            )
        with pytest.raises(ToolInventoryError, match="format/native"):
            hq.tool_inventory.register(
                _profile(
                    "tool:other-native",
                    endpoint_id="endpoint:other-native",
                    candidate_id="candidate:other-native",
                ),
                source_kind="ARTIST_DECLARED",
                source_ref="artist:other-native",
            )
        with pytest.raises(ToolInventoryError, match="candidate collision"):
            hq.tool_inventory.register(
                _profile(
                    "tool:other-candidate",
                    endpoint_id="endpoint:other-candidate",
                    native_identity="OtherVendor.OtherCandidateProduct",
                ),
                source_kind="ARTIST_DECLARED",
                source_ref="artist:other-candidate",
            )
    finally:
        hq.close()


def test_retired_tool_releases_endpoint_identity_for_explicit_new_registration(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Tool Artist")
    try:
        first = _profile()
        hq.tool_inventory.register(
            first,
            source_kind="ARTIST_DECLARED",
            source_ref="artist:first",
        )
        hq.tool_inventory.retire(
            first.tool_id,
            source_ref="artist:retire",
            reason="The old semantic identity was retired after verification.",
        )
        replacement = _profile(
            "tool:replacement",
            endpoint_id="endpoint:replacement",
            candidate_id="candidate:replacement",
        )
        hq.tool_inventory.register(
            replacement,
            source_kind="ARTIST_DECLARED",
            source_ref="artist:replacement",
        )
        exact = hq.tool_inventory.by_native_identity(
            "VST3", "TestVendor.TestCompressor"
        )
        assert exact is not None and exact[0].tool_id == "tool:replacement"
    finally:
        hq.close()


def test_inventory_revisions_and_activity_are_immutable_and_auditable(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Tool Artist")
    try:
        revision = hq.tool_inventory.register(
            _profile(),
            source_kind="ARTIST_DECLARED",
            source_ref="artist:inventory-entry",
        )
        events = [
            event
            for event in hq.activity.for_profile()
            if event.object_type == "SEMANTIC_TOOL_REVISION" and event.object_id == revision.id
        ]
        assert len(events) == 1
        assert events[0].event_type == "TOOL_INVENTORY_ACTIVE"
        assert events[0].payload == {
            "tool_id": "tool:test-compressor",
            "source_kind": "ARTIST_DECLARED",
            "state": "ACTIVE",
        }

        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            hq.store._conn.execute(
                "UPDATE tool_inventory_revisions SET source_ref='tampered' WHERE id=?",
                (revision.id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            hq.store._conn.execute(
                "DELETE FROM tool_inventory_revisions WHERE id=?",
                (revision.id,),
            )
        assert hq.tool_inventory.state("tool:test-compressor").latest_revision.id == revision.id
    finally:
        hq.close()


def test_registration_and_correction_api_require_explicit_revision_semantics(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Tool Artist")
    try:
        profile = _profile()
        with pytest.raises(ToolInventoryError, match="correct"):
            hq.tool_inventory.register(
                profile,
                source_kind="ARTIST_CORRECTION",
                source_ref="artist:wrong-api",
            )
        hq.tool_inventory.register(
            profile,
            source_kind="ARTIST_DECLARED",
            source_ref="artist:initial",
        )
        with pytest.raises(ToolInventoryError, match="already exists"):
            hq.tool_inventory.register(
                profile,
                source_kind="ARTIST_DECLARED",
                source_ref="artist:duplicate",
            )
        with pytest.raises(ToolInventoryError, match="reason"):
            hq.tool_inventory.correct(
                profile,
                source_ref="artist:correction",
                reason="",
            )
    finally:
        hq.close()
