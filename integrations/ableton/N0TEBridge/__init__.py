from __future__ import absolute_import, print_function

import hashlib
import json
import os
import platform
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from _Framework.ControlSurface import ControlSurface
except ImportError:
    class ControlSurface(object):
        """Test fallback only. Ableton supplies _Framework inside Live."""

        def __init__(self, c_instance=None):
            self._c_instance = c_instance

        def song(self):
            return self._c_instance.song()

        def application(self):
            return self._c_instance.application()

        def schedule_message(self, delay, callback):
            callback()

        def show_message(self, message):
            shower = getattr(self._c_instance, "show_message", None)
            if callable(shower):
                shower(message)

        def log_message(self, message):
            logger = getattr(self._c_instance, "log_message", None)
            if callable(logger):
                logger(message)

        def disconnect(self):
            pass


HOST = "127.0.0.1"
PORT = 9799
ADAPTER_ID = "N0TEBridge"
ADAPTER_VERSION = "2"
SCHEMA = "n0te.ableton-observation/v2"
WORKSPACE_DATA_KEY = "n0te.workspace_id.v1"
LIVE_CALL_TIMEOUT_SECONDS = 2.0
NOTICE_MAX_CHARS = 240
_NOTICE_MAX_BODY_BYTES = 2048
_ACTION_MAX_BODY_BYTES = 4096
TRACK_VOLUME_STATE_SCHEMA = "n0te.ableton-selected-track-volume-state/v1"
TRACK_VOLUME_ACTION_SCHEMA = "n0te.ableton-selected-track-volume-action/v1"
_TRACK_VOLUME_TOLERANCE = 0.0001
_SET_PATH_HASH_DOMAIN = b"n0te.ableton-set-path/v1\x00"
_ALLOWED_HOST_HEADERS = frozenset(
    {
        "127.0.0.1:9799",
        "localhost:9799",
    }
)


class _ConflictError(ValueError):
    """The requested reversible action is stale and was not applied."""


def _set_path_fingerprint(song):
    """Return a one-way stable fingerprint for a saved Live Set path."""

    raw = getattr(song, "file_path", None)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    normalized = os.path.normcase(os.path.normpath(text))
    if not os.path.isabs(normalized):
        normalized = os.path.abspath(normalized)
    normalized = normalized.replace("\\", "/")
    os_name = (platform.system() or "unknown").strip().casefold()
    material = (os_name + "\x00" + normalized).encode("utf-8", "surrogatepass")
    return hashlib.sha256(_SET_PATH_HASH_DOMAIN + material).hexdigest()


def _notice_text(value):
    if not isinstance(value, str):
        raise ValueError("message must be a string")
    text = " ".join(value.split())
    if not text:
        raise ValueError("message must not be empty")
    if len(text) > NOTICE_MAX_CHARS:
        raise ValueError("message exceeds %s characters" % NOTICE_MAX_CHARS)
    return text


def _finite_unit(value, field):
    if isinstance(value, bool):
        raise ValueError("%s must be numeric" % field)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be numeric" % field)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("%s must be finite" % field)
    if number < 0.0 or number > 1.0:
        raise ValueError("%s must be between 0 and 1" % field)
    return number


def _required_text(value, field):
    text = str(value).strip()
    if not text:
        raise ValueError("%s must not be empty" % field)
    return text


