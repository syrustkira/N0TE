from __future__ import annotations

import html
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

from .app_runtime import ApplicationRuntime
from .audition import (
    InvalidByteRange,
    UnsupportedAuditionMedia,
    UnsatisfiableByteRange,
    inspect_audition_media,
    parse_byte_range,
)
from .consumer_upload import MaterialUploadParseError, parse_material_upload
from .instance import ProcessIdentity, ProcessProbe
from .material import SongMaterialError
from .profiles import ApplicationProfile, ApplicationProfiles, ProfileResolution
from .shell_design import SHELL_CSS
from .version_approval import (
    StaleVersionApprovalError,
    VersionApprovalBinding,
    VersionApprovalError,
    VersionApprovalService,
)

_LOOPBACK_HOST = "127.0.0.1"
_MAX_FORM_BYTES = 32768
_MAX_ARTIST_NAME = 120
_MAX_SONG_TITLE = 200
_MAX_SESSION_OBJECTIVE = 500
_MAX_SESSION_DEBRIEF = 1600
_MAX_SESSION_NEXT_ACTION = 500
_MAX_SESSION_CAPTURE = 1200
_NAV_ROUTES = {"/": "Home", "/song": "Song", "/now": "Now", "/settings": "Settings"}
_FOCUS_MODE_ORDER = ("MAKE", "FINISH", "MANAGE", "RELEASE", "PERFORM")
_FOCUS_SONG_DEFAULT_MODES = {"MAKE", "FINISH"}
_FOCUS_HINTS = {
    "MAKE": "Protect creating, exploring and getting the idea out.",
    "FINISH": "Protect the decisions that move the current work toward done.",
    "MANAGE": "Protect planning and Artist Headquarters work.",
    "RELEASE": "Protect release work without dragging it into a creative session.",
    "PERFORM": "Protect rehearsal and live-performance attention.",
}
_SESSION_CAPTURE_ORDER = (
    "OBSERVATION",
    "DECISION",
    "REJECTED_IDEA",
    "UNRESOLVED",
    "MARK",
)
_SESSION_CAPTURE_LABELS = {
    "OBSERVATION": "Observation",
    "DECISION": "Decision",
    "REJECTED_IDEA": "Rejected idea",
    "UNRESOLVED": "Unresolved",
    "MARK": "MARK",
}


class ConsumerShellError(RuntimeError):
    """Invalid or unsafe consumer-shell operation."""


class ConsumerShellRecoveryRequired(ConsumerShellError):
    """The shell cannot stop safely because canonical runtime recovery is required."""


@dataclass(frozen=True)
class ShellAddress:
    host: str
    port: int

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class _Action:
    kind: str
    value: str | None = None


@dataclass(frozen=True)
class _MediaGrant:
    song_id: str
    version_id: str
    asset_id: str
    sha256: str


@dataclass(frozen=True)
class _PageState:
    kind: str
    title: str
    eyebrow: str
    message: str
    artist_name: str | None = None
    song_title: str | None = None
    profiles: tuple[ApplicationProfile, ...] = ()


class _LoopbackHTTPServer(HTTPServer):
    allow_reuse_address = False


def _clean_human_text(value: str, field: str, *, maximum: int) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise ConsumerShellError(f"{field} must not be empty")
    if len(text) > maximum:
        raise ConsumerShellError(f"{field} is too long")
    return text


