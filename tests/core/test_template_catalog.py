from __future__ import annotations

import json
import sqlite3

import pytest

from n0te.lineage import LineageCorruptionError, LineageStore
from n0te.template_catalog import (
    TEMPLATE_CATALOG_SCHEMA_VERSION,
    TemplateCatalog,
    TemplateCatalogError,
)
from n0te.templates import TemplateDefinition, TemplateRole


def _template(template_id: str = "template:test:vocal") -> TemplateDefinition:
    return TemplateDefinition(
        template_id=template_id,
        family="VOCAL",
        name="Vocal Start",
        intent="Begin vocal work from semantic roles rather than a host track layout",
        roles=(
            TemplateRole(
                role_id="lead-edit",
                capability="vocal.tighten",
                description="Tighten the lead while preserving performance intent",
                required=True,
                tags=("editing", "lead"),
            ),
            TemplateRole(
                role_id="harmony",
                capability="vocal.harmony.build",
                description="Build optional supporting harmonies",
                required=False,
                tags=("harmony",),
            ),
        ),
    )


def test_catalog_reads_do_not_initialize_schema(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Pure Read Artist")
    try:
        song = store.create_song("Pure Read Song")
        catalog = TemplateCatalog(store)
        assert catalog.templates() == ()
        assert catalog.current_selection(song.id) is None
        assert (
            store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='template_definitions'"
            ).fetchone()
            is None
        )
        assert (
            store._conn.execute(
                "SELECT value FROM metadata WHERE key='template_catalog_schema_version'"
            ).fetchone()
            is None
        )
    finally:
        store.close()


