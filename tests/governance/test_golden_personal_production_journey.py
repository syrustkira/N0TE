import hashlib

from n0te import HeadquartersMemory, SongResumeService


def test_golden_personal_production_journey_with_real_local_capability(tmp_path):
    hq = HeadquartersMemory.create(tmp_path, "TellMeN0TE")
    profile_id = hq.store.profile_id
    song = hq.store.create_song("Golden Journey")

    hq.evidence.record_claim(
        scope_kind="SONG",
        scope_id=song.id,
        key="current.intent",
        value="capture, audition, and keep a chorus revision",
        source_kind="USER_DECLARED",
        twin_domain="CREATIVE",
    )
    hq.evidence.record_claim(
        scope_kind="SONG",
        scope_id=song.id,
        key="relevant.context",
        value={"section": "chorus", "goal": "stronger lift"},
        source_kind="USER_DECLARED",
        twin_domain="CREATIVE",
    )
    hq.evidence.record_claim(
        scope_kind="SONG",
        scope_id=song.id,
        key="reasoning.plan",
        value="capture one bounded local revision, audition it, then keep or revise",
        source_kind="INFERRED",
        source_ref="golden-journey-plan",
    )
    hq.evidence.record_claim(
        scope_kind="SONG",
        scope_id=song.id,
        key="authority.decision",
        value="AUTHORIZE_LOCAL_CAPTURE_AND_AUDITION",
        source_kind="USER_DECLARED",
        source_ref="artist-authority",
    )
    hq.evidence.record_claim(
        scope_kind="SONG",
        scope_id=song.id,
        key="capability.route",
        value="LOCAL_FILESYSTEM_CAPTURE",
        source_kind="OBSERVED",
        source_ref="normal-local-route",
        twin_domain="TECHNICAL",
    )

    capture_dir = tmp_path / "observed-capture"
    capture_dir.mkdir()
    render = capture_dir / "chorus-revision.wav"
    payload = b"N0TE_GOLDEN_JOURNEY_OBSERVED_AUDIO_PLACEHOLDER\x00\x01\x02"
    render.write_bytes(payload)
    digest = hashlib.sha256(render.read_bytes()).hexdigest()
    assert render.is_file()
    assert render.stat().st_size == len(payload)

    hq.evidence.record_claim(
        scope_kind="SONG",
        scope_id=song.id,
        key="capability.observed_result",
        value={"path": str(render), "sha256": digest, "bytes": len(payload)},
        source_kind="OBSERVED",
        source_ref="local-filesystem-result",
        twin_domain="TECHNICAL",
    )
    asset = hq.store.attach_asset(
        song.id,
        name=render.name,
        sha256=digest,
        source_uri=render.as_uri(),
    )
    version = hq.store.create_version(
        song.id,
        label="chorus revision",
        asset_ids=[asset.id],
    )
    hq.evidence.record_claim(
        scope_kind="VERSION",
        scope_id=version.id,
        key="audition.judgment",
        value="KEEP",
        source_kind="USER_DECLARED",
        source_ref="artist-audition",
        twin_domain="CREATIVE",
    )
    hq.store.approve_version(song.id, version.id)
    hq.evidence.record_claim(
        scope_kind="VERSION",
        scope_id=version.id,
        key="next.action",
        value="resume from kept chorus revision",
        source_kind="USER_DECLARED",
        source_ref="golden-journey-receipt",
    )
    hq.store.select_song(song.id)
    hq.close()

    relaunched = HeadquartersMemory.open(tmp_path, profile_id)
    try:
        brief = SongResumeService(relaunched).brief()
        assert brief.artist_name == "TellMeN0TE"
        assert brief.song_title == "Golden Journey"
        assert brief.current_version.id == version.id
        assert brief.approved_version.id == version.id
        assert brief.next_action == "resume from kept chorus revision"
        observed = relaunched.evidence.active_claims(
            "SONG", song.id, "capability.observed_result"
        )
        assert observed[-1].value["sha256"] == digest
        judgment = relaunched.evidence.active_claims(
            "VERSION", version.id, "audition.judgment"
        )
        assert judgment[-1].value == "KEEP"
    finally:
        relaunched.close()
