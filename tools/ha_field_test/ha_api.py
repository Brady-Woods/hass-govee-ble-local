"""Home Assistant's REST and WebSocket APIs, with the standard library only.

The harness runs on the Home Assistant host and talks to it over loopback. The long-lived
token is read from an owner-only file and kept in memory: it is never printed, logged or
written anywhere, and :func:`scrub_token` removes it from any text that could end up in a
report. The WebSocket client is the small subset of RFC 6455 Home Assistant's API needs:
one connection, text frames, the server's pings answered, no extensions, no fragmentation
on send.
"""

from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://localhost:8123"
DEFAULT_TOKEN_FILE = "~/.ha_token"  # noqa: S105 - a path, not the token
# Home Assistant's API carries the token in clear over http: only ever to this host.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_CONT, _OP_TEXT, _OP_CLOSE, _OP_PING, _OP_PONG = 0x0, 0x1, 0x8, 0x9, 0xA
_LEN_16, _LEN_64 = 126, 127
_MAX_SHORT = 125
_MAX_16 = 0xFFFF
_HTTP_SWITCHING = 101
# A service call that controls a device can take a while (the integration's per-call
# deadline is 60 s, a zone light makes up to four calls); a read never should.
READ_TIMEOUT = 30.0
CALL_TIMEOUT = 300.0


class HAError(Exception):
    """Home Assistant refused a request or the connection failed."""


class ConnectionClosedError(HAError):
    """The WebSocket connection closed."""


@dataclass(frozen=True)
class ServiceError:
    """A service call Home Assistant refused: its WebSocket error, as sent."""

    code: str
    message: str
    translation_domain: str | None = None
    translation_key: str | None = None
    translation_placeholders: dict[str, str] | None = None

    def describe(self) -> str:
        """Return the error for a report: its key and the underlying error, not its message.

        The integration's message names the device by its config entry title
        (``device_name``); the key and the ``error`` placeholder say what went wrong
        without it. A message without a key (Home Assistant's own) is the fallback.
        """
        if self.translation_key is None:
            return f"{self.code}: {self.message}"
        detail = (self.translation_placeholders or {}).get("error")
        return f"{self.translation_key}: {detail}" if detail else self.translation_key


_TOKEN: list[str] = []  # the one token read, for scrub_token


def read_token(path: str = DEFAULT_TOKEN_FILE) -> str:
    """Return the token in ``path``, refusing a file anyone but its owner may read or write."""
    token_path = Path(path).expanduser()
    info = token_path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise HAError(f"{token_path} is not a regular file")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise HAError(f"{token_path} must be owner-only (chmod 600)")
    if info.st_uid != os.getuid():
        raise HAError(f"{token_path} is not owned by the user running the harness")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise HAError(f"{token_path} is empty")
    _TOKEN.append(token)
    return token


def scrub_token(text: str) -> str:
    """Return ``text`` without the token (defence in depth: nothing should ever carry it)."""
    for token in _TOKEN:
        text = text.replace(token, "<token>")
    return text


def check_loopback(url: str) -> urllib.parse.SplitResult:
    """Return ``url`` split, refusing anything but http(s) to this host."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname not in _LOOPBACK_HOSTS:
        raise HAError(f"refusing {url!r}: only http(s) to localhost carries the token")
    return parts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib would send the Authorization header along to it."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise HAError("Home Assistant answered with a redirect: refused (the token stays here)")


class RestClient:
    """The REST calls the harness makes: single states and config-entry diagnostics."""

    def __init__(self, url: str, token: str) -> None:
        check_loopback(url)
        self._url = url.rstrip("/")
        self._token = token
        self._opener = urllib.request.build_opener(_NoRedirect)

    def get(self, path: str, timeout: float = READ_TIMEOUT) -> Any:
        """GET ``path`` and return its JSON (None for 404).

        An error never names the path: it holds an entity or config entry id, which can
        carry a name, and errors end up in reports.
        """
        request = urllib.request.Request(  # noqa: S310 - check_loopback: http(s) to localhost
            self._url + path,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as err:
            if err.code == 404:  # noqa: PLR2004 - HTTP Not Found
                return None
            raise HAError(f"Home Assistant answered HTTP {err.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise HAError(f"Home Assistant request failed: {type(err).__name__}") from None

    def state(self, entity_id: str) -> dict[str, Any] | None:
        """Return one entity's state object, or None if it has none."""
        result = self.get(f"/api/states/{urllib.parse.quote(entity_id)}")
        return result if isinstance(result, dict) else None

    def diagnostics(self, entry_id: str) -> dict[str, Any]:
        """Return a config entry's diagnostics download (``data`` is the integration's part)."""
        result = self.get(f"/api/diagnostics/config_entry/{urllib.parse.quote(entry_id)}")
        if not isinstance(result, dict):
            raise HAError("no diagnostics for a config entry")
        return result


