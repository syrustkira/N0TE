from __future__ import annotations

from pathlib import Path

from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment
from n0te.profiles import ApplicationProfiles


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Windows", "x86_64"),
        pid=9702,
        start_token="lightweight-profile-bootstrap",
    )


def test_first_profile_bootstrap_skips_full_headquarters_composition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def forbidden_composition(self, store):  # noqa: ANN001
        raise AssertionError("first-profile bootstrap must not compose HeadquartersMemory")

    monkeypatch.setattr(HeadquartersMemory, "__init__", forbidden_composition)

    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profiles = ApplicationProfiles(data_root=data_root, state_root=state_root)

    created = profiles.resolve(
        artist_name="Lightweight Artist",
        process=process(),
        probe=Probe(),
    )

    assert created.state == "CREATED"
    assert created.selected_profile_id is not None
    assert created.profiles[0].artist_name == "Lightweight Artist"

    discovered = profiles.discover()
    assert discovered.issues == ()
    assert discovered.profiles == created.profiles
