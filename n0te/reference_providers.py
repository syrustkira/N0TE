from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .reference_calibration import (
    ReferenceCalibrationError,
    ReferenceCalibrationProfile,
    ReferenceCandidate,
)

REFERENCE_PROVIDER_CONTRACT_VERSION = 1
MAX_HTTP_RESPONSE_BYTES = 2_000_000


class ReferenceProviderError(ReferenceCalibrationError):
    """A concrete reference provider violated its discovery contract."""


def _positive_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ReferenceProviderError("limit must be an integer between 1 and 100")
    return value


def _text(value: object, field: str, maximum: int = 2048) -> str:
    if not isinstance(value, str):
        raise ReferenceProviderError(f"{field} must be text")
    text = value.strip()
    if not text:
        raise ReferenceProviderError(f"{field} must not be empty")
    if len(text) > maximum:
        raise ReferenceProviderError(f"{field} is too long")
    return text


def _timeout(value: object) -> float:
    if isinstance(value, bool):
        raise ReferenceProviderError("timeout_seconds must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReferenceProviderError("timeout_seconds must be numeric") from exc
    if not math.isfinite(number) or not 0.1 <= number <= 30.0:
        raise ReferenceProviderError(
            "timeout_seconds must be finite and between 0.1 and 30"
        )
    return number


def _endpoint(value: object) -> str:
    endpoint = _text(value, "endpoint")
    parsed = urlparse(endpoint)
    if parsed.username is not None or parsed.password is not None:
        raise ReferenceProviderError("endpoint must not embed credentials")
    if parsed.fragment:
        raise ReferenceProviderError("endpoint must not contain a URL fragment")
    host = (parsed.hostname or "").casefold()
    if parsed.scheme == "https" and host:
        return endpoint
    if parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return endpoint
    raise ReferenceProviderError(
        "endpoint must use HTTPS, except HTTP is allowed for localhost"
    )


def _headers(values: Mapping[str, str] | None) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise TypeError("headers must be a mapping")
    out: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        name = _text(raw_name, "header name", 200)
        value = _text(raw_value, f"header {name}", 4000)
        if "\n" in name or "\r" in name or "\n" in value or "\r" in value:
            raise ReferenceProviderError("HTTP headers may not contain newlines")
        out[name] = value
    return out


def _default_request_json(
    endpoint: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> object:
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **dict(headers),
    }
    request = Request(
        endpoint,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
            if not 200 <= status < 300:
                raise ReferenceProviderError(
                    f"reference provider returned HTTP status {status}"
                )
            raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    except ReferenceProviderError:
        raise
    except Exception as exc:
        raise ReferenceProviderError("reference provider request failed") from exc
    if len(raw) > MAX_HTTP_RESPONSE_BYTES:
        raise ReferenceProviderError("reference provider response is too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReferenceProviderError(
            "reference provider returned invalid JSON"
        ) from exc


class LocalReferenceLibraryProvider:
    """Offline provider over already-known typed reference candidates.

    The local provider does not invent new measurements. It merely exposes the
    artist's indexed reference library and favors candidates with more measurable
    overlap so a bounded discovery limit does not arbitrarily hide useful evidence.
    Final similarity/ranking still happens in the shared calibration layer.
    """

    def __init__(self, candidates: Iterable[ReferenceCandidate]):
        try:
            rows = tuple(candidates)
        except TypeError as exc:
            raise TypeError("candidates must be iterable") from exc
        if not all(isinstance(item, ReferenceCandidate) for item in rows):
            raise TypeError("candidates must contain ReferenceCandidate values")
        identities = tuple(
            (item.source_type, item.source_locator, item.source_ref) for item in rows
        )
        if len(identities) != len(set(identities)):
            raise ReferenceProviderError(
                "local reference library contains duplicate candidate identities"
            )
        self._candidates = rows

    def discover_references(
        self,
        *,
        target: ReferenceCalibrationProfile,
        comparison_dimensions: tuple[str, ...],
        limit: int,
    ) -> tuple[ReferenceCandidate, ...]:
        if not isinstance(target, ReferenceCalibrationProfile):
            raise TypeError("target must be ReferenceCalibrationProfile")
        limit = _positive_limit(limit)
        if not comparison_dimensions:
            raise ReferenceProviderError("comparison_dimensions must not be empty")
        target_features = set(target.feature_map())
        target_tags = set(target.semantic_tags)

        def key(candidate: ReferenceCandidate):
            overlap = len(target_features & set(candidate.profile.feature_map()))
            tag_overlap = len(target_tags & set(candidate.profile.semantic_tags))
            return (
                -overlap,
                -tag_overlap,
                candidate.title.casefold(),
                candidate.source_locator,
            )

        return tuple(sorted(self._candidates, key=key)[:limit])


class HttpJsonReferenceProvider:
    """Concrete read-only adapter for feature-enriched reference search services.

    Request contract:
      POST JSON {
        contract_version,
        target: {features, semantic_tags},
        comparison_dimensions,
        limit
      }

    Response contract:
      {"candidates": [{
        "title": str,
        "source_type": str,
        "source_locator": str,
        "profile": {"features": object, "semantic_tags": [str]},
        "comparison_dimensions": [str] (optional),
        "evidence_ref": str,
        "confidence": number (optional)
      }]}

    The remote endpoint can search a catalog, search engine, or enrichment index.
    It never supplies N0TE's final similarity score and cannot grant action authority.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        provider_source_ref: str,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 8.0,
        request_json: Callable[
            [str, Mapping[str, object], Mapping[str, str], float], object
        ] | None = None,
    ):
        self.endpoint = _endpoint(endpoint)
        self.provider_source_ref = _text(
            provider_source_ref, "provider_source_ref", 1000
        )
        self.headers = _headers(headers)
        self.timeout_seconds = _timeout(timeout_seconds)
        self._request_json = request_json or _default_request_json
        if not callable(self._request_json):
            raise TypeError("request_json must be callable")

    def _candidate(
        self,
        raw: object,
        *,
        fallback_dimensions: tuple[str, ...],
    ) -> ReferenceCandidate:
        if not isinstance(raw, dict):
            raise ReferenceProviderError("provider candidate must be an object")
        allowed = {
            "title",
            "source_type",
            "source_locator",
            "profile",
            "comparison_dimensions",
            "evidence_ref",
            "confidence",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ReferenceProviderError(
                f"provider candidate contains unsupported fields: {sorted(unknown)}"
            )
        profile = raw.get("profile")
        if not isinstance(profile, dict):
            raise ReferenceProviderError("provider candidate.profile must be an object")
        profile_unknown = set(profile) - {"features", "semantic_tags"}
        if profile_unknown:
            raise ReferenceProviderError(
                "provider candidate.profile contains unsupported fields: "
                f"{sorted(profile_unknown)}"
            )
        features = profile.get("features")
        if not isinstance(features, dict):
            raise ReferenceProviderError(
                "provider candidate.profile.features must be an object"
            )
        tags = profile.get("semantic_tags") or ()
        if isinstance(tags, (str, bytes)):
            raise ReferenceProviderError(
                "provider candidate.profile.semantic_tags must be a sequence"
            )
        dimensions = raw.get("comparison_dimensions", fallback_dimensions)
        if isinstance(dimensions, (str, bytes)):
            raise ReferenceProviderError(
                "provider candidate.comparison_dimensions must be a sequence"
            )
        evidence_ref = _text(raw.get("evidence_ref"), "candidate.evidence_ref", 1000)
        source_ref = f"{self.provider_source_ref}#{evidence_ref}"
        if len(source_ref) > 2048:
            raise ReferenceProviderError("combined candidate provenance is too long")
        return ReferenceCandidate(
            title=_text(raw.get("title"), "candidate.title", 240),
            source_type=_text(raw.get("source_type"), "candidate.source_type", 80),
            source_locator=_text(
                raw.get("source_locator"), "candidate.source_locator", 2048
            ),
            profile=ReferenceCalibrationProfile(
                features=tuple(features.items()),
                semantic_tags=tuple(tags),
            ),
            comparison_dimensions=tuple(dimensions),
            source_kind="PROVIDER_VERIFIED",
            source_ref=source_ref,
            confidence=raw.get("confidence", 1.0),
        )

    def discover_references(
        self,
        *,
        target: ReferenceCalibrationProfile,
        comparison_dimensions: tuple[str, ...],
        limit: int,
    ) -> tuple[ReferenceCandidate, ...]:
        if not isinstance(target, ReferenceCalibrationProfile):
            raise TypeError("target must be ReferenceCalibrationProfile")
        limit = _positive_limit(limit)
        if not comparison_dimensions:
            raise ReferenceProviderError("comparison_dimensions must not be empty")
        dimensions = tuple(str(item).strip().upper() for item in comparison_dimensions)
        if any(not item for item in dimensions):
            raise ReferenceProviderError(
                "comparison_dimensions must contain non-empty values"
            )
        payload = {
            "contract_version": REFERENCE_PROVIDER_CONTRACT_VERSION,
            "target": {
                "features": target.feature_map(),
                "semantic_tags": list(target.semantic_tags),
            },
            "comparison_dimensions": list(dimensions),
            "limit": limit,
        }
        response = self._request_json(
            self.endpoint,
            payload,
            dict(self.headers),
            self.timeout_seconds,
        )
        if not isinstance(response, dict) or set(response) != {"candidates"}:
            raise ReferenceProviderError(
                "reference provider response must contain only candidates"
            )
        raw_candidates = response["candidates"]
        if not isinstance(raw_candidates, list):
            raise ReferenceProviderError("provider candidates must be a list")
        if len(raw_candidates) > limit:
            raise ReferenceProviderError(
                "reference provider returned more candidates than requested"
            )
        return tuple(
            self._candidate(row, fallback_dimensions=dimensions)
            for row in raw_candidates
        )
