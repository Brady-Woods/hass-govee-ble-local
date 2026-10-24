"""The harness's WebSocket client against a scripted Home Assistant on a socket pair."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
import hashlib
import json
import socket
import threading
from typing import Any

import pytest

from tools.ha_field_test import ha_api
from tools.ha_field_test.ha_api import WebSocketClient, decode_frame, encode_frame

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _server_frame(opcode: int, payload: bytes) -> bytes:
    """An unmasked server frame."""
    head = bytearray([0x80 | opcode])
    if len(payload) < 126:
        head.append(len(payload))
    else:
        head.append(126)
        head += len(payload).to_bytes(2, "big")
    return bytes(head) + payload


class FakeServer:
    """Home Assistant's side: the upgrade, auth, then ``handler`` for each request."""

    def __init__(self, sock: socket.socket, handler: Callable[[dict[str, Any]], list[bytes]]):
        self.sock, self.handler = sock, handler
        self.requests: list[dict[str, Any]] = []
        self.pongs: list[bytes] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def send(self, message: dict[str, Any]) -> bytes:
        return _server_frame(0x1, json.dumps(message).encode())

    def _serve(self) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        head, buf = buf.split(b"\r\n\r\n", 1)
        key = next(
            line.split(":", 1)[1].strip()
            for line in head.decode().split("\r\n")
            if line.lower().startswith("sec-websocket-key")
        )
        digest = hashlib.sha1((key + _GUID).encode()).digest()  # noqa: S324 - RFC 6455
        accept = base64.b64encode(digest).decode()
        self.sock.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            + f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode()
            + self.send({"type": "auth_required"})
        )
        while True:
            parsed = decode_frame(buf)
            if parsed is None:
                try:
                    chunk = self.sock.recv(65536)
                except OSError:  # the test closed the pair
                    return
                if not chunk:
                    return
                buf += chunk
                continue
            _, opcode, payload, used = parsed
            buf = buf[used:]
            if opcode == 0xA:
                self.pongs.append(payload)
                continue
            if opcode == 0x8:
                return
            message = json.loads(payload)
            self.requests.append(message)
            if message["type"] == "auth":
                ok = message["access_token"] == "good-token"
                reply = {"type": "auth_ok" if ok else "auth_invalid", "ha_version": "2026.10.2"}
                self.sock.sendall(self.send(reply))
                continue
            for frame in self.handler(message):
                self.sock.sendall(frame)


@pytest.fixture
def pair(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., tuple[WebSocketClient, FakeServer]]]:
    sockets: list[socket.socket] = []

    def make(handler: Callable[[dict[str, Any]], list[bytes]], token: str = "good-token"):  # noqa: S107
        client_end, server_end = socket.socketpair()
        sockets.extend((client_end, server_end))
        monkeypatch.setattr(ha_api.socket, "create_connection", lambda *_a, **_k: client_end)
        server = FakeServer(server_end, handler)
        client = WebSocketClient("http://localhost:8123", token)
        return client, server

    yield make
    for sock in sockets:
        sock.close()


def _result(message: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"id": message["id"], "type": "result", **fields}


def _handler(server_box: list[FakeServer]) -> Callable[[dict[str, Any]], list[bytes]]:
    """Home Assistant's answers, as the harness uses them."""

    def handler(msg: dict[str, Any]) -> list[bytes]:
        server = server_box[0]
        if msg["type"] == "ping":
            return [server.send({"id": msg["id"], "type": "pong"})]
        if msg["type"] == "get_states":
            # A ping comes before the reply: the client answers it and keeps waiting. The
            # reply is long enough for a 16-bit frame length.
            states = [{"entity_id": "light.x", "state": "on"}] * 50
            reply = server.send(_result(msg, success=True, result=states))
            return [_server_frame(0x9, b"hb"), reply]
        if msg["type"] == "call_service" and msg["service_data"].get("effect") == "nope":
            error = {
                "code": "service_validation_error",
                "message": "Unknown scene",
                "translation_domain": "govee_ble_local",
                "translation_key": "unknown_effect",
            }
            return [server.send(_result(msg, success=False, error=error))]
        if msg["type"] == "call_service":
            # A stale reply to an earlier, interrupted request comes first: it is skipped.
            stale = server.send({"id": 999, "type": "result", "success": True})
            return [stale, server.send(_result(msg, success=True, result={"context": {}}))]
        if msg["type"] == "bluetooth/subscribe_scanner_details":
            event = {"id": msg["id"], "type": "event", "event": {"add": [{"source": "hci0"}]}}
            return [server.send(_result(msg, success=True, result=None)), server.send(event)]
        return [server.send(_result(msg, success=True, result=None))]

    return handler


def test_websocket_client(pair) -> None:
    # Frames of every length encoding round-trip, and a partial one waits for more.
    for size in (0, 125, 126, 65535, 65536):
        payload = bytes(i % 251 for i in range(size))
        frame = encode_frame(0x1, payload)
        assert decode_frame(frame) == (True, 0x1, payload, len(frame))
        assert decode_frame(frame[:-1] if size else frame[:1]) is None

    box: list[FakeServer] = []
    client, server = pair(_handler(box))
    box.append(server)
    client.connect()

    assert client.ha_version == "2026.10.2"
    assert len(client.command({"type": "get_states"})) == 50
    assert client.call_service("light", "turn_on", {"entity_id": "light.x"}) is None
    refused = client.call_service("light", "turn_on", {"entity_id": "light.x", "effect": "nope"})
    assert refused is not None
    assert (refused.code, refused.translation_key) == ("service_validation_error", "unknown_effect")
    events = client.subscribe_snapshot({"type": "bluetooth/subscribe_scanner_details"}, settle=0.3)
    assert events == [{"add": [{"source": "hci0"}]}]
    client.close()
    server.thread.join(timeout=5)

    assert server.pongs == [b"hb"]
    kinds = [r["type"] for r in server.requests]
    # Every call is preceded by a ping (an idle connection is checked first), and the
    # subscription is taken back.
    assert kinds == [
        "auth",
        "get_states",
        "ping",
        "call_service",
        "ping",
        "call_service",
        "bluetooth/subscribe_scanner_details",
        "unsubscribe_events",
    ]
    sub = next(r for r in server.requests if r["type"].startswith("bluetooth/"))
    assert server.requests[-1]["subscription"] == sub["id"]

    bad, _ = pair(lambda _msg: [], token="bad")  # noqa: S106 - a made-up token
    with pytest.raises(ha_api.HAError, match="authentication failed"):
        bad.connect()
