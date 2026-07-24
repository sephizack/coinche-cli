"""In-process HTTP + WebSocket overlay server (C3 `WebOverlayServer`).

Serves the browser UI (static files under `coinche/web/static/`) and upgrades
`/ws` to a WebSocket that mirrors the local `ClientState` to every attached
browser and relays browser actions back to the game server through U1's
`ClientLink`. It is a **proxy**: it never opens a socket to the game server and
never encodes a game-wire message (BR-U2-1); it never evaluates game legality
(BR-U2-2); and no browser fault may propagate out to cancel the terminal
session loops (BR-U2-3).

Transport decision (ADR-2): this uses a **minimal hand-rolled stdlib
WebSocket** (RFC 6455 handshake + single text frames) on top of
`asyncio.start_server`, so U2 adds **no new runtime dependency**. The frame
codec is intentionally small: it handles the common case (single unfragmented
text/close/ping frames, client->server frames masked as the RFC requires) and
rejects binary/fragmented frames. Server->client frames are unmasked.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import socket
import struct
from pathlib import Path

from coinche.session_state import ClientState, snapshot_to_dict
from coinche.web.messages import (
    WebProtocolError,
    encode_error_frame,
    encode_state_frame,
    parse_browser_message,
)

logger = logging.getLogger(__name__)

# RFC 6455 GUID appended to the client key before hashing for the handshake.
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Per-socket send bound (IN3): a slow/dead browser must not stall the next
# server `readline` in `receiver_loop`. Any send that can't complete in this
# window drops that one browser; the others are unaffected.
BROADCAST_TIMEOUT = 2.0

# Directory holding the browser UI assets (U3 fills this out; U2 ships a
# placeholder index.html so the WS can be smoke-tested now).
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _detect_lan_ip() -> str | None:
    """Best-effort local (LAN) IP, without any external call.

    Opens a UDP socket "towards" a public address: no packet is actually sent,
    but the OS picks the outbound interface, whose address we can read back.

    NOTE: this is a deliberate ~10-line duplicate of `coinche/server.py`'s
    private `_detect_lan_ip` (NFR1 forbids importing from the out-of-bounds
    server module, and this utility is too small to justify a shared refactor
    of that file)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


class WebSocketClosed(Exception):
    """Raised internally when the peer closed the WebSocket connection."""


