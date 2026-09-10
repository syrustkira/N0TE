from __future__ import annotations

from dataclasses import dataclass

from n0te import HeadquartersMemory
from n0te.capabilities import CapabilityCandidate
from n0te.creative_suggestions import CreativeSuggestion
from n0te.creative_suggestions_shell import (
    _active_tool_context_markup,
    _result_markup,
    _semantic_tool_context_markup,
)
from n0te.hosts import HostRuntimeIdentity
from n0te.shadow import ShadowEventInput
from n0te.tools import SemanticToolProfile, ToolCapabilityBinding, ToolEndpoint


@dataclass
class _Runtime:
    headquarters: HeadquartersMemory


class _Shell:
    def __init__(self, headquarters: HeadquartersMemory):
        self.runtime = _Runtime(headquarters)

    def _new_action(self, kind: str, value: str | None = None) -> str:
        return "test-action"

    @staticmethod
    def _hidden(token: str) -> str:
        return f'<input type="hidden" value="{token}">'


def _runtime() -> HostRuntimeIdentity:
    return HostRuntimeIdentity.from_runtime_labels(
        host_family="ABLETON_LIVE",
        version="12.3",
        edition="Suite",
        os_name="Darwin",
        machine="arm64",
    )


def _record_tools(headquarters, song_id, *, names, location="ableton-test:set-a"):
    workspace = headquarters.workspaces.create(
        song_id,
        runtime=_runtime(),
        location_ref=location,
        display_name="Ableton Live Test",
    )
    state = headquarters.workspaces.state(workspace.id)
    events = [
        ShadowEventInput("TRACK", "track:0", "name", "SET", "Lead", "test:track"),
        ShadowEventInput("TRACK", "track:0", "device_count", "SET", len(names), "test:chain"),
    ]
    for index, name in enumerate(names):
        ref = f"device:track:0:{index}"
        events.extend((
            ShadowEventInput("DEVICE_PLUGIN", ref, "track_ref", "SET", "track:0", f"test:device:{index}"),
            ShadowEventInput("DEVICE_PLUGIN", ref, "index", "SET", index, f"test:device:{index}"),
            ShadowEventInput("DEVICE_PLUGIN", ref, "name", "SET", name, f"test:device:{index}"),
        ))
    headquarters.shadow.record_batch(
        workspace.id,
        workspace_observation_id=state.current_observation.id,
        host_runtime_fingerprint=state.current_observation.host_runtime_fingerprint,
        coverage="FULL",
        actor="EXTERNAL",
        evidence_ref="test:shadow",
        verified=True,
        events=tuple(events),
    )
    return workspace


