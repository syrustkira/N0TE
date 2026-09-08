from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from n0te.memory import HeadquartersMemory
from n0te.person_identity import PersonIdentityMemory


def test_database_guard_blocks_second_active_review_path(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Concurrent Identity Artist")
    try:
        first = hq.people.create_person("Jordan One")
        second = hq.people.create_person("Jordan Two")
        identities = PersonIdentityMemory(hq.store, hq.people)
        external = identities.record_external_identity(
            realm="contacts",
            namespace="contacts.email",
            subject="jordan@example.com",
            source_kind="OBSERVED",
            source_ref="contacts-import:jordan@example.com",
        )
        active = identities.propose_link(external.id, first.id)
        assert active.state == "REVIEW_REQUIRED"

        # Bypass the service pre-check deliberately. The database must still
        # protect the single-active-review invariant so two concurrent callers
        # cannot both claim the same external identity for different People.
        with pytest.raises(sqlite3.IntegrityError, match="already has an active review"):
            with hq.store._tx():
                hq.store._conn.execute(
                    "INSERT INTO person_identity_reviews("
                    "id,artist_id,external_identity_id,person_id) VALUES(?,?,?,?)",
                    (
                        "idreview_concurrent_forged",
                        hq.store.primary_artist_id,
                        external.id,
                        second.id,
                    ),
                )

        assert identities.current_resolution(external.id) == active
        assert len(identities.reviews_for_identity(external.id)) == 1
    finally:
        hq.close()
