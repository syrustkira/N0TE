import json

import pytest

from n0te.semantic_definitions import (
    DefinitionChange,
    DefinitionChangeKind,
    DefinitionProjection,
    DefinitionStability,
    SemanticDefinition,
    SemanticDefinitionError,
    SemanticDefinitionRegistry,
    legacy_definition_from_mapping,
    load_legacy_definition_jsonl,
)


def definition(
    *,
    key: str = "sem-song-identity",
    version: int = 1,
    facets=frozenset({"artist-bound", "song-bound", "stable-id"}),
    stability: DefinitionStability = DefinitionStability.STABLE,
):
    return SemanticDefinition(
        semantic_key=key,
        version=version,
        definition="A Song is a stable artist-bound creative project.",
        purpose="Keep work attached to the intended Song across tools and sessions.",
        boundary="A Song is not a DAW project file and never inherits cross-Artist identity.",
        consequence="History, evidence, decisions and work remain bound to the same Song.",
        proof="Historical versions remain readable and cross-Song binding fails closed.",
        retained_facets=facets,
        stability=stability,
        source_refs=("REQ-SCOPE-159",),
    )


def same_key_change(
    kind: DefinitionChangeKind,
    *,
    change_id="DEFCHG-SONG-002",
    **kwargs,
):
    return DefinitionChange(
        change_id=change_id,
        kind=kind,
        source_keys=("sem-song-identity",),
        target_keys=("sem-song-identity",),
        rationale="Clarify the definition without silently changing retained meaning.",
        **kwargs,
    )


def test_initial_definition_requires_stable_semantic_key_and_full_contract():
    record = definition()
    assert record.semantic_key == "sem-song-identity"
    assert record.version == 1
    assert record.stability is DefinitionStability.STABLE

    with pytest.raises(SemanticDefinitionError, match="sem-"):
        definition(key="Song Identity")

    with pytest.raises(SemanticDefinitionError, match="definition"):
        SemanticDefinition(
            semantic_key="sem-song",
            version=1,
            definition=" ",
            purpose="Purpose",
            boundary="Boundary",
            consequence="Consequence",
            proof="Proof",
            retained_facets=frozenset({"identity"}),
        )


def test_clarify_preserves_exact_retained_semantic_facets():
    registry = SemanticDefinitionRegistry()
    registry.add_initial(definition())

    clarified = definition(version=2)
    registry.evolve(
        clarified,
        same_key_change(DefinitionChangeKind.CLARIFY),
    )

    assert registry.resolve("sem-song-identity").version == 2
    assert [row.version for row in registry.history("sem-song-identity")] == [1, 2]

    narrowed = definition(
        version=3,
        facets=frozenset({"artist-bound", "stable-id"}),
    )
    with pytest.raises(SemanticDefinitionError, match="cannot remove"):
        registry.evolve(
            narrowed,
            DefinitionChange(
                change_id="DEFCHG-SONG-003",
                kind="REFINE",
                source_keys=("sem-song-identity",),
                target_keys=("sem-song-identity",),
                rationale="A summary tried to quietly remove Song binding.",
            ),
        )


def test_clarify_cannot_smuggle_new_scope_and_extend_must_actually_extend():
    registry = SemanticDefinitionRegistry()
    registry.add_initial(definition())

    expanded = definition(
        version=2,
        facets=frozenset(
            {"artist-bound", "song-bound", "stable-id", "workspace-neutral"}
        ),
    )
    with pytest.raises(SemanticDefinitionError, match="CLARIFY"):
        registry.evolve(
            expanded,
            same_key_change(DefinitionChangeKind.CLARIFY),
        )

    with pytest.raises(SemanticDefinitionError, match="EXTEND"):
        registry.evolve(
            definition(version=2),
            same_key_change(DefinitionChangeKind.EXTEND),
        )

    registry.evolve(
        expanded,
        same_key_change(DefinitionChangeKind.EXTEND),
    )
    assert "workspace-neutral" in registry.resolve(
        "sem-song-identity"
    ).retained_facets


def test_breaking_change_requires_named_migration_and_reconciliation():
    with pytest.raises(SemanticDefinitionError, match="migration_ref"):
        same_key_change(DefinitionChangeKind.BREAKING)

    change = same_key_change(
        DefinitionChangeKind.BREAKING,
        migration_ref="MIG-SONG-IDENTITY-002",
        reconciliation_ref="REC-SONG-IDENTITY-002",
    )
    registry = SemanticDefinitionRegistry()
    registry.add_initial(definition())
    broken = definition(
        version=2,
        facets=frozenset({"artist-bound", "stable-id"}),
    )
    registry.evolve(broken, change)
    assert (
        registry.resolve("sem-song-identity", 1).retained_facets
        != broken.retained_facets
    )


