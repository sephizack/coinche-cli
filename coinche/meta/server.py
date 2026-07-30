"""The méta-client HTTP+WebSocket front door.

A single listener protected by HTTP Basic auth. Flow:

1. ``GET /``           → landing page: enter your name.
2. ``GET /new?name=…`` → create a fresh `MetaSession`, 302 to ``/s/<id>``.
3. ``GET /s/<id>``      → serve the game UI (the vendored `index.html`, with a
                          ``<base href="/">`` and an injected ``window.__META__``
                          so the SPA finds its per-session WebSocket + name).
4. ``/s/<id>/ws``       → WebSocket upgrade routed to that session's bridge.
5. everything else      → a static asset (`app.js`, `styles.css`, `vendor/…`).

This module owns the *only* HTTP listener; each `MetaSession`'s
`WebOverlayServer`-derived bridge is used purely as a relay (its `.serve()` is
never called). Basic auth guards every request, including the WS handshake
(browsers replay stored Basic credentials on same-origin WebSocket upgrades),
and the random session id is itself an unguessable capability.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import mimetypes
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from coinche.meta.session import MetaSession
from coinche.web.server import (
    _WS_MAGIC,
    STATIC_DIR,
    WebOverlayServer,
    _detect_lan_ip,
    _safe_close,
    _terminal_hyperlink,
    _WSConnection,
)

logger = logging.getLogger(__name__)

_MAX_NAME_LEN = 24
_REALM = "Coinche"

# Idle-session reaper defaults. A session with no browser attached and no
# activity for `IDLE_TIMEOUT_SECONDS` is kicked: it LEAVEs its table (freeing
# the seat / tearing down an abandoned table) and is torn down. The grace
# window is comfortably longer than a page refresh or a brief network blip, so
# a returning player resumes their session instead of being reaped.
IDLE_TIMEOUT_SECONDS = 15 * 60.0
REAP_INTERVAL_SECONDS = 15.0

_LANDING_PAGE = """<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <meta name="color-scheme" content="dark" />
    <title>Coinche — Casino</title>
    <link rel="stylesheet" href="/styles.css" />
  </head>
  <body>
    <div class="lobby">
      <div class="lobby__card" id="landing-card" hidden>
        <h1 class="lobby__title">Coinche — Casino</h1>
        <form id="landing-form" class="lobby__field" action="/new" method="get" autocomplete="off">
          <label for="name">Votre nom</label>
          <input id="name" name="name" type="text" maxlength="24" required placeholder="Alice" />
          <button class="rematch-btn" style="width:100%;margin-top:1rem" type="submit">Jouer</button>
        </form>
      </div>
      <div class="lobby__card" id="resume-card" hidden>
        <span class="lobby__spinner" aria-hidden="true"></span>
        <p style="margin-top:1rem">Reprise de votre partie…</p>
      </div>
    </div>
    <script>
      // Session recovery: the SPA stores its méta-session id in localStorage.
      // On the landing page we probe whether that session is still live and, if
      // so, jump straight back into it — so a refresh or an accidental tab
      // close returns the player to their seat instead of spawning a new,
      // orphaned session. A stale/expired id falls back to the name form.
      (function () {
        var KEY = "coinche.metaSessionId";
        // Last name typed here, so a fresh landing (dead/expired session, or a
        // player who explicitly went back home) pre-fills instead of showing an
        // empty field. Kept separate from the session id: it survives even when
        // the session doesn't.
        var NAME_KEY = "coinche.lastName";
        var landing = document.getElementById("landing-card");
        var resume = document.getElementById("resume-card");
        // Persist the chosen name on submit so the next landing pre-fills it.
        var form = document.getElementById("landing-form");
        if (form) {
          form.addEventListener("submit", function () {
            try {
              var input = document.getElementById("name");
              var value = input && input.value.trim();
              if (value) window.localStorage.setItem(NAME_KEY, value);
            } catch (e) {
              /* localStorage unavailable (private mode) — just don't persist */
            }
          });
        }
        function showForm() {
          resume.hidden = true;
          landing.hidden = false;
          var input = document.getElementById("name");
          if (input) {
            try {
              var last = window.localStorage.getItem(NAME_KEY);
              if (last && !input.value) input.value = last;
            } catch (e) {
              /* localStorage unavailable (private mode) — start empty */
            }
            input.focus();
            input.select();
          }
        }
        var id = null;
        try {
          id = window.localStorage.getItem(KEY);
        } catch (e) {
          /* localStorage unavailable (private mode) — just show the form */
        }
        if (!id) {
          showForm();
          return;
        }
        resume.hidden = false;
        fetch("/api/session?id=" + encodeURIComponent(id))
          .then(function (r) {
            return r.ok ? r.json() : { alive: false };
          })
          .then(function (data) {
            if (data && data.alive) {
              window.location.replace("/s/" + encodeURIComponent(id));
            } else {
              try {
                window.localStorage.removeItem(KEY);
              } catch (e) {
                /* ignore */
              }
              showForm();
            }
          })
          .catch(showForm);
      })();
    </script>
  </body>
