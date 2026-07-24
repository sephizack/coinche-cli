"""Tests for U2 web bridge: the pure message layer (`coinche.web.messages`) and
the `WebOverlayServer` HTTP+WebSocket bridge.

The round-trip tests drive a real browser-side WebSocket against a live
`WebOverlayServer` (bound to an ephemeral port) using a tiny hand-rolled RFC
6455 client, in the `asyncio.run(scenario())` real-async style of
`test_integration.py`. The `ClientLink` is replaced by a fake that records
calls, so no game server socket is involved.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct

import pytest

from coinche.session_state import ClientState
from coinche.web import WebOverlayServer
from coinche.web.messages import (
    MAX_MESSAGE_BYTES,
    WebProtocolError,
    encode_error_frame,
    encode_state_frame,
    parse_browser_message,
)

HOST = "127.0.0.1"


# --------------------------------------------------------------------------- #
# messages.py — pure layer
# --------------------------------------------------------------------------- #


def test_parse_valid_message() -> None:
    msg = parse_browser_message(json.dumps({"action": "play", "card": "A♠"}))
    assert msg == {"action": "play", "card": "A♠"}


def test_parse_accepts_bytes() -> None:
    msg = parse_browser_message(json.dumps({"action": "lobby"}).encode("utf-8"))
    assert msg["action"] == "lobby"


def test_parse_rejects_oversized() -> None:
    huge = json.dumps({"action": "chat", "text": "x" * (MAX_MESSAGE_BYTES + 10)})
    with pytest.raises(WebProtocolError):
        parse_browser_message(huge)


def test_parse_rejects_malformed_json() -> None:
    with pytest.raises(WebProtocolError):
        parse_browser_message("{not json")


def test_parse_rejects_non_object() -> None:
    with pytest.raises(WebProtocolError):
        parse_browser_message(json.dumps(["play"]))


def test_parse_rejects_missing_action() -> None:
    with pytest.raises(WebProtocolError):
        parse_browser_message(json.dumps({"card": "A♠"}))


def test_parse_rejects_unknown_action() -> None:
    with pytest.raises(WebProtocolError):
        parse_browser_message(json.dumps({"action": "hack"}))


@pytest.mark.parametrize(
    "frame",
    [
        {"action": "play"},  # missing "card"
        {"action": "bid"},  # missing "bid_action"
        {"action": "chat"},  # missing "text"
        {"action": "join"},  # missing both required fields
        {"action": "join", "table_key": "t1"},  # missing "player_name"
        {"action": "join", "player_name": "Zoe"},  # missing "table_key"
    ],
)
def test_parse_rejects_incomplete_action_frames(frame: dict) -> None:
    with pytest.raises(WebProtocolError):
        parse_browser_message(json.dumps(frame))


@pytest.mark.parametrize(
    "frame",
    [
        {"action": "play", "card": "A♠"},
        {"action": "bid", "bid_action": "pass"},
        {"action": "chat", "text": "salut"},
        {"action": "join", "table_key": "t1", "player_name": "Zoe"},
        {"action": "rematch"},  # no required fields
        {"action": "lobby"},  # no required fields
    ],
)
def test_parse_accepts_complete_action_frames(frame: dict) -> None:
    assert parse_browser_message(json.dumps(frame)) == frame


def test_encode_state_frame() -> None:
    frame = json.loads(encode_state_frame({"seat": "N"}))
    assert frame == {"type": "state", "snapshot": {"seat": "N"}}


def test_encode_error_frame() -> None:
    frame = json.loads(encode_error_frame("BAD_MESSAGE", "oops"))
    assert frame == {"type": "error", "code": "BAD_MESSAGE", "message": "oops"}


# --------------------------------------------------------------------------- #
# Fake ClientLink recording seam calls
# --------------------------------------------------------------------------- #


class FakeLink:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send_play(self, card: str) -> bool:
        self.calls.append(("play", card))
        return True

    async def send_bid(self, action, trump=None, points=None) -> bool:
        self.calls.append(("bid", action, trump, points))
        return True

    async def send_chat(self, text: str) -> bool:
        self.calls.append(("chat", text))
        return True

    async def send_join(self, table_key, player_name, team_name) -> bool:
        self.calls.append(("join", table_key, player_name, team_name))
        return True

    async def send_rematch(self) -> bool:
        self.calls.append(("rematch",))
        return True

    async def send_subscribe_lobby(self) -> bool:
        self.calls.append(("lobby",))
        return True


# --------------------------------------------------------------------------- #
# Minimal RFC 6455 browser-side WS client (for round-trip tests)
# --------------------------------------------------------------------------- #


class WSTestClient:
    """Tiny browser-side WebSocket client: HTTP upgrade + masked text frames."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, port: int, path: str = "/ws") -> WSTestClient:
        reader, writer = await asyncio.open_connection(HOST, port)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {HOST}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        # Read the 101 handshake response (headers up to the blank line).
        status = await reader.readline()
        assert status.startswith(b"HTTP/1.1 101"), status
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        return cls(reader, writer)

    async def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
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