def _semantic_tool(
    tool_id: str = "tool:test-compressor",
    *,
    display_name: str = "Test Compressor",
    native_identity: str = "TestVendor.TestCompressor",
    capability: str = "dynamics.compress",
) -> SemanticToolProfile:
    endpoint = ToolEndpoint(
        endpoint_id=f"endpoint:{tool_id}",
        format_kind="VST3",
        native_identity=native_identity,
        evidence_ref="artist:test:endpoint",
    )
    candidate = CapabilityCandidate(
        candidate_id=f"candidate:{tool_id}",
        route_kind="OWNED_TOOL",
        capability=capability,
        display_name=display_name,
        brand="Test Vendor",
        verified=True,
        compatible=True,
        evidence_ref="artist:test:capability",
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
    return SemanticToolProfile(
        tool_id=tool_id,
        display_name=display_name,
        endpoints=(endpoint,),
        capabilities=(
            ToolCapabilityBinding(
                endpoint_id=endpoint.endpoint_id,
                candidate=candidate,
            ),
        ),
    )


def _register_semantic_tool(headquarters, profile: SemanticToolProfile) -> None:
    headquarters.tool_inventory.register(
        profile,
        source_kind="ARTIST_DECLARED",
        source_ref=f"artist:test:{profile.tool_id}",
    )


def _suggestion(song_id: str, *, dimension: str) -> CreativeSuggestion:
    return CreativeSuggestion(
        semantic_key="test:idea",
        song_id=song_id,
        session_id=None,
        distance="ADJACENT",
        dimension=dimension,
        title="Try one bounded move",
        prompt="Keep the part fixed and audition one change.",
        distance_explanation="One bounded experiment.",
        song_title="Tool Context Song",
        session_objective=None,
    )


def test_active_tool_context_surfaces_verified_daw_tools_without_inventory_claim(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        song = hq.store.create_song("Tool Context Song")
        _record_tools(hq, song.id, names=("Serum", "Pro-Q 3"))
        markup = _active_tool_context_markup(_Shell(hq), "SOUND")
        assert "Latest observed DAW tools" in markup
        assert "Ableton Live" in markup
        assert "Serum (position 1)" in markup
        assert "Pro-Q 3 (position 2)" in markup
        assert "before adding another device" in markup
        assert "does not prove installed inventory" in markup
        assert "ownership" in markup
        assert "exact product identity" in markup
        assert "controllable parameters" in markup
    finally:
        hq.close()


def test_non_sound_suggestion_shows_context_without_tool_directive(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        song = hq.store.create_song("Tool Context Song")
        _record_tools(hq, song.id, names=("Serum",))
        markup = _active_tool_context_markup(_Shell(hq), "ARRANGEMENT")
        assert "Serum" in markup
        assert "before adding another device" not in markup
    finally:
        hq.close()


def test_latest_workspace_without_current_shadow_does_not_fall_back_to_old_tools(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        song = hq.store.create_song("Tool Context Song")
        _record_tools(hq, song.id, names=("Old Device",))
        hq.workspaces.create(
            song.id,
            runtime=_runtime(),
            location_ref="ableton-test:set-b",
            display_name="Newer Workspace Without Shadow",
        )
        assert _active_tool_context_markup(_Shell(hq), "SOUND") == ""
    finally:
        hq.close()


def test_active_tool_context_is_bounded_and_html_escaped(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        song = hq.store.create_song("Tool Context Song")
        names = tuple("<script>alert(1)</script>" if i == 0 else f"Device {i}" for i in range(13))
        _record_tools(hq, song.id, names=names)
        markup = _active_tool_context_markup(_Shell(hq), "DYNAMICS")
        assert "<script>" not in markup
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in markup
        assert "+1 more active device(s) omitted" in markup
    finally:
        hq.close()


def test_semantic_tool_context_is_empty_until_artist_inventory_exists(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        assert _semantic_tool_context_markup(_Shell(hq)) == ""
    finally:
        hq.close()


def test_semantic_tool_context_surfaces_explicit_inventory_without_daw_match_claim(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        _register_semantic_tool(
            hq,
            _semantic_tool(display_name="<Owned & Compressor>"),
        )
        markup = _semantic_tool_context_markup(_Shell(hq))
        assert "Explicit Tool inventory" in markup
        assert "&lt;Owned &amp; Compressor&gt;" in markup
        assert "<Owned & Compressor>" not in markup
        assert "VST3" in markup
        assert "dynamics.compress" in markup
        assert "explicit Artist declaration" in markup
        assert "does not prove that a Tool is loaded" in markup
        assert "N0TE has not matched these entries to the observed device chain" in markup
    finally:
        hq.close()


def test_semantic_tool_context_omits_retired_inventory(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        profile = _semantic_tool(display_name="Retired Compressor")
        _register_semantic_tool(hq, profile)
        hq.tool_inventory.retire(
            profile.tool_id,
            source_ref="artist:test:retire",
            reason="Artist removed this Tool from the active setup.",
        )
        assert _semantic_tool_context_markup(_Shell(hq)) == ""
    finally:
        hq.close()


def test_semantic_tool_context_is_bounded(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        for index in range(9):
            _register_semantic_tool(
                hq,
                _semantic_tool(
                    tool_id=f"tool:test-{index}",
                    display_name=f"Tool {index}",
                    native_identity=f"TestVendor.Tool{index}",
                    capability=f"audio.process.{index}",
                ),
            )
        markup = _semantic_tool_context_markup(_Shell(hq))
        assert markup.count("<li><strong>Tool ") == 8
        assert "+1 more declared Tool(s) omitted" in markup
    finally:
        hq.close()


def test_creative_result_markup_includes_current_daw_and_semantic_tool_contexts(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        song = hq.store.create_song("Tool Context Song")
        _record_tools(hq, song.id, names=("Operator",))
        _register_semantic_tool(
            hq,
            _semantic_tool(display_name="Declared Compressor"),
        )
        markup = _result_markup(_Shell(hq), _suggestion(song.id, dimension="SOUND"))
        assert "One prompt to try" in markup
        assert "Operator" in markup
        assert "DAW-aware option" in markup
        assert "Declared Compressor" in markup
        assert "Explicit Tool inventory" in markup
        assert "N0TE has not matched these entries to the observed device chain" in markup
        assert "Generated locally and deterministically" in markup
    finally:
        hq.close()
