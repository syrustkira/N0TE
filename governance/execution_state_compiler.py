from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .compiled_execution_state import compile_execution_state


class ExecutionStateCompilerError(ValueError):
    pass


def _load_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionStateCompilerError(f"cannot read JSON source: {path}") from exc
    if not isinstance(raw, dict):
        raise ExecutionStateCompilerError(f"JSON source must be an object: {path}")
    return raw


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def compile_from_manifest(
    *,
    manifest: dict,
    cursor: dict,
    facts: list[dict],
    incidents: list[str] | None = None,
    compiled_at: datetime | None = None,
) -> dict:
    if not isinstance(manifest, dict):
        raise ExecutionStateCompilerError("manifest must be an object")
    compiled_at = compiled_at or datetime.now(timezone.utc)
    if compiled_at.tzinfo is None:
        raise ExecutionStateCompilerError("compiled_at must include timezone")

    source_material = {
        "manifest": manifest,
        "cursor": cursor,
        "facts": facts,
        "incidents": incidents or [],
    }
    raw = {
        "retained_scope_refs": manifest.get("retained_scope_refs", []),
        "truth_owners": manifest.get("truth_owners", {}),
        "dependency_graph": manifest.get("dependency_graph", {}),
        "lens_dispatch": manifest.get("lens_dispatch", {}),
        "cursor": cursor,
        "facts": facts,
        "incidents": incidents or [],
        "source_fingerprint": _sha(source_material),
        "compiled_at": compiled_at.astimezone(timezone.utc).isoformat(),
    }
    compiled = compile_execution_state(raw)
    return {
        **raw,
        "compiled_fingerprint": compiled.fingerprint,
        "next_dependencies": list(compiled.next_dependencies()),
        "required_functions": list(compiled.required_functions()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile canonical N0TE execution state")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--cursor", required=True)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--incidents")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    manifest = _load_json(Path(args.manifest))
    cursor = _load_json(Path(args.cursor))
    facts_raw = _load_json(Path(args.facts))
    facts = facts_raw.get("facts")
    if not isinstance(facts, list):
        raise ExecutionStateCompilerError("facts file must contain a facts list")
    incidents: list[str] = []
    if args.incidents:
        incidents_raw = _load_json(Path(args.incidents))
        value = incidents_raw.get("incidents")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ExecutionStateCompilerError("incidents file must contain an incidents list")
        incidents = value

    result = compile_from_manifest(
        manifest=manifest,
        cursor=cursor,
        facts=facts,
        incidents=incidents,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
