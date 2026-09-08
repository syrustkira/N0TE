import tempfile
import unittest
from pathlib import Path

from n0te import (
    EvidenceMemory,
    LineageCorruptionError,
    LineageStore,
    ValidationError,
)


class Core01BScopedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = LineageStore.create(self.root, "Artist")
        self.memory = EvidenceMemory(self.store)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_song_scoped_claim_does_not_leak_to_another_song(self):
        song_a = self.store.create_song("A")
        song_b = self.store.create_song("B")
        self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song_a.id,
            key="chorus.note",
            value="needs more lift",
            source_kind="USER_DECLARED",
        )

        a = self.memory.resolve_for_song(song_id=song_a.id, key="chorus.note")
        b = self.memory.resolve_for_song(song_id=song_b.id, key="chorus.note")
        self.assertEqual(a.status, "RESOLVED")
        self.assertEqual(a.value, "needs more lift")
        self.assertEqual(a.scope_kind, "SONG")
        self.assertEqual(b.status, "UNKNOWN")

    def test_more_specific_song_claim_precedes_artist_fallback(self):
        song_a = self.store.create_song("A")
        song_b = self.store.create_song("B")
        self.memory.record_claim(
            scope_kind="ARTIST",
            scope_id=self.store.primary_artist_id,
            key="creative.energy",
            value="restrained",
            source_kind="USER_DECLARED",
        )
        self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song_a.id,
            key="creative.energy",
            value="explosive",
            source_kind="USER_DECLARED",
        )

        a = self.memory.resolve_for_song(song_id=song_a.id, key="creative.energy")
        b = self.memory.resolve_for_song(song_id=song_b.id, key="creative.energy")
        self.assertEqual((a.scope_kind, a.value), ("SONG", "explosive"))
        self.assertEqual((b.scope_kind, b.value), ("ARTIST", "restrained"))

    def test_version_specific_evidence_applies_only_to_that_version(self):
        song = self.store.create_song("Song")
        v1 = self.store.create_version(song.id, label="v1")
        v2 = self.store.create_version(song.id, label="v2", parent_version_id=v1.id)
        self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="arrangement.focus",
            value="chorus",
            source_kind="USER_DECLARED",
        )
        self.memory.record_claim(
            scope_kind="VERSION",
            scope_id=v2.id,
            key="arrangement.focus",
            value="bridge",
            source_kind="OBSERVED",
            source_ref="session:take-2",
            confidence=0.9,
        )

        base = self.memory.resolve_for_song(song_id=song.id, key="arrangement.focus")
        on_v1 = self.memory.resolve_for_song(song_id=song.id, version_id=v1.id, key="arrangement.focus")
        on_v2 = self.memory.resolve_for_song(song_id=song.id, version_id=v2.id, key="arrangement.focus")
        self.assertEqual(base.value, "chorus")
        self.assertEqual(on_v1.value, "chorus")
        self.assertEqual((on_v2.scope_kind, on_v2.value), ("VERSION", "bridge"))

    def test_contradictory_same_scope_claims_surface_conflict_without_winner(self):
        song = self.store.create_song("Song")
        first = self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="chorus.energy",
            value="needs lift",
            source_kind="USER_DECLARED",
        )
        second = self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="chorus.energy",
            value="already right",
            source_kind="INFERRED",
            confidence=0.6,
        )

        result = self.memory.resolve_for_song(song_id=song.id, key="chorus.energy")
        self.assertEqual(result.status, "CONFLICT")
        self.assertIsNone(result.value)
        self.assertEqual(set(result.claim_ids), {first.id, second.id})

    def test_explicit_reconciliation_supersedes_but_preserves_old_evidence(self):
        song = self.store.create_song("Song")
        first = self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="chorus.energy",
            value="needs lift",
            source_kind="USER_DECLARED",
        )
        second = self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="chorus.energy",
            value="already right",
            source_kind="INFERRED",
            confidence=0.6,
        )
        reconciled = self.memory.reconcile_for_song(
            song_id=song.id,
            key="chorus.energy",
            value="needs lift",
            source_kind="USER_DECLARED",
            source_ref="artist-confirmation",
        )

        result = self.memory.resolve_for_song(song_id=song.id, key="chorus.energy")
        self.assertEqual(result.status, "RESOLVED")
        self.assertEqual(result.value, "needs lift")
        self.assertEqual(result.claim_ids, (reconciled.id,))
        self.assertIsNotNone(self.memory.get_claim(first.id))
        self.assertIsNotNone(self.memory.get_claim(second.id))
        self.assertEqual(self.memory.get_claim(reconciled.id).source_ref, "artist-confirmation")

    def test_same_value_from_multiple_sources_resolves_with_all_evidence(self):
        song = self.store.create_song("Song")
        self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="key.center",
            value="F minor",
            source_kind="MEASURED",
            source_ref="analysis:1",
            confidence=0.82,
        )
        self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="key.center",
            value="F minor",
            source_kind="USER_DECLARED",
        )

        result = self.memory.resolve_for_song(song_id=song.id, key="key.center")
        self.assertEqual(result.status, "RESOLVED")
        self.assertEqual(result.value, "F minor")
        self.assertEqual(len(result.claims), 2)

    def test_provenance_and_confidence_survive_restart(self):
        song = self.store.create_song("Song")
        claim = self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="low.end",
            value={"assessment": "heavy", "band_hz": [60, 120]},
            source_kind="MEASURED",
            source_ref="analysis:abc",
            confidence=0.73,
        )
        profile_id = self.store.profile_id
        self.store.close()

        reopened = LineageStore.open(self.root, profile_id)
        self.store = reopened
        self.memory = EvidenceMemory(reopened)
        restored = self.memory.get_claim(claim.id)
        self.assertEqual(restored.source_kind, "MEASURED")
        self.assertEqual(restored.source_ref, "analysis:abc")
        self.assertAlmostEqual(restored.confidence, 0.73)
        self.assertEqual(restored.value["assessment"], "heavy")

    def test_supersession_cannot_cross_scope_or_key(self):
        song_a = self.store.create_song("A")
        song_b = self.store.create_song("B")
        claim = self.memory.record_claim(
            scope_kind="SONG",
            scope_id=song_a.id,
            key="tempo.feel",
            value="laid back",
            source_kind="USER_DECLARED",
        )
        with self.assertRaises(ValidationError):
            self.memory.record_claim(
                scope_kind="SONG",
                scope_id=song_b.id,
                key="tempo.feel",
                value="urgent",
                source_kind="USER_DECLARED",
                supersedes=[claim.id],
            )
        with self.assertRaises(ValidationError):
            self.memory.record_claim(
                scope_kind="SONG",
                scope_id=song_a.id,
                key="different.key",
                value="x",
                source_kind="USER_DECLARED",
                supersedes=[claim.id],
            )

    def test_tampered_scope_reference_fails_visibly_after_restart(self):
        self.memory  # ensure evidence schema exists
        self.store._conn.execute(
            "INSERT INTO evidence_claims(id, scope_kind, scope_id, key, value_json, source_kind, source_ref, confidence) "
            "VALUES(?, 'SONG', ?, 'tampered', '\"bad\"', 'REMEMBERED', NULL, 1.0)",
            ("claim_" + "1" * 32, "song_" + "9" * 32),
        )
        self.store._conn.commit()
        profile_id = self.store.profile_id
        self.store.close()

        reopened = LineageStore.open(self.root, profile_id)
        self.store = reopened
        with self.assertRaises(LineageCorruptionError):
            EvidenceMemory(reopened)


if __name__ == "__main__":
    unittest.main()
