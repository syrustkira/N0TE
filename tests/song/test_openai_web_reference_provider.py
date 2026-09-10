from __future__ import annotations

import json

import pytest

from n0te.openai_web_reference_provider import (
    DEFAULT_OPENAI_WEB_REFERENCE_MODEL,
    OPENAI_RESPONSES_ENDPOINT,
    OpenAIWebReferenceProvider,
    OpenAIWebReferenceProviderError,
)
from n0te.reference_calibration import ReferenceCalibrationProfile


def _response(candidates, *, cited_urls, response_id="resp_reference_1"):
    text = json.dumps({"candidates": candidates}, separators=(",", ":"))
    annotations = [
        {
            "type": "url_citation",
            "url": url,
            "title": f"Source {index}",
            "start_index": 0,
            "end_index": 1,
        }
        for index, url in enumerate(cited_urls)
    ]
    return {
        "id": response_id,
        "status": "completed",
        "output": [
            {"type": "web_search_call", "id": "ws_1", "status": "completed"},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": annotations,
                    }
                ],
            },
        ],
    }


def _candidate(*, evidence_url, source_locator, tempo=128.5, confidence=0.95):
    return {
        "title": "Unexpected Reference",
        "source_type": "STREAMING_LINK",
        "source_locator": source_locator,
        "evidence_url": evidence_url,
        "features": {
            "TEMPO_BPM": tempo,
            "ENERGY": None,
            "LOW_END": None,
            "BRIGHTNESS": None,
            "DENSITY": None,
            "DYNAMICS": 0.52,
            "STEREO_WIDTH": None,
        },
        "semantic_tags": ["electronic", "dark"],
        "confidence": confidence,
    }


def test_openai_web_provider_uses_web_search_structured_output_and_store_false():
    calls = []
    evidence_url = "https://analysis.example.test/reference"
    source_locator = "https://music.example.test/reference"

    def request_json(endpoint, payload, headers, timeout):
        calls.append((endpoint, payload, headers, timeout))
        return _response(
            [_candidate(evidence_url=evidence_url, source_locator=source_locator)],
            cited_urls=(evidence_url, source_locator),
        )

    provider = OpenAIWebReferenceProvider(
        api_key="secret-api-key",
        request_json=request_json,
        timeout_seconds=7.0,
    )
    target = ReferenceCalibrationProfile.create(
        tempo_bpm=128.0,
        dynamics=0.5,
        semantic_tags=("electronic", "dark"),
    )
    candidates = provider.discover_references(
        target=target,
        comparison_dimensions=("tempo", "dynamics", "arrangement"),
        limit=5,
    )

    assert len(calls) == 1
    endpoint, payload, headers, timeout = calls[0]
    assert endpoint == OPENAI_RESPONSES_ENDPOINT
    assert payload["model"] == DEFAULT_OPENAI_WEB_REFERENCE_MODEL
    assert payload["store"] is False
    assert payload["tools"] == [
        {"type": "web_search", "search_context_size": "medium"}
    ]
    assert payload["tool_choice"] == "required"
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    search_task = json.loads(payload["input"])
    assert search_task["target_features"] == {
        "DYNAMICS": 0.5,
        "TEMPO_BPM": 128.0,
    }
    assert search_task["comparison_dimensions"] == [
        "TEMPO",
        "DYNAMICS",
        "ARRANGEMENT",
    ]
    assert headers == {"Authorization": "Bearer secret-api-key"}
    assert timeout == 7.0

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Unexpected Reference"
    assert candidate.source_kind == "OBSERVED"
    assert candidate.source_locator == source_locator
    assert candidate.source_ref == f"openai-web:resp_reference_1:{evidence_url}"
    assert candidate.profile.feature_map() == {
        "DYNAMICS": 0.52,
        "TEMPO_BPM": 128.5,
    }
    assert candidate.confidence == 0.8


def test_openai_web_provider_rejects_claimed_evidence_not_in_url_annotations():
    evidence_url = "https://analysis.example.test/uncited"
    source_locator = "https://music.example.test/reference"
    provider = OpenAIWebReferenceProvider(
        api_key="secret-api-key",
        request_json=lambda endpoint, payload, headers, timeout: _response(
            [_candidate(evidence_url=evidence_url, source_locator=source_locator)],
            cited_urls=(source_locator,),
        ),
    )

    with pytest.raises(OpenAIWebReferenceProviderError, match="evidence_url"):
        provider.discover_references(
            target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
            comparison_dimensions=("tempo",),
            limit=3,
        )


def test_openai_web_provider_rejects_uncited_source_locator_too():
    evidence_url = "https://analysis.example.test/reference"
    source_locator = "https://music.example.test/uncited"
    provider = OpenAIWebReferenceProvider(
        api_key="secret-api-key",
        request_json=lambda endpoint, payload, headers, timeout: _response(
            [_candidate(evidence_url=evidence_url, source_locator=source_locator)],
            cited_urls=(evidence_url,),
        ),
    )

    with pytest.raises(OpenAIWebReferenceProviderError, match="source_locator"):
        provider.discover_references(
            target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
            comparison_dimensions=("tempo",),
            limit=3,
        )


def test_openai_web_provider_drops_candidate_with_no_supported_numeric_features():
    evidence_url = "https://analysis.example.test/reference"
    candidate = _candidate(
        evidence_url=evidence_url,
        source_locator=evidence_url,
        tempo=None,
    )
    candidate["features"]["DYNAMICS"] = None
    provider = OpenAIWebReferenceProvider(
        api_key="secret-api-key",
        request_json=lambda endpoint, payload, headers, timeout: _response(
            [candidate],
            cited_urls=(evidence_url,),
        ),
    )

    assert provider.discover_references(
        target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
        comparison_dimensions=("tempo",),
        limit=3,
    ) == ()


def test_openai_web_provider_requires_completed_single_structured_message():
    provider = OpenAIWebReferenceProvider(
        api_key="secret-api-key",
        request_json=lambda endpoint, payload, headers, timeout: {
            "id": "resp_bad",
            "status": "incomplete",
            "output": [],
        },
    )
    with pytest.raises(OpenAIWebReferenceProviderError, match="did not complete"):
        provider.discover_references(
            target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
            comparison_dimensions=("tempo",),
            limit=3,
        )


def test_openai_web_provider_rejects_invalid_api_key_and_limit():
    with pytest.raises(OpenAIWebReferenceProviderError, match="api_key"):
        OpenAIWebReferenceProvider(api_key="")

    provider = OpenAIWebReferenceProvider(
        api_key="secret-api-key",
        request_json=lambda endpoint, payload, headers, timeout: _response(
            [], cited_urls=()
        ),
    )
    with pytest.raises(OpenAIWebReferenceProviderError, match="between 1 and 25"):
        provider.discover_references(
            target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
            comparison_dimensions=("tempo",),
            limit=100,
        )
