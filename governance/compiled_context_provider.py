from __future__ import annotations

import json
from pathlib import Path

from .compiled_execution_state import CompiledExecutionState, compile_execution_state
from .context_budget import build_execution_projection
from .execution_state_bridge import compiled_state_to_trusted_snapshot
from .execution_state_compiler import compile_from_manifest
from .trusted_context import TrustedContextError, TrustedContextSnapshot


class CanonicalCompiledContextProvider:
    """Compile current canonical execution state on demand.

    This is deliberately not another agent. It performs no reasoning. It reads the
    manifest, resumable cursor, current facts and incident list, then compiles those
    inputs into a disposable state and the TrustedContextSnapshot used by the
    existing execution permit gate.
    """

    def __init__(
        self,
        *,
        manifest_path: str | Path,
        cursor_path: str | Path,
        facts_path: str | Path,
        incidents_path: str | Path | None = None,
        ttl_seconds: int = 300,
    ):
        self.manifest_path = Path(manifest_path)
        self.cursor_path = Path(cursor_path)
        self.facts_path = Path(facts_path)
        self.incidents_path = Path(incidents_path) if incidents_path else None
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _read_object(path: Path, label: str) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TrustedContextError(f"{label} is unavailable or unreadable: {path}") from exc
        if not isinstance(value, dict):
            raise TrustedContextError(f"{label} must be an object: {path}")
        return value

    def current_state(self) -> CompiledExecutionState:
        manifest = self._read_object(self.manifest_path, "execution manifest")
        cursor = self._read_object(self.cursor_path, "execution cursor")
        facts_obj = self._read_object(self.facts_path, "execution facts")
        facts = facts_obj.get("facts")
        if not isinstance(facts, list):
            raise TrustedContextError("execution facts must contain a facts list")
        incidents: list[str] = []
        if self.incidents_path:
            incidents_obj = self._read_object(self.incidents_path, "execution incidents")
            value = incidents_obj.get("incidents")
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise TrustedContextError("execution incidents must contain an incidents list")
            incidents = value

        raw = compile_from_manifest(
            manifest=manifest,
            cursor=cursor,
            facts=facts,
            incidents=incidents,
        )
        return compile_execution_state(raw)

    def current_snapshot(self) -> TrustedContextSnapshot:
        return compiled_state_to_trusted_snapshot(
            self.current_state(),
            ttl_seconds=self.ttl_seconds,
        )

    def current_projection(self) -> dict:
        return build_execution_projection(self.current_state())

    def get(self, snapshot_id: str) -> TrustedContextSnapshot:
        current = self.current_snapshot()
        if current.snapshot_id != str(snapshot_id).strip():
            raise TrustedContextError(
                "requested context snapshot is no longer current; recompile before execution"
            )
        return current
