from __future__ import annotations

import hashlib
import json
from typing import Iterable

from .migration import MigrationPlanError, MigrationStep


def normalize_migration_program(
    steps: Iterable[MigrationStep],
) -> tuple[MigrationStep, ...]:
    normalized = tuple(steps)
    for step in normalized:
        if not isinstance(step, MigrationStep):
            raise TypeError("schema migration program must contain MigrationStep values")
    for previous, current in zip(normalized, normalized[1:]):
        if current.from_version != previous.to_version:
            raise MigrationPlanError(
                "authenticated schema migration program must be ordered and contiguous"
            )
    return normalized


def migration_steps_payload(steps: Iterable[MigrationStep]) -> list[dict[str, object]]:
    normalized = normalize_migration_program(steps)
    return [
        {
            "from_version": step.from_version,
            "to_version": step.to_version,
            "description": step.description,
            "statements": list(step.statements),
        }
        for step in normalized
    ]


def migration_steps_fingerprint(steps: Iterable[MigrationStep]) -> str:
    payload = migration_steps_payload(steps)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def select_migration_chain(
    *,
    source_version: int,
    target_version: int,
    program: Iterable[MigrationStep],
) -> tuple[MigrationStep, ...]:
    if isinstance(source_version, bool) or not isinstance(source_version, int):
        raise MigrationPlanError("source_version must be an integer")
    if isinstance(target_version, bool) or not isinstance(target_version, int):
        raise MigrationPlanError("target_version must be an integer")
    if source_version < 1 or target_version < 1:
        raise MigrationPlanError("schema versions must be positive")
    if source_version > target_version:
        raise MigrationPlanError("schema migration cannot silently downgrade")

    normalized = normalize_migration_program(program)
    if normalized and normalized[-1].to_version != target_version:
        raise MigrationPlanError(
            "authenticated schema migration program must terminate at target_version"
        )
    if source_version == target_version:
        return ()

    start = None
    for index, step in enumerate(normalized):
        if step.from_version == source_version:
            start = index
            break
    if start is None:
        raise MigrationPlanError(
            "authenticated release has no contiguous migration path from source_version"
        )
    selected = normalized[start:]
    if not selected or selected[-1].to_version != target_version:
        raise MigrationPlanError(
            "authenticated release migration path does not reach target_version"
        )
    return selected
