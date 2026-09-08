from __future__ import annotations

from pathlib import Path

import pytest

from n0te.credits import CreditsMemory
from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory


def test_split_core_rejects_lossy_non_integer_basis_points(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    hq = HeadquartersMemory.create(root, "Exact Split Artist")
    try:
        song = hq.store.create_song("Exact Split Song")
        person = hq.people.create_person("Writer")
        credits = CreditsMemory(hq.store, hq.people)
        sheet = credits.create_split_draft(song.id)

        for invalid in (5000.5, "5000", True):
            with pytest.raises(ValidationError, match="whole basis points"):
                credits.set_draft_allocations(sheet.id, {person.id: invalid})  # type: ignore[dict-item]
            assert credits.split_allocations(sheet.id) == ()

        exact = credits.set_draft_allocations(sheet.id, {person.id: 10000})
        assert len(exact) == 1
        assert exact[0].basis_points == 10000
    finally:
        hq.close()