class ConsumerShell:
    """Local-first Artist Headquarters and bounded Song journey front door.

    Product semantics remain in ApplicationProfiles/ApplicationRuntime/Headquarters.
    This shell owns only bounded presentation, browser-session action authority and
    loopback transport. The server is deliberately single-threaded because the
    canonical SQLite Headquarters connection is thread-bound.
    """

    def __init__(
        self,
        *,
        data_root: str | Path,
        state_root: str | Path,
        process: ProcessIdentity,
        probe: ProcessProbe,
        port: int = 0,
    ) -> None:
        data = Path(data_root)
        state = Path(state_root)
        if not data.is_absolute() or not state.is_absolute():
            raise ConsumerShellError("data_root and state_root must be absolute")
        if not isinstance(process, ProcessIdentity):
            raise TypeError("process must be ProcessIdentity")
        if not callable(getattr(probe, "status", None)):
            raise TypeError("probe must implement status(process)")
        if isinstance(port, bool) or not isinstance(port, int) or not (0 <= port <= 65535):
            raise ConsumerShellError("port must be an integer from 0 to 65535")

        self.data_root = data
        self.state_root = state
        self.process = process
        self.probe = probe
        self.port = port
        self.profiles = ApplicationProfiles(data_root=data, state_root=state)
        self.runtime = ApplicationRuntime(data_root=data, state_root=state)

        self._csrf = secrets.token_urlsafe(32)
        self._actions: dict[str, _Action] = {}
        self._media_grants: dict[str, _MediaGrant] = {}
        self._server: _LoopbackHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._stop_requested = threading.Event()
        self._stop_error: str | None = None
        self._address: ShellAddress | None = None
        self._consumer_notice: str | None = None
        self._blocked_state: _PageState | None = None

    @property
    def address(self) -> ShellAddress:
        if self._address is None:
            raise ConsumerShellError("consumer shell is not started")
        return self._address

    @property
    def url(self) -> str:
        return self.address.origin + "/"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stopped.is_set()

    def start(self) -> ShellAddress:
        if self._thread is not None:
            raise ConsumerShellError("one ConsumerShell instance can be started only once")
        shell = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "N0TE"
            sys_version = ""

            def do_GET(self) -> None:  # noqa: N802
                shell._handle_get(self)

            def do_HEAD(self) -> None:  # noqa: N802
                shell._handle_head(self)

            def do_POST(self) -> None:  # noqa: N802
                shell._handle_post(self)

            def log_message(self, format: str, *args: object) -> None:
                return

        try:
            server = _LoopbackHTTPServer((_LOOPBACK_HOST, self.port), Handler)
        except OSError as exc:
            raise ConsumerShellError(f"local consumer shell could not bind safely: {exc}") from exc
        server.timeout = 0.1
        host, bound_port = server.server_address[:2]
        if host != _LOOPBACK_HOST:
            server.server_close()
            raise ConsumerShellError("consumer shell must bind only to IPv4 loopback")
        self._server = server
        self._address = ShellAddress(_LOOPBACK_HOST, int(bound_port))
        self._thread = threading.Thread(target=self._serve, name="n0te-consumer-shell", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            raise ConsumerShellError("consumer shell did not enter its serve loop")
        return self.address

    def _serve(self) -> None:
        server = self._server
        if server is None:
            self._stop_error = "consumer shell server disappeared before start"
            self._stopped.set()
            return
        self._ready.set()
        try:
            while True:
                if self._stop_requested.is_set():
                    if self.runtime.state != "STOPPED":
                        result = self.runtime.quit()
                        if result.status not in {"STOPPED", "ALREADY_STOPPED"}:
                            self._stop_error = "N0TE could not release the active Artist workspace safely"
                            self._stop_requested.clear()
                        else:
                            break
                    else:
                        break
                server.handle_request()
        finally:
            server.server_close()
            self._stopped.set()

    def stop(self, *, timeout: float = 2.0) -> None:
        if self._thread is None or self._stopped.is_set():
            return
        self._stop_requested.set()
        if not self._stopped.wait(timeout=timeout):
            raise ConsumerShellRecoveryRequired(
                self._stop_error or "consumer shell could not stop safely"
            )

    def wait_stopped(self, *, timeout: float = 2.0) -> bool:
        return self._stopped.wait(timeout=timeout)

    def _expected_host(self) -> str:
        return f"{self.address.host}:{self.address.port}"

    def _request_host_is_exact(self, handler: BaseHTTPRequestHandler) -> bool:
        return handler.headers.get("Host", "").strip() == self._expected_host()

    def _post_origin_is_allowed(self, handler: BaseHTTPRequestHandler) -> bool:
        origin = handler.headers.get("Origin")
        return origin is None or origin.strip() == self.address.origin

    @staticmethod
    def _path(handler: BaseHTTPRequestHandler) -> str:
        parsed = urlsplit(handler.path)
        if parsed.query or parsed.fragment:
            return "__invalid__"
        return parsed.path

    def _security_headers(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
        handler.send_header("Cache-Control", "no-store")

    def _send_bytes(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        payload: bytes,
        *,
        content_type: str,
    ) -> None:
        handler.send_response(status)
        self._security_headers(handler)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _send_html(self, handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
        self._send_bytes(
            handler,
            status,
            body.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )

    def _redirect(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        handler.send_response(303)
        self._security_headers(handler)
        handler.send_header("Location", path)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _new_action(self, kind: str, value: str | None = None) -> str:
        token = secrets.token_urlsafe(24)
        self._actions[token] = _Action(kind, value)
        return token

    def _consume_action(self, token: str, expected_kind: str) -> _Action | None:
        action = self._actions.pop(str(token), None)
        if action is None or action.kind != expected_kind:
            return None
        return action

    def _new_media_grant(self, grant: _MediaGrant) -> str:
        token = secrets.token_urlsafe(24)
        self._media_grants[token] = grant
        return token

    def _reset_actions(self) -> None:
        self._actions.clear()
        self._media_grants.clear()

    def _read_form(self, handler: BaseHTTPRequestHandler) -> Mapping[str, str] | None:
        content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return None
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > _MAX_FORM_BYTES:
            return None
        try:
            parsed = parse_qs(
                handler.rfile.read(length).decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=False,
            )
        except UnicodeDecodeError:
            return None
        values: dict[str, str] = {}
        for key, items in parsed.items():
            if len(items) != 1:
                return None
            values[str(key)] = str(items[0])
        return values

    def _form_authorized(self, form: Mapping[str, str]) -> bool:
        return secrets.compare_digest(form.get("csrf", ""), self._csrf)

    def _media_token(self, path: str) -> str | None:
        prefix = "/media/song-version/"
        if not path.startswith(prefix):
            return None
        token = path[len(prefix):]
        if not token or "/" in token:
            return None
        return token

    def _resolve_media_grant(self, token: str):
        if self.runtime.state != "RUNNING":
            raise ConsumerShellError("Artist Headquarters is not open for audition")
        grant = self._media_grants.get(token)
        if grant is None:
            raise ConsumerShellError("That audition link expired. Reload the Song and try again.")
        store = self.runtime.headquarters.store
        song = store.active_song()
        if song is None or song.id != grant.song_id:
            raise ConsumerShellError("The active Song changed. Reload the Song before auditioning a Version.")
        version = store.get_version(grant.version_id)
        if version is None or version.song_id != song.id:
            raise ConsumerShellError("That Version no longer belongs to the active Song.")
        if grant.asset_id not in store.version_asset_ids(version.id):
            raise ConsumerShellError("That material no longer belongs to this Version.")
        asset = store.get_asset(grant.asset_id)
        if asset is None or asset.song_id != song.id or asset.sha256 != grant.sha256:
            raise ConsumerShellError("That material no longer matches the rendered Version.")
        material = self.runtime.headquarters.materials.resolve_asset(asset)
        media = inspect_audition_media(material.path)
        if media.size_bytes != material.size_bytes:
            raise ConsumerShellError("That material changed after it was verified.")
        return media

    def _serve_media(self, handler: BaseHTTPRequestHandler, token: str, *, head_only: bool) -> None:
        try:
            media = self._resolve_media_grant(token)
        except UnsupportedAuditionMedia:
            self._send_html(handler, 415, self._simple_error("That local material format is not auditionable here."))
            return
        except (SongMaterialError, ConsumerShellError):
            self._send_html(handler, 409, self._simple_error("That audition is no longer safely available. Reload the Song."))
            return
        try:
            byte_range = parse_byte_range(handler.headers.get("Range"), size_bytes=media.size_bytes)
        except InvalidByteRange:
            self._send_html(handler, 400, self._simple_error("That media range request is not supported."))
            return
        except UnsatisfiableByteRange:
            handler.send_response(416)
            self._security_headers(handler)
            handler.send_header("Content-Range", f"bytes */{media.size_bytes}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return

        start = 0 if byte_range is None else byte_range.start
        end = media.size_bytes - 1 if byte_range is None else byte_range.end
        length = end - start + 1
        handler.send_response(200 if byte_range is None else 206)
        self._security_headers(handler)
        handler.send_header("Content-Type", media.content_type)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Length", str(length))
        if byte_range is not None:
            handler.send_header("Content-Range", byte_range.content_range)
        handler.end_headers()
        if head_only:
            return
        with media.path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ConsumerShellError("audition material ended before its verified size")
                handler.wfile.write(chunk)
                remaining -= len(chunk)

    def _handle_head(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._request_host_is_exact(handler):
            self._send_html(handler, 421, self._simple_error("This N0TE window is available only from its exact local address."))
            return
        path = self._path(handler)
        token = self._media_token(path)
        if token is None:
            self._send_html(handler, 405, self._simple_error("HEAD is available only for local audition media."))
            return
        self._serve_media(handler, token, head_only=True)

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._request_host_is_exact(handler):
            self._send_html(
                handler,
                421,
                self._simple_error("This N0TE window is available only from its exact local address."),
            )
            return
        path = self._path(handler)
        token = self._media_token(path)
        if token is not None:
            self._serve_media(handler, token, head_only=False)
            return
        if path == "/assets/shell.css":
            self._send_bytes(
                handler,
                200,
                SHELL_CSS.encode("utf-8"),
                content_type="text/css; charset=utf-8",
            )
            return
        if path not in _NAV_ROUTES:
            self._send_html(handler, 404, self._simple_error("That N0TE page is not available."))
            return
        try:
            if self.runtime.state != "RUNNING":
                state = self._ensure_runtime()
                if state is not None:
                    self._send_html(handler, 200, self._render_state(state, path="/"))
                    return
            self._send_html(handler, 200, self._render_running(path))
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE protected your local work, but this page could not be prepared. Try again from the Home page."
                ),
            )

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._request_host_is_exact(handler) or not self._post_origin_is_allowed(handler):
            self._send_html(
                handler,
                403,
                self._simple_error("That action did not come from this N0TE window."),
            )
            return
        path = self._path(handler)
        if path not in {
            "/profile/create",
            "/profile/select",
            "/song/start",
            "/song/material",
            "/song/version/resume",
            "/song/version/approve",
            "/session/start",
            "/session/capture",
            "/session/finish",
            "/focus/set",
            "/focus/end",
            "/quit",
        }:
            self._send_html(handler, 404, self._simple_error("That N0TE action is not available."))
            return
        if path == "/song/material":
            self._post_song_material(handler)
            return
        form = self._read_form(handler)
        if form is None or not self._form_authorized(form):
            self._send_html(
                handler,
                403,
                self._simple_error("That action expired. Reload N0TE and try again."),
            )
            return
        try:
            if path == "/profile/create":
                self._post_create_profile(handler, form)
            elif path == "/profile/select":
                self._post_select_profile(handler, form)
            elif path == "/song/start":
                self._post_start_song(handler, form)
            elif path == "/song/version/resume":
                self._post_resume_version(handler, form)
            elif path == "/song/version/approve":
                self._post_approve_version(handler, form)
            elif path == "/session/start":
                self._post_session_start(handler, form)
            elif path == "/session/capture":
                self._post_session_capture(handler, form)
            elif path == "/session/finish":
                self._post_session_finish(handler, form)
            elif path == "/focus/set":
                self._post_focus_set(handler, form)
            elif path == "/focus/end":
                self._post_focus_end(handler, form)
            else:
                self._post_quit(handler, form)
        except ConsumerShellError as exc:
            self._consumer_notice = str(exc)
            self._redirect(
                handler,
                "/song" if path.startswith("/session/") or path.startswith("/song/") else "/",
            )
        except Exception:
            self._send_html(
                handler,
                500,
                self._simple_error(
                    "N0TE stopped that action before it could become an unclear consumer state."
                ),
            )

    def _ensure_runtime(self) -> _PageState | None:
        if self._blocked_state is not None:
            state = self._blocked_state
            self._blocked_state = None
            return state
        if self.runtime.state == "RECOVERY_REQUIRED":
            return _PageState(
                "recovery",
                "Your Artist workspace needs recovery",
                "Protected state",
                "N0TE will not guess, steal ownership, or overwrite local creative state.",
            )
        resolution = self.profiles.resolve()
        if resolution.state == "NEEDS_CREATION":
            return _PageState(
                "create-profile",
                "Welcome to your Headquarters",
                "Start with the artist",
                "Tell N0TE the artist name for this local workspace. DAWs, AI, and services can wait until your work actually needs them.",
            )
        if resolution.state == "NEEDS_SELECTION":
            return _PageState(
                "select-profile",
                "Who are you working as today?",
                "Choose an Artist",
                "Each Artist workspace stays separate. Choosing one never merges or copies another Artist's history.",
                profiles=resolution.profiles,
            )
        if resolution.state == "RECOVERY_REQUIRED":
            return _PageState(
                "recovery",
                "A local Artist workspace needs recovery",
                "Protected state",
                "N0TE found local profile state it cannot safely interpret. Nothing was merged, replaced, or silently repaired.",
            )
        if resolution.state == "BOOTSTRAP_BUSY":
            return _PageState(
                "blocked",
                "N0TE is already preparing an Artist workspace",
                "Another N0TE action is active",
                "Return to the other N0TE window, or try again after it finishes.",
            )
        if resolution.state in {"CREATED", "SELECTED_EXISTING"} and resolution.selected_profile_id:
            return self._launch_profile(resolution.selected_profile_id)
        raise ConsumerShellError("N0TE could not determine a safe Artist workspace")

    def _launch_profile(self, profile_id: str) -> _PageState | None:
        result = self.runtime.launch(
            profile_id=profile_id,
            process=self.process,
            probe=self.probe,
        )
        if result.status in {"STARTED", "ALREADY_RUNNING"}:
            self._blocked_state = None
            return None
        states = {
            "REOPEN_EXISTING": _PageState(
                "blocked",
                "N0TE is already open for this Artist",
                "Existing window",
                "Return to the N0TE window that already owns this Artist workspace instead of opening a duplicate.",
            ),
            "HELD_BY_OTHER": _PageState(
                "blocked",
                "This Artist is already open",
                "Existing N0TE session",
                "Use the existing N0TE window. Your local Artist workspace was not changed.",
            ),
            "UNCERTAIN": _PageState(
                "recovery",
                "N0TE cannot prove this Artist is safe to reopen yet",
                "Protected state",
                "N0TE kept the existing ownership record instead of guessing. Close any other N0TE session and try again.",
            ),
            "RECOVERY_REQUIRED": _PageState(
                "recovery",
                "This Artist workspace could not open safely",
                "Recovery needed",
                "Your local creative state was not replaced. Try again after the workspace is safe to reopen.",
            ),
            "START_FAILED": _PageState(
                "recovery",
                "This Artist workspace could not open safely",
                "Recovery needed",
                "Your local creative state was not replaced. Try again after the workspace is safe to reopen.",
            ),
        }
        if result.status in states:
            return states[result.status]
        raise ConsumerShellError("N0TE returned an unsupported Artist launch state")

    def _post_create_profile(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        action = self._consume_action(form.get("action", ""), "profile-create")
        if action is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That create action was already handled or expired."),
            )
            return
        artist_name = _clean_human_text(
            form.get("artist_name", ""),
            "Artist name",
            maximum=_MAX_ARTIST_NAME,
        )
        resolution = self.profiles.resolve(
            artist_name=artist_name,
            process=self.process,
            probe=self.probe,
        )
        if resolution.state not in {"CREATED", "SELECTED_EXISTING"} or not resolution.selected_profile_id:
            self._blocked_state = self._resolution_state(resolution)
            self._redirect(handler, "/")
            return
        self._blocked_state = self._launch_profile(resolution.selected_profile_id)
        self._redirect(handler, "/")

    def _post_select_profile(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        action = self._consume_action(form.get("action", ""), "profile-select")
        if action is None or action.value is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That Artist choice was already handled or expired."),
            )
            return
        resolution = self.profiles.resolve(selected_profile_id=action.value)
        if resolution.state != "SELECTED_EXISTING" or not resolution.selected_profile_id:
            self._blocked_state = self._resolution_state(resolution)
            self._redirect(handler, "/")
            return
        self._blocked_state = self._launch_profile(resolution.selected_profile_id)
        self._redirect(handler, "/")

    def _post_start_song(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before starting a Song."),
            )
            return
        action = self._consume_action(form.get("action", ""), "song-start")
        if action is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That Song action was already handled or expired."),
            )
            return
        title = _clean_human_text(
            form.get("song_title", ""),
            "Song title",
            maximum=_MAX_SONG_TITLE,
        )
        song = self.runtime.headquarters.store.create_song(title)
        self._consumer_notice = f"{song.title} is now your active Song."
        self._redirect(handler, "/song")

    def _post_song_material(self, handler: BaseHTTPRequestHandler) -> None:
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before adding Song material."),
            )
            return
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        try:
            with parse_material_upload(
                handler.rfile,
                content_type=handler.headers.get("Content-Type", ""),
                content_length=length,
            ) as upload:
                if not secrets.compare_digest(upload.csrf, self._csrf):
                    self._send_html(
                        handler,
                        403,
                        self._simple_error("That material action expired. Reload the Song and try again."),
                    )
                    return
                action = self._consume_action(upload.action, "song-material")
                if action is None or action.value is None:
                    self._send_html(
                        handler,
                        409,
                        self._simple_error("That material action was already handled or expired."),
                    )
                    return
                song = self.runtime.headquarters.store.active_song()
                if song is None or song.id != action.value:
                    self._send_html(
                        handler,
                        409,
                        self._simple_error("The active Song changed. Reload the Song before adding this material."),
                    )
                    return
                imported = self.runtime.headquarters.materials.ingest_stream(
                    song.id,
                    filename=upload.filename,
                    stream=upload.file_stream(),
                    declared_size=upload.size_bytes,
                )
        except MaterialUploadParseError:
            self._send_html(
                handler,
                400,
                self._simple_error("That local file upload was malformed or outside N0TE's safe ingest bounds."),
            )
            return
        except SongMaterialError:
            self._consumer_notice = "N0TE could not preserve that local file safely, so the Song lineage was not advanced."
            self._redirect(handler, "/song")
            return
        self._consumer_notice = f"{imported.asset.name} is preserved locally as the current Song version. Approval was not changed."
        self._redirect(handler, "/song")

    def _post_resume_version(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before resuming a Song Version."),
            )
            return
        action = self._consume_action(form.get("action", ""), "song-version-resume")
        if action is None or action.value is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That Version action was already handled or expired."),
            )
            return
        parts = action.value.split("|", 2)
        if len(parts) != 3:
            self._send_html(handler, 409, self._simple_error("That Version action is no longer valid."))
            return
        song_id, target_version_id, expected_current_id = parts
        store = self.runtime.headquarters.store
        song = store.active_song()
        if song is None or song.id != song_id:
            self._send_html(
                handler,
                409,
                self._simple_error("The active Song changed. Reload the Song before resuming a Version."),
            )
            return
        if song.current_version_id != expected_current_id:
            self._send_html(
                handler,
                409,
                self._simple_error("The current Version changed. Reload the Song before choosing what to resume."),
            )
            return
        target = store.get_version(target_version_id)
        if target is None or target.song_id != song.id:
            self._send_html(
                handler,
                409,
                self._simple_error("That Version does not belong to the active Song."),
            )
            return
        if target.id == song.current_version_id:
            self._send_html(
                handler,
                409,
                self._simple_error("That Version is already current."),
            )
            return
        store.set_current_version(song.id, target.id)
        self._consumer_notice = f"Resumed {target.label} as the current Version. Approval was not changed."
        self._redirect(handler, "/song")

    def _post_approve_version(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before approving a Song Version."),
            )
            return
        action = self._consume_action(form.get("action", ""), "song-version-approve")
        if action is None or action.value is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That approval action was already handled or expired."),
            )
            return
        parts = action.value.split("|", 3)
        if len(parts) != 4:
            self._send_html(handler, 409, self._simple_error("That approval action is no longer valid."))
            return
        song_id, target_version_id, current_marker, approved_marker = parts
        binding = VersionApprovalBinding(
            song_id=song_id,
            target_version_id=target_version_id,
            expected_current_version_id=None if current_marker == "~" else current_marker,
            expected_approved_version_id=None if approved_marker == "~" else approved_marker,
        )
        try:
            result = VersionApprovalService(self.runtime.headquarters.store).approve(binding)
        except (StaleVersionApprovalError, VersionApprovalError):
            self._send_html(
                handler,
                409,
                self._simple_error("The Song Version state changed. Reload the Song before approving a Version."),
            )
            return
        self._consumer_notice = (
            f"Approved {result.approved_version.label}. The current Version was not changed. "
            "This approval does not publish or release anything."
        )
        self._redirect(handler, "/song")

    def _post_session_start(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before starting a work Session."),
            )
            return
        action = self._consume_action(form.get("action", ""), "session-start")
        if action is None or action.value is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That work Session action was already handled or expired."),
            )
            return
        store = self.runtime.headquarters.store
        song = store.active_song()
        if song is None or song.id != action.value:
            self._send_html(
                handler,
                409,
                self._simple_error("The active Song changed. Reload the Song before starting this Session."),
            )
            return
        latest = self.runtime.headquarters.sessions.latest_for_song(song.id)
        if latest is not None and latest.state == "OPEN":
            self._send_html(
                handler,
                409,
                self._simple_error("This Song already has an open work Session."),
            )
            return
        objective = _clean_human_text(
            form.get("objective", ""),
            "Session objective",
            maximum=_MAX_SESSION_OBJECTIVE,
        )
        session = self.runtime.headquarters.sessions.start_session(
            song_id=song.id,
            objective=objective,
        )
        self._consumer_notice = f"Work Session started: {session.objective}"
        self._redirect(handler, "/song")

    def _post_session_capture(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before capturing Session history."),
            )
            return
        action = self._consume_action(form.get("action", ""), "session-capture")
        if action is None or action.value is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That capture action was already handled or expired."),
            )
            return
        session_id, separator, item_kind = action.value.partition("|")
        if not separator or item_kind not in _SESSION_CAPTURE_ORDER:
            self._send_html(
                handler,
                409,
                self._simple_error("That capture action is no longer valid."),
            )
            return
        store = self.runtime.headquarters.store
        song = store.active_song()
        session = self.runtime.headquarters.sessions.get_session(session_id)
        if (
            song is None
            or session is None
            or session.song_id != song.id
            or session.state != "OPEN"
        ):
            self._send_html(
                handler,
                409,
                self._simple_error("That work Session is no longer open for this Song."),
            )
            return
        latest = self.runtime.headquarters.sessions.latest_for_song(song.id)
        if latest is None or latest.id != session.id or latest.state != "OPEN":
            self._send_html(
                handler,
                409,
                self._simple_error("The Song Session changed. Reload the Song before capturing this note."),
            )
            return
        body = _clean_human_text(
            form.get("body", ""),
            "Session capture",
            maximum=_MAX_SESSION_CAPTURE,
        )
        self.runtime.headquarters.sessions.append_scratch(
            session.id,
            kind=item_kind,
            body=body,
        )
        self._consumer_notice = f"{_SESSION_CAPTURE_LABELS[item_kind]} captured."
        self._redirect(handler, "/song")

    def _post_session_finish(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before finishing a work Session."),
            )
            return
        action = self._consume_action(form.get("action", ""), "session-finish")
        if action is None or action.value is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That finish action was already handled or expired."),
            )
            return
        store = self.runtime.headquarters.store
        song = store.active_song()
        session = self.runtime.headquarters.sessions.get_session(action.value)
        if (
            song is None
            or session is None
            or session.song_id != song.id
            or session.state != "OPEN"
        ):
            self._send_html(
                handler,
                409,
                self._simple_error("That work Session is no longer the open Session for this Song."),
            )
            return
        latest = self.runtime.headquarters.sessions.latest_for_song(song.id)
        if latest is None or latest.id != session.id or latest.state != "OPEN":
            self._send_html(
                handler,
                409,
                self._simple_error("The Song Session changed. Reload the Song before finishing it."),
            )
            return
        debrief = _clean_human_text(
            form.get("debrief", ""),
            "Session debrief",
            maximum=_MAX_SESSION_DEBRIEF,
        )
        next_action = _clean_human_text(
            form.get("next_action", ""),
            "Next action",
            maximum=_MAX_SESSION_NEXT_ACTION,
        )
        closed = self.runtime.headquarters.sessions.close_session(
            session.id,
            debrief_summary=debrief,
            next_action=next_action,
        )
        self._consumer_notice = f"Work Session finished. Next: {closed.next_action}"
        self._redirect(handler, "/song")

    def _post_focus_set(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before choosing Focus."),
            )
            return
        action = self._consume_action(form.get("action", ""), "focus-set")
        if action is None or action.value not in _FOCUS_MODE_ORDER:
            self._send_html(
                handler,
                409,
                self._simple_error("That Focus choice was already handled or expired."),
            )
            return
        store = self.runtime.headquarters.store
        song = store.active_song()
        song_id = (
            song.id
            if song is not None and action.value in _FOCUS_SONG_DEFAULT_MODES
            else None
        )
        focus = self.runtime.headquarters.attention.start_focus(
            action.value,
            song_id=song_id,
        )
        if focus.song_id is not None:
            bound_song = store.get_song(focus.song_id)
            label = "this Song" if bound_song is None else bound_song.title
            self._consumer_notice = f"{focus.mode} Focus is active for {label}."
        else:
            self._consumer_notice = f"{focus.mode} Focus is active for your Artist Headquarters."
        self._redirect(handler, "/now")

    def _post_focus_end(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        if self.runtime.state != "RUNNING":
            self._send_html(
                handler,
                409,
                self._simple_error("Open an Artist workspace before changing Focus."),
            )
            return
        action = self._consume_action(form.get("action", ""), "focus-end")
        if action is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That End Focus action was already handled or expired."),
            )
            return
        ended = self.runtime.headquarters.attention.end_focus()
        self._consumer_notice = (
            "No Focus Session is active."
            if ended is None
            else f"{ended.mode} Focus ended. Headquarters is open again."
        )
        self._redirect(handler, "/now")

    def _post_quit(
        self,
        handler: BaseHTTPRequestHandler,
        form: Mapping[str, str],
    ) -> None:
        action = self._consume_action(form.get("action", ""), "quit")
        if action is None:
            self._send_html(
                handler,
                409,
                self._simple_error("That Quit action was already handled or expired."),
            )
            return
        result_status = (
            "ALREADY_STOPPED"
            if self.runtime.state == "STOPPED"
            else self.runtime.quit().status
        )
        if result_status not in {"STOPPED", "ALREADY_STOPPED"}:
            self._send_html(
                handler,
                503,
                self._render_state(
                    _PageState(
                        "recovery",
                        "N0TE could not finish quitting safely",
                        "Recovery needed",
                        "Your Artist workspace remains protected. N0TE will keep this local window available instead of pretending it closed cleanly.",
                    ),
                    path="/settings",
                ),
            )
            return
        self._send_html(handler, 200, self._closed_page())
        self._stop_requested.set()

    def _resolution_state(self, resolution: ProfileResolution) -> _PageState:
        if resolution.state == "NEEDS_SELECTION":
            return _PageState(
                "select-profile",
                "Who are you working as today?",
                "Choose an Artist",
                "Each Artist workspace stays separate.",
                profiles=resolution.profiles,
            )
        if resolution.state == "BOOTSTRAP_BUSY":
            return _PageState(
                "blocked",
                "N0TE is already preparing an Artist workspace",
                "Another N0TE action is active",
                "Return to the other N0TE window, or try again after it finishes.",
            )
        return _PageState(
            "recovery",
            "A local Artist workspace needs recovery",
            "Protected state",
            "N0TE stopped before merging, replacing, or guessing about local Artist state.",
        )

    def _running_state(self, path: str) -> _PageState:
        store = self.runtime.headquarters.store
        artist = store.artist()
        song = store.active_song()
        if path == "/song":
            if song is None:
                return _PageState(
                    "running-no-song",
                    "Start with the Song",
                    "Creative Studio",
                    "Name the Song you want to work on. N0TE will make it the active Song and keep it selected across Headquarters.",
                    artist_name=artist.display_name,
                )
            return _PageState(
                "running-song",
                song.title,
                "Active Song",
                "Hear preserved local audio Versions, understand their lineage, approve the exact Version you mean, and keep work Session continuity around that decision.",
                artist_name=artist.display_name,
                song_title=song.title,
            )
        if path == "/now":
            return _PageState(
                "running-now",
                "What matters now",
                "Attention",
                "Choose the kind of work N0TE should protect. Focus changes attention posture only; it does not send, publish, purchase, connect or mutate a DAW.",
                artist_name=artist.display_name,
                song_title=None if song is None else song.title,
            )
        if path == "/settings":
            return _PageState(
                "running-settings",
                "Your N0TE",
                "Settings",
                "This shell is local. DAWs, AI, and external services are set up only when a job needs them.",
                artist_name=artist.display_name,
                song_title=None if song is None else song.title,
            )
        if song is None:
            return _PageState(
                "running-no-song",
                "What are we making today?",
                "Artist Headquarters",
                "Start a Song now. N0TE can learn the rest of your setup when the work actually asks for it.",
                artist_name=artist.display_name,
            )
        return _PageState(
            "running-home",
            "Pick up where you left off",
            "Artist Headquarters",
            "Your active Song is ready. N0TE keeps the Artist and Song context intact while you move through Headquarters.",
            artist_name=artist.display_name,
            song_title=song.title,
        )

    def _render_running(self, path: str) -> str:
        self._blocked_state = None
        return self._render_state(self._running_state(path), path=path)

    def _render_state(self, state: _PageState, *, path: str) -> str:
        self._reset_actions()
        notice = self._consumer_notice
        self._consumer_notice = None
        nav = self._nav(path) if self.runtime.state == "RUNNING" else ""
        content = self._state_content(state)
        notice_html = (
            ""
            if not notice
            else f'<div class="notice" role="status" aria-live="polite">{html.escape(notice)}</div>'
        )
        artist_context = (
            f'<span class="local-badge">{html.escape(state.artist_name)} · Local</span>'
            if state.artist_name
            else '<span class="local-badge">Local-first</span>'
        )
        focus_context = ""
        if self.runtime.state == "RUNNING":
            focus = self.runtime.headquarters.attention.active_focus()
            if focus is not None:
                focus_context = (
                    f'<span class="local-badge">{html.escape(focus.mode.title())} Focus</span>'
                )
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>{html.escape(state.title)} · N0TE</title>
  <link rel="stylesheet" href="/assets/shell.css">
