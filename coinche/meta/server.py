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
never called). Basic auth guards every request, including the WS handshake,
and a successful landing-page login establishes a persistent browser cookie.
The random session id is itself an unguessable capability.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from coinche import protocol
from coinche.meta.session import MetaSession
from coinche.timeouts import DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS
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
_TABLE_KEY_PATTERN = re.compile(rf"^[A-Za-z0-9]{{{protocol.TABLE_KEY_MIN_LENGTH},{protocol.TABLE_KEY_MAX_LENGTH}}}$")
_PAIR_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_PAIR_CODE_LENGTH = 6
_PAIR_CODE_TTL_SECONDS = 10 * 60
_BROWSER_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
_ACCESS_COOKIE_NAME = "coinche_access"
COINCHE_PUBLIC_URL = os.environ.get("COINCHE_PUBLIC_URL", "").rstrip("/")

# Idle-session reaper defaults. A session with no browser attached and no
# activity for `IDLE_TIMEOUT_SECONDS` is kicked: it LEAVEs its table (freeing
# the seat / tearing down an abandoned table) and is torn down. The grace
# window is comfortably longer than a page refresh or a brief network blip, so
# a returning player resumes their session instead of being reaped.
IDLE_TIMEOUT_SECONDS = DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS
REAP_INTERVAL_SECONDS = 15.0
META_STATIC_DIR = Path(__file__).resolve().parent / "static"
LANDING_PAGE_PATH = META_STATIC_DIR / "landing.html"


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
        if idle_timeout <= 0:
            raise ValueError("idle timeout must be strictly positive")
        self.game_host = game_host
        self.game_port = game_port
        self.auth_user = auth_user
        self.auth_pass = auth_pass
        self.host = host
        self.port = port
        self.idle_timeout = idle_timeout
        self.reap_interval = reap_interval
        self.sessions: dict[str, MetaSession] = {}
        self.pairing_codes: dict[str, float] = {}
        self.browser_sessions: dict[str, float] = {}
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
            route = urlsplit(path).path
            pairing_code = self._pairing_code_from_path(path)
            if method == "GET" and (pairing_code is not None or route == "/a"):
                if pairing_code is None:
                    await self._serve_pairing_entry(writer)
                else:
                    await self._redeem_pairing_code(pairing_code, writer)
                return
            auth_source = self._authorization_source(headers)
            if auth_source is None:
                await self._write_unauthorized(writer)
                _safe_close(writer)
                return
            if auth_source == "basic" and method == "GET" and route == "/":
                await self._serve_landing_page(writer, self._new_access_cookie())
                return
            set_cookie = self._new_access_cookie() if auth_source == "basic" and method == "GET" else None
            await self._route(method, path, headers, reader, writer, set_cookie)
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
        set_cookie: str | None,
    ) -> None:
        split = urlsplit(path)
        route = split.path

        # WebSocket for a session: /s/<id>/ws
        if headers.get("upgrade", "").lower() == "websocket":
            session = self._session_for_ws(route)
            if session is None:
                await WebOverlayServer._write_http(
                    writer, 404, "text/plain; charset=utf-8", b"Unknown session", set_cookie
                )
                _safe_close(writer)
                return
            await self._upgrade_and_route_ws(session, reader, writer, headers, set_cookie)
            return

        if method != "GET":
            await WebOverlayServer._write_http(
                writer, 405, "text/plain; charset=utf-8", b"Method Not Allowed", set_cookie
            )
            _safe_close(writer)
            return

        if route == "/":
            await self._serve_landing_page(writer, set_cookie)
            return

        if route == "/pair":
            await self._create_pairing_page(writer, set_cookie)
            return

        if route == "/new":
            await self._create_and_redirect(split.query, writer, set_cookie)
            return

        # Liveness probe for a stored session id (browser localStorage): the
        # landing page hits this before auto-resuming, so a stale/expired id
        # falls back to the name form instead of a dead reconnect. Returns the
        # remembered player name so the SPA can restore it too.
        if route == "/api/session":
            await self._session_status(split.query, writer, set_cookie)
            return

        session_id = route[len("/s/") :].strip("/") if route.startswith("/s/") else ""
        if session_id and "/" not in session_id:
            session = self.sessions.get(session_id)
            if session is None:
                await self._redirect(writer, "/", set_cookie)
                return
            params = parse_qs(split.query)
            table_key = (params.get("table", [""])[0] or "").strip()
            preferred_seat = (params.get("seat", [""])[0] or "").strip().upper()
            spectate = (params.get("spectate", [""])[0] or "").lower() == "true"
            await self._serve_game_page(
                session,
                writer,
                table_key if _TABLE_KEY_PATTERN.fullmatch(table_key) else None,
                preferred_seat if preferred_seat in {"N", "E", "S", "W"} else None,
                spectate,
                set_cookie,
            )
            return

        # Anything else is a static asset (app.js, styles.css, vendor/…),
        # served from the same static root as the mono-session overlay.
        await self._serve_static(writer, route, set_cookie)

    def _session_for_ws(self, route: str) -> MetaSession | None:
        if not (route.startswith("/s/") and route.endswith("/ws")):
            return None
        session_id = route[len("/s/") : -len("/ws")].strip("/")
        return self.sessions.get(session_id)

    async def _create_and_redirect(self, query: str, writer: asyncio.StreamWriter, set_cookie: str | None) -> None:
        params = parse_qs(query)
        name = (params.get("name", [""])[0] or "").strip()[:_MAX_NAME_LEN]
        if not name:
            # No name → back to the landing page.
            await self._redirect(writer, "/", set_cookie)
            return
        session_id = secrets.token_urlsafe(9)
        session = MetaSession(session_id, self.game_host, self.game_port, name)
        self.sessions[session_id] = session
        session.start()
        logger.info("Nouvelle session %s pour « %s »", session_id, name)
        table_key = (params.get("table", [""])[0] or "").strip()
        preferred_seat = (params.get("seat", [""])[0] or "").strip().upper()
        spectate = (params.get("spectate", [""])[0] or "").lower() == "true"
        suffix = f"?table={quote(table_key, safe='')}" if _TABLE_KEY_PATTERN.fullmatch(table_key) else ""
        if suffix and preferred_seat in {"N", "E", "S", "W"}:
            suffix += f"&seat={preferred_seat}"
        if suffix and spectate:
            suffix += "&spectate=true"
        await self._redirect(writer, f"/s/{session_id}{suffix}", set_cookie)

    async def _session_status(self, query: str, writer: asyncio.StreamWriter, set_cookie: str | None) -> None:
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
        await WebOverlayServer._write_http(
            writer, 200, "application/json; charset=utf-8", body.encode("utf-8"), set_cookie
        )
        _safe_close(writer)

    async def _create_pairing_page(self, writer: asyncio.StreamWriter, set_cookie: str | None) -> None:
        """Mint a one-use, short-lived link for another trusted LAN device."""
        self._purge_expired_access()
        code = "".join(secrets.choice(_PAIR_CODE_ALPHABET) for _ in range(_PAIR_CODE_LENGTH))
        self.pairing_codes[code] = time.monotonic() + _PAIR_CODE_TTL_SECONDS
        display_code = code
        access_url = f"{self._pairing_base_url()}/a/{display_code}"
        body = f"""<!doctype html>
<html lang=\"fr\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>Accès voiture</title><link rel=\"stylesheet\" href=\"/styles.css\"></head>
<body><main class=\"lobby\"><section class=\"lobby__card\">
<h1 class=\"lobby__title\">Accès voiture</h1>
<p>Ouvrez ce lien sur la voiture dans les 10 minutes.</p>
<p><strong>{html.escape(access_url)}</strong></p>
<p>Ou ajoutez <strong>{html.escape(self._pairing_base_url())}/a</strong> aux favoris de la voiture,</p>
<p>puis saisissez ce code : <strong>{display_code}</strong></p>
<p>La voiture restera connectée pendant 30 jours, sauf redémarrage du serveur. Ce lien ne fonctionne qu'une fois.</p>
<p><a class=\"rematch-btn\" href=\"/\">Retour</a></p>
</section></main></body></html>"""
        await WebOverlayServer._write_http(writer, 200, "text/html; charset=utf-8", body.encode("utf-8"), set_cookie)
        _safe_close(writer)

    async def _serve_pairing_entry(self, writer: asyncio.StreamWriter) -> None:
        """Serve the public page where a second device enters a pairing code."""
        body = b"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Acces rapide</title><style>
    body{margin:0;background:#10251c;color:#f7f1df;font:18px system-ui,sans-serif}
    main{max-width:28rem;margin:10vh auto;padding:1.5rem}.card{padding:1.5rem;border:1px solid #d4af37}
    input,button{box-sizing:border-box;width:100%;padding:.75rem;font:inherit}button{margin-top:1rem}
    </style></head><body><main><section class="card">
    <h1>Acces tapide</h1>
<p>Saisissez le code affiche sur le telephone.</p>
<form class="lobby__field"
    onsubmit="event.preventDefault();location.href='/a/'+this.code.value.replace(/[^A-Za-z0-9]/g,'').toUpperCase()">
<label for="code">Code</label>
<input id="code" name="code" type="text" maxlength="6" pattern="[A-Za-z2-9]{6}" required
    autocomplete="one-time-code" autocapitalize="characters" placeholder="ABCDEF">
<button type="submit">Ouvrir</button>
</form></section></main></body></html>"""
        await WebOverlayServer._write_http(writer, 200, "text/html; charset=utf-8", body)
        _safe_close(writer)

    async def _redeem_pairing_code(self, code: str, writer: asyncio.StreamWriter) -> None:
        """Exchange a valid pairing link for a persistent browser-only cookie."""
        self._purge_expired_access()
        expires_at = self.pairing_codes.pop(code, None)
        if expires_at is None or expires_at <= time.monotonic():
            await WebOverlayServer._write_http(
                writer, 404, "text/plain; charset=utf-8", b"Lien d'acces invalide ou expire."
            )
            _safe_close(writer)
            return
        await self._redirect(writer, "/", set_cookie=self._new_access_cookie())

    async def _serve_game_page(
        self,
        session: MetaSession,
        writer: asyncio.StreamWriter,
        table_key: str | None = None,
        preferred_seat: str | None = None,
        spectate: bool = False,
        set_cookie: str | None = None,
    ) -> None:
        """Serve the vendored SPA shell with a per-session `window.__META__`."""
        try:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            await WebOverlayServer._write_http(writer, 404, "text/plain; charset=utf-8", b"Not Found", set_cookie)
            _safe_close(writer)
            return
        meta = json.dumps(
            {
                "wsPath": f"/s/{session.session_id}/ws",
                "name": session.player_name,
                "sessionId": session.session_id,
                "metaClient": True,
                "tableKey": table_key,
                "preferredSeat": preferred_seat,
                "spectate": spectate,
            }
        ).replace("<", "\\u003c")
        inject = f'\n    <base href="/" />\n    <script>window.__META__ = {meta};</script>'
        html = html.replace("<head>", "<head>" + inject, 1)
        await WebOverlayServer._write_http(writer, 200, "text/html; charset=utf-8", html.encode("utf-8"), set_cookie)
        _safe_close(writer)

    async def _serve_static(self, writer: asyncio.StreamWriter, route: str, set_cookie: str | None) -> None:
        """Serve a file from `STATIC_DIR`, guarding against path traversal."""
        rel = route.lstrip("/")
        if not rel:
            rel = "index.html"
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            await WebOverlayServer._write_http(writer, 403, "text/plain; charset=utf-8", b"Forbidden", set_cookie)
            _safe_close(writer)
            return
        if not target.is_file():
            await WebOverlayServer._write_http(writer, 404, "text/plain; charset=utf-8", b"Not Found", set_cookie)
            _safe_close(writer)
            return
        content_type, _ = mimetypes.guess_type(str(target))
        if content_type is None:
            content_type = "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type = f"{content_type}; charset=utf-8"
        await WebOverlayServer._write_http(writer, 200, content_type, target.read_bytes(), set_cookie)
        _safe_close(writer)

    async def _upgrade_and_route_ws(
        self,
        session: MetaSession,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        set_cookie: str | None,
    ) -> None:
        key = headers.get("sec-websocket-key")
        if not key:
            await WebOverlayServer._write_http(
                writer, 400, "text/plain; charset=utf-8", b"Bad WebSocket Request", set_cookie
            )
            _safe_close(writer)
            return
        accept = base64.b64encode(hashlib.sha1((key + _WS_MAGIC).encode("ascii")).digest()).decode("ascii")
        cookie_header = f"Set-Cookie: {set_cookie}\r\n" if set_cookie is not None else ""
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n{cookie_header}\r\n"
        )
        writer.write(handshake.encode("ascii"))
        await writer.drain()
        await session.handle_ws(_WSConnection(reader, writer))

    async def _serve_landing_page(self, writer: asyncio.StreamWriter, set_cookie: str | None = None) -> None:
        try:
            landing_page = LANDING_PAGE_PATH.read_bytes()
        except OSError:
            logger.exception("Méta-client : template de page d'accueil introuvable")
            await WebOverlayServer._write_http(
                writer, 500, "text/plain; charset=utf-8", b"Landing page unavailable", set_cookie
            )
        else:
            await WebOverlayServer._write_http(writer, 200, "text/html; charset=utf-8", landing_page, set_cookie)
        _safe_close(writer)

    # ---------------------------------------------------------------- helpers
    def _authorization_source(self, headers: dict[str, str]) -> str | None:
        self._purge_expired_access()
        cookie = self._cookie_value(headers.get("cookie", ""), _ACCESS_COOKIE_NAME)
        if cookie is not None and cookie in self.browser_sessions:
            return "cookie"
        header = headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return None
        try:
            decoded = base64.b64decode(header[len("basic ") :].strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        user, _, password = decoded.partition(":")
        # Constant-time comparison to avoid leaking length/prefix via timing.
        if hmac.compare_digest(user, self.auth_user) and hmac.compare_digest(password, self.auth_pass):
            return "basic"
        return None

    def _new_access_cookie(self) -> str:
        session_token = secrets.token_urlsafe(32)
        self.browser_sessions[session_token] = time.monotonic() + _BROWSER_SESSION_TTL_SECONDS
        return (
            f"{_ACCESS_COOKIE_NAME}={session_token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={_BROWSER_SESSION_TTL_SECONDS}"
        )

    @staticmethod
    def _pairing_code_from_path(path: str) -> str | None:
        route = urlsplit(path).path
        if not route.startswith("/a/"):
            return None
        code = route[len("/a/") :]
        normalized = code.upper()
        if len(normalized) != _PAIR_CODE_LENGTH or any(char not in _PAIR_CODE_ALPHABET for char in normalized):
            return None
        return normalized

    def _pairing_base_url(self) -> str:
        if COINCHE_PUBLIC_URL:
            return COINCHE_PUBLIC_URL
        return next((url for url in self.urls if not url.startswith("http://127.0.0.1:")), self.urls[0])

    def _purge_expired_access(self) -> None:
        now = time.monotonic()
        self.pairing_codes = {code: expires_at for code, expires_at in self.pairing_codes.items() if expires_at > now}
        self.browser_sessions = {
            token: expires_at for token, expires_at in self.browser_sessions.items() if expires_at > now
        }

    @staticmethod
    def _cookie_value(cookie_header: str, name: str) -> str | None:
        for part in cookie_header.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and hmac.compare_digest(key, name):
                return value
        return None

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
    async def _redirect(writer: asyncio.StreamWriter, location: str, set_cookie: str | None = None) -> None:
        cookie_header = f"Set-Cookie: {set_cookie}\r\n" if set_cookie is not None else ""
        head = (
            f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n{cookie_header}"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1"))
        await writer.drain()
        # We announced `Connection: close`; actually close so a client reading to
        # EOF (e.g. `reader.read()`) unblocks immediately instead of waiting for
        # the writer to be GC'd — a wait that can never end on some platforms.
        _safe_close(writer)
