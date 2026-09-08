from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

SEMANTIC_DEFINITION_SCHEMA_VERSION = 1

_SEMANTIC_KEY_RE = re.compile(r"^sem-[a-z0-9]+(?:-[a-z0-9]+)*$")
_CHANGE_ID_RE = re.compile(r"^DEFCHG-[A-Z0-9][A-Z0-9._-]*$")


class SemanticDefinitionError(RuntimeError):
    """Semantic definition history cannot be represented truthfully."""


class DefinitionChangeKind(str, Enum):
    CLARIFY = "CLARIFY"
    REFINE = "REFINE"
    EXTEND = "EXTEND"
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    DEPRECATE = "DEPRECATE"
    SUPERSEDE = "SUPERSEDE"
    BREAKING = "BREAKING"


class DefinitionStability(str, Enum):
    STABLE = "STABLE"
    EVOLVING = "EVOLVING"
    DEPRECATED = "DEPRECATED"


def _text(value: str, field: str, *, maximum: int = 4_000) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    normalized = " ".join(value.split())
    if not normalized:
        raise SemanticDefinitionError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise SemanticDefinitionError(f"{field} is too long")
    return normalized


def _semantic_key(value: str, field: str = "semantic_key") -> str:
    value = _text(value, field, maximum=160)
    if not _SEMANTIC_KEY_RE.fullmatch(value):
        raise SemanticDefinitionError(
            f"{field} must be a stable lower-case sem-* key"
        )
    return value


def _positive_int(value: int, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer")
    if value < 1:
        raise SemanticDefinitionError(f"{field} must be positive")
    return value


def _facet_set(values: Iterable[str], field: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field} must be an iterable of facet keys")
    normalized = frozenset(_text(value, field, maximum=120) for value in values)
    if not normalized:
        raise SemanticDefinitionError(f"{field} must not be empty")
    return normalized


def _refs(values: Iterable[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field} must be an iterable of references")
    return tuple(
        dict.fromkeys(_text(value, field, maximum=300) for value in values)
    )


@dataclass(frozen=True)
class SemanticDefinition:
    semantic_key: str
    version: int
    definition: str
    purpose: str
    boundary: str
    consequence: str
    proof: str
    retained_facets: frozenset[str]
    stability: DefinitionStability | str = DefinitionStability.STABLE
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_key", _semantic_key(self.semantic_key))
        object.__setattr__(self, "version", _positive_int(self.version, "version"))
        for field in ("definition", "purpose", "boundary", "consequence", "proof"):
            object.__setattr__(self, field, _text(getattr(self, field), field))

        object.__setattr__(
            self,
            "retained_facets",
            _facet_set(self.retained_facets, "retained_facets"),
        )
        try:
            stability = DefinitionStability(self.stability)
        except ValueError as exc:
            raise SemanticDefinitionError(
                f"unsupported definition stability: {self.stability}"
            ) from exc
        object.__setattr__(self, "stability", stability)
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))


@dataclass(frozen=True)
class DefinitionChange:
    change_id: str
    kind: DefinitionChangeKind | str
    source_keys: tuple[str, ...]
    target_keys: tuple[str, ...]
    rationale: str
    migration_ref: str | None = None
    reconciliation_ref: str | None = None
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        change_id = _text(self.change_id, "change_id", maximum=160).upper()
        if not _CHANGE_ID_RE.fullmatch(change_id):
            raise SemanticDefinitionError(
                "change_id must use the DEFCHG-* immutable identifier form"
            )
        object.__setattr__(self, "change_id", change_id)

        try:
            kind = DefinitionChangeKind(self.kind)
        except ValueError as exc:
            raise SemanticDefinitionError(
                f"unsupported change kind: {self.kind}"
            ) from exc
        object.__setattr__(self, "kind", kind)

        sources = tuple(
            dict.fromkeys(_semantic_key(key, "source_keys") for key in self.source_keys)
        )
        targets = tuple(
            dict.fromkeys(_semantic_key(key, "target_keys") for key in self.target_keys)
        )
        if not sources:
            raise SemanticDefinitionError("source_keys must not be empty")
        object.__setattr__(self, "source_keys", sources)
        object.__setattr__(self, "target_keys", targets)
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))

        for field in ("migration_ref", "reconciliation_ref", "terminal_reason"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _text(value, field, maximum=500))

        self._validate_shape()

    def _validate_shape(self) -> None:
        same_key_kinds = {
            DefinitionChangeKind.CLARIFY,
            DefinitionChangeKind.REFINE,
            DefinitionChangeKind.EXTEND,
            DefinitionChangeKind.BREAKING,
        }
        if self.kind in same_key_kinds:
            if len(self.source_keys) != 1 or self.target_keys != self.source_keys:
                raise SemanticDefinitionError(
                    f"{self.kind.value} must evolve exactly one stable semantic key"
                )

        if self.kind is DefinitionChangeKind.SPLIT:
            if len(self.source_keys) != 1 or len(self.target_keys) < 2:
                raise SemanticDefinitionError(
                    "SPLIT requires one source and at least two successor keys"
                )
            if self.source_keys[0] in self.target_keys:
                raise SemanticDefinitionError(
                    "SPLIT successors must be new semantic keys"
                )

        if self.kind is DefinitionChangeKind.MERGE:
            if len(self.source_keys) < 2 or len(self.target_keys) != 1:
                raise SemanticDefinitionError(
                    "MERGE requires at least two sources and exactly one successor key"
                )
            if self.target_keys[0] in self.source_keys:
                raise SemanticDefinitionError(
                    "MERGE successor must be a new semantic key"
                )

        if self.kind is DefinitionChangeKind.SUPERSEDE:
            if len(self.source_keys) != 1 or not self.target_keys:
                raise SemanticDefinitionError(
                    "SUPERSEDE requires one source and at least one successor key"
                )
            if self.source_keys[0] in self.target_keys:
                raise SemanticDefinitionError(
                    "SUPERSEDE successor must be a new semantic key"
                )

        if self.kind is DefinitionChangeKind.DEPRECATE:
            if len(self.source_keys) != 1:
                raise SemanticDefinitionError(
                    "DEPRECATE requires exactly one source key"
                )
            if self.source_keys[0] in self.target_keys:
                raise SemanticDefinitionError(
                    "DEPRECATE successor must differ from source"
                )
            if not self.target_keys and self.terminal_reason is None:
                raise SemanticDefinitionError(
                    "DEPRECATE without a successor requires an explicit terminal_reason"
                )

        if self.kind is DefinitionChangeKind.BREAKING:
            if self.migration_ref is None or self.reconciliation_ref is None:
                raise SemanticDefinitionError(
                    "BREAKING requires explicit migration_ref and reconciliation_ref"
                )


