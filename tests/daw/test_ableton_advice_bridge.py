from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from integrations.ableton.N0TEBridge import (
    N0TEBridge,
    NOTICE_MAX_CHARS,
    TRACK_VOLUME_ACTION_SCHEMA,
    TRACK_VOLUME_STATE_SCHEMA,
)
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


class _Parameter:
    def __init__(self, value=0.5):
        self.min = 0.0
        self.max = 1.0
        self.value = float(value)
        self.is_enabled = True


class _Mixer:
    def __init__(self, value=0.5):
        self.volume = _Parameter(value)


class _ActionTrack:
    def __init__(self, name, value=0.5):
        self.name = name
        self.mixer_device = _Mixer(value)


class _ActionView:
    def __init__(self, selected):
        self.selected_track = selected


class _ActionSong:
    def __init__(self):
        self.tracks = (_ActionTrack("Drums", 0.4), _ActionTrack("Bass", 0.5))
        self.return_tracks = ()
        self.master_track = _ActionTrack("Master", 0.7)
        self.view = _ActionView(self.tracks[1])
        self.tempo = 128.0
        self.is_playing = False
        self.current_song_time = 0.0
        self.file_path = ""

    def get_data(self, key, default=None):
        return default


class _ActionApplication:
    def get_major_version(self):
        return 12

    def get_minor_version(self):
        return 4

    def get_bugfix_version(self):
        return 5


class _ActionCInstance:
    def __init__(self, song):
        self._song = song
        self.messages = []

    def song(self):
        return self._song

    def application(self):
        return _ActionApplication()

    def show_message(self, message):
        self.messages.append(str(message))


def _read_json(url):
    with urlopen(Request(url, method="GET"), timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url, payload, *, headers=None):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = Request(
        url,
        data=raw,
        method="POST",
        headers=request_headers,
    )
    with urlopen(request, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def test_selected_track_volume_action_is_preconditioned_and_read_back():
    song = _ActionSong()
    bridge = N0TEBridge(_ActionCInstance(song), port=0, start_server=True)
    endpoint = f"http://127.0.0.1:{bridge.port}/action/selected-track-volume"
    try:
        state = _read_json(endpoint)
        assert state["schema"] == TRACK_VOLUME_STATE_SCHEMA
        assert state["track_ref"] == "track:1"
        assert state["normalized"] == pytest.approx(0.5)
        assert state["parameter_enabled"] is True

        applied = _post_json(
            endpoint,
            {
                "schema": TRACK_VOLUME_ACTION_SCHEMA,
                "bridge_session_id": state["bridge_session_id"],
                "action_id": "action:test-volume",
                "track_ref": "track:1",
                "expected_normalized": 0.5,
                "desired_normalized": 0.625,
            },
        )
        assert applied["ok"] is True
        assert applied["before_normalized"] == pytest.approx(0.5)
        assert applied["after_normalized"] == pytest.approx(0.625)
        assert song.tracks[1].mixer_device.volume.value == pytest.approx(0.625)

        stale_body = {
            "schema": TRACK_VOLUME_ACTION_SCHEMA,
            "bridge_session_id": state["bridge_session_id"],
            "action_id": "action:stale",
            "track_ref": "track:1",
            "expected_normalized": 0.5,
            "desired_normalized": 0.7,
        }
        with pytest.raises(HTTPError) as stale:
            _post_json(endpoint, stale_body)
        assert stale.value.code == 409
        assert song.tracks[1].mixer_device.volume.value == pytest.approx(0.625)
    finally:
        bridge.disconnect()


def test_track_volume_action_refuses_selection_session_origin_and_body_drift():
    song = _ActionSong()
    c_instance = _ActionCInstance(song)
    bridge = N0TEBridge(c_instance, port=0, start_server=True)
    endpoint = f"http://127.0.0.1:{bridge.port}/action/selected-track-volume"
    try:
        state = _read_json(endpoint)
        base = {
            "schema": TRACK_VOLUME_ACTION_SCHEMA,
            "bridge_session_id": state["bridge_session_id"],
            "action_id": "action:guarded",
            "track_ref": "track:1",
            "expected_normalized": 0.5,
            "desired_normalized": 0.6,
        }

        song.view.selected_track = song.tracks[0]
        with pytest.raises(HTTPError) as selection:
            _post_json(endpoint, base)
        assert selection.value.code == 409
        assert song.tracks[1].mixer_device.volume.value == pytest.approx(0.5)

        song.view.selected_track = song.tracks[1]
        c_instance._song = _ActionSong()
        with pytest.raises(HTTPError) as session:
            _post_json(endpoint, base)
        assert session.value.code == 409

        browser_body = dict(base)
        browser_body["bridge_session_id"] = _read_json(endpoint)["bridge_session_id"]
        with pytest.raises(HTTPError) as browser:
            _post_json(
                endpoint,
                browser_body,
                headers={"Origin": "http://example.test"},
            )
        assert browser.value.code == 403

        smuggled = dict(browser_body, tempo=160)
        with pytest.raises(HTTPError) as extra:
            _post_json(endpoint, smuggled)
        assert extra.value.code == 400
    finally:
        bridge.disconnect()