def _frame(opcode: int, payload: bytes) -> bytes:
    """Return one masked client frame (FIN set)."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length <= _MAX_SHORT:
        header.append(0x80 | length)
    elif length <= _MAX_16:
        header.append(0x80 | _LEN_16)
        header += length.to_bytes(2, "big")
    else:
        header.append(0x80 | _LEN_64)
        header += length.to_bytes(8, "big")
    mask = os.urandom(4)
    return bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """Return the masked client frame for ``payload`` (exposed for the tests)."""
    return _frame(opcode, payload)


def decode_frame(data: bytes) -> tuple[bool, int, bytes, int] | None:
    """Parse one frame at the start of ``data``: (fin, opcode, payload, bytes used), or None.

    None means ``data`` doesn't hold a whole frame yet. A masked frame (only a client
    sends one) is unmasked, so the tests can feed the client's own frames back.
    """
    if len(data) < 2:  # noqa: PLR2004 - the two header bytes
        return None
    fin, opcode = bool(data[0] & 0x80), data[0] & 0x0F
    masked, length, offset = bool(data[1] & 0x80), data[1] & 0x7F, 2
    if length == _LEN_16:
        if len(data) < offset + 2:
            return None
        length, offset = int.from_bytes(data[2:4], "big"), offset + 2
    elif length == _LEN_64:
        if len(data) < offset + 8:  # a 64-bit length
            return None
        length, offset = int.from_bytes(data[2:10], "big"), offset + 8
    mask = b""
    if masked:
        if len(data) < offset + 4:  # the masking key
            return None
        mask, offset = data[offset : offset + 4], offset + 4
    if len(data) < offset + length:
        return None
    payload = data[offset : offset + length]
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, offset + length


class WebSocketClient:
    """Home Assistant's WebSocket API over one connection, one request at a time.

    Commands are sent and answered in order; event messages of a subscription that arrive
    meanwhile are kept for :meth:`events`. Nothing runs in the background: a ping from the
    server is answered while a reply is awaited. A connection idle too long (while the
    harness waits for the user) is dropped by Home Assistant, so :meth:`command` reconnects
    once for a read; a service call is never retried (it may have run).
    """

    def __init__(self, url: str, token: str) -> None:
        parts = check_loopback(url)
        self._host = parts.hostname or "localhost"
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._tls = parts.scheme == "https"
        self._token = token
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._next_id = 1
        self._events: dict[int, list[Any]] = {}
        self.ha_version: str | None = None

    # -- connection --------------------------------------------------------

    def connect(self) -> None:
        """Open the connection and authenticate."""
        self.close()
        sock = socket.create_connection((self._host, self._port), timeout=READ_TIMEOUT)
        if self._tls:
            import ssl  # noqa: PLC0415 - only for an https URL

            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self._host)
        self._sock, self._buffer = sock, b""
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                "GET /api/websocket HTTP/1.1\r\n"
                f"Host: {self._host}:{self._port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        while b"\r\n\r\n" not in self._buffer:
            self._recv_more(time.monotonic() + READ_TIMEOUT)
        head, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ")
        accept = hashlib.sha1((key + _WS_GUID).encode()).digest()  # noqa: S324 - RFC 6455
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])
        }
        if len(status) < 2 or status[1] != str(_HTTP_SWITCHING):  # noqa: PLR2004
            raise HAError(f"WebSocket upgrade refused: {lines[0]}")
        if headers.get("sec-websocket-accept") != base64.b64encode(accept).decode():
            raise HAError("WebSocket upgrade: bad Sec-WebSocket-Accept")
        if self._receive(READ_TIMEOUT).get("type") != "auth_required":
            raise HAError("WebSocket: expected auth_required")
        self._send({"type": "auth", "access_token": self._token})
        reply = self._receive(READ_TIMEOUT)
        if reply.get("type") != "auth_ok":
            raise HAError(f"WebSocket authentication failed ({reply.get('type')})")
        self.ha_version = reply.get("ha_version")

    def close(self) -> None:
        """Close the connection, if open."""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.sendall(_frame(_OP_CLOSE, (1000).to_bytes(2, "big")))
            self._sock.close()
        self._sock = None

    def __enter__(self) -> WebSocketClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- frames ------------------------------------------------------------

    def _recv_more(self, deadline: float) -> None:
        if self._sock is None:
            raise ConnectionClosedError("not connected")
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("no reply from Home Assistant in time")
        self._sock.settimeout(left)
        try:
            chunk = self._sock.recv(65536)
        except TimeoutError:
            raise
        except OSError as err:
            self._sock = None
            raise ConnectionClosedError(f"connection lost: {err}") from None
        if not chunk:
            self._sock = None
            raise ConnectionClosedError("connection closed by Home Assistant")
        self._buffer += chunk

    def _send(self, message: dict[str, Any]) -> None:
        if self._sock is None:
            raise ConnectionClosedError("not connected")
        try:
            self._sock.sendall(_frame(_OP_TEXT, json.dumps(message).encode()))
        except OSError as err:
            self._sock = None
            raise ConnectionClosedError(f"connection lost: {err}") from None

    def _receive(self, timeout: float) -> dict[str, Any]:
        """Return the next JSON message, answering pings on the way."""
        deadline = time.monotonic() + timeout
        parts: list[bytes] = []
        while True:
            parsed = decode_frame(self._buffer)
            if parsed is None:
                self._recv_more(deadline)
                continue
            fin, opcode, payload, used = parsed
            self._buffer = self._buffer[used:]
            if opcode == _OP_PING:
                if self._sock is not None:
                    self._sock.sendall(_frame(_OP_PONG, payload))
                continue
            if opcode == _OP_CLOSE:
                self._sock = None
                raise ConnectionClosedError("connection closed by Home Assistant")
            if opcode in (_OP_TEXT, _OP_CONT):
                parts.append(payload)
                if fin:
                    message = json.loads(b"".join(parts))
                    if not isinstance(message, dict):
                        raise HAError("unexpected WebSocket message")
                    return message

    # -- requests ----------------------------------------------------------

    def _request(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        msg_id = self._next_id
        self._next_id += 1
        self._send({**message, "id": msg_id})
        deadline = time.monotonic() + timeout
        while True:
            reply = self._receive(max(0.1, deadline - time.monotonic()))
            if reply.get("type") == "event":
                self._events.setdefault(int(reply.get("id", 0)), []).append(reply.get("event"))
                continue
            if reply.get("id") == msg_id and reply.get("type") in ("result", "pong"):
                return reply

    def command(self, message: dict[str, Any], timeout: float = READ_TIMEOUT) -> Any:
        """Send a read-only command and return its result (reconnecting once if dropped)."""
        if self._sock is None:
            self.connect()
        try:
            reply = self._request(message, timeout)
        except ConnectionClosedError:
            self.connect()
            reply = self._request(message, timeout)
        if not reply.get("success", True):
            error = reply.get("error") or {}
            raise HAError(f"{message['type']}: {error.get('code')}: {error.get('message')}")
        return reply.get("result")

    def ping(self) -> None:
        """Check the connection is alive (reconnecting if it was dropped)."""
        self.command({"type": "ping"})

    def subscribe_snapshot(self, message: dict[str, Any], settle: float = 2.0) -> list[Any]:
        """Subscribe, collect the events that arrive within ``settle`` s, then unsubscribe.

        For subscriptions that send the current state at once (bluetooth scanner details).
        """
        if self._sock is None:
            self.connect()
        reply = self._request(message, READ_TIMEOUT)
        if not reply.get("success", True):
            error = reply.get("error") or {}
            raise HAError(f"{message['type']}: {error.get('code')}: {error.get('message')}")
        msg_id = int(reply["id"])  # the subscription is the request's id
        end = time.monotonic() + settle
        while (left := end - time.monotonic()) > 0:
            try:
                reply = self._receive(left)
            except TimeoutError:
                break
            if reply.get("type") == "event":
                self._events.setdefault(int(reply.get("id", 0)), []).append(reply.get("event"))
        self.command({"type": "unsubscribe_events", "subscription": msg_id})
        return self._events.pop(msg_id, [])

    def call_service(
        self, domain: str, service: str, data: dict[str, Any], timeout: float = CALL_TIMEOUT
    ) -> ServiceError | None:
        """Call a service and wait for it to finish; return its error, or None if it ran.

        Never retried: a call whose connection dropped may have run. The connection is
        checked first, so an idle drop doesn't cost the call.
        """
        self.ping()
        reply = self._request(
            {"type": "call_service", "domain": domain, "service": service, "service_data": data},
            timeout,
        )
        if reply.get("success", False):
            return None
        error = reply.get("error") or {}
        return ServiceError(
            code=str(error.get("code")),
            message=str(error.get("message")),
            translation_domain=error.get("translation_domain"),
            translation_key=error.get("translation_key"),
            translation_placeholders=error.get("translation_placeholders"),
        )