@dataclass(frozen=True)
class DefinitionProjection:
    semantic_key: str
    version: int
    summary: str
    retained_facets: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_key", _semantic_key(self.semantic_key))
        object.__setattr__(self, "version", _positive_int(self.version, "version"))
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        object.__setattr__(
            self,
            "retained_facets",
            _facet_set(self.retained_facets, "retained_facets"),
        )


@dataclass(frozen=True)
class LegacyDefinitionView:
    record_id: str
    name: str
    version: int
    current_value: str
    source_refs: tuple[str, ...]
    supersedes: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "record_id", _text(self.record_id, "record_id", maximum=160)
        )
        object.__setattr__(self, "name", _text(self.name, "name", maximum=160))
        object.__setattr__(self, "version", _positive_int(self.version, "version"))
        object.__setattr__(
            self, "current_value", _text(self.current_value, "current_value")
        )
        object.__setattr__(
            self, "source_refs", _refs(self.source_refs, "source_refs")
        )
        if self.supersedes is not None:
            object.__setattr__(
                self,
                "supersedes",
                _text(self.supersedes, "supersedes", maximum=160),
            )


class SemanticDefinitionRegistry:
    """Validation/read model over canonical semantic definition evidence."""

    def __init__(self) -> None:
        self._definitions: dict[str, dict[int, SemanticDefinition]] = {}
        self._changes: list[DefinitionChange] = []
        self._change_ids: set[str] = set()

    def add_initial(self, definition: SemanticDefinition) -> None:
        if not isinstance(definition, SemanticDefinition):
            raise TypeError("definition must be SemanticDefinition")
        if definition.version != 1:
            raise SemanticDefinitionError("initial definition version must be 1")
        if definition.semantic_key in self._definitions:
            raise SemanticDefinitionError(
                f"semantic key already exists: {definition.semantic_key}"
            )
        self._definitions[definition.semantic_key] = {1: definition}

    def resolve(
        self, semantic_key: str, version: int | None = None
    ) -> SemanticDefinition:
        key = _semantic_key(semantic_key)
        versions = self._definitions.get(key)
        if not versions:
            raise SemanticDefinitionError(f"unknown semantic key: {key}")
        if version is None:
            version = max(versions)
        version = _positive_int(version, "version")
        try:
            return versions[version]
        except KeyError as exc:
            raise SemanticDefinitionError(
                f"unknown definition version: {key}@{version}"
            ) from exc

    def history(self, semantic_key: str) -> tuple[SemanticDefinition, ...]:
        key = _semantic_key(semantic_key)
        versions = self._definitions.get(key)
        if not versions:
            raise SemanticDefinitionError(f"unknown semantic key: {key}")
        return tuple(versions[version] for version in sorted(versions))

    def evolve(
        self,
        definition: SemanticDefinition,
        change: DefinitionChange,
    ) -> None:
        if not isinstance(definition, SemanticDefinition):
            raise TypeError("definition must be SemanticDefinition")
        if not isinstance(change, DefinitionChange):
            raise TypeError("change must be DefinitionChange")
        if change.change_id in self._change_ids:
            raise SemanticDefinitionError(
                f"change already recorded: {change.change_id}"
            )

        if change.kind not in {
            DefinitionChangeKind.CLARIFY,
            DefinitionChangeKind.REFINE,
            DefinitionChangeKind.EXTEND,
            DefinitionChangeKind.BREAKING,
        }:
            raise SemanticDefinitionError(
                f"{change.kind.value} is a structural change, not same-key evolution"
            )
        if change.source_keys != (definition.semantic_key,) or change.target_keys != (
            definition.semantic_key,
        ):
            raise SemanticDefinitionError(
                "definition key does not match change lineage"
            )

        previous = self.resolve(definition.semantic_key)
        if definition.version != previous.version + 1:
            raise SemanticDefinitionError(
                "definition versions must advance exactly one step"
            )

        removed = previous.retained_facets - definition.retained_facets
        added = definition.retained_facets - previous.retained_facets

        if change.kind in {
            DefinitionChangeKind.CLARIFY,
            DefinitionChangeKind.REFINE,
            DefinitionChangeKind.EXTEND,
        } and removed:
            raise SemanticDefinitionError(
                "compatible semantic evolution cannot remove retained facets: "
                + ", ".join(sorted(removed))
            )
        if change.kind is DefinitionChangeKind.CLARIFY and added:
            raise SemanticDefinitionError(
                "CLARIFY may explain existing meaning but cannot add retained facets"
            )
        if change.kind is DefinitionChangeKind.EXTEND and not added:
            raise SemanticDefinitionError(
                "EXTEND must add at least one retained semantic facet"
            )

        self._definitions[definition.semantic_key][definition.version] = definition
        self._record_change(change)

    def record_structural_change(self, change: DefinitionChange) -> None:
        if not isinstance(change, DefinitionChange):
            raise TypeError("change must be DefinitionChange")
        if change.kind not in {
            DefinitionChangeKind.SPLIT,
            DefinitionChangeKind.MERGE,
            DefinitionChangeKind.DEPRECATE,
            DefinitionChangeKind.SUPERSEDE,
        }:
            raise SemanticDefinitionError(
                f"{change.kind.value} is not a structural lineage change"
            )
        if change.change_id in self._change_ids:
            raise SemanticDefinitionError(
                f"change already recorded: {change.change_id}"
            )

        for key in change.source_keys:
            self.resolve(key)
        for key in change.target_keys:
            if key not in self._definitions:
                raise SemanticDefinitionError(
                    f"successor definition must be registered before lineage: {key}"
                )
        self._record_change(change)

    def validate_projection(self, projection: DefinitionProjection) -> None:
        if not isinstance(projection, DefinitionProjection):
            raise TypeError("projection must be DefinitionProjection")
        definition = self.resolve(projection.semantic_key, projection.version)
        missing = definition.retained_facets - projection.retained_facets
        if missing:
            raise SemanticDefinitionError(
                "projection silently narrows stable meaning; missing facets: "
                + ", ".join(sorted(missing))
            )

    def successors(self, semantic_key: str) -> tuple[str, ...]:
        key = _semantic_key(semantic_key)
        self.resolve(key)
        successors: list[str] = []
        for change in self._changes:
            if key in change.source_keys:
                successors.extend(change.target_keys)
        return tuple(dict.fromkeys(successors))

    def lineage(self) -> tuple[DefinitionChange, ...]:
        return tuple(self._changes)

    def _record_change(self, change: DefinitionChange) -> None:
        self._changes.append(change)
        self._change_ids.add(change.change_id)


