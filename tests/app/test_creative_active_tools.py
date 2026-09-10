from __future__ import annotations

from dataclasses import dataclass

from n0te import HeadquartersMemory
from n0te.creative_suggestions import CreativeSuggestion
from n0te.creative_suggestions_shell import _active_tool_context_markup, _result_markup
from n0te.hosts import HostRuntimeIdentity
from n0te.shadow import ShadowEventInput


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


def test_creative_result_markup_includes_current_daw_tool_context(tmp_path):
    hq = HeadquartersMemory.create(tmp_path / "hq", "Context Artist")
    try:
        song = hq.store.create_song("Tool Context Song")
        _record_tools(hq, song.id, names=("Operator",))
        markup = _result_markup(_Shell(hq), _suggestion(song.id, dimension="SOUND"))
        assert "One prompt to try" in markup
        assert "Operator" in markup
        assert "DAW-aware option" in markup
        assert "Generated locally and deterministically" in markup
    finally:
        hq.close()