def test_multi_role_template_round_trips_and_selection_is_append_only(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Template Artist")
    song = store.create_song("Template Song")
    catalog = TemplateCatalog(store)
    first = catalog.save(_template())
    second = catalog.create(
        family="MIX",
        name="Mix Start",
        intent="Prepare a reversible technical mix starting point",
        roles=(
            TemplateRole(
                role_id="repair",
                capability="audio.repair",
                description="Repair obvious technical defects",
                required=True,
            ),
        ),
    )
    first_selection = catalog.select_for_song(
        song_id=song.id, template_id=first.template_id
    )
    second_selection = catalog.select_for_song(
        song_id=song.id, template_id=second.template_id
    )
    assert first_selection.source_kind == "ARTIST_DECLARED"
    assert second_selection.sequence > first_selection.sequence
    assert tuple(item.template_id for item in catalog.selection_history(song.id)) == (
        first.template_id,
        second.template_id,
    )
    assert catalog.selected_template(song.id) == second
    store.close()

    reopened = LineageStore.open(tmp_path, store.profile_id)
    try:
        catalog = TemplateCatalog(reopened)
        assert catalog.templates() == (first, second)
        assert catalog.selected_template(song.id) == second
        restored = catalog.get(first.template_id)
        assert restored == first
        assert restored is not None
        assert restored.roles[0].role_id == "harmony"
        assert restored.roles[1].role_id == "lead-edit"
    finally:
        reopened.close()


def test_capability_keys_are_canonicalized_before_immutable_persistence(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Canonical Capability Artist")
    try:
        store.create_song("Canonical Capability Song")
        catalog = TemplateCatalog(store)
        definition = TemplateDefinition(
            template_id="template:canonical",
            family="VOCAL",
            name="Canonical",
            intent="Canonicalize capability identities before durable persistence",
            roles=(
                TemplateRole(
                    role_id="lead",
                    capability="  VOCAL.TIGHTEN  ",
                    description="Tighten lead",
                ),
                TemplateRole(
                    role_id="harmony",
                    capability="Vocal.Harmony.Build",
                    description="Build harmony",
                    required=False,
                ),
            ),
        )
        saved = catalog.save(definition)
        assert tuple(role.capability for role in saved.roles) == (
            "vocal.harmony.build",
            "vocal.tighten",
        )
        restored = catalog.get(saved.template_id)
        assert restored == saved
        assert tuple(role.capability for role in restored.roles) == (
            "vocal.harmony.build",
            "vocal.tighten",
        )
    finally:
        store.close()


def test_unknown_capability_fails_before_durable_catalog_initialization(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Unknown Capability Artist")
    try:
        store.create_song("Unknown Capability Song")
        catalog = TemplateCatalog(store)
        invalid = TemplateDefinition(
            template_id="template:typo",
            family="VOCAL",
            name="Typo",
            intent="This typo must never become immutable durable Template meaning",
            roles=(
                TemplateRole(
                    role_id="lead",
                    capability="vocal.tigten",
                    description="Typo that resembles a real capability",
                ),
            ),
        )
        with pytest.raises(TemplateCatalogError, match="unsupported Template capability key"):
            catalog.save(invalid)
        assert (
            store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='template_definitions'"
            ).fetchone()
            is None
        )
        assert (
            store._conn.execute(
                "SELECT value FROM metadata WHERE key='template_catalog_schema_version'"
            ).fetchone()
            is None
        )
    finally:
        store.close()


def test_clear_selection_is_append_only_exact_bound_and_relaunches_as_none(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Clear Artist")
    profile_id = store.profile_id
    song = store.create_song("Clear Song")
    catalog = TemplateCatalog(store)
    first = catalog.save(_template("template:clear:first"))
    second = catalog.create(
        family="MIX",
        name="Second",
        intent="Second reusable start",
        roles=(
            TemplateRole(
                "repair",
                "audio.repair",
                "Repair obvious technical defects",
            ),
        ),
    )
    selected = catalog.select_for_song(song_id=song.id, template_id=first.template_id)
    with pytest.raises(TemplateCatalogError, match="selection changed"):
        catalog.clear_selection_for_song(
            song_id=song.id,
            expected_selection_id="tsel_stale",
        )
    assert catalog.current_selection(song.id) == selected

    cleared = catalog.clear_selection_for_song(
        song_id=song.id,
        expected_selection_id=selected.id,
    )
    assert cleared.template_id is None
    assert cleared.sequence > selected.sequence
    assert catalog.current_selection(song.id) is None
    assert catalog.selected_template(song.id) is None
    assert tuple(item.template_id for item in catalog.selection_history(song.id)) == (
        first.template_id,
        None,
    )
    store.close()

    reopened = LineageStore.open(tmp_path, profile_id)
    try:
        catalog = TemplateCatalog(reopened)
        assert catalog.current_selection(song.id) is None
        assert catalog.selected_template(song.id) is None
        assert tuple(item.template_id for item in catalog.selection_history(song.id)) == (
            first.template_id,
            None,
        )
        reselection = catalog.select_for_song(
            song_id=song.id,
            template_id=second.template_id,
        )
        assert catalog.current_selection(song.id) == reselection
        assert catalog.selected_template(song.id) == second
        assert tuple(item.template_id for item in catalog.selection_history(song.id)) == (
            first.template_id,
            None,
            second.template_id,
        )
    finally:
        reopened.close()


def test_v1_catalog_migrates_without_rewriting_existing_history(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Migration Artist")
    try:
        song = store.create_song("Migration Song")
        definition = _template("template:v1")
        roles_json = json.dumps(
            [
                {
                    "role_id": role.role_id,
                    "capability": (
                        "VOCAL.TIGHTEN"
                        if role.role_id == "lead-edit"
                        else role.capability
                    ),
                    "description": role.description,
                    "required": role.required,
                    "tags": list(role.tags),
                }
                for role in definition.roles
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        with store._tx():
            store._conn.execute(
                """CREATE TABLE template_definitions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    artist_id TEXT NOT NULL REFERENCES artists(id),
                    family TEXT NOT NULL CHECK(length(trim(family)) > 0),
                    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
                    intent TEXT NOT NULL CHECK(length(trim(intent)) > 0),
                    roles_json TEXT NOT NULL CHECK(length(trim(roles_json)) > 0)
                )"""
            )
            store._conn.execute(
                """CREATE TABLE template_selections (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    artist_id TEXT NOT NULL REFERENCES artists(id),
                    song_id TEXT NOT NULL REFERENCES songs(id),
                    template_id TEXT NOT NULL REFERENCES template_definitions(id),
                    source_kind TEXT NOT NULL CHECK(source_kind='ARTIST_DECLARED')
                )"""
            )
            store._conn.execute(
                "CREATE INDEX template_selections_by_song "
                "ON template_selections(song_id,seq)"
            )
            for statement in TemplateCatalog._trigger_statements():
                store._conn.execute(statement)
            store._conn.execute(
                "INSERT INTO metadata(key,value) VALUES('template_catalog_schema_version','1')"
            )
            store._conn.execute(
                "INSERT INTO template_definitions("
                "id,artist_id,family,name,intent,roles_json) VALUES(?,?,?,?,?,?)",
                (
                    definition.template_id,
                    store.primary_artist_id,
                    definition.family,
                    definition.name,
                    definition.intent,
                    roles_json,
                ),
            )
            store._conn.execute(
                "INSERT INTO template_selections("
                "id,artist_id,song_id,template_id,source_kind) VALUES(?,?,?,?,?)",
                (
                    "tsel_v1",
                    store.primary_artist_id,
                    song.id,
                    definition.template_id,
                    "ARTIST_DECLARED",
                ),
            )

        catalog = TemplateCatalog(store)
        version = store._conn.execute(
            "SELECT value FROM metadata WHERE key='template_catalog_schema_version'"
        ).fetchone()
        assert version is not None
        assert version["value"] == str(TEMPLATE_CATALOG_SCHEMA_VERSION)
        columns = {
            str(row["name"]): int(row["notnull"])
            for row in store._conn.execute("PRAGMA table_info(template_selections)")
        }
        assert columns["template_id"] == 0
        history = catalog.selection_history(song.id)
        assert len(history) == 1
        assert history[0].id == "tsel_v1"
        assert history[0].template_id == definition.template_id

        restored = catalog.get(definition.template_id)
        assert restored is not None
        lead = next(role for role in restored.roles if role.role_id == "lead-edit")
        assert lead.capability == "VOCAL.TIGHTEN"
        assert catalog.save(restored) == restored
        assert catalog.get(definition.template_id) == restored

        cleared = catalog.clear_selection_for_song(
            song_id=song.id,
            expected_selection_id="tsel_v1",
        )
        assert cleared.template_id is None
        assert tuple(item.template_id for item in catalog.selection_history(song.id)) == (
            definition.template_id,
            None,
        )
    finally:
        store.close()


def test_template_identity_cannot_be_rewritten_or_deleted(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Immutable Artist")
    try:
        song = store.create_song("Immutable Song")
        catalog = TemplateCatalog(store)
        definition = catalog.save(_template())
        selection = catalog.select_for_song(
            song_id=song.id, template_id=definition.template_id
        )

        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute(
                "UPDATE template_definitions SET name='Rewritten' WHERE id=?",
                (definition.template_id,),
            )
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute(
                "DELETE FROM template_definitions WHERE id=?",
                (definition.template_id,),
            )
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute(
                "UPDATE template_selections SET source_kind='ARTIST_DECLARED' WHERE id=?",
                (selection.id,),
            )
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute(
                "DELETE FROM template_selections WHERE id=?",
                (selection.id,),
            )

        assert catalog.get(definition.template_id) == definition
        assert catalog.current_selection(song.id) == selection
    finally:
        store.close()


def test_same_identity_with_different_meaning_is_rejected(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Identity Artist")
    try:
        store.create_song("Identity Song")
        catalog = TemplateCatalog(store)
        original = catalog.save(_template("stable-template"))
        assert catalog.save(original) == original
        conflicting = TemplateDefinition(
            template_id=original.template_id,
            family="VOCAL",
            name="Different meaning",
            intent=original.intent,
            roles=original.roles,
        )
        with pytest.raises(TemplateCatalogError):
            catalog.save(conflicting)
    finally:
        store.close()


def test_missing_integrity_trigger_fails_closed_on_reopen(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Corruption Artist")
    profile_id = store.profile_id
    try:
        store.create_song("Corruption Song")
        TemplateCatalog(store).save(_template())
        store._conn.execute("DROP TRIGGER template_definitions_immutable_update")
        store._conn.commit()
    finally:
        store.close()

    reopened = LineageStore.open(tmp_path, profile_id)
    try:
        with pytest.raises(LineageCorruptionError):
            TemplateCatalog(reopened)
    finally:
        reopened.close()


def test_catalog_bounds_semantic_roles_without_host_fields(tmp_path) -> None:
    store = LineageStore.create(tmp_path, "Bounds Artist")
    try:
        store.create_song("Bounds Song")
        catalog = TemplateCatalog(store)
        too_many = tuple(
            TemplateRole(
                role_id=f"r{index}",
                capability=f"cap.{index}",
                description=f"Role {index}",
            )
            for index in range(9)
        )
        definition = TemplateDefinition(
            template_id="too-many",
            family="SONG",
            name="Too many",
            intent="Bound the reusable start",
            roles=too_many,
        )
        with pytest.raises(TemplateCatalogError):
            catalog.save(definition)

        assert "host" not in TemplateDefinition.__dataclass_fields__
        assert "provider" not in TemplateDefinition.__dataclass_fields__
        assert "track" not in TemplateDefinition.__dataclass_fields__
        assert "host" not in TemplateRole.__dataclass_fields__
        assert "provider" not in TemplateRole.__dataclass_fields__
        assert "track" not in TemplateRole.__dataclass_fields__
    finally:
        store.close()