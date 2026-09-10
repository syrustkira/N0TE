from __future__ import annotations

import pytest

from n0te.reference_calibration import (
    ReferenceCalibrationProfile,
    ReferenceCandidate,
    discover_ranked_references,
)
from n0te.reference_providers import (
    HttpJsonReferenceProvider,
    LocalReferenceLibraryProvider,
    ReferenceProviderError,
)


def _candidate(title: str, *, tempo: float, dynamics: float = 0.5, tags=()):
    slug = title.casefold().replace(" ", "-")
    return ReferenceCandidate(
        title=title,
        source_type="CATALOG_RECORDING",
        source_locator=f"catalog:{slug}",
        profile=ReferenceCalibrationProfile.create(
            tempo_bpm=tempo,
            dynamics=dynamics,
            semantic_tags=tags,
        ),
        comparison_dimensions=("tempo", "dynamics"),
        source_kind="OBSERVED",
        source_ref=f"local-index:{slug}",
    )


def test_local_library_provider_is_offline_and_prefers_evidence_coverage_before_limit():
    target = ReferenceCalibrationProfile.create(
        tempo_bpm=128.0,
        dynamics=0.5,
        semantic_tags=("electronic",),
    )
    sparse = ReferenceCandidate(
        title="Sparse",
        source_type="CATALOG_RECORDING",
        source_locator="catalog:sparse",
        profile=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
        comparison_dimensions=("tempo",),
        source_kind="OBSERVED",
        source_ref="local-index:sparse",
    )
    full = _candidate(
        "Full",
        tempo=129.0,
        dynamics=0.48,
        tags=("electronic",),
    )
    provider = LocalReferenceLibraryProvider((sparse, full))

    discovered = provider.discover_references(
        target=target,
        comparison_dimensions=("TEMPO", "DYNAMICS"),
        limit=1,
    )
    assert discovered == (full,)


def test_local_library_rejects_duplicate_reference_identity():
    candidate = _candidate("Same", tempo=128.0)
    with pytest.raises(ReferenceProviderError, match="duplicate"):
        LocalReferenceLibraryProvider((candidate, candidate))


def test_http_json_provider_builds_strict_request_and_typed_candidates():
    calls = []

    def request_json(endpoint, payload, headers, timeout):
        calls.append((endpoint, payload, headers, timeout))
        return {
            "candidates": [
                {
                    "title": "Remote Near",
                    "source_type": "CATALOG_RECORDING",
                    "source_locator": "catalog:remote-near",
                    "profile": {
                        "features": {
                            "TEMPO_BPM": 128.5,
                            "DYNAMICS": 0.52,
                        },
                        "semantic_tags": ["electronic", "dark"],
                    },
                    "evidence_ref": "recording:remote-near:analysis-v3",
                    "confidence": 0.94,
                }
            ]
        }

    provider = HttpJsonReferenceProvider(
        endpoint="https://references.example.test/search",
        provider_source_ref="provider:reference-search-v1",
        headers={"Authorization": "Bearer test-token"},
        timeout_seconds=4.0,
        request_json=request_json,
    )
    target = ReferenceCalibrationProfile.create(
        tempo_bpm=128.0,
        dynamics=0.5,
        semantic_tags=("electronic",),
    )

    candidates = provider.discover_references(
        target=target,
        comparison_dimensions=("TEMPO", "DYNAMICS"),
        limit=8,
    )

    assert len(calls) == 1
    endpoint, payload, headers, timeout = calls[0]
    assert endpoint == "https://references.example.test/search"
    assert payload == {
        "contract_version": 1,
        "target": {
            "features": {"DYNAMICS": 0.5, "TEMPO_BPM": 128.0},
            "semantic_tags": ["ELECTRONIC"],
        },
        "comparison_dimensions": ["TEMPO", "DYNAMICS"],
        "limit": 8,
    }
    assert headers == {"Authorization": "Bearer test-token"}
    assert timeout == 4.0
    assert candidates[0].title == "Remote Near"
    assert candidates[0].source_kind == "PROVIDER_VERIFIED"
    assert candidates[0].source_ref == (
        "provider:reference-search-v1#recording:remote-near:analysis-v3"
    )
    assert candidates[0].comparison_dimensions == ("TEMPO", "DYNAMICS")


