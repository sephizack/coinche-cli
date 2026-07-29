"""Tests for the méta-client (`coinche.meta`): the multi-session web front door.

These drive the real `MetaClientServer` over HTTP (basic auth, landing page,
session creation, per-session WebSocket routing) against a *fake* game server —
a tiny asyncio TCP listener speaking the line-based game protocol — so no real
game logic is involved. The browser-side WebSocket client is the same minimal
RFC 6455 helper used by `test_web_bridge.py`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct

from coinche import protocol
from coinche.meta.server import MetaClientServer

HOST = "127.0.0.1"
AUTH = base64.b64encode(b"coinche:secret").decode("ascii")


# --------------------------------------------------------------------------- #
# Fake game server: records joins, can be told to send frames to a client.
# --------------------------------------------------------------------------- #


class FakeGameServer:
    """A minimal TCP server speaking the game wire protocol (line-delimited)."""

    def __init__(self) -> None:
        self.received: list[tuple[str, dict]] = []
        self.writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, HOST, 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg_type, payload = protocol.decode(line)
                except protocol.ProtocolError:
                    continue
                self.received.append((msg_type, payload))
        except (ConnectionError, OSError):
            pass

    async def send_to_all(self, msg_type: str, payload: dict) -> None:
        for w in list(self.writers):
            w.write(protocol.encode(msg_type, payload))
            await w.drain()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()


# --------------------------------------------------------------------------- #
# Minimal HTTP + browser-side WS helpers
# --------------------------------------------------------------------------- #


async def http_get(port: int, path: str, auth: str | None = AUTH) -> tuple[int, dict[str, str], bytes]:
    """Perform a single HTTP/1.1 GET (Connection: close) and return status,
    headers, body."""
    reader, writer = await asyncio.open_connection(HOST, port)
    lines = [f"GET {path} HTTP/1.1", f"Host: {HOST}:{port}", "Connection: close"]
    if auth is not None:
        lines.append(f"Authorization: Basic {auth}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split(" ")[1])
    headers = {}
    for line in head_lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


class WSClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, port: int, path: str, auth: str = AUTH) -> WSClient:
        reader, writer = await asyncio.open_connection(HOST, port)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {HOST}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Authorization: Basic {auth}\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        status = await reader.readline()
        assert status.startswith(b"HTTP/1.1 101"), status
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        return cls(reader, writer)

    async def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.writer.write(bytes(header) + masked)
        await self.writer.drain()

    async def recv(self) -> str:
        first_two = await self.reader.readexactly(2)
        length = first_two[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", await self.reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await self.reader.readexactly(8))[0]
        payload = await self.reader.readexactly(length) if length else b""
        return payload.decode("utf-8")

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _start_meta(game_port: int) -> tuple[MetaClientServer, asyncio.Task, int]:
    server = MetaClientServer(
        game_host=HOST,
        game_port=game_port,
        auth_user="coinche",
        auth_pass="secret",
        host=HOST,
        port=0,
    )
    task = asyncio.ensure_future(server.serve())
    for _ in range(200):
        if server._bound is not None:
            break
        await asyncio.sleep(0.01)
    assert server._bound is not None, "méta-client never bound"
    return server, task, server._bound[1]


async def _stop(server: MetaClientServer, task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_requires_basic_auth() -> None:
    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            status, headers, _ = await http_get(port, "/", auth=None)
            assert status == 401
            assert "basic" in headers.get("www-authenticate", "").lower()

            status, _, _ = await http_get(port, "/", auth=base64.b64encode(b"coinche:wrong").decode())
            assert status == 401

            status, _, body = await http_get(port, "/", auth=AUTH)
            assert status == 200
            assert b"Votre nom" in body
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Session creation + redirect
# --------------------------------------------------------------------------- #


def test_new_session_redirects_and_connects_to_game() -> None:
    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            status, headers, _ = await http_get(port, "/new?name=Alice")
            assert status == 302
            location = headers["location"]
            assert location.startswith("/s/")
            session_id = location[len("/s/") :]
            assert session_id in server.sessions
            assert server.sessions[session_id].player_name == "Alice"

            # The session opened a TCP connection to the (fake) game server and
            # subscribed to the lobby (no join yet).
            for _ in range(200):
                if game.received:
                    break
                await asyncio.sleep(0.01)
            assert (protocol.SUBSCRIBE_LOBBY, {}) in game.received

            # The game page carries the per-session websocket path + name.
            status, _, body = await http_get(port, location)
            assert status == 200
            text = body.decode("utf-8")
            assert f"/s/{session_id}/ws" in text
            assert "Alice" in text
            assert "window.__META__" in text
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


def test_new_without_name_bounces_to_landing() -> None:
    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            status, headers, _ = await http_get(port, "/new?name=")
            assert status == 302
            assert headers["location"] == "/"
            assert not server.sessions
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# WebSocket routing per session
# --------------------------------------------------------------------------- #


def test_ws_routes_to_session_and_relays_actions() -> None:
    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            _, headers, _ = await http_get(port, "/new?name=Bob")
            session_id = headers["location"][len("/s/") :]

            ws = await WSClient.connect(port, f"/s/{session_id}/ws")
            # Initial resync frame.
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert first["type"] == "state"

            # A join action from the browser reaches the fake game server.
            await ws.send(json.dumps({"action": "join", "table_key": "table1", "player_name": "Bob"}))
            for _ in range(200):
                if any(m[0] == protocol.JOIN for m in game.received):
                    break
                await asyncio.sleep(0.01)
            join = next(m for m in game.received if m[0] == protocol.JOIN)
            assert join[1]["table_key"] == "table1"
            assert join[1]["player_name"] == "Bob"

            # A server->client frame is mirrored to the browser: send a
            # LOBBY_UPDATE and expect a state broadcast reflecting it.
            await game.send_to_all(
                protocol.LOBBY_UPDATE,
                {"players": [{"seat": "N", "name": "Bob", "team_name": "Equipe 1"}], "seats_filled": 1},
            )
            # Drain frames until we see the updated status.
            saw = False
            for _ in range(50):
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if frame["type"] == "state" and "1/4" in (frame["snapshot"].get("status_message") or ""):
                    saw = True
                    break
            assert saw, "lobby update was not mirrored to the browser"
            await ws.close()
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


def test_ws_unknown_session_is_404() -> None:
    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            reader, writer = await asyncio.open_connection(HOST, port)
            request = (
                "GET /s/nope/ws HTTP/1.1\r\n"
                f"Host: {HOST}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Authorization: Basic {AUTH}\r\n"
                "Sec-WebSocket-Key: x\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            status = await reader.readline()
            assert status.startswith(b"HTTP/1.1 404"), status
            writer.close()
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Static assets are served (and auth-guarded)
# --------------------------------------------------------------------------- #


def test_static_assets_served_with_auth() -> None:
    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            status, _, body = await http_get(port, "/app.js")
            assert status == 200
            assert body  # non-empty JS

            status, _, _ = await http_get(port, "/app.js", auth=None)
            assert status == 401

            # Path traversal is refused.
            status, _, _ = await http_get(port, "/../pyproject.toml")
            assert status in (403, 404)
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())
