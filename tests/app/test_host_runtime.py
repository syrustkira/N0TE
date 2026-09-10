from __future__ import annotations

from pathlib import Path

import pytest

from n0te.host_runtime import (
    CoordinatorProcessSupervisor,
    HostRuntimeError,
    reference_backend,
    reference_route_kind,
    resolve_profile_id,
    resolve_provider_id,
)


def _profile(root: Path, suffix: str) -> str:
    profile_id = "prf_" + suffix * 32
    path = root / "profiles" / profile_id
    path.mkdir(parents=True)
    (path / "lineage.sqlite3").write_bytes(b"placeholder")
    return profile_id


def test_profile_resolution_is_host_neutral_and_fail_closed(
    tmp_path: Path,
) -> None:
    profile_id = _profile(tmp_path, "a")
    assert (
        resolve_profile_id(
            tmp_path,
            explicit=None,
            environment={},
        )
        == profile_id
    )
    _profile(tmp_path, "b")
    with pytest.raises(
        HostRuntimeError,
        match="multiple N0TE profiles",
    ):
        resolve_profile_id(
            tmp_path,
            explicit=None,
            environment={},
        )


def test_reference_provider_and_route_resolution_are_shared() -> None:
    assert (
        reference_backend({"OPENAI_API_KEY": "test-key"})
        == "openai_web"
    )
    provider, child = resolve_provider_id(
        explicit=None,
        environment={"OPENAI_API_KEY": "test-key"},
    )
    assert provider == "openai-web-reference-search"
    assert child["N0TE_REFERENCE_SEARCH_BACKEND"] == "openai_web"

    assert (
        reference_route_kind(
            {
                "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
                "N0TE_REFERENCE_SEARCH_ENDPOINT":
                    "http://127.0.0.1:9001/search",
            }
        )
        == "LOCALHOST"
    )
    assert (
        reference_route_kind(
            {
                "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
                "N0TE_REFERENCE_SEARCH_ENDPOINT":
                    "https://references.example.test/search",
            }
        )
        == "INTERNET"
    )


def test_coordinator_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(
        HostRuntimeError,
        match="loopback http /mcp",
    ):
        CoordinatorProcessSupervisor(
            "https://example.test/mcp",
            environment={},
        )


def test_fl_runtime_surface_has_no_ableton_launcher_dependency() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "n0te/fl_studio.py",
        "n0te/fl_studio_session_launcher.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "ableton_session_launcher" not in source
