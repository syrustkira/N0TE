from __future__ import annotations

from pathlib import Path

from n0te import HeadquartersMemory
from n0te.activity_timeline import SongActivityTimeline


def test_song_bound_skill_assessment_has_artist_readable_activity(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path, "Artist")
    try:
        song = hq.store.create_song("Song")
        session = hq.sessions.start_session(song_id=song.id, objective="Practice compression")
        hq.sessions.close_session(
            session.id,
            debrief_summary="Completed the exercise",
            next_action="Repeat without prompts",
        )
        evidence = hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=song.id,
            key="skill.compression",
            value="completed",
            source_kind="OBSERVED",
        )
        hq.skills.record_assessment(
            skill_id="Compression",
            level="PRACTICED",
            source_kind="N0TE_ASSESSED",
            source_ref="test:closed-session",
            confidence=0.8,
            assistance_level=0.5,
            session_id=session.id,
            evidence_claim_ids=(evidence.id,),
        )

        timeline = SongActivityTimeline(hq.store, hq.activity).for_song(song.id)
        skill_items = [item for item in timeline if item.summary == "Skill assessment recorded"]
        assert len(skill_items) == 1
        assert skill_items[0].detail is None
        assert all("skillassess_" not in item.summary for item in timeline)
    finally:
        hq.close()