async def _start_overlay(state: ClientState, link: FakeLink) -> tuple[WebOverlayServer, asyncio.Task, int]:
    server = WebOverlayServer(state, link, host=HOST, port=0)
    task = asyncio.ensure_future(server.serve())
    # Wait for the listener to bind.
    for _ in range(100):
        if server._bound is not None:
            break
        await asyncio.sleep(0.01)
    assert server._bound is not None, "web server never bound"
    return server, task, server._bound[1]


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# --------------------------------------------------------------------------- #
# TR-1 — round-trip
# --------------------------------------------------------------------------- #


def test_round_trip_initial_frame_and_seam() -> None:
    async def scenario() -> None:
        state = ClientState()
        state.seat = None
        link = FakeLink()
        server, task, port = await _start_overlay(state, link)
        try:
            client = await WSTestClient.connect(port)
            # (a) connecting browser gets an initial state frame (resync).
            first = json.loads(await asyncio.wait_for(client.recv(), timeout=5))
            assert first["type"] == "state"
            assert "snapshot" in first

            # (b) an action frame reaches the right ClientLink seam with args.
            await client.send(json.dumps({"action": "play", "card": "10♥"}))
            for _ in range(100):
                if link.calls:
                    break
                await asyncio.sleep(0.01)
            assert ("play", "10♥") in link.calls

            await client.send(json.dumps({"action": "bid", "bid_action": "bid", "trump": "♠", "points": 90}))
            await client.send(json.dumps({"action": "chat", "text": "salut"}))
            await client.send(json.dumps({"action": "join", "table_key": "t1", "player_name": "Zoe"}))
            await client.send(json.dumps({"action": "rematch"}))
            await client.send(json.dumps({"action": "lobby"}))
            for _ in range(200):
                if len(link.calls) >= 6:
                    break
                await asyncio.sleep(0.01)
            assert ("bid", "bid", "♠", 90) in link.calls
            assert ("chat", "salut") in link.calls
            assert ("join", "t1", "Zoe", None) in link.calls
            assert ("rematch",) in link.calls
            assert ("lobby",) in link.calls

            # (c) a state change triggers a broadcast frame.
            state.status_message = "En attente de joueurs (2/4)..."
            await server.broadcast_state(state)
            frame = json.loads(await asyncio.wait_for(client.recv(), timeout=5))
            assert frame["type"] == "state"
            assert frame["snapshot"]["status_message"] == "En attente de joueurs (2/4)..."
            await client.close()
        finally:
            await _stop(task)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# TR-2 — error boundary
# --------------------------------------------------------------------------- #


def test_malformed_frame_yields_error_and_keeps_connection() -> None:
    async def scenario() -> None:
        state = ClientState()
        link = FakeLink()
        server, task, port = await _start_overlay(state, link)
        try:
            client = await WSTestClient.connect(port)
            await asyncio.wait_for(client.recv(), timeout=5)  # initial state frame

            await client.send("{not valid json")
            err = json.loads(await asyncio.wait_for(client.recv(), timeout=5))
            assert err["type"] == "error"
            assert err["code"] == "BAD_MESSAGE"

            # Connection still alive: a valid action still reaches the seam.
            await client.send(json.dumps({"action": "rematch"}))
            for _ in range(100):
                if link.calls:
                    break
                await asyncio.sleep(0.01)
            assert ("rematch",) in link.calls
            # The server task itself never raised out.
            assert not task.done()
            await client.close()
        finally:
            await _stop(task)

    asyncio.run(scenario())


def test_dropped_client_does_not_stop_broadcast_to_others() -> None:
    async def scenario() -> None:
        state = ClientState()
        link = FakeLink()
        server, task, port = await _start_overlay(state, link)
        try:
            a = await WSTestClient.connect(port)
            b = await WSTestClient.connect(port)
            await asyncio.wait_for(a.recv(), timeout=5)  # initial frames
            await asyncio.wait_for(b.recv(), timeout=5)
            for _ in range(100):
                if len(server.clients) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert len(server.clients) == 2

            # Drop client A abruptly.
            await a.close()

            state.status_message = "toujours vivant"
            await server.broadcast_state(state)
            # B still receives the broadcast.
            frame = json.loads(await asyncio.wait_for(b.recv(), timeout=5))
            assert frame["snapshot"]["status_message"] == "toujours vivant"
            assert not task.done()
            await b.close()
        finally:
            await _stop(task)

    asyncio.run(scenario())


def test_broadcast_with_no_clients_is_noop() -> None:
    async def scenario() -> None:
        state = ClientState()
        server = WebOverlayServer(state, FakeLink(), host=HOST, port=0)
        # No serve() call: clients is empty; must not raise.
        await server.broadcast_state(state)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# TR-3 — own-seat-only
# --------------------------------------------------------------------------- #