</head>
<body>
<a class="skip-link" href="#main">Skip to your work</a>
<div class="shell">
  <header class="topbar">
    <a class="brand" href="/" aria-label="N0TE Artist Headquarters home"><span class="brand-mark" aria-hidden="true">N0</span><span>N0TE</span></a>
    <div class="row">{focus_context}{artist_context}</div>
  </header>
  <div class="layout">
    {nav}
    <main id="main" tabindex="-1">
      <section class="hero" aria-labelledby="page-title">
        <p class="eyebrow">{html.escape(state.eyebrow)}</p>
        <h1 id="page-title">{html.escape(state.title)}</h1>
        <p class="lede">{html.escape(state.message)}</p>
      </section>
      {notice_html}
      {content}
    </main>
  </div>
  <footer>N0TE keeps the Artist and Song above individual tools.</footer>
</div>
</body>
</html>"""

    def _nav(self, current: str) -> str:
        links = []
        for route, label in _NAV_ROUTES.items():
            current_attr = ' aria-current="page"' if route == current else ""
            links.append(f'<a href="{route}"{current_attr}>{html.escape(label)}</a>')
        return f'<nav class="nav" aria-label="Headquarters">{"".join(links)}</nav>'

    def _hidden(self, action_token: str) -> str:
        return (
            f'<input type="hidden" name="csrf" value="{html.escape(self._csrf, quote=True)}">'
            f'<input type="hidden" name="action" value="{html.escape(action_token, quote=True)}">'
        )

    def _csrf_hidden(self) -> str:
        return f'<input type="hidden" name="csrf" value="{html.escape(self._csrf, quote=True)}">'

    def _start_song_form(self) -> str:
        token = self._new_action("song-start")
        return f"""<form class="stack" method="post" action="/song/start">
{self._hidden(token)}
<div><label for="song-title">Song title</label><input id="song-title" name="song_title" type="text" maxlength="{_MAX_SONG_TITLE}" autocomplete="off" required></div>
<div class="row"><button class="primary" type="submit">Start this Song</button></div>
</form>"""

    def _material_upload_form(self, song_id: str) -> str:
        token = self._new_action("song-material", song_id)
        return f"""<form class="stack" method="post" action="/song/material" enctype="multipart/form-data">
{self._hidden(token)}
<div><label for="song-material">Add a local demo, mix, MIDI file, or other Song material</label><input id="song-material" name="material" type="file" required></div>
<div class="row"><button class="primary" type="submit">Add to this Song</button></div>
<p class="muted">N0TE preserves a verified local copy and creates a new current Version. Importing never approves the Version.</p>
</form>"""

    def _audition_control(self, song_id: str, version_id: str, view) -> str:
        if view.status != "VERIFIED_MANAGED":
            return ""
        try:
            material = self.runtime.headquarters.materials.resolve_asset(view.asset)
            inspect_audition_media(material.path)
        except UnsupportedAuditionMedia:
            return '<p class="muted">Local audition is not available for this material format.</p>'
        except SongMaterialError:
            return ""
        token = self._new_media_grant(
            _MediaGrant(
                song_id=song_id,
                version_id=version_id,
                asset_id=view.asset.id,
                sha256=view.asset.sha256,
            )
        )
        name = html.escape(view.asset.name)
        src = html.escape(f"/media/song-version/{token}", quote=True)
        return (
            f'<audio controls preload="metadata" src="{src}" aria-label="Audition {name}">'
            'Your browser cannot play this local audio Version.'
            '</audio><p class="muted">Local playback only. No loudness matching or A/B processing is applied.</p>'
        )

    def _material_status(self, version_id: str) -> str:
        version = self.runtime.headquarters.store.get_version(version_id)
        if version is None:
            raise ConsumerShellError("Song Version is missing")
        views = self.runtime.headquarters.materials.version_materials(version_id)
        if not views:
            return '<p class="muted">No material attached to this Version.</p>'
        rows = []
        for view in views:
            if view.status == "VERIFIED_MANAGED":
                status = "Verified local copy"
                status_class = "good"
            elif view.status == "INTEGRITY_ERROR":
                status = "Protected integrity problem"
                status_class = "caution"
            else:
                status = "External reference"
                status_class = "caution"
            audition = self._audition_control(version.song_id, version.id, view)
            rows.append(
                '<li class="stack"><strong>'
                + html.escape(view.asset.name)
                + '</strong><p class="status '
                + status_class
                + '">'
                + html.escape(status)
                + '</p>'
                + audition
                + '</li>'
            )
        return '<ul class="stack" aria-label="Song Version material">' + "".join(rows) + "</ul>"

    def _material_card(self, song) -> str:
        store = self.runtime.headquarters.store
        current = None if song.current_version_id is None else store.get_version(song.current_version_id)
        if song.current_version_id is not None and current is None:
            raise ConsumerShellError("current Song Version is missing")
        if current is None:
            current_html = '<p class="status caution">No material Version yet</p><p>Add the actual file you are working from so N0TE can preserve the Song, not only its title and notes.</p>'
        else:
            current_html = (
                '<p>Current Version</p>'
                f'<p class="song-name">{html.escape(current.label)}</p>'
                f'{self._material_status(current.id)}'
            )
        if song.approved_version_id is None:
            approval = '<p class="status caution">No approved Version yet</p><p>Current and approved stay separate. Adding material does not approve it.</p>'
        elif song.approved_version_id == song.current_version_id:
            approval = '<p class="status good">Current Version is approved</p>'
        else:
            approved = store.get_version(song.approved_version_id)
            if approved is None:
                raise ConsumerShellError("approved Song Version is missing")
            approval = (
                '<p>Approved Version</p>'
                f'<p><strong>{html.escape(approved.label)}</strong></p>'
                '<p class="status caution">Approved remains different from current</p>'
            )
        return (
            '<div class="card"><h2>Your Song material</h2>'
            f'{current_html}{approval}{self._material_upload_form(song.id)}</div>'
        )

    def _version_history_card(self, song) -> str:
        store = self.runtime.headquarters.store
        versions = store.versions_for_song(song.id)
        if not versions:
            return (
                '<div class="card"><h2>Version history</h2>'
                '<p class="muted">Your Version history starts when you add Song material.</p></div>'
            )
        by_id = {version.id: version for version in versions}
        rows: list[str] = []
        for version in reversed(versions):
            states: list[str] = []
            if version.id == song.current_version_id:
                states.append('<span class="status good">Current</span>')
            if version.id == song.approved_version_id:
                states.append('<span class="status good">Approved</span>')
            if not states:
                states.append('<span class="status">History</span>')
            parent = by_id.get(version.parent_version_id) if version.parent_version_id else None
            parent_html = (
                '<p class="muted">First preserved Version</p>'
                if version.parent_version_id is None
                else (
                    f'<p class="muted">Based on Version {parent.ordinal}: {html.escape(parent.label)}</p>'
                    if parent is not None
                    else '<p class="status caution">Parent lineage could not be read safely</p>'
                )
            )
            resume = ""
            if version.id != song.current_version_id:
                if song.current_version_id is None:
                    raise ConsumerShellError("Version history exists without a current Version")
                token = self._new_action(
                    "song-version-resume",
                    f"{song.id}|{version.id}|{song.current_version_id}",
                )
                resume = (
                    '<form method="post" action="/song/version/resume">'
                    f'{self._hidden(token)}'
                    '<button type="submit">Resume from this Version</button></form>'
                )
            approve = ""
            if version.id != song.approved_version_id:
                current_marker = song.current_version_id or "~"
                approved_marker = song.approved_version_id or "~"
                token = self._new_action(
                    "song-version-approve",
                    f"{song.id}|{version.id}|{current_marker}|{approved_marker}",
                )
                exact_label = html.escape(f"Approve Version {version.ordinal}: {version.label}")
                approve = (
                    '<form class="stack" method="post" action="/song/version/approve">'
                    f'{self._hidden(token)}'
                    f'<button type="submit">{exact_label}</button>'
                    '<p class="muted">Marks this exact Version approved in N0TE. It does not make it current or publish, release, send, or purchase anything.</p>'
                    '</form>'
                )
            rows.append(
                '<li class="stack">'
                f'<p><strong>Version {version.ordinal}: {html.escape(version.label)}</strong></p>'
                f'<div class="row">{"".join(states)}</div>'
                f'{parent_html}{self._material_status(version.id)}{resume}{approve}'
                '</li>'
            )
        return (
            '<div class="card"><h2>Version history</h2>'
            '<p>Hear preserved local audio, choose which Version is current, and explicitly approve the exact Version you mean. Current and approved remain separate.</p>'
            f'<ol class="stack" aria-label="Song Version history">{"".join(rows)}</ol></div>'
        )

    def _start_session_form(self, song_id: str) -> str:
        token = self._new_action("session-start", song_id)
        return f"""<form class="stack" method="post" action="/session/start">
{self._hidden(token)}
<div><label for="session-objective">What are you trying to accomplish?</label><textarea id="session-objective" name="objective" maxlength="{_MAX_SESSION_OBJECTIVE}" rows="3" required></textarea></div>
<div class="row"><button class="primary" type="submit">Start work Session</button></div>
</form>"""

    def _capture_session_form(self, session_id: str) -> str:
        buttons = []
        for item_kind in _SESSION_CAPTURE_ORDER:
            token = self._new_action("session-capture", f"{session_id}|{item_kind}")
            buttons.append(
                '<button type="submit" name="action" '
                f'value="{html.escape(token, quote=True)}">'
                f'{html.escape(_SESSION_CAPTURE_LABELS[item_kind])}</button>'
            )
        return f"""<form class="stack" method="post" action="/session/capture">
{self._csrf_hidden()}
<div><label for="session-capture">Capture what matters</label><textarea id="session-capture" name="body" maxlength="{_MAX_SESSION_CAPTURE}" rows="3" required></textarea></div>
<div class="row">{"".join(buttons)}</div>
<p class="muted">Choose what this is. N0TE keeps it in this work Session; it does not silently turn the note into permanent Song doctrine.</p>
</form>"""

    def _session_history(self, session_id: str) -> str:
        items = self.runtime.headquarters.sessions.items_for_session(session_id)
        if not items:
            return '<p class="muted">Nothing captured in this Session yet.</p>'
        rows = "".join(
            '<li><strong>'
            f'{html.escape(_SESSION_CAPTURE_LABELS[item.kind])}'
            '</strong><p>'
            f'{html.escape(item.body)}'
            '</p></li>'
            for item in items
        )
        return f'<ol class="stack" aria-label="Work Session history">{rows}</ol>'

    def _finish_session_form(self, session_id: str) -> str:
        token = self._new_action("session-finish", session_id)
        return f"""<form class="stack" method="post" action="/session/finish">
{self._hidden(token)}
<div><label for="session-debrief">What changed or became clear?</label><textarea id="session-debrief" name="debrief" maxlength="{_MAX_SESSION_DEBRIEF}" rows="5" required></textarea></div>
<div><label for="session-next-action">What should you do next?</label><input id="session-next-action" name="next_action" type="text" maxlength="{_MAX_SESSION_NEXT_ACTION}" autocomplete="off" required></div>
<div class="row"><button class="primary" type="submit">Finish Session</button></div>
</form>"""

    def _song_content(self, state: _PageState) -> str:
        assert state.song_title is not None
        store = self.runtime.headquarters.store
        song = store.active_song()
        if song is None or song.title != state.song_title:
            raise ConsumerShellError("active Song changed while preparing the Song page")
        latest = self.runtime.headquarters.sessions.latest_for_song(song.id)
        song_card = (
            '<div class="card"><h2>Your active Song</h2>'
            f'<p class="song-name">{html.escape(song.title)}</p>'
            '<p class="status good">Song context active</p></div>'
        )
        material_card = self._material_card(song)
        version_history_card = self._version_history_card(song)
        if latest is None:
            session_card = (
                '<div class="card"><h2>Start this work Session</h2>'
                '<p>Give this stretch of work one clear objective. N0TE keeps it with the Song so you can leave and come back without reconstructing your intent.</p>'
                f'{self._start_session_form(song.id)}</div>'
            )
            history_card = ""
        elif latest.state == "OPEN":
            session_card = (
                '<div class="card"><h2>Current work Session</h2>'
                '<p class="status good">Session open</p>'
                '<p>Objective</p>'
                f'<p class="song-name">{html.escape(latest.objective)}</p>'
                '<p>Capture useful decisions and loose ends while you work. When the stretch is done, finish with what changed and the next concrete action.</p>'
                f'{self._capture_session_form(latest.id)}'
                f'{self._finish_session_form(latest.id)}</div>'
            )
            history_card = (
                '<div class="card"><h2>What happened in this Session</h2>'
                f'{self._session_history(latest.id)}</div>'
            )
        else:
            assert latest.debrief_summary is not None and latest.next_action is not None
            session_card = (
                '<div class="card"><h2>Pick up the thread</h2>'
                '<p class="status good">Last Session closed</p>'
                '<p>Last objective</p>'
                f'<p><strong>{html.escape(latest.objective)}</strong></p>'
                '<p>What happened</p>'
                f'<p>{html.escape(latest.debrief_summary)}</p>'
                '<p>Next action</p>'
                f'<p class="song-name">{html.escape(latest.next_action)}</p>'
                '<p>That next action belongs to this Song Session history. Start a new Session when you are ready to work it.</p>'
                f'{self._start_session_form(song.id)}</div>'
            )
            history_card = (
                '<div class="card"><h2>Last Session history</h2>'
                f'{self._session_history(latest.id)}</div>'
            )
        return f'<section class="grid">{song_card}{material_card}{version_history_card}{session_card}{history_card}</section>'

    def _focus_content(self, state: _PageState) -> str:
        focus = self.runtime.headquarters.attention.active_focus()
        store = self.runtime.headquarters.store
        mode_forms: list[str] = []
        for mode in _FOCUS_MODE_ORDER:
            token = self._new_action("focus-set", mode)
            current = focus is not None and focus.mode == mode
            button_class = "primary" if current else ""
            pressed = "true" if current else "false"
            mode_forms.append(
                f'<form method="post" action="/focus/set">{self._hidden(token)}'
                f'<button class="{button_class}" type="submit" aria-pressed="{pressed}">'
                f'{html.escape(mode.title())}</button></form>'
            )
        mode_buttons = "".join(mode_forms)

        if focus is None:
            status = '<p class="status caution">No Focus Session active</p>'
            binding = (
                f'<p>Make and Finish will follow {html.escape(state.song_title)}. Other modes stay Artist-wide unless a later journey supplies a more exact context.</p>'
                if state.song_title
                else '<p>You can choose an Artist-wide Focus now. Make or Finish can be rebound once a Song is active.</p>'
            )
            end_form = ""
        else:
            status = f'<p class="status good">{html.escape(focus.mode.title())} Focus active</p>'
            if focus.song_id is None:
                binding = '<p>Scope: your Artist Headquarters.</p>'
            else:
                song = store.get_song(focus.song_id)
                song_title = "this Song" if song is None else song.title
                binding = f'<p>Focused Song: <strong>{html.escape(song_title)}</strong></p>'
            end_token = self._new_action("focus-end")
            end_form = (
                f'<form method="post" action="/focus/end">{self._hidden(end_token)}'
                '<button type="submit">End Focus</button></form>'
            )

        hints = "".join(
            f'<p><strong>{html.escape(mode.title())}</strong> · {html.escape(_FOCUS_HINTS[mode])}</p>'
            for mode in _FOCUS_MODE_ORDER
        )
        return (
            '<section class="grid">'
            '<div class="card"><h2>Your Focus</h2>'
            f'{status}{binding}<div class="row">{mode_buttons}{end_form}</div></div>'
            '<div class="card"><h2>Choose the lane, not the plumbing</h2>'
            f'{hints}<p>Changing Focus only changes what Headquarters should protect. It does not perform work in your DAW or on external services.</p></div>'
            '</section>'
        )

    def _recovery_content(self) -> str:
        if self.runtime.state == "RECOVERY_REQUIRED":
            token = self._new_action("quit")
            return f"""<section class="grid"><div class="card"><h2>Nothing was overwritten</h2><p class="status caution">Protected local state</p><p>N0TE still owns this Artist workspace because the previous close could not finish safely. Retry the same safe cleanup; N0TE will not claim it closed until Headquarters and its exact lease are released.</p><form method="post" action="/quit">{self._hidden(token)}<button class="danger" type="submit">Retry safe close</button></form></div></section>"""
        return '<section class="grid"><div class="card"><h2>Nothing was overwritten</h2><p class="status caution">Protected local state</p><p>Try Home again after the other N0TE session or recovery condition is resolved.</p><a class="button" href="/">Try again</a></div></section>'

    def _state_content(self, state: _PageState) -> str:
        if state.kind == "create-profile":
            token = self._new_action("profile-create")
            return f"""<section class="grid" aria-label="Get started"><div class="card"><h2>Your Artist workspace</h2><p>One local Artist World keeps your Songs and context separate from everyone else's.</p><form class="stack" method="post" action="/profile/create">{self._hidden(token)}<div><label for="artist-name">Artist name</label><input id="artist-name" name="artist_name" type="text" maxlength="{_MAX_ARTIST_NAME}" autocomplete="nickname" required></div><button class="primary" type="submit">Open my Headquarters</button></form></div><div class="card"><h2>Setup can wait</h2><p>You do not need to connect a DAW, AI provider, account, or service to begin.</p><p class="status good">Local-first by default</p></div></section>"""
        if state.kind == "select-profile":
            choices = []
            for profile in state.profiles:
                token = self._new_action("profile-select", profile.profile_id)
                choices.append(
                    f'<form method="post" action="/profile/select">{self._hidden(token)}<button class="primary" type="submit">Work as {html.escape(profile.artist_name)}</button></form>'
                )
            return f'<section class="grid" aria-label="Artist selection"><div class="card stack"><h2>Your local Artists</h2>{"".join(choices)}</div><div class="card"><h2>Separate on purpose</h2><p>N0TE never merges Artist histories just because they live on the same machine.</p></div></section>'
        if state.kind == "recovery":
            return self._recovery_content()
        if state.kind == "blocked":
            return '<section class="grid"><div class="card"><h2>Nothing was overwritten</h2><p class="status caution">Protected local state</p><p>Try Home again after the other N0TE session is resolved.</p><a class="button" href="/">Try again</a></div></section>'
        if state.kind == "running-settings":
            quit_token = self._new_action("quit")
            song_line = (
                "No active Song yet"
                if not state.song_title
                else f'Active Song: {html.escape(state.song_title)}'
            )
            return f"""<section class="grid"><div class="card"><h2>Session</h2><p>{song_line}</p><p class="status good">Local shell active</p></div><div class="card"><h2>Connections</h2><p>DAWs, AI and services stay out of the way until a specific job needs them.</p></div><div class="card"><h2>Quit N0TE</h2><p>Quit closes Headquarters and releases this Artist workspace. Closing only the browser tab does not count as quitting.</p><form method="post" action="/quit">{self._hidden(quit_token)}<button class="danger" type="submit">Quit N0TE</button></form></div></section>"""
        if state.kind == "running-no-song":
            return f'<section class="grid"><div class="card"><h2>Start a Song</h2><p>Create the durable Song you want N0TE to keep in context.</p>{self._start_song_form()}</div><div class="card"><h2>Nothing else required</h2><p>Production tools and connections can be added when this Song actually needs them.</p><p class="status good">Ready locally</p></div></section>'
        if state.kind == "running-home":
            assert state.song_title is not None
            return f'<section class="grid"><div class="card"><h2>Your active Song</h2><p class="song-name">{html.escape(state.song_title)}</p><a class="button primary" href="/song">Resume Song</a></div><div class="card"><h2>Context stays with you</h2><p>Move through Headquarters and come back. This Artist and Song remain the active creative context.</p><p class="status good">Song context active</p></div></section>'
        if state.kind == "running-song":
            return self._song_content(state)
        if state.kind == "running-now":
            return self._focus_content(state)
        raise ConsumerShellError(f"unsupported page state: {state.kind}")

    def _closed_page(self) -> str:
        return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="dark"><title>N0TE closed</title></head><body><main><p>Artist Headquarters</p><h1>N0TE closed safely.</h1><p>Your local Artist and Song state is preserved. You can close this browser tab.</p></main></body></html>"""

    def _simple_error(self, message: str) -> str:
        safe = html.escape(message)
        return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>N0TE</title></head><body><main><h1>N0TE</h1><p>{safe}</p><p><a href="/">Return Home</a></p></main></body></html>'