from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .evidence import EvidenceClaim, EvidenceMemory
from .graph import GraphNode, SongKnowledgeMap, SongKnowledgeMapService
from .lineage import LineageStore, NotFoundError, ValidationError

_VISIBLE_TWIN_DOMAINS = ("TECHNICAL", "CREATIVE", "UNSPECIFIED")


@dataclass(frozen=True)
class TwinConflict:
    key: str
    technical_claims: tuple[EvidenceClaim, ...]
    creative_claims: tuple[EvidenceClaim, ...]

    @property
    def claim_ids(self) -> tuple[str, ...]:
        return tuple(
            claim.id
            for claim in self.technical_claims + self.creative_claims
        )


@dataclass(frozen=True)
class SongTwinView:
    song_id: str
    version_id: str | None
    technical_claims: tuple[EvidenceClaim, ...]
    creative_claims: tuple[EvidenceClaim, ...]
    unspecified_claims: tuple[EvidenceClaim, ...]
    conflicts: tuple[TwinConflict, ...]

    def claims_for_domain(self, twin_domain: str) -> tuple[EvidenceClaim, ...]:
        domain = str(twin_domain).strip().upper()
        if domain == "TECHNICAL":
            return self.technical_claims
        if domain == "CREATIVE":
            return self.creative_claims
        if domain == "UNSPECIFIED":
            return self.unspecified_claims
        raise ValidationError(f"unsupported Twin domain: {domain}")


class TwinEvidenceService:
    """Pure-read Technical/Creative Twin lenses over canonical scoped evidence.

    Scope is applied independently per Twin domain. This lets a version-specific
    technical observation coexist with broader Song creative intent rather than
    allowing one lens to erase the other. Conflicts are surfaced; no winner is
    selected by this service.
    """

    def __init__(self, memory: EvidenceMemory):
        if not isinstance(memory, EvidenceMemory):
            raise TypeError("TwinEvidenceService requires canonical EvidenceMemory")
        self.memory = memory
        self.store: LineageStore = memory.store

    @staticmethod
    def _canonical_value(value: Any) -> str:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    def _scopes(
        self, song_id: str, version_id: str | None
    ) -> tuple[tuple[str, str], ...]:
        song = self.store.get_song(song_id)
        if song is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        scopes: list[tuple[str, str]] = []
        if version_id is not None:
            version = self.store.get_version(version_id)
            if version is None:
                raise NotFoundError(f"version not found: {version_id}")
            if version.song_id != song_id:
                raise ValidationError("version belongs to a different Song")
            scopes.append(("VERSION", version_id))
        scopes.extend(
            [
                ("SONG", song_id),
                ("ARTIST", song.artist_id),
                ("PROFILE", self.store.profile_id),
            ]
        )
        return tuple(scopes)

    def for_song(
        self, *, song_id: str, version_id: str | None = None
    ) -> SongTwinView:
        scopes = self._scopes(song_id, version_id)

        selected: dict[
            str, dict[str, tuple[EvidenceClaim, ...]]
        ] = {domain: {} for domain in _VISIBLE_TWIN_DOMAINS}

        for scope_kind, scope_id in scopes:
            scope_claims = self.memory.active_claims_for_scope(
                scope_kind, scope_id
            )
            grouped: dict[str, dict[str, list[EvidenceClaim]]] = {
                domain: {} for domain in _VISIBLE_TWIN_DOMAINS
            }
            for claim in scope_claims:
                grouped[claim.twin_domain].setdefault(claim.key, []).append(
                    claim
                )

            for domain in _VISIBLE_TWIN_DOMAINS:
                for key, claims in grouped[domain].items():
                    if key not in selected[domain]:
                        selected[domain][key] = tuple(
                            sorted(claims, key=lambda claim: claim.sequence)
                        )

        technical = tuple(
            claim
            for key in sorted(selected["TECHNICAL"])
            for claim in selected["TECHNICAL"][key]
        )
        creative = tuple(
            claim
            for key in sorted(selected["CREATIVE"])
            for claim in selected["CREATIVE"][key]
        )
        unspecified = tuple(
            claim
            for key in sorted(selected["UNSPECIFIED"])
            for claim in selected["UNSPECIFIED"][key]
        )

        conflicts: list[TwinConflict] = []
        for key in sorted(
            set(selected["TECHNICAL"]) & set(selected["CREATIVE"])
        ):
            technical_claims = selected["TECHNICAL"][key]
            creative_claims = selected["CREATIVE"][key]
            values = {
                self._canonical_value(claim.value)
                for claim in technical_claims + creative_claims
            }
            if len(values) > 1:
                conflicts.append(
                    TwinConflict(
                        key=key,
                        technical_claims=technical_claims,
                        creative_claims=creative_claims,
                    )
                )

        return SongTwinView(
            song_id=song_id,
            version_id=version_id,
            technical_claims=technical,
            creative_claims=creative,
            unspecified_claims=unspecified,
            conflicts=tuple(conflicts),
        )


class TwinAwareSongKnowledgeMapService:
    """Read-only decorator that adds Twin domain to evidence graph nodes."""

    def __init__(
        self,
        base: SongKnowledgeMapService,
        memory: EvidenceMemory,
    ):
        if not isinstance(base, SongKnowledgeMapService):
            raise TypeError(
                "TwinAwareSongKnowledgeMapService requires SongKnowledgeMapService"
            )
        if not isinstance(memory, EvidenceMemory):
            raise TypeError(
                "TwinAwareSongKnowledgeMapService requires EvidenceMemory"
            )
        self.base = base
        self.memory = memory

    def for_song(self, song_id: str) -> SongKnowledgeMap:
        graph = self.base.for_song(song_id)
        nodes: list[GraphNode] = []
        for node in graph.nodes:
            if node.kind != "EVIDENCE_CLAIM":
                nodes.append(node)
                continue
            claim = self.memory.get_claim(node.id)
            if claim is None:
                raise NotFoundError(
                    f"evidence claim disappeared while projecting Song map: {node.id}"
                )
            data = dict(node.data)
            data["twin_domain"] = claim.twin_domain
            nodes.append(
                GraphNode(
                    kind=node.kind,
                    id=node.id,
                    data_json=json.dumps(
                        data,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
            )
        return SongKnowledgeMap(
            song_id=graph.song_id,
            nodes=tuple(nodes),
            edges=graph.edges,
        )
