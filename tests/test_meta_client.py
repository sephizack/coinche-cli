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
from coinche.meta.server import LANDING_PAGE_PATH, MetaClientServer

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
        finally:
            # Close the server side too, so its transport isn't left dangling to
            # be GC'd after the event loop is gone (PytestUnraisableException
            # warning from StreamWriter.__del__ at interpreter shutdown).
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def send_to_all(self, msg_type: str, payload: dict) -> None:
        for w in list(self.writers):
            w.write(protocol.encode(msg_type, payload))
            await w.drain()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for w in list(self.writers):
            try:
                w.close()
            except (ConnectionError, OSError):
                pass


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


async def _start_meta(
    game_port: int,
    idle_timeout: float = 120.0,
    reap_interval: float = 15.0,
) -> tuple[MetaClientServer, asyncio.Task, int]:
    server = MetaClientServer(
        game_host=HOST,
        game_port=game_port,
        auth_user="coinche",
        auth_pass="secret",
        host=HOST,
        port=0,
        idle_timeout=idle_timeout,
        reap_interval=reap_interval,
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


def test_table_deep_link_reaches_session_page() -> None:
    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            table_key = "CosmoCanyonLongNameX"
            status, headers, _ = await http_get(port, f"/new?name=Alice&table={table_key}&seat=S")
            assert status == 302
            location = headers["location"]
            assert location.startswith("/s/")
            assert location.endswith(f"?table={table_key}&seat=S")

            status, _, body = await http_get(port, location)
            assert status == 200
            text = body.decode("utf-8")
            assert f'"tableKey": "{table_key}"' in text
            assert '"preferredSeat": "S"' in text
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


def test_unknown_session_page_redirects_to_landing() -> None:
    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            status, headers, _ = await http_get(port, "/s/unknown")

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


def test_leave_forgets_remembered_join() -> None:
    """A browser 'leave' clears the session's remembered JOIN so a later TCP
    reconnect returns to the lobby instead of re-seating at the abandoned
    table; a 'join' re-arms it."""

    async def scenario() -> None:
        from coinche.meta.session import MetaSession

        session = MetaSession("sid", host="127.0.0.1", port=1, player_name="Bob")

        # The bridge drops browser actions until a game-server link exists; a
        # tiny stub with the two seams these actions relay is enough here.
        class _StubLink:
            async def send_join(self, *a, **k) -> bool:
                return True

            async def send_leave(self) -> bool:
                return True

        session.bridge.link = _StubLink()  # type: ignore[assignment]

        # A browser join is remembered (drives auto-rejoin after a drop).
        await session.bridge.on_browser_message({"action": "join", "table_key": "table1", "player_name": "Bob"})
        assert session._join_args == ("table1", "Bob", None, False)

        # Leaving forgets it.
        await session.bridge.on_browser_message({"action": "leave"})
        assert session._join_args is None

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


# --------------------------------------------------------------------------- #
# Session recovery: liveness probe + session id in the game page
# --------------------------------------------------------------------------- #


def test_session_status_probe() -> None:
    """`/api/session?id=…` reports liveness so the landing page can auto-resume
    a stored session (localStorage) and clean it up when it's gone."""

    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            _, headers, _ = await http_get(port, "/new?name=Alice")
            session_id = headers["location"][len("/s/") :]

            status, hdrs, body = await http_get(port, f"/api/session?id={session_id}")
            assert status == 200
            assert "application/json" in hdrs.get("content-type", "")
            data = json.loads(body)
            assert data == {"alive": True, "name": "Alice"}

            # An unknown/expired id (e.g. after a server reboot) reports dead so
            # the browser drops its stale stored id and shows the name form.
            status, _, body = await http_get(port, "/api/session?id=nope")
            assert status == 200
            assert json.loads(body) == {"alive": False}

            status, _, body = await http_get(port, "/api/session")
            assert json.loads(body) == {"alive": False}
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


def test_landing_page_stores_deep_link_before_resuming_session() -> None:
    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            _, headers, _ = await http_get(port, "/new?name=Alice")
            session_id = headers["location"][len("/s/") :]

            status, _, body = await http_get(port, "/?table=CosmoCanyon&seat=S")
            assert status == 200
            text = body.decode("utf-8")
            assert 'var PENDING_JOIN_KEY = "coinche.pendingJoin"' in text
            assert "JSON.stringify({ tableKey: table, preferredSeat: seat })" in text
            assert 'window.location.replace("/s/" + encodeURIComponent(id))' in text

            status, _, body = await http_get(port, f"/api/session?id={session_id}")
            assert status == 200
            assert json.loads(body) == {"alive": True, "name": "Alice"}

            status, _, body = await http_get(port, "/app.js")
            assert status == 200
            script = body.decode("utf-8")
            assert "function tryPendingJoin(snap)" in script
            assert "tryPendingJoin(snap);" in script
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


def test_game_page_carries_session_id() -> None:
    """The SPA shell exposes the session id so app.js can persist it to
    localStorage for recovery."""

    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            _, headers, _ = await http_get(port, "/new?name=Bob")
            location = headers["location"]
            session_id = location[len("/s/") :]

            _, _, body = await http_get(port, location)
            text = body.decode("utf-8")
            assert f'"sessionId": "{session_id}"' in text or f'"sessionId":"{session_id}"' in text
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


def test_landing_page_probes_stored_session() -> None:
    """The landing page ships the recovery script that reads localStorage and
    probes `/api/session` before falling back to the name form."""

    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port)
        try:
            _, _, body = await http_get(port, "/")
            assert body == LANDING_PAGE_PATH.read_bytes()
            text = body.decode("utf-8")
            assert "coinche.metaSessionId" in text
            assert "/api/session?id=" in text
            assert "localStorage.removeItem" in text  # cleans a dead id
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Idle reaper: kicks a browser-less, quiet session and frees its table seat
# --------------------------------------------------------------------------- #