@pytest.mark.parametrize(
    ("kind", "sources", "targets"),
    [
        ("SPLIT", ("sem-old",), ("sem-new-a", "sem-new-b")),
        ("MERGE", ("sem-old-a", "sem-old-b"), ("sem-new",)),
        ("SUPERSEDE", ("sem-old",), ("sem-new",)),
    ],
)
def test_structural_change_shapes_are_explicit(kind, sources, targets):
    change = DefinitionChange(
        change_id=f"DEFCHG-{kind}-001",
        kind=kind,
        source_keys=sources,
        target_keys=targets,
        rationale="Represent a structural semantic change without rewriting history.",
    )
    assert change.kind.value == kind


def test_deprecate_requires_successor_or_terminal_reason():
    with pytest.raises(SemanticDefinitionError, match="terminal_reason"):
        DefinitionChange(
            change_id="DEFCHG-OLD-001",
            kind="DEPRECATE",
            source_keys=("sem-old",),
            target_keys=(),
            rationale="Retire an obsolete concept.",
        )

    change = DefinitionChange(
        change_id="DEFCHG-OLD-002",
        kind="DEPRECATE",
        source_keys=("sem-old",),
        target_keys=(),
        rationale="Retire an obsolete concept with no replacement.",
        terminal_reason="The concept encoded a provider-specific implementation detail.",
    )
    assert change.target_keys == ()


def test_structural_lineage_preserves_old_definition_and_requires_successor():
    registry = SemanticDefinitionRegistry()
    registry.add_initial(
        definition(key="sem-old", facets=frozenset({"meaning"}))
    )

    change = DefinitionChange(
        change_id="DEFCHG-OLD-003",
        kind="SUPERSEDE",
        source_keys=("sem-old",),
        target_keys=("sem-new",),
        rationale="Replace an old name while preserving discoverable lineage.",
    )
    with pytest.raises(SemanticDefinitionError, match="successor definition"):
        registry.record_structural_change(change)

    registry.add_initial(
        definition(key="sem-new", facets=frozenset({"meaning"}))
    )
    registry.record_structural_change(change)

    assert registry.resolve("sem-old", 1).semantic_key == "sem-old"
    assert registry.successors("sem-old") == ("sem-new",)


def test_projection_cannot_silently_narrow_stable_definition():
    registry = SemanticDefinitionRegistry()
    registry.add_initial(definition())

    with pytest.raises(SemanticDefinitionError, match="silently narrows"):
        registry.validate_projection(
            DefinitionProjection(
                semantic_key="sem-song-identity",
                version=1,
                summary="Song identity remains stable.",
                retained_facets=frozenset({"stable-id"}),
            )
        )

    registry.validate_projection(
        DefinitionProjection(
            semantic_key="sem-song-identity",
            version=1,
            summary="Song identity is Artist-bound, Song-bound and stable.",
            retained_facets=frozenset(
                {"artist-bound", "song-bound", "stable-id"}
            ),
        )
    )


def test_change_ids_are_immutable_and_duplicate_changes_fail():
    registry = SemanticDefinitionRegistry()
    registry.add_initial(definition())
    change = same_key_change(DefinitionChangeKind.CLARIFY)
    registry.evolve(definition(version=2), change)

    with pytest.raises(SemanticDefinitionError, match="already recorded"):
        registry.evolve(definition(version=3), change)


def test_legacy_governance_rows_remain_readable_without_inventing_keys():
    row = {
        "id": "DEF-FINISHED-001",
        "recorded_at": "2026-09-01",
        "version": 1,
        "kind": "CONSTITUTIONAL",
        "name": "FINISHED",
        "current_value": "No currently justified construction work exists.",
        "source": ["INV-LIFE-001"],
        "supersedes": None,
    }
    view = legacy_definition_from_mapping(row)
    assert view.record_id == "DEF-FINISHED-001"
    assert view.name == "FINISHED"
    assert view.source_refs == ("INV-LIFE-001",)
    assert not hasattr(view, "semantic_key")

    loaded = load_legacy_definition_jsonl(json.dumps(row) + "\n")
    assert loaded == (view,)


def test_legacy_adapter_fails_closed_on_malformed_rows():
    with pytest.raises(SemanticDefinitionError, match="missing required"):
        legacy_definition_from_mapping({"id": "DEF-X"})

    with pytest.raises(SemanticDefinitionError, match="line 1"):
        load_legacy_definition_jsonl("{not-json}\n")


def test_semantic_evolution_grants_no_action_authority():
    record = definition()
    registry = SemanticDefinitionRegistry()
    registry.add_initial(record)

    assert not hasattr(record, "execute")
    assert not hasattr(record, "authorize")
    assert not hasattr(registry, "send")
    assert not hasattr(registry, "mutate_song")
