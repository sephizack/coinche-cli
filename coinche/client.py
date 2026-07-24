"""Coinche TCP client: connection, live-redraw table view, keyboard-menu prompts.

Run with: python -m coinche.client [--host HOST] [--port PORT] [--table KEY] [--name NAME]
                                    [--team TEAM_NAME]
When --table and --team are omitted, the client connects, queries the server
for existing tables (LIST_TABLES), shows a two-step interactive picker:
step 1 — select a table (or create a new one), step 2 — pick Equipe 1 or
Equipe 2 (showing members already on each side), and joins.
--table/--team flags still bypass every interactive step (back-compat).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import select
import signal
import sys
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from rich.live import Live
from rich.text import Text

from coinche import __version__, protocol, ui
from coinche.session_state import ClientState, _build_last_round_contract, apply_message
from coinche.web import WebOverlayServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BACKOFF_DELAYS = (1, 2, 4, 8, 16)

# Thread-safety flag: when set, `_read_single_key` returns "" promptly so the
# executor's worker thread frees up.  This allows shutdown_default_executor()
# to complete within ms rather than blocking for up to 5 minutes waiting on a
# thread stuck in os.read(stdin).
_key_interrupt = threading.Event()


@dataclass
class ClientLink:
    """The single write path to the server socket (BR-U1-5).

    Every client->server message goes through one of these thin async seams;
    keyboard handlers (and, in later units, the web bridge) share them, and no
    other code path encodes/writes a server message. Each seam uses
    `protocol.encode` only — no parallel encoder. Writes are best-effort: a
    dropped connection is swallowed here (the receiver loop notices EOF and
    tears the session down) exactly as the former inline handlers did."""

    writer: asyncio.StreamWriter

    async def _send(self, msg_type: str, payload: dict) -> bool:
        """Encode and write one message; returns True on success, False if the
        connection dropped. Gameplay handlers ignore the result (best-effort,
        the receiver loop notices EOF); the handshake/rematch sites check it to
        preserve their control flow."""
        try:
            self.writer.write(protocol.encode(msg_type, payload))
            await self.writer.drain()
            return True
        except (ConnectionError, OSError):
            return False

    async def send_bid(self, action: str, trump: str | None = None, points: str | int | None = None) -> bool:
        payload: dict = {"action": action}
        if trump is not None:
            payload["trump"] = trump
        if points is not None:
            payload["points"] = points
        return await self._send(protocol.BID, payload)

    async def send_bid_payload(self, payload: dict) -> bool:
        """Send a pre-built bid payload (as produced by the stage-1 bid menu
        tokens, which already carry the exact {action[, trump, points]} shape)."""
        return await self._send(protocol.BID, payload)

    async def send_play(self, card: str) -> bool:
        return await self._send(protocol.PLAY_CARD, {"card": card})

    async def send_chat(self, text: str) -> bool:
        return await self._send(protocol.CHAT, {"text": text})

    async def send_join(self, table_key: str, player_name: str, team_name: str | None) -> bool:
        return await self._send(
            protocol.JOIN,
            {"table_key": table_key, "player_name": player_name, "team_name": team_name},
        )

    async def send_rematch(self) -> bool:
        return await self._send(protocol.REMATCH, {})

    async def send_subscribe_lobby(self) -> bool:
        return await self._send(protocol.SUBSCRIBE_LOBBY, {})


async def run_session(
    host: str,
    port: int,
    table_key: str,
    player_name: str,
    team_name: str | None = None,
    connection: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None,
    web_port: int = 0,
) -> str:
    """Run one connection attempt end-to-end.

    `team_name`, if given, is a free-text label (e.g. "A"/"B") shared with a
    teammate to try to be seated on the same team (best-effort; the server
    falls back to normal seating if no other player joined with the same
    label yet, or their team is already full).

    When *connection* is provided, reuse it (the lobby picker already opened
    it); otherwise open a fresh connection.

    Returns "not_joined" if the session never completed a join/resync,
    "game_over" if the game concluded normally, or "disconnected" if the
    connection dropped mid-session after having joined (worth retrying).
    """
    state = ClientState()

    if connection is not None:
        reader, writer = connection
    else:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            print(f"Impossible de se connecter à {host}:{port} ({exc})")
            return "not_joined"

    link = ClientLink(writer)
    if not await link.send_join(table_key, player_name, team_name):
        return "not_joined"

    # Web overlay bridge (U2): mirrors this session's state to any attached
    # browser and relays browser actions back through the same `link`. It runs
    # as a 3rd coroutine in the gather below and has its own error boundary, so
    # a web fault can never cancel receiver_loop/input_loop (BR-U2-3).
    web = WebOverlayServer(state, link, host="0.0.0.0", port=web_port)

    action_event = asyncio.Event()
    live = Live(auto_refresh=False, screen=True)
    live.start()

    def redraw() -> None:
        if state.server_version is not None and state.server_version != __version__ and not state.update_notice_shown:
            state.update_notice_shown = True
            live.console.print(ui.render_update_notice(__version__, state.server_version))
        if state.seat is None or not state.players:
            status = Text(state.status_message)
            if web.urls:
                status.append("\n\n\U0001f310 Interface web : ", style="grey50")
                status.append(web.urls[0], style="bold cyan link " + web.urls[0])
            live.update(status)
        elif state.round_over_screen and not state.game_over:
            # End-of-round recap (score of the manche just finished, whether
            # the announced contract was honored, cumulative score so far):
            # shown in place of the table until the next DEAL arrives, which
            # the server delays just long enough for this to be readable.
            local_team = state.team_of.get(state.seat, "NS")
            live.update(
                ui.render_round_score(
                    state.last_round_score,
                    state.cumulative_scores,
                    local_team,
                    team_names=state.team_names,
                    contract=state.last_round_contract,
                )
            )
        else:
            local_team = state.team_of.get(state.seat, "NS")
            # While bidding is still open (no settled contract yet), show the
            # current highest bid and its author instead; both share the same
            # "Annonce : ..." footer line via `contract_bidder_name` below.
            if state.contract_bidder is not None:
                display_trump = state.trump
                display_points = state.contract_points
                display_bidder = state.contract_bidder
            else:
                display_trump = state.current_bid_trump
                display_points = state.current_bid_points
                display_bidder = state.current_bid_seat
            contract_bidder_name = (
                state.players.get(display_bidder, display_bidder.value) if display_bidder is not None else None
            )
            # During bidding, no cards have been played yet, so `bid_marks`
            # (each seat's last bid action) is shown at the same table
            # position a played card would occupy.
            table_marks = state.bid_marks or state.current_trick
            # Stage-2 (typing the point value) takes priority over stage-1
            # (the choice grid) when both are technically set, since a trump
            # has already been picked at that point.
            bid_menu = None
            if state.active_bid_value_prompt is not None:
                trump, legal_actions = state.active_bid_value_prompt
                bid_menu, _ = ui.render_bid_value_prompt(trump, legal_actions)
            else:
                # Show the stage-1 bid menu as soon as it's our turn — driven
                # directly by `pending_bid_request` so the options appear
                # without needing a keypress to "reveal" them first.
                req = state.pending_bid_request
                if req is not None:
                    bid_menu, _ = ui.render_bid_menu(
                        req["legal_actions"], req["current_highest_bid"], req["can_coinche"], req["can_surcoinche"]
                    )
            if isinstance(bid_menu, Text) and state.active_bid_value_prompt is not None:
                # Echo the point value typed so far (see `_handle_bid_value_key`)
                # right inside the same Live-tracked renderable instead of
                # relying on the terminal's own line-input echo.
                bid_menu.append(state.bid_value_buffer, style="bold white")
                if state.bid_value_error:
                    bid_menu.append("  \u26a0 valeur invalide", style="bold red")
            view = ui.build_table_view(
                state.seat,
                state.players,
                state.team_of,
                table_marks,
                state.whose_turn,
                state.hand,
                state.cumulative_scores,
                local_team,
                state.last_action,
                connection_status=state.connection_status,
                # Number every card in hand while a play is pending (not just
                # the legal ones) so every card has a clickable-by-number
                # button; illegal choices are rejected locally with a warning
                # instead of being hidden.
                legal_cards=state.hand if state.legal_cards else None,
                trump=display_trump,
                contract_points=display_points,
                contract_bidder_name=contract_bidder_name,
                coinche_level=state.coinche_level,
                last_trick=state.last_trick,
                dealer_seat=state.dealer_seat,
                bid_menu=bid_menu,
                team_names=state.team_names,
                web_url=web.urls[0] if web.urls else None,
            )
            game_focused = state.active_pane == "game"
            left_border = "bold cyan" if game_focused else "grey50"
            left_panel = ui.Panel(
                view,
                title="Table",
                border_style=left_border,
                expand=True,
            )
            local_team = state.team_of.get(state.seat, "NS") if state.seat else None
            chat = ui.build_chat_panel(
                state.chat_messages,
                state.chat_buffer,
                active=not game_focused,
                error=state.chat_error,
                local_team=local_team,
                cursor=state.chat_cursor,
            )
            live.update(ui.build_split_view(left_panel, chat, state.active_pane, height=live.console.size.height))
        live.refresh()

    async def receiver_loop() -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg_type, payload = protocol.decode(line)
                except protocol.ProtocolError:
                    continue
                result = apply_message(state, msg_type, payload)
                if result.action_requested:
                    action_event.set()
                redraw()
                # Web parallel of redraw (FR3.1): push the fresh state to every
                # attached browser. Own error boundary inside broadcast_state.
                await web.broadcast_state(state)
        finally:
            action_event.set()  # wake the input loop so it notices the session ended

    async def input_loop() -> None:
        key_task = asyncio.ensure_future(asyncio.to_thread(_read_single_key))
        try:
            while True:
                # Race persistent key task against action_event (session
                # teardown / game-state update signals EOF).  The same
                # key_task is reused across iterations so only one
                # stdin-reading thread is active at a time — the old
                # pattern spawned a fresh thread every iteration, and when
                # action_event fired first the still-pending thread leaked,
                # producing zombie readers that consumed keystrokes silently.
                event_waiter = asyncio.ensure_future(action_event.wait())
                done, _ = await asyncio.wait(
                    [key_task, event_waiter],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    t.cancel()

                if action_event.is_set():
                    action_event.clear()
                    if reader.at_eof():
                        return
                    key_task = asyncio.ensure_future(asyncio.to_thread(_read_single_key))
                    continue

                # Key completed — consume result but DON'T recreate the
                # task yet: game_over does its own blocking read and must
                # not race with a lingering key reader thread.
                key = key_task.result()

                if not key:
                    return
                if state.game_over:
                    # key_task is consumed (done), no competing thread.
                    choice = await _prompt_game_over_screen(live, state)
                    if choice != "rematch":
                        try:
                            writer.close()
                        except Exception:
                            pass
                        return
                    state.game_over = False
                    action_event.clear()
                    if not await link.send_rematch():
                        return
                    key_task = asyncio.ensure_future(asyncio.to_thread(_read_single_key))
                    continue
                if state.round_over_screen:
                    state.round_over_screen = False
                    redraw()
                elif key == "\t":
                    state.active_pane = "chat" if state.active_pane == "game" else "game"
                    redraw()
                elif state.active_pane == "chat":
                    await _handle_chat_key(state, key, link)
                    redraw()
                elif state.active_bid_value_prompt is not None:
                    # Stage-2 (typing the point value) takes priority: it's set
                    # while pending_bid_request is still present.
                    await _handle_bid_value_key(state, key, link, redraw)
                elif state.pending_bid_request is not None:
                    # Stage-1 bid menu is already on screen (redraw shows it
                    # straight from pending_bid_request); act on the first key.
                    await _handle_bid_key(state, key, link, redraw)
                elif state.legal_cards:
                    # Play menu is already on screen (cards numbered under the
                    # hand); act on the first key.
                    await _handle_play_key(state, key, link, redraw)

                key_task = asyncio.ensure_future(asyncio.to_thread(_read_single_key))
        finally:
            key_task.cancel()

    # Redraw as soon as the web listener binds so the URL shows immediately
    # (even on the pre-join waiting screen), not only after the next server msg.
    web.on_ready = redraw

    try:
        redraw()
        # The web overlay runs forever (serve_forever), so it can't be a peer in
        # the gather that ends when the two session loops finish -- run it as a
        # background task and cancel it once the session is over. Its own
        # try/except boundary means a web fault stays contained; only the two
        # session loops decide when the session ends (BR-U2-3).
        web_task = asyncio.ensure_future(web.serve())
        try:
            await asyncio.gather(receiver_loop(), input_loop())
        finally:
            web_task.cancel()
            try:
                await web_task
            except asyncio.CancelledError:
                pass
    finally:
        live.stop()
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    # Dump every server ERROR from this session now that Live has been torn
    # down (clean terminal). Covers both a rejected join -- where the client
    # would otherwise just vanish silently -- and any in-game errors, so
    # nothing the server complained about is lost.
    if state.errors:
        print("Erreurs signalées par le serveur :")
        for ts, text in state.errors:
            stamp = time.strftime("%H:%M:%S", time.localtime(ts))
            print(f"  [{stamp}] {text}")

    if state.game_over:
        return "game_over"
    if state.joined_once:
        return "disconnected"
    return "not_joined"


_raw_mode_applied: bool = False


def _enable_raw_mode() -> None:
    """Put stdin into byte-at-a-time mode once for the entire session.

    Once applied, the mode persists until the process exits.  The original
    settings are restored by the outermost ``cli()`` try/finally (which
    catches ``KeyboardInterrupt`` and restores the saved termios state).

    Leaving OPOST enabled so Rich's ``\\n`` → ``\\r\\n`` conversion still
    works — clearing it breaks ``Live(screen=True)`` rendering.
    """
    global _raw_mode_applied
    if _raw_mode_applied or not sys.stdin.isatty():
        return
    _IFLAG, _CFLAG, _LFLAG, _CC = 0, 2, 3, 6
    mode = termios.tcgetattr(sys.stdin)
    mode[_IFLAG] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
    mode[_CFLAG] &= ~(termios.CSIZE | termios.PARENB)
    mode[_CFLAG] |= termios.CS8
    mode[_LFLAG] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN)
    mode[_CC][termios.VMIN] = 1
    mode[_CC][termios.VTIME] = 0
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, mode)
    _raw_mode_applied = True


def _read_single_key() -> str:
    """Read one keystroke with no Enter needed (POSIX cbreak mode; falls back
    to a line read when stdin isn't a real terminal, e.g. piped input).

    Polls stdin with a 100 ms timeout so that ``_key_interrupt`` can unblock
    the thread when Ctrl+C is pressed, allowing a clean shutdown."""
    _enable_raw_mode()
    fd = sys.stdin.fileno()

    # Wait for a byte, polling every 100 ms so we notice shutdown requests.
    while not _key_interrupt.is_set():
        rlist, _, _ = select.select([fd], [], [], 0.1)
        if rlist:
            break
    if _key_interrupt.is_set():
        return ""
    ch = os.read(fd, 1).decode("utf-8")

    # \x1b is either the bare Esc key or the start of an escape sequence (eg. arrow keys).
    if ch != "\x1b":
        return ch
    # Read the rest of an escape sequence (up to 2 more bytes: [ + letter).
    # Timeout must be generous: when _read_key runs in a background thread
    # (via asyncio.to_thread), thread scheduling can add tens of ms of
    # delay before we get here — the bytes are already in the terminal
    # buffer, but we need select() to see them.  100ms keeps bare-Esc
    # responsive while making arrow keys reliable even under load.
    rest = ""
    for _ in range(2):
        if _key_interrupt.is_set():
            return ""
        rlist, _, _ = select.select([fd], [], [], 0.1)
        if not rlist:
            break
        rest += os.read(fd, 1).decode("utf-8")
    if rest == "[A":
        return "up"
    if rest == "[B":
        return "down"
    if rest == "[C":
        return "right"
    if rest == "[D":
        return "left"
    return "esc"


async def _prompt_game_over_screen(live: Live, state: ClientState) -> str:
    """Print the end-of-game screen and wait for the player to pick "1" (rematch) or "2" (quit).

    This is the only remaining blocking prompt: game-over is full-screen and
    doesn't participate in the split-pane Tab/Chat dispatch.  Returns
    ``"rematch"`` or ``"quit"`` (also ``"quit"`` if stdin closes)."""
    local_team = state.team_of.get(state.seat, "NS") if state.seat is not None else "NS"
    contract = _build_last_round_contract(state)
    screen = ui.render_game_over(state.final_scores, state.winning_team or "", local_team, contract, state.team_names)
    live.console.print(screen)
    while True:
        key = await asyncio.to_thread(_read_single_key)
        if not key:
            return "quit"
        if key == "1":
            return "rematch"
        if key == "2":
            return "quit"


_MAX_CHAT_LEN = ui.MAX_CHAT_LEN


async def _handle_chat_key(state: ClientState, key: str, link: ClientLink) -> None:
    """Per-key dispatch for the chat pane (called from input_loop)."""
    if key in ("\r", "\n"):
        text = state.chat_buffer.strip()
        if text:
            await link.send_chat(text)
        state.chat_buffer = ""
        state.chat_cursor = 0
        state.chat_error = False
    elif key in ("\x7f", "\x08"):
        if state.chat_cursor > 0:
            state.chat_buffer = state.chat_buffer[: state.chat_cursor - 1] + state.chat_buffer[state.chat_cursor :]
            state.chat_cursor -= 1
        state.chat_error = False
    elif key == "left":
        state.chat_cursor = max(0, state.chat_cursor - 1)
    elif key == "right":
        state.chat_cursor = min(len(state.chat_buffer), state.chat_cursor + 1)
    elif key == "up":
        state.chat_cursor = 0
    elif key == "down":
        state.chat_cursor = len(state.chat_buffer)
    elif key == "\x01":
        state.chat_cursor = 0
    elif key == "\x05":
        state.chat_cursor = len(state.chat_buffer)
    elif key.isprintable() and len(state.chat_buffer) < _MAX_CHAT_LEN:
        state.chat_buffer = state.chat_buffer[: state.chat_cursor] + key + state.chat_buffer[state.chat_cursor :]
        state.chat_cursor += 1
        state.chat_error = False
    elif key.isprintable():
        state.chat_error = True


async def _handle_bid_key(state: ClientState, key: str, link: ClientLink, redraw: Callable[[], None]) -> bool:
    """Per-key dispatch for stage-1 bid menu. Returns True if the key was consumed."""
    req = state.pending_bid_request
    if req is None:
        return False
    _, tokens = ui.render_bid_menu(
        req["legal_actions"], req["current_highest_bid"], req["can_coinche"], req["can_surcoinche"]
    )
    if key not in tokens:
        return False
    choice = tokens[key]
    if choice["action"] == "select_trump":
        trump = choice["trump"]
        state.active_bid_value_prompt = (trump, req["legal_actions"])
        state.bid_value_buffer = ""
        state.bid_value_error = False
        redraw()
        return True
    # A terminal bid action (pass / coinche / surcoinche): consume the request
    # locally and send. BID_UPDATE from the server will also clear it.
    state.pending_bid_request = None
    state.active_bid_value_prompt = None
    redraw()
    await link.send_bid_payload(choice)
    return True


async def _handle_bid_value_key(state: ClientState, key: str, link: ClientLink, redraw: Callable[[], None]) -> bool:
    """Per-key dispatch for stage-2 bid value prompt. Returns True if the key was consumed."""
    if state.active_bid_value_prompt is None:
        return False
    trump, legal_actions = state.active_bid_value_prompt
    _, valid_points = ui.render_bid_value_prompt(trump, legal_actions)
    valid_tokens = {str(p) for p in valid_points}
    if key in ("\r", "\n"):
        token = state.bid_value_buffer.strip().lower()
        if token in valid_tokens:
            points = int(token) if token.isdigit() else token
            state.pending_bid_request = None
            state.active_bid_value_prompt = None
            redraw()
            await link.send_bid("bid", trump=trump, points=points)
            return True
        state.bid_value_error = True
        state.bid_value_buffer = ""
        redraw()
        return True
    if key in ("\x7f", "\x08"):
        state.bid_value_buffer = state.bid_value_buffer[:-1]
        state.bid_value_error = False
        redraw()
        return True
    if key.isprintable():
        state.bid_value_buffer += key
        state.bid_value_error = False
        redraw()
        return True
    return False


async def _handle_play_key(state: ClientState, key: str, link: ClientLink, redraw: Callable[[], None]) -> None:
    """Per-key dispatch for play card selection."""
    _, tokens = ui.render_play_menu(state.hand)
    if key not in tokens:
        return
    choice = tokens[key]
    if choice not in state.legal_cards:
        state.last_action = f"⚠ Impossible de jouer {choice} maintenant (carte non autorisée)."
        redraw()
        return
    state.legal_cards = []
    redraw()
    await link.send_play(choice)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BACKOFF_DELAYS = (1, 2, 4, 8, 16)


def _prompt_host_port(args: argparse.Namespace) -> tuple[str, int]:
    host = args.host or input(f"Adresse du serveur [{DEFAULT_HOST}]: ").strip() or DEFAULT_HOST
    if args.port is not None:
        port = args.port
    else:
        raw_port = input(f"Port [{DEFAULT_PORT}]: ").strip()
        port = int(raw_port) if raw_port else DEFAULT_PORT
    return host, port


def _auto_generate_table_key(existing_tables: list[dict]) -> str:
    """Generate the next available 'Table N' key (displayed as 'Table N',
    stored as lowercase alphanumeric 'tableN' for the server)."""
    existing_keys = {t["table_key"].lower() for t in existing_tables}
    for n in range(1, 10**7):
        key = f"table{n}"
        if len(key) > 12:
            break
        if key not in existing_keys:
            return key
    # Fallback: all 'tableN' keys (N up to the limit) are taken.
    return f"table{len(existing_keys) + 1}"


def _reconnectable_seat(table_entry: dict, player_name: str) -> dict | None:
    """Return the disconnected player entry matching *player_name* on this table,
    or None. A table that is in progress but holds a disconnected seat whose name
    matches (case-insensitive) is one this player can rejoin via the server's
    RESYNC path -- so the picker must let them select it despite being "en cours"."""
    name = player_name.strip().lower()
    if not name:
        return None
    for p in table_entry.get("players", []):
        if not p.get("connected", True) and p.get("name", "").lower() == name:
            return p
    return None


async def _lobby_picker(
    host: str,
    port: int,
    player_name: str = "",
) -> tuple[str, str | None, asyncio.StreamReader, asyncio.StreamWriter] | None:
    """Live-updating interactive lobby picker using a two-step flow.

    Step 1 — table selection: browse tables, Enter to pick one.
    Step 2 — team selection: pick Equipe 1 or Equipe 2, Enter to JOIN.

    Returns ``(table_key, team_name, reader, writer)`` on success, or ``None``
    on cancel / connection error.  The reader/writer are kept open so the
    caller can reuse them for JOIN via ``run_session``.
    """
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        print(f"Impossible de se connecter à {host}:{port} ({exc})")
        return None

    if not await ClientLink(writer).send_subscribe_lobby():
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return None

    latest_tables: list[dict] = []
    latest_event = asyncio.Event()

    async def _receiver() -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg_type, payload = protocol.decode(line)
                except protocol.ProtocolError:
                    continue
                if msg_type == protocol.TABLE_LISTING:
                    latest_tables.clear()
                    latest_tables.extend(payload["tables"])
                    latest_event.set()
        except (ConnectionError, OSError):
            pass
        finally:
            latest_event.set()

    recv_task = asyncio.create_task(_receiver())

    step = "table"
    table_cursor = 0
    selected_table: dict | None = None
    new_table_mode = False
    team_cursor = 0
    lobby_error = ""

    live = Live(
        ui.render_lobby(latest_tables, table_cursor, error=lobby_error, player_name=player_name),
        auto_refresh=False,
        screen=True,
    )
    live.start()

    def redraw() -> None:
        if step == "table":
            live.update(ui.render_lobby(latest_tables, table_cursor, error=lobby_error, player_name=player_name))
        elif step == "team" and selected_table is not None:
            live.update(ui.render_team_picker(selected_table, team_cursor, error=lobby_error))
        live.refresh()

    key_task = asyncio.ensure_future(asyncio.to_thread(_read_single_key))

    try:
        while True:
            # Handle any pending live update before waiting for I/O.
            if latest_event.is_set():
                latest_event.clear()
                if recv_task.done():
                    live.stop()
                    print("Connexion perdue.")
                    return None
                # Refresh state from updated table list.
                if step == "table":
                    table_cursor = min(table_cursor, len(latest_tables))
                elif step == "team" and selected_table is not None:
                    tk = selected_table["table_key"]
                    match = next((t for t in latest_tables if t["table_key"] == tk), None)
                    if new_table_mode:
                        # The new table doesn't exist on the server yet; only
                        # bounce if its key got taken by an in-progress/full table.
                        if match is not None and (match["in_progress"] or match["seats_filled"] >= 4):
                            step = "table"
                            selected_table = None
                            new_table_mode = False
                            lobby_error = "Clé de table déjà prise."
                    else:
                        if match is None:
                            step = "table"
                            selected_table = None
                            lobby_error = "Table disparue."
                        elif match["in_progress"] or match["seats_filled"] >= 4:
                            step = "table"
                            selected_table = None
                            lobby_error = "Table en cours ou complète."
                        else:
                            selected_table = match
                redraw()

            # Race: the persistent key-read task vs. the next live update.
            event_waiter = asyncio.ensure_future(latest_event.wait())
            done, _ = await asyncio.wait(
                [key_task, event_waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                t.cancel()

            if latest_event.is_set():
                continue  # handled at the top of the next iteration

            # Key read completed — consume.
            try:
                key = key_task.result()
            except Exception as exc:
                live.stop()
                print(f"[lobby] Erreur de lecture clavier : {exc}", file=sys.stderr)
                return None

            if not key:
                live.stop()
                print("Entrée fermée.", file=sys.stderr)
                return None

            lobby_error = ""

            if key == "esc":
                if step == "team":
                    step = "table"
                    selected_table = None
                    new_table_mode = False
                else:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                    return None

            # Capture the step before dispatch so that a mid-dispatch
            # transition (table→team) doesn't double-dispatch the same key.
            current_step = step

            if current_step == "table":
                total = len(latest_tables) + 1

                if key == "up":
                    table_cursor = max(0, table_cursor - 1)
                elif key == "down":
                    table_cursor = min(total - 1, table_cursor + 1)
                elif key in ("\r", "\n"):
                    if table_cursor == 0:
                        new_key = _auto_generate_table_key(latest_tables)
                        selected_table = {
                            "table_key": new_key,
                            "in_progress": False,
                            "seats_filled": 0,
                            "players": [],
                        }
                        new_table_mode = True
                        step = "team"
                        team_cursor = 0
                    elif table_cursor - 1 >= len(latest_tables):
                        lobby_error = "Table introuvable."
                    else:
                        selected = latest_tables[table_cursor - 1]
                        reconnect = _reconnectable_seat(selected, player_name)
                        if reconnect is not None:
                            # Name matches a disconnected seat on this table: rejoin
                            # directly. The server's RESYNC path restores our seat, so
                            # we skip team selection and reuse the disconnected seat's
                            # team label.
                            return selected["table_key"], reconnect.get("team_name"), reader, writer
                        if selected["in_progress"] or selected["seats_filled"] >= 4:
                            lobby_error = "Table en cours ou complète."
                        else:
                            step = "team"
                            selected_table = selected
                            team_cursor = 0

            elif current_step == "team" and selected_table is not None:
                if selected_table["in_progress"] or selected_table["seats_filled"] >= 4:
                    step = "table"
                    selected_table = None
                    new_table_mode = False
                    lobby_error = "Table en cours ou complète."
                elif key in ("up", "down"):
                    team_cursor = 1 - team_cursor
                elif key == "1":
                    team_cursor = 0
                elif key == "2":
                    team_cursor = 1
                elif key in ("\r", "\n"):
                    team_label = "Equipe 1" if team_cursor == 0 else "Equipe 2"
                    equipes: dict[str, list[str]] = {"Equipe 1": [], "Equipe 2": []}
                    for p in selected_table["players"]:
                        tn = p.get("team_name")
                        if tn in equipes:
                            equipes[tn].append(p["name"])
                    if len(equipes[team_label]) >= 2:
                        lobby_error = f"{team_label} est complète."
                    else:
                        return selected_table["table_key"], team_label, reader, writer

            redraw()
            key_task = asyncio.ensure_future(asyncio.to_thread(_read_single_key))
    except Exception as exc:
        live.stop()
        print(f"[lobby] Erreur inattendue : {exc}", file=sys.stderr)
        return None
    finally:
        live.stop()
        key_task.cancel()
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coinche network game client")
    parser.add_argument("--host", help="Server host/IP")
    parser.add_argument("--port", type=int, help="Server port")
    parser.add_argument("--table", help="Table key (skips interactive table picker)")
    parser.add_argument("--name", help="Player name")
    parser.add_argument(
        "--team",
        help="Team label ('Equipe 1'/'Equipe 2'); skips interactive team picker",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=0,
        help="Port for the web overlay (0 = auto). The reachable URL(s) are printed on start.",
    )
    return parser


async def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    host, port = _prompt_host_port(args)
    player_name = args.name or input("Votre nom : ").strip()

    # --- table + team selection (interactive lobby or --table/--team bypass) ---
    if args.table is not None:
        table_key = args.table
        team_name = args.team.strip() if args.team else None
        connection: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
    else:
        result = await _lobby_picker(host, port, player_name)
        if result is None:
            return
        table_key, team_name, conn_reader, conn_writer = result
        connection = (conn_reader, conn_writer)

    result = await run_session(
        host, port, table_key, player_name, team_name, connection=connection, web_port=args.web_port
    )
    if result == "not_joined":
        return
    if result == "game_over":
        print("Partie terminée. Au revoir !")
        return

    for delay in BACKOFF_DELAYS:
        print(f"Connexion perdue. Nouvelle tentative dans {delay}s...")
        await asyncio.sleep(delay)
        result = await run_session(host, port, table_key, player_name, team_name, web_port=args.web_port)
        if result == "game_over":
            print("Partie terminée. Au revoir !")
            return
        # "disconnected" or "not_joined" (e.g. server temporarily unreachable): keep retrying.

    print("Impossible de se reconnecter après plusieurs tentatives. Fin du programme.")


def cli() -> None:
    """Entry point. Catches Ctrl+C at the top level so the player gets a clean
    "Au revoir" message instead of a raw asyncio KeyboardInterrupt traceback.

    Installs a custom SIGINT handler that sets ``_key_interrupt`` so the
    worker thread blocked in ``_read_single_key`` returns immediately,
    then raises ``KeyboardInterrupt``.  This keeps ``asyncio.run`` from
    installing its own handler (which would leave the thread blocked while
    ``shutdown_default_executor`` waits up to 5 minutes), and it lets the
    worker thread exit cleanly so ``Runner.close()`` completes in ms.

    Also defensively restores the terminal's mode (``_enable_raw_mode`` puts
    stdin into cbreak mode once for the session): if Ctrl+C lands while a
    background thread is still waking up, the mode stays applied beyond our
    exit, leaving the shell in a broken/no-echo state.
    """
    try:
        fd = sys.stdin.fileno()
        original_settings: list | None = termios.tcgetattr(fd)
    except (termios.error, ValueError, OSError):
        fd = None
        original_settings = None

    # Save the SIGINT handler that was in place before us (usually the
    # default_int_handler installed by site.py).
    prev_sigint = signal.getsignal(signal.SIGINT)

    def _on_sigint(signum: int, frame: object) -> None:
        _key_interrupt.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)

    # Reset the flag so a prior Ctrl+C doesn't break a fresh session
    # (the flag survives across retries in the main() reconnection loop).
    _key_interrupt.clear()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrompu. À bientôt !")
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
        if fd is not None and original_settings is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, original_settings)


if __name__ == "__main__":
    cli()
