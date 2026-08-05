"""One headless client session hosted by the méta-client.

A `MetaSession` is a full client *minus the terminal*: it owns a TCP connection
to the game server, a `ClientState`, a `ClientLink` (the single write path,
reused verbatim from `coinche.client`), and a per-session WebSocket bridge that
mirrors the state to the browser(s) driving this session and relays their
actions back through the link.

It deliberately reuses the existing, tested pieces:

* `ClientState` + `apply_message` — the pure state reducer (`session_state`).
* `ClientLink` — the sole client->server write seam (`coinche.client`).
* `WebOverlayServer` — the HTTP+WS bridge from the mono-session overlay, used
  here **without ever calling `.serve()`**: the méta-client owns the single
  HTTP listener and hands each upgraded WebSocket to `bridge._handle_ws`, while
  `broadcast_state` / `on_browser_message` / `clients` are reused as-is.

No terminal, no `rich`, no termios, no stdin: a méta-client process can host
many of these at once on one asyncio event loop (BR: no per-session thread).
"""

from __future__ import annotations

import asyncio
import logging
import time

from coinche import protocol
from coinche.client import BACKOFF_DELAYS, ClientLink
from coinche.session_state import ClientState, apply_message
from coinche.web import WebOverlayServer

logger = logging.getLogger(__name__)


class _SessionBridge(WebOverlayServer):
    """Per-session WS bridge that also remembers the last JOIN so the session
    can re-join automatically after a dropped game-server connection.

    Overrides `on_browser_message` only to (a) drop actions while the game
    server link is not yet established (a browser can connect before the TCP
    connection is up), and (b) capture join args for reconnection; everything
    else delegates to the untouched base relay."""

    def __init__(self, session: MetaSession, state: ClientState) -> None:
        # link starts as None: it's set on the session once the TCP connection
        # is established. host/port are irrelevant here (serve() is never
        # called), so pass placeholders.
        super().__init__(state, link=None, host="", port=0, on_round_continue=session._dismiss_round_recap)
        self._session = session

    async def on_browser_message(self, msg: dict) -> None:
        # Any browser action counts as activity, keeping the idle reaper away
        # even before a game-server link exists (a browser can connect and act
        # while the TCP connection is still coming up).
        self._session.touch()
        if self.link is None:
            # Not connected to the game server yet — silently ignore; the
            # browser retries actions itself (it resends "lobby" on reconnect).
            return
        if msg["action"] == "join":
            self._session.remember_join(
                msg["table_key"], msg["player_name"], msg.get("team_name"), bool(msg.get("spectate"))
            )
        elif msg["action"] == "leave":
            # Left the table: forget the remembered JOIN so a later TCP drop
            # reconnects into the lobby (subscribe) instead of silently
            # re-seating us at the table we just walked away from.
            self._session.forget_join()
        await super().on_browser_message(msg)


