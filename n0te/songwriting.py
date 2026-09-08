from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass

from .evidence import EvidenceClaim
from .lineage import LineageStore, NotFoundError, ValidationError
from .session import SessionItem, SessionMemory, SongSession

SONGWRITING_ASPECTS = (
    "LYRICS",
    "TOPLINE",
    "MELODY",
    "PHRASING",
    "TAKE_COMP",
    "LYRIC_ALIGNMENT",
    "PITCH_TIMING",
    "DOUBLES",
    "HARMONIES",
    "AD_LIBS",
    "PERFORMANCE",
    "VOCAL_PRODUCTION",
)
SONGWRITING_ENTRY_KINDS = (
    "MARK",
    "OBSERVATION",
    "DECISION",
    "REJECTED_IDEA",
    "UNRESOLVED",
)
SONGWRITING_SOURCE_KIND = "USER_DECLARED"
MAX_SONGWRITING_TEXT_CHARS = 12_000
MAX_SONGWRITING_SECTION_CHARS = 160
# The shared consumer shell accepts at most 32,768 URL-encoded bytes. 2,400
# four-byte UTF-8 characters plus the maximal section and action metadata remain
# safely below that transport ceiling.
MAX_SONGWRITING_SURFACE_TEXT_CHARS = 2_400

_ENTRY_MARKER = "\n\n[N0TE-SONGWRITE/1] "
_KEY_PART = re.compile(r"[^a-z0-9]+")


class SongwritingCaseHistoryError(RuntimeError):
    """A Song-bound writing case-history operation cannot proceed truthfully."""


class SongwritingCaseHistoryIntegrityError(SongwritingCaseHistoryError):
    """N0TE-owned writing scratch no longer matches its canonical encoding."""


class StaleSongwritingCaseHistoryError(SongwritingCaseHistoryError):
    """A rendered songwriting action no longer matches canonical live state."""


@dataclass(frozen=True)
class SongwritingEntry:
    item_id: str
    sequence: int
    song_id: str
    session_id: str
    version_id: str | None
    session_state: str
    session_objective: str
    aspect: str
    kind: str
    section: str | None
    text: str
    promoted_claim_id: str | None
    source_kind: str = SONGWRITING_SOURCE_KIND
    provider_used: bool = False
    host_mutated: bool = False
    action_authority_granted: bool = False

    @property
    def promoted(self) -> bool:
        return self.promoted_claim_id is not None


@dataclass(frozen=True)
class SongwritingPromotion:
    entry: SongwritingEntry
    claim: EvidenceClaim
    semantic_key: str


