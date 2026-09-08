from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .lineage import LineageCorruptionError, LineageStore, NotFoundError


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class GraphNode:
    kind: str
    id: str
    data_json: str = "{}"

    @property
    def data(self) -> Any:
        return json.loads(self.data_json)

    @property
    def identity(self) -> tuple[str, str]:
        return (self.kind, self.id)


@dataclass(frozen=True)
class GraphEdge:
    kind: str
    source_kind: str
    source_id: str
    target_kind: str
    target_id: str
    data_json: str = "{}"

    @property
    def data(self) -> Any:
        return json.loads(self.data_json)

    @property
    def identity(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.kind,
            self.source_kind,
            self.source_id,
            self.target_kind,
            self.target_id,
            self.data_json,
        )


@dataclass(frozen=True)
class SongKnowledgeMap:
    song_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]

    def node(self, kind: str, node_id: str) -> GraphNode | None:
        target = (str(kind).upper(), str(node_id))
        return next((node for node in self.nodes if node.identity == target), None)

    def edges_of_kind(self, kind: str) -> tuple[GraphEdge, ...]:
        kind = str(kind).upper()
        return tuple(edge for edge in self.edges if edge.kind == kind)


class SongKnowledgeMapService:
    """Pure-read typed projection over canonical N0TE2 Song memory.

    This service owns no tables and performs no writes. It derives one bounded
    Song map from existing canonical relational owners.
    """

    REQUIRED_TABLES = {
        "artists",
        "songs",
        "versions",
        "assets",
        "version_assets",
        "evidence_claims",
        "evidence_supersessions",
        "activity_events",
        "provenance_records",
    }

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("SongKnowledgeMapService requires the canonical LineageStore")
        self.store = store
        self._conn = store._conn
        found = {
            str(row["name"])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = self.REQUIRED_TABLES - found
        if missing:
            raise LineageCorruptionError(
                f"Song Knowledge Map canonical owners are incomplete: {sorted(missing)}"
            )

    @staticmethod
    def _external_id(ref: str) -> str:
        return "ext_" + hashlib.sha256(ref.encode("utf-8")).hexdigest()

    def for_song(self, song_id: str) -> SongKnowledgeMap:
        song = self._conn.execute(
            "SELECT id,artist_id,title,current_version_id,approved_version_id "
            "FROM songs WHERE id=?",
            (song_id,),
        ).fetchone()
        if song is None:
            raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")

        nodes: dict[tuple[str, str], GraphNode] = {}
        edges: dict[tuple[str, str, str, str, str, str], GraphEdge] = {}

        def add_node(kind: str, node_id: str, data: Any) -> GraphNode:
            kind = str(kind).upper()
            node_id = str(node_id)
            node = GraphNode(kind, node_id, _json(data))
            key = node.identity
            previous = nodes.get(key)
            if previous is not None and previous != node:
                raise LineageCorruptionError(
                    f"typed graph identity has conflicting canonical data: {kind}/{node_id}"
                )
            nodes[key] = node
            return node

        def add_edge(
            kind: str,
            source_kind: str,
            source_id: str,
            target_kind: str,
            target_id: str,
            data: Any | None = None,
        ) -> GraphEdge:
            edge = GraphEdge(
                str(kind).upper(),
                str(source_kind).upper(),
                str(source_id),
                str(target_kind).upper(),
                str(target_id),
                _json({} if data is None else data),
            )
            edges[edge.identity] = edge
            return edge

        artist = self._conn.execute(
            "SELECT id,display_name FROM artists WHERE id=?",
            (song["artist_id"],),
        ).fetchone()
        if artist is None:
            raise LineageCorruptionError("Song artist identity is missing")
        add_node("ARTIST", artist["id"], {"display_name": artist["display_name"]})
        add_node(
            "SONG",
            song["id"],
            {
                "title": song["title"],
                "current_version_id": song["current_version_id"],
                "approved_version_id": song["approved_version_id"],
            },
        )
        add_edge("ARTIST_OWNS_SONG", "ARTIST", artist["id"], "SONG", song["id"])

        version_rows = self._conn.execute(
            "SELECT id,song_id,ordinal,label,parent_version_id "
            "FROM versions WHERE song_id=? ORDER BY ordinal,id",
            (song_id,),
        ).fetchall()
        version_ids = {str(row["id"]) for row in version_rows}
        for row in version_rows:
            version_id = str(row["id"])
            parent_id = None if row["parent_version_id"] is None else str(row["parent_version_id"])
            add_node(
                "VERSION",
                version_id,
                {
                    "ordinal": int(row["ordinal"]),
                    "label": str(row["label"]),
                    "parent_version_id": parent_id,
                    "is_current": version_id == song["current_version_id"],
                    "is_approved": version_id == song["approved_version_id"],
                },
            )
            add_edge("SONG_HAS_VERSION", "SONG", song_id, "VERSION", version_id)
            if parent_id is not None:
                if parent_id not in version_ids:
                    raise LineageCorruptionError(
                        "Song version parent is missing or crosses Songs"
                    )
                add_edge("VERSION_PARENT", "VERSION", version_id, "VERSION", parent_id)

        current = song["current_version_id"]
        if current is not None:
            current = str(current)
            if current not in version_ids:
                raise LineageCorruptionError("CURRENT points outside the Song version set")
            add_edge("CURRENT_VERSION", "SONG", song_id, "VERSION", current)
        approved = song["approved_version_id"]
        if approved is not None:
            approved = str(approved)
            if approved not in version_ids:
                raise LineageCorruptionError("APPROVED points outside the Song version set")
            add_edge("APPROVED_VERSION", "SONG", song_id, "VERSION", approved)

        asset_rows = self._conn.execute(
            "SELECT id,song_id,name,sha256,source_uri FROM assets WHERE song_id=? ORDER BY id",
            (song_id,),
        ).fetchall()
        asset_ids = {str(row["id"]) for row in asset_rows}
        for row in asset_rows:
            asset_id = str(row["id"])
            add_node(
                "ASSET",
                asset_id,
                {
                    "name": str(row["name"]),
                    "sha256": str(row["sha256"]),
                    "source_uri": None if row["source_uri"] is None else str(row["source_uri"]),
                },
            )
            add_edge("SONG_HAS_ASSET", "SONG", song_id, "ASSET", asset_id)

        for row in self._conn.execute(
            "SELECT va.version_id,va.asset_id,va.role "
            "FROM version_assets va JOIN versions v ON v.id=va.version_id "
            "WHERE v.song_id=? ORDER BY va.version_id,va.asset_id,va.role",
            (song_id,),
        ):
            version_id = str(row["version_id"])
            asset_id = str(row["asset_id"])
            if version_id not in version_ids or asset_id not in asset_ids:
                raise LineageCorruptionError("version/asset relation crosses Song boundary")
            add_edge(
                "VERSION_USES_ASSET",
                "VERSION",
                version_id,
                "ASSET",
                asset_id,
                {"role": str(row["role"])},
            )

        claim_rows = self._conn.execute(
            "SELECT c.seq,c.id,c.scope_kind,c.scope_id,c.key,c.value_json,"
            "c.source_kind,c.source_ref,c.confidence,"
            "EXISTS(SELECT 1 FROM evidence_supersessions s WHERE s.old_claim_id=c.id) AS superseded "
            "FROM evidence_claims c "
            "WHERE (c.scope_kind='SONG' AND c.scope_id=?) "
            "OR (c.scope_kind='VERSION' AND EXISTS("
            "SELECT 1 FROM versions v WHERE v.id=c.scope_id AND v.song_id=?)) "
            "ORDER BY c.seq",
            (song_id, song_id),
        ).fetchall()
        claim_ids: set[str] = set()
        for row in claim_rows:
            claim_id = str(row["id"])
            claim_ids.add(claim_id)
            scope_kind = str(row["scope_kind"])
            scope_id = str(row["scope_id"])
            if scope_kind == "SONG":
                target_kind = "SONG"
                target_id = song_id
            elif scope_kind == "VERSION" and scope_id in version_ids:
                target_kind = "VERSION"
                target_id = scope_id
            else:
                raise LineageCorruptionError("Song evidence scope is invalid")
            try:
                value = json.loads(str(row["value_json"]))
            except Exception as exc:
                raise LineageCorruptionError("Song evidence contains invalid JSON") from exc
            add_node(
                "EVIDENCE_CLAIM",
                claim_id,
                {
                    "sequence": int(row["seq"]),
                    "scope_kind": scope_kind,
                    "scope_id": scope_id,
                    "key": str(row["key"]),
                    "value": value,
                    "source_kind": str(row["source_kind"]),
                    "source_ref": None if row["source_ref"] is None else str(row["source_ref"]),
                    "confidence": float(row["confidence"]),
                    "superseded": bool(row["superseded"]),
                },
            )
            add_edge(
                "EVIDENCE_ABOUT",
                "EVIDENCE_CLAIM",
                claim_id,
                target_kind,
                target_id,
            )

        supersessions = self._conn.execute(
            "SELECT new_claim_id,old_claim_id FROM evidence_supersessions"
        ).fetchall()
        for row in supersessions:
            new_id = str(row["new_claim_id"])
            old_id = str(row["old_claim_id"])
            if new_id in claim_ids or old_id in claim_ids:
                if new_id not in claim_ids or old_id not in claim_ids:
                    raise LineageCorruptionError(
                        "evidence supersession escapes the Song evidence set"
                    )
                add_edge(
                    "EVIDENCE_SUPERSEDES",
                    "EVIDENCE_CLAIM",
                    new_id,
                    "EVIDENCE_CLAIM",
                    old_id,
                )

        activity_rows = self._conn.execute(
            "SELECT seq,id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json "
            "FROM activity_events WHERE song_id=? ORDER BY seq",
            (song_id,),
        ).fetchall()
        known_targets = {
            "SONG": {song_id},
            "VERSION": version_ids,
            "ASSET": asset_ids,
            "EVIDENCE_CLAIM": claim_ids,
        }
        for row in activity_rows:
            event_id = str(row["id"])
            try:
                payload = json.loads(str(row["payload_json"]))
            except Exception as exc:
                raise LineageCorruptionError("Song Activity contains invalid JSON") from exc
            object_type = str(row["object_type"]).upper()
            object_id = str(row["object_id"])
            target_ids = known_targets.get(object_type)
            if target_ids is None or object_id not in target_ids:
                raise LineageCorruptionError(
                    f"Song Activity target is not represented canonically: {object_type}/{object_id}"
                )
            add_node(
                "ACTIVITY_EVENT",
                event_id,
                {
                    "sequence": int(row["seq"]),
                    "event_type": str(row["event_type"]),
                    "version_id": None if row["version_id"] is None else str(row["version_id"]),
                    "object_type": object_type,
                    "object_id": object_id,
                    "payload": payload,
                },
            )
            add_edge(
                "ACTIVITY_ABOUT",
                "ACTIVITY_EVENT",
                event_id,
                object_type,
                object_id,
            )

        provenance_rows = self._conn.execute(
            "SELECT * FROM provenance_records WHERE song_id=? ORDER BY seq",
            (song_id,),
        ).fetchall()
        for row in provenance_rows:
            record_id = str(row["id"])
            output_kind = str(row["output_kind"]).upper()
            output_id = str(row["output_id"])
            input_kind = str(row["input_kind"]).upper()
            input_ref = str(row["input_ref"])
            if output_kind == "VERSION":
                if output_id not in version_ids:
                    raise LineageCorruptionError("provenance output version crosses Song boundary")
            elif output_kind == "ASSET":
                if output_id not in asset_ids:
                    raise LineageCorruptionError("provenance output asset crosses Song boundary")
            else:
                raise LineageCorruptionError("provenance output kind is unsupported")
            add_node(
                "PROVENANCE_RECORD",
                record_id,
                {
                    "sequence": int(row["seq"]),
                    "operation": str(row["operation"]),
                    "input_kind": input_kind,
                    "input_ref": input_ref,
                    "tool_ref": None if row["tool_ref"] is None else str(row["tool_ref"]),
                    "provider_ref": None if row["provider_ref"] is None else str(row["provider_ref"]),
                    "model_ref": None if row["model_ref"] is None else str(row["model_ref"]),
                    "recipe_ref": None if row["recipe_ref"] is None else str(row["recipe_ref"]),
                    "rights_ref": None if row["rights_ref"] is None else str(row["rights_ref"]),
                    "consent_ref": None if row["consent_ref"] is None else str(row["consent_ref"]),
                    "cost_ref": None if row["cost_ref"] is None else str(row["cost_ref"]),
                    "evidence_source_kind": str(row["evidence_source_kind"]),
                    "evidence_ref": None if row["evidence_ref"] is None else str(row["evidence_ref"]),
                },
            )
            add_edge(
                "PROVENANCE_DESCRIBES",
                "PROVENANCE_RECORD",
                record_id,
                output_kind,
                output_id,
            )
            if input_kind == "VERSION":
                if input_ref not in version_ids:
                    raise LineageCorruptionError("provenance input version crosses Song boundary")
                input_target_kind = "VERSION"
                input_target_id = input_ref
            elif input_kind == "ASSET":
                if input_ref not in asset_ids:
                    raise LineageCorruptionError("provenance input asset crosses Song boundary")
                input_target_kind = "ASSET"
                input_target_id = input_ref
            elif input_kind == "EXTERNAL":
                input_target_kind = "EXTERNAL_REF"
                input_target_id = self._external_id(input_ref)
                add_node("EXTERNAL_REF", input_target_id, {"ref": input_ref})
            else:
                raise LineageCorruptionError("provenance input kind is unsupported")
            add_edge(
                "DERIVED_FROM",
                "PROVENANCE_RECORD",
                record_id,
                input_target_kind,
                input_target_id,
            )

        ordered_nodes = tuple(sorted(nodes.values(), key=lambda n: (n.kind, n.id)))
        ordered_edges = tuple(
            sorted(
                edges.values(),
                key=lambda e: (
                    e.kind,
                    e.source_kind,
                    e.source_id,
                    e.target_kind,
                    e.target_id,
                    e.data_json,
                ),
            )
        )
        return SongKnowledgeMap(song_id=str(song_id), nodes=ordered_nodes, edges=ordered_edges)