class _WSConnection:
    """A single upgraded WebSocket connection over an asyncio stream pair.

    Implements just enough of RFC 6455 for this bridge: read masked text/close/
    ping frames from the browser, write unmasked text frames back. Fragmented
    or binary data frames are rejected (this UI only exchanges small JSON text
    frames)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def send(self, text: str) -> None:
        """Send one unmasked text frame (server->client frames are never masked)."""
        if self._closed:
            raise WebSocketClosed
        payload = text.encode("utf-8")
        header = bytearray()
        header.append(0x81)  # FIN + opcode 0x1 (text)
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        self._writer.write(bytes(header) + payload)
        await self._writer.drain()

    async def recv(self) -> str:
        """Read one text frame's payload, transparently answering pings and
        raising :class:`WebSocketClosed` on a close frame or EOF."""
        while True:
            opcode, payload = await self._read_frame()
            if opcode == 0x1:  # text
                return payload.decode("utf-8", errors="replace")
            if opcode == 0x8:  # close
                await self._send_close()
                raise WebSocketClosed
            if opcode == 0x9:  # ping -> pong
                await self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # pong — ignore
                continue
            # Binary (0x2) or any other opcode is unsupported here: close.
            await self._send_close()
            raise WebSocketClosed

    async def _read_exact(self, n: int) -> bytes:
        data = await self._reader.readexactly(n)
        return data

    async def _read_frame(self) -> tuple[int, bytes]:
        try:
            first_two = await self._read_exact(2)
            b0, b1 = first_two[0], first_two[1]
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", await self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", await self._read_exact(8))[0]
            mask = await self._read_exact(4) if masked else b""
            payload = await self._read_exact(length) if length else b""
        except asyncio.IncompleteReadError as exc:
            # A disconnect mid-frame (any of the reads above) is a normal close,
            # not a protocol fault — surface it as WebSocketClosed so _handle_ws
            # treats it the same as the initial-read EOF case.
            raise WebSocketClosed from exc
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload

    async def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        frame = bytes([0x80 | opcode, len(payload)]) + payload
        self._writer.write(frame)
        await self._writer.drain()

    async def _send_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._send_control(0x8)
        except (ConnectionError, OSError):
            pass

    async def close(self) -> None:
        await self._send_close()
        try:
            self._writer.close()
        except (ConnectionError, OSError):
            pass


class WebOverlayServer:
    """HTTP + WebSocket bridge mirroring the local session to browsers (C3)."""

    def __init__(
        self,
        state: ClientState,
        link: object,
        host: str = "0.0.0.0",
        port: int = 0,
        on_ready: object | None = None,
    ) -> None:
        self.state = state
        self.link = link
        self.host = host
        self.port = port
        # Optional zero-arg callback fired once the listener has bound and
        # `self.urls` is populated, so the terminal UI can redraw immediately to
        # show the web address rather than waiting for the next server message.
        self.on_ready = on_ready
        self.clients: set[_WSConnection] = set()
        self._bound: tuple[str, int] | None = None
        # Reachable URL(s) resolved once the listener binds, cached so the
        # terminal UI can read them synchronously and keep showing them in-game
        # (the launch-time print scrolls away under the full-screen live view).
        self.urls: list[str] = []

    async def serve(self) -> None:
        """Bind the HTTP+WS listener and serve until cancelled.

        Wraps the whole body in an error boundary (BR-U2-3): only
        `CancelledError` re-raises (so `gather` teardown works); every other
        runtime fault is logged and swallowed so a web failure can never cancel
        `receiver_loop`/`input_loop`."""
        server: asyncio.AbstractServer | None = None
        try:
            server = await asyncio.start_server(self._on_connection, self.host, self.port)
            self._bound = server.sockets[0].getsockname()[:2] if server.sockets else (self.host, self.port)
            self.urls = await self.bound_url()
            for url in self.urls:
                print(f"Interface web disponible : {url}")
            if self.on_ready is not None:
                try:
                    self.on_ready()
                except Exception:  # noqa: BLE001 — a UI callback fault must not break the server
                    logger.exception("Web overlay on_ready callback failed")
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — deliberate error boundary (BR-U2-3)
            logger.exception("Web overlay server stopped on an unexpected error")
        finally:
            for client in list(self.clients):
                try:
                    await client.close()
                except (ConnectionError, OSError):
                    pass
            self.clients.clear()
            if server is not None:
                server.close()

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Parse one HTTP request: upgrade `/ws` to a WebSocket, otherwise serve
        a static file. Every per-connection fault is contained here (BR-U2-3)."""
        try:
            request_line, headers = await self._read_http_request(reader)
        except (ConnectionError, OSError, asyncio.IncompleteReadError, ValueError):
            _safe_close(writer)
            return

        if request_line is None:
            _safe_close(writer)
            return

        method, path, _ = request_line
        try:
            if headers.get("upgrade", "").lower() == "websocket" and path.split("?", 1)[0] == "/ws":
                await self._upgrade_and_handle_ws(reader, writer, headers)
            elif method == "GET":
                await self._serve_static(writer, path)
            else:
                await self._write_http(writer, 405, "text/plain; charset=utf-8", b"Method Not Allowed")
                _safe_close(writer)
        except (ConnectionError, OSError):
            _safe_close(writer)
        except Exception:  # noqa: BLE001 — per-connection boundary (BR-U2-3)
            logger.exception("Web overlay connection handler failed")
            _safe_close(writer)

    @staticmethod
    async def _read_http_request(
        reader: asyncio.StreamReader,
    ) -> tuple[tuple[str, str, str] | None, dict[str, str]]:
        """Read and parse the request line + headers of an HTTP request."""
        raw_line = await reader.readline()
        if not raw_line:
            return None, {}
        parts = raw_line.decode("latin-1").rstrip("\r\n").split(" ")
        if len(parts) != 3:
            return None, {}
        headers: dict[str, str] = {}
        while True:
            header_line = await reader.readline()
            if header_line in (b"\r\n", b"\n", b""):
                break
            decoded = header_line.decode("latin-1").rstrip("\r\n")
            if ":" in decoded:
                name, _, value = decoded.partition(":")
                headers[name.strip().lower()] = value.strip()
        return (parts[0], parts[1], parts[2]), headers

    async def _upgrade_and_handle_ws(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
    ) -> None:
        key = headers.get("sec-websocket-key")
        if not key:
            await self._write_http(writer, 400, "text/plain; charset=utf-8", b"Bad WebSocket Request")
            _safe_close(writer)
            return
        accept = base64.b64encode(hashlib.sha1((key + _WS_MAGIC).encode("ascii")).digest()).decode("ascii")
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        writer.write(handshake.encode("ascii"))
        await writer.drain()
        await self._handle_ws(_WSConnection(reader, writer))

    async def _handle_ws(self, ws: _WSConnection) -> None:
        """Per-browser loop: resync (BR-U2-7), then relay parsed actions.

        A malformed frame yields an error frame and continues; a close/OSError
        drops only this browser (BR-U2-3)."""
        self.clients.add(ws)
        try:
            await ws.send(encode_state_frame(snapshot_to_dict(self.state)))
            while True:
                raw = await ws.recv()
                try:
                    msg = parse_browser_message(raw)
                except WebProtocolError as exc:
                    await ws.send(encode_error_frame("BAD_MESSAGE", str(exc)))
                    continue
                await self.on_browser_message(msg)
        except (WebSocketClosed, ConnectionError, OSError):
            pass
        finally:
            self.clients.discard(ws)
            try:
                await ws.close()
            except (ConnectionError, OSError):
                pass

    async def on_browser_message(self, msg: dict) -> None:
        """Relay one validated browser action to the matching `ClientLink` seam.

        No legality checks (BR-U2-2), no game-wire encoding (BR-U2-1): the seam
        is the sole path to the untouched server socket. The server's own
        validation applies and any `ERROR` flows back through the normal state
        path."""
        action = msg["action"]
        if action == "play":
            await self.link.send_play(msg["card"])
        elif action == "bid":
            await self.link.send_bid(
                msg.get("bid_action", msg.get("action_type", "pass")),
                msg.get("trump"),
                msg.get("points"),
            )
        elif action == "chat":
            await self.link.send_chat(msg["text"])
        elif action == "join":
            await self.link.send_join(msg["table_key"], msg["player_name"], msg.get("team_name"))
        elif action == "rematch":
            await self.link.send_rematch()
        elif action == "lobby":
            await self.link.send_subscribe_lobby()

    async def broadcast_state(self, state: ClientState) -> None:
        """Push the current projected state to every connected browser (FR3.1).

        Each send is bounded by :data:`BROADCAST_TIMEOUT` (IN3): a slow or dead
        socket is dropped, leaving the others (and the terminal loops)
        unaffected."""
        if not self.clients:
            return
        frame = encode_state_frame(snapshot_to_dict(state))
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send(frame), timeout=BROADCAST_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError, WebSocketClosed, ConnectionError, OSError):
                self.clients.discard(ws)

    async def bound_url(self) -> list[str]:
        """Reachable URL(s) for the browser UI (FR1.3).

        The LAN-IP probe can block (a UDP `connect`), so it runs in an executor
        matching how `coinche/server.py` guards the same call. Omits the LAN
        URL when detection fails rather than emitting `http://None:<port>`."""
        if self._bound is None:
            return []
        port = self._bound[1]
        urls = [f"http://127.0.0.1:{port}"]
        lan_ip = await asyncio.get_running_loop().run_in_executor(None, _detect_lan_ip)
        if lan_ip:
            urls.append(f"http://{lan_ip}:{port}")
        return urls

    async def _serve_static(self, writer: asyncio.StreamWriter, path: str) -> None:
        """Serve a file from `STATIC_DIR` (index.html at `/`), guarding against
        path traversal outside the static root."""
        rel = path.split("?", 1)[0].lstrip("/")
        if not rel:
            rel = "index.html"
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            await self._write_http(writer, 403, "text/plain; charset=utf-8", b"Forbidden")
            _safe_close(writer)
            return
        if not target.is_file():
            await self._write_http(writer, 404, "text/plain; charset=utf-8", b"Not Found")
            _safe_close(writer)
            return
        content_type, _ = mimetypes.guess_type(str(target))
        if content_type is None:
            content_type = "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type = f"{content_type}; charset=utf-8"
        await self._write_http(writer, 200, content_type, target.read_bytes())
        _safe_close(writer)

    @staticmethod
    async def _write_http(writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes) -> None:
        reasons = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}
        head = (
            f"HTTP/1.1 {status} {reasons.get(status, 'OK')}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()


def _safe_close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except (ConnectionError, OSError):
        pass