</html>
"""


class MetaClientServer:
    """Hosts many headless client sessions behind one authenticated web door."""

    def __init__(
        self,
        game_host: str,
        game_port: int,
        auth_user: str,
        auth_pass: str,
        host: str = "0.0.0.0",
        port: int = 0,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        reap_interval: float = REAP_INTERVAL_SECONDS,
    ) -> None:
        self.game_host = game_host
        self.game_port = game_port
        self.auth_user = auth_user
        self.auth_pass = auth_pass
        self.host = host
        self.port = port
        self.idle_timeout = idle_timeout
        self.reap_interval = reap_interval
        self.sessions: dict[str, MetaSession] = {}
        self._bound: tuple[str, int] | None = None
        self.urls: list[str] = []

    # ---------------------------------------------------------------- lifecycle
    async def serve(self) -> None:
        """Bind the listener and serve until cancelled."""
        server = await asyncio.start_server(self._on_connection, self.host, self.port)
        self._bound = server.sockets[0].getsockname()[:2] if server.sockets else (self.host, self.port)
        self.urls = await self._bound_urls()
        print(f"Méta-client Coinche : sessions vers le serveur de jeu {self.game_host}:{self.game_port}")
        for url in self.urls:
            print(f"Interface web disponible : {_terminal_hyperlink(url)}")
        reaper = asyncio.ensure_future(self._reap_idle_sessions())
        try:
            async with server:
                await server.serve_forever()
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            for session in list(self.sessions.values()):
                await session.stop()
            self.sessions.clear()
            server.close()

    async def _reap_idle_sessions(self) -> None:
        """Kick sessions that have had no browser and no activity for too long.

        Runs forever on a fixed interval. A reaped session LEAVEs its table
        first (`leave_and_stop`), so an abandoned seat is freed / a bot takes
        over / an empty table is torn down — this doubles as table housekeeping,
        not just process cleanup. Never lets a single session's teardown fault
        stop the loop."""
        try:
            while True:
                await asyncio.sleep(self.reap_interval)
                now = time.monotonic()
                stale = [
                    (sid, session)
                    for sid, session in list(self.sessions.items())
                    if session.is_idle(self.idle_timeout, now)
                ]
                for sid, session in stale:
                    logger.info(
                        "Session %s (« %s ») inactive %.0fs — expulsée",
                        sid,
                        session.player_name,
                        session.idle_seconds(now),
                    )
                    self.sessions.pop(sid, None)
                    try:
                        await session.leave_and_stop()
                    except Exception:  # noqa: BLE001 — one bad teardown must not stop the reaper
                        logger.exception("Échec de l'expulsion de la session %s", sid)
        except asyncio.CancelledError:
            raise

    async def _bound_urls(self) -> list[str]:
        if self._bound is None:
            return []
        port = self._bound[1]
        urls = [f"http://127.0.0.1:{port}"]
        lan_ip = await asyncio.get_running_loop().run_in_executor(None, _detect_lan_ip)
        if lan_ip:
            urls.append(f"http://{lan_ip}:{port}")
        return urls

    # ---------------------------------------------------------------- routing
    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle one HTTP request: auth, then route. Faults are contained here."""
        try:
            request_line, headers = await WebOverlayServer._read_http_request(reader)
        except (ConnectionError, OSError, asyncio.IncompleteReadError, ValueError):
            _safe_close(writer)
            return
        if request_line is None:
            _safe_close(writer)
            return

        method, path, _ = request_line
        try:
            if not self._authorized(headers):
                await self._write_unauthorized(writer)
                _safe_close(writer)
                return
            await self._route(method, path, headers, reader, writer)
        except (ConnectionError, OSError):
            _safe_close(writer)
        except Exception:  # noqa: BLE001 — per-connection boundary
            logger.exception("Méta-client : gestionnaire de connexion en erreur")
            _safe_close(writer)

    async def _route(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        split = urlsplit(path)
        route = split.path

        # WebSocket for a session: /s/<id>/ws
        if headers.get("upgrade", "").lower() == "websocket":
            session = self._session_for_ws(route)
            if session is None:
                await WebOverlayServer._write_http(writer, 404, "text/plain; charset=utf-8", b"Unknown session")
                _safe_close(writer)
                return
            await self._upgrade_and_route_ws(session, reader, writer, headers)
            return

        if method != "GET":
            await WebOverlayServer._write_http(writer, 405, "text/plain; charset=utf-8", b"Method Not Allowed")
            _safe_close(writer)
            return

        if route == "/":
            await WebOverlayServer._write_http(writer, 200, "text/html; charset=utf-8", _LANDING_PAGE.encode("utf-8"))
            _safe_close(writer)
            return

        if route == "/new":
            await self._create_and_redirect(split.query, writer)
            return

        # Liveness probe for a stored session id (browser localStorage): the
        # landing page hits this before auto-resuming, so a stale/expired id
        # falls back to the name form instead of a dead reconnect. Returns the
        # remembered player name so the SPA can restore it too.
        if route == "/api/session":
            await self._session_status(split.query, writer)
            return

        session_id = route[len("/s/") :].strip("/") if route.startswith("/s/") else ""
        if session_id and "/" not in session_id:
            session = self.sessions.get(session_id)
            if session is None:
                await self._redirect(writer, "/")
                return
            await self._serve_game_page(session, writer)
            return

        # Anything else is a static asset (app.js, styles.css, vendor/…),
        # served from the same static root as the mono-session overlay.
        await self._serve_static(writer, route)

    def _session_for_ws(self, route: str) -> MetaSession | None:
        if not (route.startswith("/s/") and route.endswith("/ws")):
            return None
        session_id = route[len("/s/") : -len("/ws")].strip("/")
        return self.sessions.get(session_id)

    async def _create_and_redirect(self, query: str, writer: asyncio.StreamWriter) -> None:
        params = parse_qs(query)
        name = (params.get("name", [""])[0] or "").strip()[:_MAX_NAME_LEN]
        if not name:
            # No name → back to the landing page.
            await self._redirect(writer, "/")
            return
        session_id = secrets.token_urlsafe(9)
        session = MetaSession(session_id, self.game_host, self.game_port, name)
        self.sessions[session_id] = session
        session.start()
        logger.info("Nouvelle session %s pour « %s »", session_id, name)
        await self._redirect(writer, f"/s/{session_id}")

    async def _session_status(self, query: str, writer: asyncio.StreamWriter) -> None:
        """Report whether a stored session id is still live (JSON).

        `{"alive": true, "name": "Alice"}` when the id maps to a live session,
        `{"alive": false}` otherwise. Deliberately does NOT `touch()` the
        session: a background liveness poll must not keep a truly abandoned
        session alive — only a real browser attach/action does."""
        session_id = (parse_qs(query).get("id", [""])[0] or "").strip()
        session = self.sessions.get(session_id) if session_id else None
        body = (
            json.dumps({"alive": True, "name": session.player_name})
            if session is not None
            else json.dumps({"alive": False})
        )
        await WebOverlayServer._write_http(writer, 200, "application/json; charset=utf-8", body.encode("utf-8"))
        _safe_close(writer)

    async def _serve_game_page(self, session: MetaSession, writer: asyncio.StreamWriter) -> None:
        """Serve the vendored SPA shell with a per-session `window.__META__`."""
        try:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            await WebOverlayServer._write_http(writer, 404, "text/plain; charset=utf-8", b"Not Found")
            _safe_close(writer)
            return
        meta = json.dumps(
            {
                "wsPath": f"/s/{session.session_id}/ws",
                "name": session.player_name,
                "sessionId": session.session_id,
            }
        ).replace("<", "\\u003c")
        inject = f'\n    <base href="/" />\n    <script>window.__META__ = {meta};</script>'
        html = html.replace("<head>", "<head>" + inject, 1)
        await WebOverlayServer._write_http(writer, 200, "text/html; charset=utf-8", html.encode("utf-8"))
        _safe_close(writer)

    async def _serve_static(self, writer: asyncio.StreamWriter, route: str) -> None:
        """Serve a file from `STATIC_DIR`, guarding against path traversal."""
        rel = route.lstrip("/")
        if not rel:
            rel = "index.html"
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            await WebOverlayServer._write_http(writer, 403, "text/plain; charset=utf-8", b"Forbidden")
            _safe_close(writer)
            return
        if not target.is_file():
            await WebOverlayServer._write_http(writer, 404, "text/plain; charset=utf-8", b"Not Found")
            _safe_close(writer)
            return
        content_type, _ = mimetypes.guess_type(str(target))
        if content_type is None:
            content_type = "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type = f"{content_type}; charset=utf-8"
        await WebOverlayServer._write_http(writer, 200, content_type, target.read_bytes())
        _safe_close(writer)

    async def _upgrade_and_route_ws(
        self,
        session: MetaSession,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
    ) -> None:
        key = headers.get("sec-websocket-key")
        if not key:
            await WebOverlayServer._write_http(writer, 400, "text/plain; charset=utf-8", b"Bad WebSocket Request")
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
        await session.handle_ws(_WSConnection(reader, writer))

    # ---------------------------------------------------------------- helpers
    def _authorized(self, headers: dict[str, str]) -> bool:
        header = headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(header[len("basic ") :].strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, _, password = decoded.partition(":")
        # Constant-time comparison to avoid leaking length/prefix via timing.
        return hmac.compare_digest(user, self.auth_user) and hmac.compare_digest(password, self.auth_pass)

    async def _write_unauthorized(self, writer: asyncio.StreamWriter) -> None:
        body = b"Authentification requise."
        head = (
            "HTTP/1.1 401 Unauthorized\r\n"
            f'WWW-Authenticate: Basic realm="{_REALM}", charset="UTF-8"\r\n'
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()

    @staticmethod
    async def _redirect(writer: asyncio.StreamWriter, location: str) -> None:
        head = f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        writer.write(head.encode("latin-1"))
        await writer.drain()