def legacy_definition_from_mapping(
    row: Mapping[str, object],
) -> LegacyDefinitionView:
    if not isinstance(row, Mapping):
        raise TypeError("legacy definition row must be a mapping")
    try:
        record_id = row["id"]
        name = row["name"]
        version = row["version"]
        current_value = row["current_value"]
    except KeyError as exc:
        raise SemanticDefinitionError(
            f"legacy definition row missing required field: {exc.args[0]}"
        ) from exc

    source = row.get("source", ())
    if source is None:
        source = ()
    if not isinstance(source, (list, tuple)):
        raise SemanticDefinitionError("legacy definition source must be a list")

    supersedes = row.get("supersedes")
    if supersedes is not None and not isinstance(supersedes, str):
        raise SemanticDefinitionError("legacy supersedes must be text or null")

    return LegacyDefinitionView(
        record_id=record_id,  # type: ignore[arg-type]
        name=name,  # type: ignore[arg-type]
        version=version,  # type: ignore[arg-type]
        current_value=current_value,  # type: ignore[arg-type]
        source_refs=tuple(source),  # type: ignore[arg-type]
        supersedes=supersedes,
    )


def load_legacy_definition_jsonl(text: str) -> tuple[LegacyDefinitionView, ...]:
    if not isinstance(text, str):
        raise TypeError("legacy definition JSONL must be text")
    rows: list[LegacyDefinitionView] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise SemanticDefinitionError(
                f"invalid legacy definition JSON on line {line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise SemanticDefinitionError(
                f"legacy definition line {line_number} must contain an object"
            )
        rows.append(legacy_definition_from_mapping(value))
    return tuple(rows)
