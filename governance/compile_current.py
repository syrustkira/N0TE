from __future__ import annotations

from pathlib import Path

from .compiled_context_provider import CanonicalCompiledContextProvider


def default_provider(root: str | Path = ".") -> CanonicalCompiledContextProvider:
    root = Path(root)
    return CanonicalCompiledContextProvider(
        manifest_path=root / "governance" / "execution_manifest.json",
        cursor_path=root / "governance" / "current_execution_cursor.json",
        facts_path=root / "governance" / "current_execution_facts.json",
        incidents_path=root / "governance" / "current_execution_incidents.json",
    )


def compile_current(root: str | Path = ".") -> dict:
    provider = default_provider(root)
    state = provider.current_state()
    snapshot = provider.current_snapshot()
    projection = provider.current_projection()
    return {
        "state_fingerprint": state.fingerprint,
        "snapshot_id": snapshot.snapshot_id,
        "projection": projection,
    }
