from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from integrations.ableton.N0TEBridge import N0TEBridge, NOTICE_MAX_CHARS
from n0te.ableton_host_bridge import AbletonHostBridgeError
from n0te.ableton_observer_service import AbletonAdviceClient, AbletonObserverServiceError


class _CInstance:
    def __init__(self):
        self.messages = []
        self.logs = []

    def show_message(self, message):
        self.messages.append(str(message))

    def log_message(self, message):
        self.logs.append(str(message))


def test_advice_client_roundtrips_to_live_show_message_without_song_mutation():
    c_instance = _CInstance()
    bridge = N0TEBridge(c_instance, port=0, start_server=True)
    try:
        client = AbletonAdviceClient(
            endpoint=f"http://127.0.0.1:{bridge.port}",
        )
        client.show_notice("Reference: THRILL\n low-end density")
        assert c_instance.messages == ["N0TE: Reference: THRILL low-end density"]
    finally:
        bridge.disconnect()


def test_notice_endpoint_rejects_browser_origin_and_unknown_fields():
    c_instance = _CInstance()
    bridge = N0TEBridge(c_instance, port=0, start_server=True)
    endpoint = f"http://127.0.0.1:{bridge.port}/notice"
    try:
        raw = json.dumps({"message": "hello"}).encode("utf-8")
        browser_request = Request(
            endpoint,
            data=raw,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://example.test",
            },
        )
        with pytest.raises(HTTPError) as browser_error:
            urlopen(browser_request, timeout=2.0)
        assert browser_error.value.code == 403
        assert c_instance.messages == []

        smuggled = json.dumps({"message": "hello", "tempo": 140}).encode("utf-8")
        smuggled_request = Request(
            endpoint,
            data=smuggled,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as smuggled_error:
            urlopen(smuggled_request, timeout=2.0)
        assert smuggled_error.value.code == 400
        assert c_instance.messages == []
    finally:
        bridge.disconnect()


def test_advice_client_rejects_oversized_or_nonlocal_notice_before_delivery():
    with pytest.raises(AbletonObserverServiceError, match="exceeds"):
        AbletonAdviceClient("http://127.0.0.1:9799").show_notice(
            "x" * (NOTICE_MAX_CHARS + 1)
        )

    with pytest.raises(AbletonHostBridgeError, match="loopback-only"):
        AbletonAdviceClient("http://example.com:9799")
