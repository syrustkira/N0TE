from n0te import HeadquartersMemory, SongResumeService


def test_artist_song_intent_receipt_quit_relaunch_resume_loop(tmp_path):
    hq = HeadquartersMemory.create(tmp_path, "TellMeN0TE")
    profile = hq.store.profile_id
    song = hq.store.create_song("Clean Journey")
    asset = hq.store.attach_asset(song.id, name="idea.wav", sha256="a" * 64)
    v1 = hq.store.create_version(song.id, label="captured idea", asset_ids=[asset.id])
    hq.store.approve_version(song.id, v1.id)
    v2 = hq.store.create_version(song.id, label="revision", parent_version_id=v1.id, asset_ids=[asset.id])
    hq.evidence.record_claim(scope_kind="SONG", scope_id=song.id, key="current.intent", value="evaluate chorus revision", source_kind="USER_DECLARED")
    hq.evidence.record_claim(scope_kind="VERSION", scope_id=v2.id, key="next.action", value="audition chorus and keep or revise", source_kind="USER_DECLARED", source_ref="journey-checkpoint")
    hq.evidence.record_claim(scope_kind="VERSION", scope_id=v2.id, key="authority.decision", value="KEEP_PENDING_AUDITION", source_kind="USER_DECLARED")
    hq.store.select_song(song.id)
    hq.close()

    relaunched = HeadquartersMemory.open(tmp_path, profile)
    try:
        brief = SongResumeService(relaunched).brief()
        assert brief.artist_name == "TellMeN0TE"
        assert brief.song_title == "Clean Journey"
        assert brief.current_version.id == v2.id
        assert brief.approved_version.id == v1.id
        assert brief.next_action == "audition chorus and keep or revise"
        assert brief.next_action_evidence[0].source_ref == "journey-checkpoint"
    finally:
        relaunched.close()
