from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
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

    The provider caches one compiled state while its source content is unchanged.
    That makes a snapshot stable long enough for a one-time permit to be issued and
    consumed. Any canonical source change invalidates the snapshot immediately.
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
        self._cached_source_digest: str | None = None
        self._cached_state: CompiledExecutionState | None = None
        self._cached_snapshot: TrustedContextSnapshot | None = None

    @staticmethod
    def _read_object(path: Path, label: str) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TrustedContextError(f"{label} is unavailable or unreadable: {path}") from exc
        if not isinstance(value, dict):
            raise TrustedContextError(f"{label} must be an object: {path}")
        return value

    def _sources(self) -> tuple[dict, dict, list[dict], list[str], str]:
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

        source_material = {
            "manifest": manifest,
            "cursor": cursor,
            "facts": facts,
            "incidents": incidents,
        }
        digest = hashlib.sha256(
            json.dumps(
                source_material,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return manifest, cursor, facts, incidents, digest

    def current_manifest(self) -> dict:
        manifest, _, _, _, _ = self._sources()
        return manifest

    def current_state(self) -> CompiledExecutionState:
        manifest, cursor, facts, incidents, digest = self._sources()
        if self._cached_source_digest == digest and self._cached_state is not None:
            return self._cached_state

        raw = compile_from_manifest(
            manifest=manifest,
            cursor=cursor,
            facts=facts,
            incidents=incidents,
            compiled_at=datetime.now(timezone.utc),
        )
        state = compile_execution_state(raw)
        self._cached_source_digest = digest
        self._cached_state = state
        self._cached_snapshot = compiled_state_to_trusted_snapshot(
            state,
            ttl_seconds=self.ttl_seconds,
        )
        return state

    def current_snapshot(self) -> TrustedContextSnapshot:
        self.current_state()
        if self._cached_snapshot is None:
            raise TrustedContextError("compiled context snapshot was not produced")
        return self._cached_snapshot

    def current_projection(self) -> dict:
        projection = build_execution_projection(self.current_state())
        manifest = self.current_manifest()
        constraints = manifest.get("decision_constraints", {})
        standing = manifest.get("standing_authority", {})
        if not isinstance(constraints, dict):
            raise TrustedContextError("execution manifest decision_constraints must be an object")
        if not isinstance(standing, dict):
            raise TrustedContextError("execution manifest standing_authority must be an object")
        projection["decision_constraints"] = constraints
        projection["standing_authority"] = standing
        projection["manifest_version"] = manifest.get("version")
        return projection

    def get(self, snapshot_id: str) -> TrustedContextSnapshot:
        current = self.current_snapshot()
        if current.snapshot_id != str(snapshot_id).strip():
            raise TrustedContextError(
                "requested context snapshot is no longer current; recompile before execution"
            )
        return current
