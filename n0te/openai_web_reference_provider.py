from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .reference_calibration import (
    ReferenceCalibrationProfile,
    ReferenceCandidate,
)
from .reference_providers import MAX_HTTP_RESPONSE_BYTES, ReferenceProviderError

OPENAI_RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_WEB_REFERENCE_MODEL = "gpt-5.6-luna"
_OPENAI_PROVIDER_MAX_CONFIDENCE = 0.8
_FEATURE_NAMES = (
    "TEMPO_BPM",
    "ENERGY",
    "LOW_END",
    "BRIGHTNESS",
    "DENSITY",
    "DYNAMICS",
    "STEREO_WIDTH",
)


class OpenAIWebReferenceProviderError(ReferenceProviderError):
    """OpenAI web-reference discovery failed provenance or response validation."""


def _text(value: object, field: str, maximum: int = 2048) -> str:
    if not isinstance(value, str):
        raise OpenAIWebReferenceProviderError(f"{field} must be text")
    text = value.strip()
    if not text:
        raise OpenAIWebReferenceProviderError(f"{field} must not be empty")
    if len(text) > maximum:
        raise OpenAIWebReferenceProviderError(f"{field} is too long")
    return text


def _timeout(value: object) -> float:
    if isinstance(value, bool):
        raise OpenAIWebReferenceProviderError("timeout_seconds must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OpenAIWebReferenceProviderError(
            "timeout_seconds must be numeric"
        ) from exc
    if not math.isfinite(number) or not 0.1 <= number <= 60.0:
        raise OpenAIWebReferenceProviderError(
            "timeout_seconds must be finite and between 0.1 and 60"
        )
    return number


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 25:
        raise OpenAIWebReferenceProviderError(
            "limit must be an integer between 1 and 25"
        )
    return value


