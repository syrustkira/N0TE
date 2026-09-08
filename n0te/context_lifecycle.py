from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .lineage import ValidationError
from .retention import RETENTION_SECTIONS, SongRetentionService

CONTEXT_POLICY_VERSION = "CTX-LIFECYCLE-001"
DEFAULT_MAX_ITEMS_PER_SECTION = 12

_SECTION_OUTPUT_KEYS = {
    "DURABLE_FACTS": "durable_facts",
    "IMPORTED_CONTEXT": "imported_context",
    "SESSIONS": "sessions",
    "LEARNING": "learning",
    "SUCCESS": "success_patterns",
    "FRICTION": "friction",
    "SKILLS": "skills",
    "ACTIVITY": "activity",
}

_SOURCE_MANIFEST = {
    "DURABLE_FACTS": ("EvidenceMemory", "active supersedable claims"),
    "IMPORTED_CONTEXT": ("ContextIsolationService", "evidence-only imported context"),
    "SESSIONS": ("SessionMemory", "canonical session history"),
    "LEARNING": ("LearningMemory", "explicit change/observation/decision history"),
    "SUCCESS": ("SuccessMemory", "association-only outcome patterns"),
    "FRICTION": ("FrictionMemory", "explicit friction observations"),
    "SKILLS": ("SkillMemory", "latest explicit skill state"),
    "ACTIVITY": ("ActivityLog", "canonical activity chronology"),
}

_SOURCE_RANK = {
    "provider-verified": 6,
    "measured": 5,
    "observed in real work": 4,
    "artist-reported": 3,
    "remembered": 2,
    "inferred": 1,
}


@dataclass(frozen=True)
class ContextBudget:
    max_items_per_section: int = DEFAULT_MAX_ITEMS_PER_SECTION

    def __post_init__(self) -> None:
        if isinstance(self.max_items_per_section, bool) or not isinstance(
            self.max_items_per_section, int
        ):
            raise TypeError("max_items_per_section must be an integer")
        if self.max_items_per_section <= 0:
            raise ValidationError("max_items_per_section must be positive")


