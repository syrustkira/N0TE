from __future__ import annotations

import json
from pathlib import Path

import pytest

from governance.compiled_context_provider import CanonicalCompiledContextProvider
from governance.trusted_context import TrustedContextError


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_provider_compiles_one_current_state_without_second_agent(tmp_path):
    manifest = tmp_path / "manifest.json"
    cursor = tmp_path / "cursor.json"
    facts = tmp_path / "facts.json"
    incidents = tmp_path / "incidents.json"
    _write(manifest, {
        "retained_scope_refs": ["services"],
        "truth_owners": {"WHY": "w", "WHAT": "x", "HOW": "y", "NOW": "n", "PROOF": "p"},
        "dependency_graph": {"services": ["service_acquisition"]},
        "lens_dispatch": {"services": ["epistemology", "offer"]},
    })
    _write(cursor, {
        "job_id": "job",
        "outcome": "conversion",
        "active_object": "services",
        "current_step": "verify path",
        "acceptance": "path verified",
        "state": "READY",
        "blockers": [],
    })
    _write(facts, {"facts": []})
    _write(incidents, {"incidents": ["KNOWN_LOOP"]})

    provider = CanonicalCompiledContextProvider(
        manifest_path=manifest,
        cursor_path=cursor,
        facts_path=facts,
        incidents_path=incidents,
    )
    projection = provider.current_projection()
    assert projection["cursor"]["job_id"] == "job"
    assert projection["required_functions"] == ["epistemology", "offer"]
    snapshot = provider.current_snapshot()
    assert provider.get(snapshot.snapshot_id).snapshot_id == snapshot.snapshot_id


def test_provider_refuses_old_snapshot_after_canon_changes(tmp_path):
    manifest = tmp_path / "manifest.json"
    cursor = tmp_path / "cursor.json"
    facts = tmp_path / "facts.json"
    _write(manifest, {
        "retained_scope_refs": ["services"],
        "truth_owners": {"WHY": "w", "WHAT": "x", "HOW": "y", "NOW": "n", "PROOF": "p"},
        "dependency_graph": {"services": []},
        "lens_dispatch": {"services": ["epistemology"]},
    })
    _write(cursor, {
        "job_id": "job",
        "outcome": "conversion",
        "active_object": "services",
        "current_step": "step one",
        "acceptance": "accepted",
        "state": "READY",
        "blockers": [],
    })
    _write(facts, {"facts": []})
    provider = CanonicalCompiledContextProvider(manifest_path=manifest, cursor_path=cursor, facts_path=facts)
    snapshot = provider.current_snapshot()
    cursor_value = json.loads(cursor.read_text())
    cursor_value["current_step"] = "step two"
    _write(cursor, cursor_value)
    with pytest.raises(TrustedContextError, match="no longer current"):
        provider.get(snapshot.snapshot_id)