def _url(value: object, field: str) -> str:
    url = _text(value, field)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OpenAIWebReferenceProviderError(f"{field} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise OpenAIWebReferenceProviderError(f"{field} must not embed credentials")
    return url


def _confidence(value: object) -> float:
    if isinstance(value, bool):
        raise OpenAIWebReferenceProviderError("confidence must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OpenAIWebReferenceProviderError("confidence must be numeric") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise OpenAIWebReferenceProviderError(
            "confidence must be between 0 and 1"
        )
    return min(number, _OPENAI_PROVIDER_MAX_CONFIDENCE)


def _default_request_json(
    endpoint: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> object:
    request = Request(
        endpoint,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **dict(headers),
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
            if not 200 <= status < 300:
                raise OpenAIWebReferenceProviderError(
                    f"OpenAI Responses API returned HTTP status {status}"
                )
            raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    except OpenAIWebReferenceProviderError:
        raise
    except Exception as exc:
        raise OpenAIWebReferenceProviderError(
            "OpenAI Responses API request failed"
        ) from exc
    if len(raw) > MAX_HTTP_RESPONSE_BYTES:
        raise OpenAIWebReferenceProviderError(
            "OpenAI Responses API response is too large"
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenAIWebReferenceProviderError(
            "OpenAI Responses API returned invalid JSON"
        ) from exc


def _response_schema() -> dict:
    features = {
        name: {"type": ["number", "null"]}
        for name in _FEATURE_NAMES
    }
    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "source_type": {
                            "type": "string",
                            "enum": [
                                "STREAMING_LINK",
                                "CATALOG_RECORDING",
                                "OTHER",
                            ],
                        },
                        "source_locator": {"type": "string"},
                        "evidence_url": {"type": "string"},
                        "features": {
                            "type": "object",
                            "properties": features,
                            "required": list(_FEATURE_NAMES),
                            "additionalProperties": False,
                        },
                        "semantic_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "confidence": {"type": "number"},
                    },
                    "required": [
                        "title",
                        "source_type",
                        "source_locator",
                        "evidence_url",
                        "features",
                        "semantic_tags",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }


def _extract_structured_output(response: object) -> tuple[str, set[str], str]:
    if not isinstance(response, dict):
        raise OpenAIWebReferenceProviderError("OpenAI response must be an object")
    if response.get("status") != "completed":
        raise OpenAIWebReferenceProviderError(
            f"OpenAI response did not complete: {response.get('status')}"
        )
    response_id = _text(response.get("id"), "response.id", 200)
    output = response.get("output")
    if not isinstance(output, list):
        raise OpenAIWebReferenceProviderError("OpenAI response.output must be a list")

    texts: list[str] = []
    cited_urls: set[str] = set()
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
            annotations = part.get("annotations") or []
            if not isinstance(annotations, list):
                raise OpenAIWebReferenceProviderError(
                    "output_text.annotations must be a list"
                )
            for annotation in annotations:
                if (
                    isinstance(annotation, dict)
                    and annotation.get("type") == "url_citation"
                ):
                    cited_urls.add(_url(annotation.get("url"), "citation.url"))

    if len(texts) != 1:
        raise OpenAIWebReferenceProviderError(
            "structured web search must return exactly one output_text payload"
        )
    return texts[0], cited_urls, response_id


class OpenAIWebReferenceProvider:
    """Optional evidence-gated web discovery using OpenAI Responses + web_search.

    Web search discovers candidates. Structured output constrains the proposal.
    URL-citation annotations are checked before a candidate becomes OBSERVED evidence.
    N0TE's deterministic reference calibration remains the final ranking authority.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_OPENAI_WEB_REFERENCE_MODEL,
        timeout_seconds: float = 20.0,
        request_json: Callable[
            [str, Mapping[str, object], Mapping[str, str], float], object
        ] | None = None,
    ):
        self._api_key = _text(api_key, "api_key", 4000)
        self.model = _text(model, "model", 200)
        self.timeout_seconds = _timeout(timeout_seconds)
        self._request_json = request_json or _default_request_json
        if not callable(self._request_json):
            raise TypeError("request_json must be callable")

    def _payload(
        self,
        *,
        target: ReferenceCalibrationProfile,
        comparison_dimensions: tuple[str, ...],
        limit: int,
    ) -> dict:
        search_task = {
            "target_features": target.feature_map(),
            "target_semantic_tags": list(target.semantic_tags),
            "comparison_dimensions": list(comparison_dimensions),
            "candidate_limit": limit,
        }
        instructions = (
            "Act only as N0TE's music-reference web discovery adapter. Search the public web for "
            "released tracks that can serve as useful production references for the supplied measurable "
            "target and comparison dimensions. Prefer evidence-backed, non-obvious adjacent references when "
            "they fit. Do not claim rights, licensing, artistic superiority, or certainty. For every numeric "
            "feature, return null unless the searched sources support that value or a defensible normalized "
            "observation. source_locator and evidence_url must be URLs actually cited by your web-search "
            "response. A candidate with no supported numeric calibration feature is allowed, but N0TE may drop it."
        )
        return {
            "model": self.model,
            "store": False,
            "tools": [{"type": "web_search", "search_context_size": "medium"}],
            "tool_choice": "required",
            "instructions": instructions,
            "input": json.dumps(search_task, separators=(",", ":")),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "n0te_reference_candidates",
                    "description": "Evidence-backed music reference candidates for local N0TE calibration.",
                    "strict": True,
                    "schema": _response_schema(),
                }
            },
        }

    def discover_references(
        self,
        *,
        target: ReferenceCalibrationProfile,
        comparison_dimensions: tuple[str, ...],
        limit: int,
    ) -> tuple[ReferenceCandidate, ...]:
        if not isinstance(target, ReferenceCalibrationProfile):
            raise TypeError("target must be ReferenceCalibrationProfile")
        limit = _limit(limit)
        dimensions = tuple(str(item).strip().upper() for item in comparison_dimensions)
        if not dimensions or any(not item for item in dimensions):
            raise OpenAIWebReferenceProviderError(
                "comparison_dimensions must contain non-empty values"
            )

        response = self._request_json(
            OPENAI_RESPONSES_ENDPOINT,
            self._payload(
                target=target,
                comparison_dimensions=dimensions,
                limit=limit,
            ),
            {"Authorization": f"Bearer {self._api_key}"},
            self.timeout_seconds,
        )
        output_text, cited_urls, response_id = _extract_structured_output(response)
        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise OpenAIWebReferenceProviderError(
                "structured output was not valid JSON"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {"candidates"}:
            raise OpenAIWebReferenceProviderError(
                "structured output must contain only candidates"
            )
        raw_candidates = payload["candidates"]
        if not isinstance(raw_candidates, list) or len(raw_candidates) > limit:
            raise OpenAIWebReferenceProviderError(
                "structured output candidate count exceeds request contract"
            )

        out: list[ReferenceCandidate] = []
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                raise OpenAIWebReferenceProviderError("candidate must be an object")
            evidence_url = _url(raw.get("evidence_url"), "candidate.evidence_url")
            source_locator = _url(
                raw.get("source_locator"), "candidate.source_locator"
            )
            if evidence_url not in cited_urls:
                raise OpenAIWebReferenceProviderError(
                    "candidate evidence_url was not present in web-search URL citations"
                )
            if source_locator not in cited_urls:
                raise OpenAIWebReferenceProviderError(
                    "candidate source_locator was not present in web-search URL citations"
                )
            features = raw.get("features")
            if not isinstance(features, dict) or set(features) != set(_FEATURE_NAMES):
                raise OpenAIWebReferenceProviderError(
                    "candidate features do not match the calibration contract"
                )
            supported_features = tuple(
                (name, value)
                for name, value in features.items()
                if value is not None
            )
            if not supported_features:
                continue
            tags = raw.get("semantic_tags")
            if not isinstance(tags, list) or not all(
                isinstance(item, str) for item in tags
            ):
                raise OpenAIWebReferenceProviderError(
                    "candidate semantic_tags must be a list of strings"
                )
            source_ref = f"openai-web:{response_id}:{evidence_url}"
            if len(source_ref) > 2048:
                raise OpenAIWebReferenceProviderError(
                    "candidate web provenance is too long"
                )
            out.append(
                ReferenceCandidate(
                    title=_text(raw.get("title"), "candidate.title", 240),
                    source_type=_text(
                        raw.get("source_type"), "candidate.source_type", 80
                    ),
                    source_locator=source_locator,
                    profile=ReferenceCalibrationProfile(
                        features=supported_features,
                        semantic_tags=tuple(tags),
                    ),
                    comparison_dimensions=dimensions,
                    source_kind="OBSERVED",
                    source_ref=source_ref,
                    confidence=_confidence(raw.get("confidence")),
                )
            )
        return tuple(out)