class ContextProjectionService:
    """Create bounded, disposable context views over canonical Song retention.

    A projection never owns persistence. The canonical retention packet is hashed
    before budgeting so a consumer can detect when a flattened view no longer
    represents the same source state. Truncation removes material only from the
    active projection, never from canonical memory.
    """

    def __init__(self, retention: SongRetentionService):
        if not isinstance(retention, SongRetentionService):
            raise TypeError("ContextProjectionService requires SongRetentionService")
        self.retention = retention

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        )

    @classmethod
    def _digest(cls, value: object) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_sections(sections: Iterable[str] | None) -> tuple[str, ...]:
        if sections is None:
            return tuple(sorted(RETENTION_SECTIONS))
        normalized = tuple(dict.fromkeys(str(item).strip().upper() for item in sections))
        invalid = [item for item in normalized if item not in RETENTION_SECTIONS]
        if invalid:
            raise ValidationError(
                f"unsupported context projection sections: {', '.join(invalid)}"
            )
        return normalized

    @staticmethod
    def _source_score(item: Mapping[str, Any]) -> int:
        return _SOURCE_RANK.get(str(item.get("source") or "").lower(), 0)

    @classmethod
    def _rank_section(cls, section: str, items: list[Any]) -> list[Any]:
        if section == "DURABLE_FACTS":
            return sorted(
                items,
                key=lambda item: (
                    -cls._source_score(item),
                    -float(item.get("confidence") or 0.0),
                    str(item.get("scope") or ""),
                    str(item.get("key") or ""),
                ),
            )
        if section == "FRICTION":
            return sorted(
                items,
                key=lambda item: (
                    -int(item.get("recurring_session_count") or 0),
                    -float(item.get("confidence") or 0.0),
                    str(item.get("key") or ""),
                ),
            )
        if section == "SKILLS":
            return sorted(
                items,
                key=lambda item: (
                    -float(item.get("confidence") or 0.0),
                    str(item.get("skill") or ""),
                ),
            )
        if section == "LEARNING":
            # Unresolved episodes are retained first, then the newest resolved work.
            unresolved = [item for item in items if item.get("decision") is None]
            resolved = [item for item in items if item.get("decision") is not None]
            unresolved.sort(key=lambda item: int(item.get("sequence") or 0), reverse=True)
            resolved.sort(key=lambda item: int(item.get("sequence") or 0), reverse=True)
            return unresolved + resolved
        if section in {"SESSIONS", "ACTIVITY"}:
            return sorted(
                items,
                key=lambda item: int(item.get("sequence") or 0),
                reverse=True,
            )
        return list(items)

    @classmethod
    def _bounded_items(
        cls,
        section: str,
        items: list[Any],
        limit: int,
    ) -> tuple[list[Any], int]:
        ranked = cls._rank_section(section, items)
        selected = ranked[:limit]
        omitted = max(0, len(ranked) - len(selected))
        if section in {"SESSIONS", "LEARNING", "ACTIVITY"}:
            selected = list(reversed(selected))
        return selected, omitted

    @staticmethod
    def _durable_contradictions(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
        seen: dict[tuple[str, str], Any] = {}
        conflicts: list[dict[str, Any]] = []
        for fact in packet.get("durable_facts", []):
            key = (str(fact.get("scope")), str(fact.get("key")))
            value = fact.get("value")
            if key in seen and seen[key] != value:
                conflicts.append(
                    {
                        "kind": "DURABLE_FACT_CONFLICT",
                        "scope": key[0],
                        "key": key[1],
                        "status": "UNRESOLVED",
                        "blocks_autonomous_mutation": True,
                    }
                )
            else:
                seen[key] = value
        return conflicts

    def projection_for_song(
        self,
        song_id: str,
        *,
        purpose: str,
        sections: Iterable[str] | None = None,
        budget: ContextBudget | None = None,
    ) -> dict[str, Any]:
        purpose_text = str(purpose).strip()
        if not purpose_text:
            raise ValidationError("context projection purpose must not be empty")
        selected = self._normalize_sections(sections)
        budget = budget or ContextBudget()

        before = self.retention.store._conn.total_changes
        canonical = self.retention.context_packet_for_song(song_id, sections=selected)
        if "DURABLE_FACTS" in selected:
            contradiction_source = canonical
        else:
            # Contradiction safety is authority metadata, not optional display
            # content. A narrow projection may omit durable-fact bodies while it
            # must still know whether conflicting active durable truth blocks
            # autonomous mutation.
            contradiction_source = self.retention.context_packet_for_song(
                song_id,
                sections=("DURABLE_FACTS",),
            )
        after = self.retention.store._conn.total_changes
        if before != after:
            raise RuntimeError("context projection source read unexpectedly mutated canonical memory")

        source_digest = self._digest(
            {
                "selected": canonical,
                "contradiction_source": contradiction_source,
            }
        )
        projected_context: dict[str, Any] = {
            "schema": canonical["schema"],
            "song": canonical["song"],
            "retention_policy": canonical["retention_policy"],
        }
        truncated: dict[str, int] = {}
        for section in selected:
            output_key = _SECTION_OUTPUT_KEYS[section]
            value = canonical.get(output_key)
            if isinstance(value, list):
                bounded, omitted = self._bounded_items(
                    section,
                    value,
                    budget.max_items_per_section,
                )
                projected_context[output_key] = bounded
                if omitted:
                    truncated[section] = omitted
            elif value is not None:
                projected_context[output_key] = value

        contradictions = self._durable_contradictions(contradiction_source)
        source_manifest = [
            {
                "section": section,
                "canonical_surface": _SOURCE_MANIFEST[section][0],
                "source_semantics": _SOURCE_MANIFEST[section][1],
            }
            for section in selected
        ]
        projection = {
            "schema": "n0te.context-projection.v1",
            "state": "ACTIVE",
            "scope": {
                "kind": "SONG",
                "title": canonical["song"]["title"],
            },
            "purpose": purpose_text,
            "policy_version": CONTEXT_POLICY_VERSION,
            "authority_ceiling": "READ_ONLY_CONTEXT",
            "source_manifest": source_manifest,
            "source_digest": source_digest,
            "lossiness": "BOUNDED_PROJECTION" if truncated else "LOSSLESS_SELECTION",
            "selected_sections": list(selected),
            "budget": {
                "max_items_per_section": budget.max_items_per_section,
                "truncated_sections": truncated,
                "canonical_history_deleted": False,
            },
            "retrieval_order": [
                "AUTHORITY",
                "CURRENT_SCOPE",
                "UNRESOLVED_STATUS",
                "EVIDENCE_CONFIDENCE",
                "RECENCY",
            ],
            "contradictions": contradictions,
            "mutation_policy": {
                "grants_action_authority": False,
                "automatic_durable_promotion": False,
                "critical_contradiction_blocks_autonomous_mutation": bool(contradictions),
            },
            "context": projected_context,
        }
        json.dumps(projection, sort_keys=True, allow_nan=False)
        return projection