class SongwritingCaseHistoryService:
    """Durable Songwriting/Vocal case history built on canonical Session memory.

    The service deliberately does not create a second lyrics database. Drafts,
    observations, unresolved questions, rejected ideas and decisions remain
    Session scratch until the artist explicitly promotes a DECISION. The
    Session already owns exact Artist/Song/Version binding, append-only history
    and promotion provenance, so this layer adds writing semantics without
    weakening those boundaries.

    Render-bound consumer mutations use the LineageStore BEGIN IMMEDIATE
    transaction across both freshness validation and the write. That keeps the
    displayed Song/Version/Session contract exact even with another Headquarters
    process writing the same profile concurrently.

    This is case history, not pitch correction, transcription, comp execution,
    voice cloning, provider generation, or a claim that N0TE heard audio.
    """

    def __init__(self, store: LineageStore, sessions: SessionMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("SongwritingCaseHistoryService requires LineageStore")
        if not isinstance(sessions, SessionMemory) or sessions.store is not store:
            raise TypeError(
                "SongwritingCaseHistoryService requires SessionMemory for the same LineageStore"
            )
        self.store = store
        self.sessions = sessions
        self._conn = store._conn

    @staticmethod
    def normalize_aspect(value: str) -> str:
        aspect = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        if aspect not in SONGWRITING_ASPECTS:
            raise ValidationError(f"unsupported songwriting aspect: {aspect}")
        return aspect

    @staticmethod
    def normalize_kind(value: str) -> str:
        kind = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        if kind not in SONGWRITING_ENTRY_KINDS:
            raise ValidationError(f"unsupported songwriting entry kind: {kind}")
        return kind

    @staticmethod
    def _clean_text(value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("songwriting text must be text")
        text = value.strip()
        if not text:
            raise ValidationError("songwriting text must not be empty")
        if len(text) > MAX_SONGWRITING_TEXT_CHARS:
            raise ValidationError("songwriting text exceeds the local case-history limit")
        return text

    @staticmethod
    def _clean_section(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError("songwriting section must be text")
        section = " ".join(value.split()).strip()
        if not section:
            return None
        if len(section) > MAX_SONGWRITING_SECTION_CHARS:
            raise ValidationError("songwriting section label is too long")
        return section

    @classmethod
    def _encode(cls, *, aspect: str, section: str | None, text: str) -> str:
        metadata = json.dumps(
            {"aspect": aspect, "section": section},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return text + _ENTRY_MARKER + metadata

    @classmethod
    def _decode(cls, body: str) -> tuple[str, str | None, str] | None:
        if _ENTRY_MARKER not in body:
            return None
        text, marker, metadata = body.rpartition(_ENTRY_MARKER)
        if not marker:
            return None
        try:
            payload = json.loads(metadata)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SongwritingCaseHistoryIntegrityError(
                "Songwriting case-history metadata is unreadable"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {"aspect", "section"}:
            raise SongwritingCaseHistoryIntegrityError(
                "Songwriting case-history metadata has an invalid shape"
            )
        try:
            aspect = cls.normalize_aspect(payload["aspect"])
            section = cls._clean_section(payload["section"])
            clean_text = cls._clean_text(text)
        except ValidationError as exc:
            raise SongwritingCaseHistoryIntegrityError(
                "Songwriting case-history metadata is not canonical"
            ) from exc
        if aspect != payload["aspect"] or section != payload["section"] or clean_text != text:
            raise SongwritingCaseHistoryIntegrityError(
                "Songwriting case-history encoding changed after capture"
            )
        return aspect, section, clean_text

    @classmethod
    def presentation_text(cls, body: str) -> str | None:
        """Return artist-authored text for one valid N0TE songwriting envelope."""
        decoded = cls._decode(body)
        return None if decoded is None else decoded[2]

    def presentation_replacements_for_song(
        self, song_id: str
    ) -> tuple[tuple[str, str | None], ...]:
        """Map owned storage envelopes to safe consumer text.

        Malformed owned envelopes return a None presentation so the shell can
        hide the storage payload while its normal integrity path reports
        recovery instead of leaking internal metadata.
        """
        song = self.store.get_song(str(song_id))
        if song is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        rows = self._conn.execute(
            "SELECT i.body FROM session_items i "
            "JOIN sessions s ON s.id=i.session_id "
            "WHERE s.song_id=? ORDER BY i.seq",
            (song.id,),
        ).fetchall()
        replacements: list[tuple[str, str | None]] = []
        for row in rows:
            body = str(row["body"])
            if _ENTRY_MARKER not in body:
                continue
            try:
                visible = self.presentation_text(body)
            except SongwritingCaseHistoryIntegrityError:
                visible = None
            replacements.append((body, visible))
        return tuple(replacements)

    def _item(self, item_id: str) -> SessionItem:
        row = self._conn.execute(
            "SELECT seq,id,session_id,kind,body FROM session_items WHERE id=?",
            (str(item_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Session item not found in profile {self.store.profile_id}: {item_id}"
            )
        return SessionItem(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            kind=str(row["kind"]),
            body=str(row["body"]),
        )

    def _entry(self, item: SessionItem, session: SongSession) -> SongwritingEntry | None:
        if item.session_id != session.id:
            raise SongwritingCaseHistoryIntegrityError(
                "Songwriting item crossed its Session binding"
            )
        decoded = self._decode(item.body)
        if decoded is None:
            return None
        aspect, section, text = decoded
        promotion = self.sessions.promotion_for_item(item.id)
        return SongwritingEntry(
            item_id=item.id,
            sequence=item.sequence,
            song_id=session.song_id,
            session_id=session.id,
            version_id=session.version_id,
            session_state=session.state,
            session_objective=session.objective,
            aspect=aspect,
            kind=item.kind,
            section=section,
            text=text,
            promoted_claim_id=None if promotion is None else promotion.claim_id,
        )

    def _insert_item_locked(
        self,
        session: SongSession,
        *,
        kind: str,
        body: str,
    ) -> SessionItem:
        item_id = f"sitem_{uuid.uuid4().hex}"
        self._conn.execute(
            "INSERT INTO session_items(id,session_id,kind,body) VALUES(?,?,?,?)",
            (item_id, session.id, kind, body),
        )
        return self._item(item_id)

    def capture(
        self,
        *,
        song_id: str,
        session_id: str,
        aspect: str,
        text: str,
        section: str | None = None,
        kind: str = "MARK",
    ) -> SongwritingEntry:
        aspect = self.normalize_aspect(aspect)
        kind = self.normalize_kind(kind)
        section = self._clean_section(section)
        text = self._clean_text(text)
        body = self._encode(aspect=aspect, section=section, text=text)

        try:
            with self.store._tx():
                song = self.store.get_song(str(song_id))
                if song is None:
                    raise NotFoundError(
                        f"Song not found in profile {self.store.profile_id}: {song_id}"
                    )
                session = self.sessions.get_session(str(session_id))
                if session is None:
                    raise NotFoundError(
                        f"Session not found in profile {self.store.profile_id}: {session_id}"
                    )
                if session.song_id != song.id:
                    raise ValidationError("songwriting Session belongs to a different Song")
                if session.state != "OPEN":
                    raise ValidationError(
                        "songwriting case history can be added only to an open Session"
                    )
                item = self._insert_item_locked(session, kind=kind, body=body)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot append songwriting case history: {exc}") from exc

        current_session = self.sessions.get_session(item.session_id)
        if current_session is None:
            raise SongwritingCaseHistoryIntegrityError(
                "captured songwriting item lost its Session"
            )
        entry = self._entry(item, current_session)
        if entry is None:
            raise SongwritingCaseHistoryIntegrityError(
                "captured songwriting item could not be read back"
            )
        return entry

    def _validate_capture_binding_locked(
        self,
        *,
        song_id: str,
        session_id: str,
        expected_current_version_id: str | None,
        expected_session_version_id: str | None,
    ) -> tuple[object, SongSession]:
        song = self.store.active_song()
        if song is None or song.id != str(song_id):
            raise StaleSongwritingCaseHistoryError(
                "The active Song changed. Reload the Song before capturing this writing note."
            )
        if song.current_version_id != expected_current_version_id:
            raise StaleSongwritingCaseHistoryError(
                "The current Version changed. Reload the Song before capturing this writing note."
            )
        session = self.sessions.get_session(str(session_id))
        latest = self.sessions.latest_for_song(song.id)
        if (
            session is None
            or session.song_id != song.id
            or session.state != "OPEN"
            or session.version_id != expected_session_version_id
            or latest is None
            or latest.id != session.id
            or latest.state != "OPEN"
        ):
            raise StaleSongwritingCaseHistoryError(
                "The work Session changed. Reload the Song before capturing this writing note."
            )
        return song, session

    def capture_bound(
        self,
        *,
        song_id: str,
        session_id: str,
        expected_current_version_id: str | None,
        expected_session_version_id: str | None,
        aspect: str,
        text: str,
        section: str | None = None,
        kind: str = "MARK",
    ) -> SongwritingEntry:
        """Validate rendered Song/Version/Session state and append in one write tx."""
        aspect = self.normalize_aspect(aspect)
        kind = self.normalize_kind(kind)
        section = self._clean_section(section)
        text = self._clean_text(text)
        body = self._encode(aspect=aspect, section=section, text=text)

        try:
            with self.store._tx():
                _, session = self._validate_capture_binding_locked(
                    song_id=str(song_id),
                    session_id=str(session_id),
                    expected_current_version_id=expected_current_version_id,
                    expected_session_version_id=expected_session_version_id,
                )
                item = self._insert_item_locked(session, kind=kind, body=body)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot append songwriting case history: {exc}") from exc

        current_session = self.sessions.get_session(item.session_id)
        if current_session is None:
            raise SongwritingCaseHistoryIntegrityError(
                "captured songwriting item lost its Session"
            )
        entry = self._entry(item, current_session)
        if entry is None:
            raise SongwritingCaseHistoryIntegrityError(
                "captured songwriting item could not be read back"
            )
        return entry

    def entries_for_session(self, session_id: str) -> tuple[SongwritingEntry, ...]:
        session = self.sessions.get_session(str(session_id))
        if session is None:
            raise NotFoundError(
                f"Session not found in profile {self.store.profile_id}: {session_id}"
            )
        entries: list[SongwritingEntry] = []
        for item in self.sessions.items_for_session(session.id):
            entry = self._entry(item, session)
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    def entries_for_song(self, song_id: str) -> tuple[SongwritingEntry, ...]:
        song = self.store.get_song(str(song_id))
        if song is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        rows = self._conn.execute(
            "SELECT id FROM sessions WHERE song_id=? ORDER BY seq",
            (song.id,),
        ).fetchall()
        entries: list[SongwritingEntry] = []
        for row in rows:
            entries.extend(self.entries_for_session(str(row["id"])))
        return tuple(sorted(entries, key=lambda entry: entry.sequence))

    def entry(self, item_id: str) -> SongwritingEntry:
        item = self._item(str(item_id))
        session = self.sessions.get_session(item.session_id)
        if session is None:
            raise SongwritingCaseHistoryIntegrityError(
                "Songwriting item lost its Session"
            )
        entry = self._entry(item, session)
        if entry is None:
            raise ValidationError("Session item is not N0TE songwriting case history")
        return entry

    @staticmethod
    def semantic_key(entry: SongwritingEntry) -> str:
        if not isinstance(entry, SongwritingEntry):
            raise TypeError("entry must be SongwritingEntry")
        section = "general" if entry.section is None else entry.section.lower()
        section = _KEY_PART.sub("_", section).strip("_") or "section"
        return f"songwriting.{entry.aspect.lower()}.{section[:64]}"

    def _promotion_request_locked(
        self,
        entry: SongwritingEntry,
        *,
        scope_kind: str,
        key: str,
    ) -> tuple[str, str]:
        scope_kind = str(scope_kind).strip().upper()
        if scope_kind not in {"SONG", "VERSION"}:
            raise ValidationError(f"unsupported Session promotion scope: {scope_kind}")
        if scope_kind == "SONG":
            scope_id = entry.song_id
        else:
            if entry.version_id is None:
                raise ValidationError("Session has no bound Version to promote into")
            scope_id = entry.version_id

        item = self._item(entry.item_id)
        value_json = json.dumps(
            item.body, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        request = self._conn.execute(
            "SELECT id,source_ref,scope_kind,scope_id,key,value_json,"
            "source_kind,twin_domain,confidence "
            "FROM session_promotion_requests WHERE item_id=?",
            (entry.item_id,),
        ).fetchone()
        expected = (
            scope_kind,
            scope_id,
            key,
            value_json,
            SONGWRITING_SOURCE_KIND,
            "CREATIVE",
            1.0,
        )
        if request is None:
            request_id = f"spr_{uuid.uuid4().hex}"
            source_ref = f"session-promotion:{request_id}"
            self._conn.execute(
                "INSERT INTO session_promotion_requests("
                "id,item_id,source_ref,scope_kind,scope_id,key,value_json,"
                "source_kind,twin_domain,confidence) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    entry.item_id,
                    source_ref,
                    scope_kind,
                    scope_id,
                    key,
                    value_json,
                    SONGWRITING_SOURCE_KIND,
                    "CREATIVE",
                    1.0,
                ),
            )
            return scope_id, source_ref

        actual = (
            str(request["scope_kind"]),
            str(request["scope_id"]),
            str(request["key"]),
            str(request["value_json"]),
            str(request["source_kind"]),
            str(request["twin_domain"]),
            float(request["confidence"]),
        )
        if actual != expected:
            raise ValidationError(
                "Session item already has a different immutable promotion request"
            )
        return scope_id, str(request["source_ref"])

    def _promote_entry_locked(
        self,
        entry: SongwritingEntry,
        *,
        scope_kind: str,
    ) -> tuple[str, str]:
        if entry.kind != "DECISION":
            raise ValidationError(
                "only an explicit songwriting DECISION can become durable evidence"
            )
        if entry.promoted:
            raise ValidationError("songwriting decision is already promoted")
        key = self.semantic_key(entry)
        scope_id, source_ref = self._promotion_request_locked(
            entry, scope_kind=scope_kind, key=key
        )
        item = self._item(entry.item_id)
        value_json = json.dumps(
            item.body, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        claim_id = f"claim_{uuid.uuid4().hex}"
        self._conn.execute(
            "INSERT INTO evidence_claims("
            "id,scope_kind,scope_id,key,value_json,source_kind,source_ref,"
            "confidence,twin_domain) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                claim_id,
                str(scope_kind).strip().upper(),
                scope_id,
                key,
                value_json,
                SONGWRITING_SOURCE_KIND,
                source_ref,
                1.0,
                "CREATIVE",
            ),
        )
        linked = self._conn.execute(
            "SELECT claim_id FROM session_promotions WHERE item_id=?",
            (entry.item_id,),
        ).fetchone()
        if linked is None or str(linked["claim_id"]) != claim_id:
            raise SongwritingCaseHistoryIntegrityError(
                "Evidence promotion committed without matching Session link"
            )
        return claim_id, key

    def promote_decision(
        self,
        item_id: str,
        *,
        scope_kind: str = "SONG",
    ) -> SongwritingPromotion:
        try:
            with self.store._tx():
                entry = self.entry(item_id)
                claim_id, key = self._promote_entry_locked(
                    entry, scope_kind=scope_kind
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot promote songwriting decision: {exc}") from exc

        claim = self.sessions.evidence.get_claim(claim_id)
        promoted = self.entry(item_id)
        if claim is None or promoted.promoted_claim_id != claim.id:
            raise SongwritingCaseHistoryIntegrityError(
                "songwriting decision promotion lost its Session provenance"
            )
        return SongwritingPromotion(entry=promoted, claim=claim, semantic_key=key)

    def _validate_promotion_binding_locked(
        self,
        *,
        item_id: str,
        expected_song_id: str,
        expected_session_id: str,
        expected_entry_version_id: str | None,
        expected_current_version_id: str | None,
    ) -> tuple[object, SongwritingEntry]:
        song = self.store.active_song()
        if song is None or song.id != str(expected_song_id):
            raise StaleSongwritingCaseHistoryError(
                "The active Song changed. Reload the Song before remembering this decision."
            )
        if song.current_version_id != expected_current_version_id:
            raise StaleSongwritingCaseHistoryError(
                "The current Version changed. Reload the Song before remembering this decision."
            )
        entry = self.entry(str(item_id))
        if (
            entry.song_id != song.id
            or entry.session_id != str(expected_session_id)
            or entry.version_id != expected_entry_version_id
            or entry.kind != "DECISION"
            or entry.promoted
        ):
            raise StaleSongwritingCaseHistoryError(
                "That writing decision changed or is no longer eligible. Reload the Song and try again."
            )
        return song, entry

    def promote_decision_bound(
        self,
        item_id: str,
        *,
        expected_song_id: str,
        expected_session_id: str,
        expected_entry_version_id: str | None,
        expected_current_version_id: str | None,
        scope_kind: str = "SONG",
    ) -> SongwritingPromotion:
        """Validate rendered Song/Version/entry state and promote in one write tx."""
        try:
            with self.store._tx():
                song, entry = self._validate_promotion_binding_locked(
                    item_id=str(item_id),
                    expected_song_id=str(expected_song_id),
                    expected_session_id=str(expected_session_id),
                    expected_entry_version_id=expected_entry_version_id,
                    expected_current_version_id=expected_current_version_id,
                )
                claim_id, key = self._promote_entry_locked(
                    entry, scope_kind=scope_kind
                )
                if str(scope_kind).strip().upper() == "SONG":
                    claim_scope = self._conn.execute(
                        "SELECT scope_id FROM evidence_claims WHERE id=?",
                        (claim_id,),
                    ).fetchone()
                    if claim_scope is None or str(claim_scope["scope_id"]) != song.id:
                        raise SongwritingCaseHistoryIntegrityError(
                            "Songwriting decision promotion crossed the active Song binding"
                        )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot promote songwriting decision: {exc}") from exc

        claim = self.sessions.evidence.get_claim(claim_id)
        promoted = self.entry(item_id)
        if claim is None or promoted.promoted_claim_id != claim.id:
            raise SongwritingCaseHistoryIntegrityError(
                "songwriting decision promotion lost its Session provenance"
            )
        return SongwritingPromotion(entry=promoted, claim=claim, semantic_key=key)
