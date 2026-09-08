from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from typing import Callable

from .consumer_shell import ConsumerShell, _MAX_FORM_BYTES

_DRAIN_CHUNK_BYTES = 8192
_DRAIN_TIMEOUT_SECONDS = 0.25
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


def _bounded_content_length(handler: BaseHTTPRequestHandler) -> int | None:
    """Return only a body length that is already inside the shell's safe form bound."""
    if handler.headers.get("Transfer-Encoding"):
        return None
    raw = handler.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except (TypeError, ValueError):
        return None
    if length < 0 or length > _MAX_FORM_BYTES:
        return None
    return length


def _rejection_left_bounded_body_unread(
    shell: ConsumerShell,
    handler: BaseHTTPRequestHandler,
    status: int,
) -> bool:
    if getattr(handler, "command", "") != "POST":
        return False
    if not shell._request_host_is_exact(handler) or not shell._post_origin_is_allowed(handler):
        return True
    if status == 404:
        return True
    if status != 403 or shell._path(handler) == "/song/material":
        return False
    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    return content_type != _FORM_CONTENT_TYPE


def _drain_bounded_body(handler: BaseHTTPRequestHandler) -> None:
    """Discard raw leftover bytes without parsing them or granting any authority."""
    remaining = _bounded_content_length(handler)
    if remaining is None or remaining == 0:
        return

    connection = getattr(handler, "connection", None)
    previous_timeout = None
    timeout_changed = False
    if connection is not None:
        try:
            previous_timeout = connection.gettimeout()
            connection.settimeout(_DRAIN_TIMEOUT_SECONDS)
            timeout_changed = True
        except OSError:
            timeout_changed = False

    try:
        while remaining:
            chunk = handler.rfile.read(min(remaining, _DRAIN_CHUNK_BYTES))
            if not chunk:
                break
            remaining -= len(chunk)
    except OSError:
        # The response decision is already complete. An incomplete/malicious body
        # must not turn cleanup into a second authority path or hang Headquarters.
        return
    finally:
        if timeout_changed:
            try:
                connection.settimeout(previous_timeout)
            except OSError:
                pass


def install_loopback_transport_reliability() -> None:
    """Keep early local HTTP rejections transport-stable without changing policy."""
    if getattr(ConsumerShell, "_loopback_transport_reliability_installed", False):
        return

    original_send_html: Callable[[ConsumerShell, BaseHTTPRequestHandler, int, str], None]
    original_send_html = ConsumerShell._send_html

    def with_rejected_body_cleanup(
        self: ConsumerShell,
        handler: BaseHTTPRequestHandler,
        status: int,
        body: str,
    ) -> None:
        should_drain = _rejection_left_bounded_body_unread(self, handler, status)
        original_send_html(self, handler, status, body)
        if should_drain:
            _drain_bounded_body(handler)

    ConsumerShell._send_html = with_rejected_body_cleanup
    ConsumerShell._loopback_transport_reliability_installed = True