def test_http_provider_discovery_plugs_into_existing_local_ranker():
    def request_json(endpoint, payload, headers, timeout):
        return {
            "candidates": [
                {
                    "title": "Remote Far",
                    "source_type": "CATALOG_RECORDING",
                    "source_locator": "catalog:far",
                    "profile": {
                        "features": {"TEMPO_BPM": 92.0, "DYNAMICS": 0.9},
                        "semantic_tags": ["electronic"],
                    },
                    "comparison_dimensions": ["tempo", "dynamics"],
                    "evidence_ref": "far-v1",
                },
                {
                    "title": "Remote Near",
                    "source_type": "CATALOG_RECORDING",
                    "source_locator": "catalog:near",
                    "profile": {
                        "features": {"TEMPO_BPM": 128.5, "DYNAMICS": 0.48},
                        "semantic_tags": ["electronic"],
                    },
                    "comparison_dimensions": ["tempo", "dynamics"],
                    "evidence_ref": "near-v1",
                },
            ]
        }

    provider = HttpJsonReferenceProvider(
        endpoint="https://references.example.test/search",
        provider_source_ref="provider:test",
        request_json=request_json,
    )
    target = ReferenceCalibrationProfile.create(
        tempo_bpm=128.0,
        dynamics=0.5,
        semantic_tags=("electronic",),
    )

    ranked = discover_ranked_references(
        provider,
        target,
        comparison_dimensions=("tempo", "dynamics"),
        required_features=("tempo_bpm", "dynamics"),
        desired_tags=("electronic",),
        result_limit=2,
    )
    assert [row.candidate.title for row in ranked] == [
        "Remote Near",
        "Remote Far",
    ]


def test_http_provider_rejects_plaintext_remote_endpoint_and_embedded_credentials():
    with pytest.raises(ReferenceProviderError, match="HTTPS"):
        HttpJsonReferenceProvider(
            endpoint="http://example.com/search",
            provider_source_ref="provider:test",
        )
    with pytest.raises(ReferenceProviderError, match="credentials"):
        HttpJsonReferenceProvider(
            endpoint="https://user:secret@example.com/search",
            provider_source_ref="provider:test",
        )


def test_http_provider_allows_plain_http_only_for_localhost_service():
    provider = HttpJsonReferenceProvider(
        endpoint="http://127.0.0.1:8765/search",
        provider_source_ref="provider:local-service",
        request_json=lambda endpoint, payload, headers, timeout: {"candidates": []},
    )
    assert provider.discover_references(
        target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
        comparison_dimensions=("tempo",),
        limit=3,
    ) == ()


def test_http_provider_rejects_malformed_or_overbroad_candidate_payloads():
    def unknown_field(endpoint, payload, headers, timeout):
        return {
            "candidates": [
                {
                    "title": "Bad",
                    "source_type": "CATALOG_RECORDING",
                    "source_locator": "catalog:bad",
                    "profile": {"features": {"TEMPO_BPM": 128.0}},
                    "evidence_ref": "bad-v1",
                    "rights_verified": True,
                }
            ]
        }

    provider = HttpJsonReferenceProvider(
        endpoint="https://references.example.test/search",
        provider_source_ref="provider:test",
        request_json=unknown_field,
    )
    with pytest.raises(ReferenceProviderError, match="unsupported fields"):
        provider.discover_references(
            target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
            comparison_dimensions=("tempo",),
            limit=3,
        )

    too_many = HttpJsonReferenceProvider(
        endpoint="https://references.example.test/search",
        provider_source_ref="provider:test",
        request_json=lambda endpoint, payload, headers, timeout: {
            "candidates": [{
                "title": f"Candidate {index}",
                "source_type": "CATALOG_RECORDING",
                "source_locator": f"catalog:{index}",
                "profile": {"features": {"TEMPO_BPM": 128.0}},
                "evidence_ref": f"candidate:{index}",
            } for index in range(4)]
        },
    )
    with pytest.raises(ReferenceProviderError, match="more candidates"):
        too_many.discover_references(
            target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
            comparison_dimensions=("tempo",),
            limit=3,
        )