class MetaSession:
    """A single headless client session (one seat's worth of client)."""

    def __init__(
        self,
        session_id: str,
        host: str,
        port: int,
        player_name: str,
    ) -> None:
        self.session_id = session_id
        self.host = host
        self.port = port
        self.player_name = player_name

        self.state = ClientState()
        self.link: ClientLink | None = None
        self.bridge = _SessionBridge(self, self.state)

        # Remembered JOIN args, so a reconnect after a drop re-joins the same
        # table/seat (the server's RESYNC path restores the seat). The 4th element
        # is the spectate flag, so a dropped spectator reconnects as a spectator
        # rather than trying to take a seat.
        self._join_args: tuple[str, str, str | None, bool] | None = None

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._reconnect_index = 0

        # Idle bookkeeping (for the méta-client's reaper). `last_active` is the
        # monotonic time of the last sign of life (browser attach/detach or any
        # browser action); a session is a reap candidate only once no browser is
        # attached AND it has been quiet for longer than the reaper's timeout.
        self._last_active = time.monotonic()

    # ----------------------------------------------------------- idle tracking
    def touch(self) -> None:
        """Mark the session as active right now (resets the idle timer)."""
        self._last_active = time.monotonic()

    @property
    def browser_count(self) -> int:
        """How many browsers are currently attached to this session's bridge."""
        return len(self.bridge.clients)

    def idle_seconds(self, now: float | None = None) -> float:
        """Seconds since the last sign of life on this session."""
        return (now if now is not None else time.monotonic()) - self._last_active

    def is_idle(self, timeout: float, now: float | None = None) -> bool:
        """True when no browser is attached and the session has been quiet for
        at least `timeout` seconds — i.e. safe for the reaper to kick."""
        return self.browser_count == 0 and self.idle_seconds(now) >= timeout

    def remember_join(self, table_key: str, player_name: str, team_name: str | None, spectate: bool = False) -> None:
        self._join_args = (table_key, player_name, team_name, spectate)

    def forget_join(self) -> None:
        """Drop the remembered JOIN so a reconnect returns to the lobby, not the
        table just left (see `_run`'s reconnect branch)."""
        self._join_args = None

    def start(self) -> None:
        """Launch the background connection/receiver loop (idempotent)."""
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def leave_and_stop(self) -> None:
        """Kick an idle session: leave its table (freeing the seat), then stop.

        Sending LEAVE while a link is up lets the game server vacate the seat
        pre-game (or hand it to a bot mid-game) and tear down an abandoned
        table, so an idle méta-client session doesn't pin a seat forever. The
        `forget_join` keeps `stop()`'s reconnect logic from re-seating us if the
        LEAVE races a drop. Best-effort: any failure still proceeds to stop()."""
        self.forget_join()
        link = self.link
        if link is not None:
            try:
                await link.send_leave()
            except (ConnectionError, OSError):
                pass
        await self.stop()

    async def stop(self) -> None:
        """Tear the session down: stop retrying, close browsers and the socket."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for client in list(self.bridge.clients):
            try:
                await client.close()
            except (ConnectionError, OSError):
                pass
        self.bridge.clients.clear()

    async def handle_ws(self, ws: object) -> None:
        """Route one upgraded browser WebSocket to this session's bridge.

        Delegates to the reused `WebOverlayServer._handle_ws`, which sends the
        current snapshot, then loops relaying validated actions. Bracketed with
        `touch()` so both the arrival and the departure of a browser reset the
        idle timer — a session a player just closed the tab on stays alive for
        the full grace period (long enough to survive a refresh/reconnect)."""
        self.touch()
        try:
            await self.bridge._handle_ws(ws)  # type: ignore[arg-type]
        finally:
            self.touch()

    async def _dismiss_round_recap(self) -> None:
        """Mirror the CLI keypress that dismisses the end-of-round recap, then
        rebroadcast so every attached browser advances together."""
        if not self.state.round_over_screen:
            return
        self.state.round_over_screen = False
        await self.bridge.broadcast_state(self.state)

    async def _run(self) -> None:
        """Connect to the game server and pump messages, retrying on drops.

        Mirrors `coinche.client.main`'s reconnection loop but headless: no
        terminal, and the join is driven by the browser (or replayed from
        `_join_args` after a reconnect)."""
        first_attempt = True
        while not self._stop.is_set():
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
            except OSError as exc:
                logger.warning("[%s] connexion à %s:%s échouée : %s", self.session_id, self.host, self.port, exc)
                if not await self._sleep_backoff(first_attempt):
                    return
                first_attempt = False
                continue

            self.link = ClientLink(writer)
            self.bridge.link = self.link

            # Re-join automatically after a reconnect; otherwise start streaming
            # lobby updates so the browser's join screen is live immediately.
            if self._join_args is not None:
                table_key, player_name, team_name, spectate = self._join_args
                await self.link.send_join(table_key, player_name, team_name, spectate=spectate)
            else:
                await self.link.send_subscribe_lobby()

            await self._receiver_loop(reader, writer)

            self.link = None
            self.bridge.link = None
            if self._stop.is_set():
                return
            # Drop happened. Only keep retrying if we had actually joined a game;
            # otherwise (still in the lobby) reconnect promptly too so the page
            # keeps working. Either way, back off between attempts.
            if not await self._sleep_backoff(first_attempt=False):
                return

    async def _receiver_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read server messages, reduce them into `state`, mirror to browsers.

        This is the headless twin of `client.run_session.receiver_loop`: same
        decode/reduce/broadcast, without the terminal redraw or `action_event`
        (no keyboard loop to wake)."""
        try:
            while not self._stop.is_set():
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg_type, payload = protocol.decode(line)
                except protocol.ProtocolError:
                    continue
                apply_message(self.state, msg_type, payload)
                if msg_type == protocol.ERROR:
                    self.forget_join()
                elif msg_type == protocol.TURN_TIMEOUT:
                    # The server has authoritatively expelled this session.
                    # Reconnect without a remembered JOIN so the browser lands
                    # in the live lobby rather than reclaiming the bot seat.
                    self.forget_join()
                await self.bridge.broadcast_state(self.state)
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _sleep_backoff(self, first_attempt: bool) -> bool:
        """Sleep before the next reconnect attempt; return False if asked to
        stop while waiting. The first-ever attempt uses no delay."""
        if first_attempt:
            return not self._stop.is_set()
        delay = BACKOFF_DELAYS[min(self._reconnect_index, len(BACKOFF_DELAYS) - 1)]
        self._reconnect_index += 1
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            return True
        return False