class _SnapshotHandler(BaseHTTPRequestHandler):
    server_version = "N0TEBridge/4"
    sys_version = ""

    def log_message(self, format, *args):
        return

    def _json(self, status, payload):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _request_is_local(self):
        if self.headers.get("Origin") is not None:
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        allowed = getattr(self.server, "allowed_host_headers", _ALLOWED_HOST_HEADERS)
        return host in allowed

    def _read_json_object(self, maximum_bytes):
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(length_header)
        except ValueError:
            raise ValueError("Content-Length is invalid")
        if length <= 0 or length > maximum_bytes:
            raise ValueError("request body size is invalid")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("request body is incomplete")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("request body must be UTF-8 JSON")
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    def do_GET(self):
        if not self._request_is_local():
            self._json(403, {"ok": False, "error": "loopback host/origin policy rejected request"})
            return
        try:
            if self.path == "/snapshot":
                payload = self.server.bridge.request_snapshot()
            elif self.path == "/action/selected-track-volume":
                payload = self.server.bridge.request_selected_track_volume_state()
            else:
                self._json(404, {"ok": False, "error": "not found"})
                return
        except Exception as error:
            self._json(503, {"ok": False, "error": "bridge state unavailable: %s" % error})
            return
        self._json(200, payload)

    def do_POST(self):
        if not self._request_is_local():
            self._json(403, {"ok": False, "error": "loopback host/origin policy rejected request"})
            return
        try:
            if self.path == "/notice":
                payload = self._read_json_object(_NOTICE_MAX_BODY_BYTES)
                if set(payload) != {"message"}:
                    raise ValueError("notice body must contain only message")
                self.server.bridge.request_notice(payload["message"])
                response = {"ok": True}
            elif self.path == "/action/selected-track-volume":
                payload = self._read_json_object(_ACTION_MAX_BODY_BYTES)
                response = self.server.bridge.request_selected_track_volume_action(payload)
            else:
                self._json(404, {"ok": False, "error": "not found"})
                return
        except _ConflictError as error:
            self._json(409, {"ok": False, "error": str(error)})
            return
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)})
            return
        except Exception as error:
            self._json(503, {"ok": False, "error": "action unavailable: %s" % error})
            return
        self._json(200, response)

    def do_PUT(self):
        self._json(405, {"ok": False, "error": "method not allowed"})

    def do_PATCH(self):
        self.do_PUT()

    def do_DELETE(self):
        self.do_PUT()


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class N0TEBridge(ControlSurface):
    """Dependency-light Ableton observation, advice and reversible action bridge.

    Observation remains read-only. The sole mutation route is selected-track mixer
    volume, guarded by current Live Song session identity, exact selected-track
    identity and an optimistic current-value precondition. N0TE's external
    AuthorityService and transaction journal remain the authority owners; this
    bridge only supplies the narrow host primitive and refuses stale mutations.
    """

    def __init__(self, c_instance, host=HOST, port=PORT, start_server=True):
        ControlSurface.__init__(self, c_instance)
        self._queue_lock = threading.Lock()
        self._pending_live_calls = []
        self._observed_song = None
        self._bridge_session_id = None
        self._server = None
        self._server_thread = None
        self.host = host
        self.port = int(port)
        if start_server:
            self._start_server()

    def _log(self, message):
        try:
            self.log_message("N0TEBridge: %s" % message)
        except Exception:
            pass

    def _start_server(self):
        if self.host != HOST:
            raise RuntimeError("N0TEBridge may bind only to 127.0.0.1")
        server = _BridgeServer((self.host, self.port), _SnapshotHandler)
        server.bridge = self
        actual_port = int(server.server_address[1])
        server.allowed_host_headers = frozenset(
            {
                "127.0.0.1:%s" % actual_port,
                "localhost:%s" % actual_port,
            }
        )
        thread = threading.Thread(target=server.serve_forever, name="N0TEBridgeHTTP")
        thread.daemon = True
        thread.start()
        self._server = server
        self._server_thread = thread
        self.port = actual_port
        self._log("N0TE bridge listening on http://%s:%s" % (self.host, self.port))

    def disconnect(self):
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            finally:
                server.server_close()
        self._server_thread = None
        self._observed_song = None
        self._bridge_session_id = None
        ControlSurface.disconnect(self)

    def _drain_live_calls(self):
        with self._queue_lock:
            calls = self._pending_live_calls
            self._pending_live_calls = []
        for call in calls:
            try:
                call["result"]["value"] = call["callback"]()
            except Exception as error:
                call["result"]["error"] = error
            finally:
                call["event"].set()

    def _call_live_thread(self, callback):
        event = threading.Event()
        result = {"value": None, "error": None}
        with self._queue_lock:
            self._pending_live_calls.append(
                {"callback": callback, "event": event, "result": result}
            )
        self.schedule_message(1, self._drain_live_calls)
        if not event.wait(LIVE_CALL_TIMEOUT_SECONDS):
            raise RuntimeError("timed out waiting for Ableton Live API")
        if result["error"] is not None:
            raise result["error"]
        return result["value"]

    def request_snapshot(self):
        return self._call_live_thread(self._capture_snapshot)

    def request_notice(self, message):
        text = _notice_text(message)
        return self._call_live_thread(lambda: self._show_notice(text))

    def request_selected_track_volume_state(self):
        return self._call_live_thread(self._selected_track_volume_state)

    def request_selected_track_volume_action(self, payload):
        return self._call_live_thread(
            lambda: self._apply_selected_track_volume_action(payload)
        )

    def _show_notice(self, text):
        self.show_message("N0TE: %s" % text)
        return text

    def _session_id_for_song(self, song):
        if song is not self._observed_song or self._bridge_session_id is None:
            self._observed_song = song
            self._bridge_session_id = uuid.uuid4().hex
        return self._bridge_session_id

    @staticmethod
    def _index_by_identity(items, target):
        for index, item in enumerate(items):
            if item is target:
                return index
        return None

    def _selected_track(self, song):
        selected = getattr(getattr(song, "view", None), "selected_track", None)
        if selected is None:
            return None
        tracks = tuple(getattr(song, "tracks", ()) or ())
        returns = tuple(getattr(song, "return_tracks", ()) or ())
        index = self._index_by_identity(tracks, selected)
        if index is not None:
            kind = "TRACK"
        else:
            index = self._index_by_identity(returns, selected)
            if index is not None:
                kind = "RETURN"
            elif selected is getattr(song, "master_track", None):
                kind = "MASTER"
                index = None
            else:
                return None
        return {
            "kind": kind,
            "index": index,
            "name": str(getattr(selected, "name", kind)).strip() or kind,
        }

    @staticmethod
    def _track_ref(selected):
        if selected["kind"] == "MASTER":
            return "master:main"
        prefix = "track" if selected["kind"] == "TRACK" else "return"
        return "%s:%s" % (prefix, int(selected["index"]))

    def _selected_track_and_volume_parameter(self, song):
        selected_info = self._selected_track(song)
        if selected_info is None:
            raise ValueError("Ableton has no safely identified selected track")
        selected = getattr(getattr(song, "view", None), "selected_track", None)
        mixer = getattr(selected, "mixer_device", None)
        parameter = getattr(mixer, "volume", None)
        if parameter is None:
            raise ValueError("selected track mixer volume is unavailable")
        minimum = float(getattr(parameter, "min", 0.0))
        maximum = float(getattr(parameter, "max", 1.0))
        value = float(getattr(parameter, "value"))
        if not maximum > minimum:
            raise ValueError("selected track mixer volume range is invalid")
        normalized = (value - minimum) / (maximum - minimum)
        if normalized < -_TRACK_VOLUME_TOLERANCE or normalized > 1.0 + _TRACK_VOLUME_TOLERANCE:
            raise ValueError("selected track mixer volume is outside its advertised range")
        return selected_info, parameter, min(1.0, max(0.0, normalized)), minimum, maximum

    def _selected_track_volume_state(self):
        song = self.song()
        session = self._session_id_for_song(song)
        selected, parameter, normalized, minimum, maximum = self._selected_track_and_volume_parameter(song)
        return {
            "schema": TRACK_VOLUME_STATE_SCHEMA,
            "bridge_session_id": session,
            "track_ref": self._track_ref(selected),
            "normalized": normalized,
            "parameter_enabled": bool(getattr(parameter, "is_enabled", True)),
            "parameter_minimum": minimum,
            "parameter_maximum": maximum,
        }

    def _apply_selected_track_volume_action(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("action body must be an object")
        allowed = {
            "schema",
            "bridge_session_id",
            "action_id",
            "track_ref",
            "expected_normalized",
            "desired_normalized",
        }
        if set(payload) != allowed:
            raise ValueError("track-volume action contains unsupported or missing fields")
        if payload.get("schema") != TRACK_VOLUME_ACTION_SCHEMA:
            raise ValueError("unsupported track-volume action schema")
        requested_session = _required_text(payload.get("bridge_session_id"), "bridge_session_id")
        action_id = _required_text(payload.get("action_id"), "action_id")
        requested_track = _required_text(payload.get("track_ref"), "track_ref")
        expected = _finite_unit(payload.get("expected_normalized"), "expected_normalized")
        desired = _finite_unit(payload.get("desired_normalized"), "desired_normalized")

        song = self.song()
        current_session = self._session_id_for_song(song)
        if requested_session != current_session:
            raise _ConflictError("Ableton Song session changed before action; nothing applied")
        selected, parameter, before, minimum, maximum = self._selected_track_and_volume_parameter(song)
        current_track = self._track_ref(selected)
        if requested_track != current_track:
            raise _ConflictError("selected Ableton track changed before action; nothing applied")
        if abs(before - expected) > _TRACK_VOLUME_TOLERANCE:
            raise _ConflictError("selected track volume changed before action; nothing applied")
        if not bool(getattr(parameter, "is_enabled", True)):
            raise _ConflictError("selected track volume parameter is disabled; nothing applied")

        parameter.value = minimum + desired * (maximum - minimum)
        after_raw = float(getattr(parameter, "value"))
        after = (after_raw - minimum) / (maximum - minimum)
        if abs(after - desired) > _TRACK_VOLUME_TOLERANCE:
            raise RuntimeError("Ableton did not confirm the requested track-volume value")
        return {
            "ok": True,
            "schema": TRACK_VOLUME_ACTION_SCHEMA,
            "bridge_session_id": current_session,
            "action_id": action_id,
            "track_ref": current_track,
            "before_normalized": before,
            "after_normalized": after,
        }

    def _runtime(self):
        app = self.application()
        version = "%s.%s.%s" % (
            int(app.get_major_version()),
            int(app.get_minor_version()),
            int(app.get_bugfix_version()),
        )
        return {
            "host_family": "ABLETON_LIVE",
            "version": version,
            "edition": "Unknown",
            "os_name": platform.system() or "unknown",
            "machine": platform.machine() or "unknown",
        }

    def _workspace_id(self, song):
        getter = getattr(song, "get_data", None)
        if not callable(getter):
            return None
        value = getter(WORKSPACE_DATA_KEY, None)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _capture_snapshot(self):
        song = self.song()
        tempo = float(song.tempo)
        song_time = float(song.current_song_time)
        payload = {
            "schema": SCHEMA,
            "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
            "bridge_session_id": self._session_id_for_song(song),
            "workspace_id": self._workspace_id(song),
            "set_path_fingerprint": _set_path_fingerprint(song),
            "runtime": self._runtime(),
            "observed_at_epoch_seconds": int(time.time()),
            "tempo_bpm": tempo,
            "transport": {
                "is_playing": bool(song.is_playing),
                "current_song_time": song_time,
            },
            "selected_track": self._selected_track(song),
        }
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return payload


def create_instance(c_instance):
    return N0TEBridge(c_instance)