def test_snapshot_has_no_foreign_hand() -> None:
    async def scenario() -> None:
        from coinche.cards import Seat

        state = ClientState()
        state.seat = Seat.N
        state.hand = ["A♠", "K♠"]
        link = FakeLink()
        server, task, port = await _start_overlay(state, link)
        try:
            client = await WSTestClient.connect(port)
            frame = json.loads(await asyncio.wait_for(client.recv(), timeout=5))
            snapshot = frame["snapshot"]
            # Only the local seat's hand is present; there is no per-seat hand map.
            assert snapshot["hand"] == ["A♠", "K♠"]
            assert "hands" not in snapshot
            for value in snapshot.values():
                if isinstance(value, dict):
                    # No nested structure carries a foreign seat's cards.
                    assert "hand" not in value
            await client.close()
        finally:
            await _stop(task)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# on_browser_message unit-level (no socket) + static serving
# --------------------------------------------------------------------------- #


def test_on_browser_message_dispatch_direct() -> None:
    async def scenario() -> None:
        link = FakeLink()
        server = WebOverlayServer(ClientState(), link, host=HOST, port=0)
        await server.on_browser_message({"action": "play", "card": "7♦"})
        await server.on_browser_message({"action": "bid", "bid_action": "pass"})
        assert ("play", "7♦") in link.calls
        assert ("bid", "pass", None, None) in link.calls

    asyncio.run(scenario())


class _HangingWS:
    """Fake WS connection whose send never completes within the timeout.

    Simulates a slow/dead browser: `broadcast_state` bounds the send with
    `asyncio.wait_for`, which raises `asyncio.TimeoutError` (a DISTINCT class
    from builtin `TimeoutError` on Python 3.10)."""

    closed = False

    async def send(self, text: str) -> None:
        await asyncio.sleep(3600)  # far beyond BROADCAST_TIMEOUT


class _TimeoutRaisingWS:
    """Fake WS connection whose send raises `asyncio.TimeoutError` directly."""

    closed = False

    async def send(self, text: str) -> None:
        raise asyncio.TimeoutError


def test_broadcast_drops_socket_that_times_out_on_send() -> None:
    """F1 regression: a browser whose send times out must be dropped, and the
    timeout must NOT escape `broadcast_state` (which would crash receiver_loop
    on Python 3.10, where asyncio.TimeoutError != builtin TimeoutError)."""

    async def scenario() -> None:
        state = ClientState()
        server = WebOverlayServer(state, FakeLink(), host=HOST, port=0)
        # Shrink the bound so the hanging send resolves fast.
        import coinche.web.server as server_mod

        original = server_mod.BROADCAST_TIMEOUT
        server_mod.BROADCAST_TIMEOUT = 0.05
        try:
            hanging = _HangingWS()
            raising = _TimeoutRaisingWS()
            server.clients.add(hanging)
            server.clients.add(raising)
            # Must not raise out of broadcast_state.
            await server.broadcast_state(state)
            # Both dead sockets were dropped.
            assert hanging not in server.clients
            assert raising not in server.clients
        finally:
            server_mod.BROADCAST_TIMEOUT = original

    asyncio.run(scenario())


def test_mid_frame_disconnect_is_clean_close() -> None:
    """F3: a browser that sends a partial frame then disconnects mid-frame must
    be treated as a normal close (WebSocketClosed) by _handle_ws, not crash the
    connection handler. The server task keeps running."""

    async def scenario() -> None:
        state = ClientState()
        server, task, port = await _start_overlay(state, FakeLink())
        try:
            reader, writer = await asyncio.open_connection(HOST, port)
            # Perform the WS handshake manually.
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET /ws HTTP/1.1\r\nHost: {HOST}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), timeout=5)
            assert status.startswith(b"HTTP/1.1 101")
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
            await asyncio.wait_for(reader.readexactly(2), timeout=5)  # initial state frame header
            # Announce a 126-byte payload (extended length follows) but hang up
            # before sending the extended length / mask / payload — a mid-frame
            # disconnect that exercises the later _read_exact calls.
            writer.write(bytes([0x81, 0x80 | 126]))
            await writer.drain()
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            # The connection is dropped without crashing the server loop.
            for _ in range(200):
                if not server.clients:
                    break
                await asyncio.sleep(0.01)
            assert server.clients == set()
            assert not task.done()
        finally:
            await _stop(task)

    asyncio.run(scenario())


def test_static_index_served() -> None:
    async def scenario() -> None:
        state = ClientState()
        _server, task, port = await _start_overlay(state, FakeLink())
        try:
            reader, writer = await asyncio.open_connection(HOST, port)
            writer.write(f"GET / HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), timeout=5)
            assert status.startswith(b"HTTP/1.1 200")
            body = await asyncio.wait_for(reader.read(), timeout=5)
            # U3 replaced the U2 placeholder with the real casino UI: assert on
            # stable markers of the served shell (the Vue mount point + title)
            # rather than the retired "U3 UI à venir" placeholder string.
            assert b'id="app"' in body
            assert b"Coinche" in body
            writer.close()
        finally:
            await _stop(task)

    asyncio.run(scenario())