def test_idle_session_is_reaped_and_leaves_table() -> None:
    """A session with no browser attached and no activity past the idle timeout
    is kicked: it sends LEAVE (freeing its seat) and is removed from the
    registry — table housekeeping falls out of the same reaper."""

    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        # Tiny timeout + fast interval so the reaper fires within the test.
        server, task, port = await _start_meta(game.port, idle_timeout=0.05, reap_interval=0.05)
        try:
            _, headers, _ = await http_get(port, "/new?name=Alice")
            session_id = headers["location"][len("/s/") :]

            # Wait until the reaper kicks the (browser-less) session.
            for _ in range(200):
                if session_id not in server.sessions:
                    break
                await asyncio.sleep(0.02)
            assert session_id not in server.sessions, "idle session was not reaped"

            # The reaped session left its table before stopping.
            for _ in range(200):
                if any(m[0] == protocol.LEAVE for m in game.received):
                    break
                await asyncio.sleep(0.02)
            assert any(m[0] == protocol.LEAVE for m in game.received), "reaped session never sent LEAVE"
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())


def test_attached_browser_is_not_reaped() -> None:
    """A session with a live browser attached is never reaped, even past the
    idle timeout — `browser_count > 0` protects it."""

    async def scenario() -> None:
        game = FakeGameServer()
        await game.start()
        server, task, port = await _start_meta(game.port, idle_timeout=0.05, reap_interval=0.05)
        try:
            _, headers, _ = await http_get(port, "/new?name=Bob")
            session_id = headers["location"][len("/s/") :]

            ws = await WSClient.connect(port, f"/s/{session_id}/ws")
            await asyncio.wait_for(ws.recv(), timeout=5)  # initial state frame

            # Give the reaper several intervals; the attached browser keeps the
            # session alive.
            await asyncio.sleep(0.3)
            assert session_id in server.sessions, "session with an attached browser was wrongly reaped"

            await ws.close()
        finally:
            await _stop(server, task)
            await game.stop()

    asyncio.run(scenario())
