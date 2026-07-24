"""Web overlay bridge (U2): in-process HTTP + WebSocket server that mirrors the
local Coinche session to browsers and relays browser actions through U1's
`ClientLink`. See `coinche/web/server.py` and `coinche/web/messages.py`."""

from __future__ import annotations

from coinche.web.server import WebOverlayServer

__all__ = ["WebOverlayServer"]
