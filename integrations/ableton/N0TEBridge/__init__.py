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
ADAPTER_VERSION = "3"
SCHEMA = "n0te.ableton-observation/v2"
WORKSPACE_DATA_KEY = "n0te.workspace_id.v1"
LIVE_CALL_TIMEOUT_SECONDS = 2.0
NOTICE_MAX_CHARS = 240
_NOTICE_MAX_BODY_BYTES = 2048
_SET_PATH_HASH_DOMAIN = b"n0te.ableton-set-path/v1\x00"
_ALLOWED_HOST_HEADERS = frozenset(
    {
        "127.0.0.1:9799",
        "localhost:9799",
    }
)


def _set_path_fingerprint(song):
    """Return a one-way stable fingerprint for a saved Live Set path.

    The raw filesystem path never crosses the bridge boundary. Unsaved Sets expose
    an empty file_path in Live and therefore intentionally return no durable path
    identity until the Set is saved.
    """

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


class _SnapshotHandler(BaseHTTPRequestHandler):
    server_version = "N0TEBridge/3"
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

    def _read_json_object(self):
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
        if length <= 0 or length > _NOTICE_MAX_BODY_BYTES:
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
        if self.path != "/snapshot":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            payload = self.server.bridge.request_snapshot()
        except Exception as error:
            self._json(503, {"ok": False, "error": "snapshot unavailable: %s" % error})
            return
        self._json(200, payload)

    def do_POST(self):
        if not self._request_is_local():
            self._json(403, {"ok": False, "error": "loopback host/origin policy rejected request"})
            return
        if self.path != "/notice":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            payload = self._read_json_object()
            if set(payload) != {"message"}:
                raise ValueError("notice body must contain only message")
            self.server.bridge.request_notice(payload["message"])
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)})
            return
        except Exception as error:
            self._json(503, {"ok": False, "error": "notice unavailable: %s" % error})
            return
        self._json(200, {"ok": True})

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
    """Dependency-light Ableton observation + advice-display bridge.

    The bridge exposes no tempo/transport/device/track setters, no Song selection,
    no calibration/provider input and no persistent Set writes. It observes Live on
    Live's main thread, serves one bounded JSON snapshot over IPv4 loopback, and may
    display a bounded N0TE advice notice through ControlSurface.show_message().
    The bridge session identifier is scoped to the current Live Song object so a
    save keeps continuity while loading another Set rotates session identity.
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
        self._log("observation/advice bridge listening on http://%s:%s" % (self.host, self.port))

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
