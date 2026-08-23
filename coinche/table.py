"""Table/session registry: seat assignment, disconnection, and reconnection (A14-A16)."""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from pathlib import Path

from coinche import protocol, rules
from coinche.bot import DEFAULT_BOT_TYPE
from coinche.game import PARTNER_OF, Game, Seat
from coinche.timeouts import (
    DEFAULT_BOT_THINK_SECONDS,
    DEFAULT_ROUND_PAUSE_SECONDS,
    DEFAULT_TRICK_PAUSE_SECONDS,
    DEFAULT_TURN_TIMEOUT_SECONDS,
)

SEAT_ORDER: tuple[Seat, ...] = (Seat.N, Seat.E, Seat.S, Seat.W)

BOT_NAMES: tuple[str, ...] = (
    # Final Fantasy
    "Cloud",
    "Tifa",
    "Aerith",
    "Sephiroth",
    "Barret",
    "Yuffie",
    "Vincent",
    "Squall",
    "Linoa",
    "Zell",
    "Tidus",
    "Yuna",
    "Auron",
    "Lightning",
    "Noctis",
    "Terra",
    "Locke",
    "Celes",
    "Cait Sith",
    "Cid",
    "Clive",
    "Rydia",
    "Rosa",
    "Kain",
    "Reno",
    "Rude",
    "Firion",
    # Metal Gear Solid
    "Snake",
    "Liquid",
    "Meryl",
    "Otacon",
    "Raiden",
    "Ocelot",
    "Grey Fox",
    "Psycho Mantis",
    "Big Boss",
    "The Boss",
    "Quiet",
    "Miller",
    "Naomi",
    # Clair Obscur
    "Gustave",
    "Lune",
    "Sciel",
    "Maelle",
    "Verso",
    "Monoko",
    "Renoir",
    "Esquie",
    # Dragon Ball Z
    "Goku",
    "Vegeta",
    "Gohan",
    "Piccolo",
    "Bulma",
    "Trunks",
    "Frieza",
    "Cell",
    "C-18",
    "C-17",
    "Beerus",
    "Broly",
    "Tapion",
    "Goten",
    # The Legend of Zelda
    "Link",
    "Zelda",
    "Ganondorf",
    "Navi",
    "Sheik",
)

TABLE_NAMES_PATH = Path(__file__).resolve().parent / "web" / "static" / "table_names.json"
# Table keys must remain ASCII alphanumeric and fit the shared protocol limit.
# The browser fetches this same static file; a numeric suffix is added on collision.
TABLE_NAMES: tuple[str, ...] = tuple(json.loads(TABLE_NAMES_PATH.read_text(encoding="utf-8")))
BOT_THINK_JITTER_SECONDS = 1.0


def _pick_bot_name(used_names: set[str]) -> str:
    """Return a random unused name from BOT_NAMES, or a suffixed fallback."""
    available = [name for name in BOT_NAMES if name.lower() not in used_names]
    if available:
        return random.choice(available)
    base = random.choice(BOT_NAMES)
    suffix = 2
    name = f"{base} {suffix}"
    while name.lower() in used_names:
        suffix += 1
        name = f"{base} {suffix}"
    return name


class TableError(Exception):
    """Base class for table-level join/session errors."""


class TableFullError(TableError):
    pass


class NameTakenError(TableError):
    pass


class GameInProgressError(TableError):
    pass


@dataclass
class ClientSession:
    seat: Seat
    name: str
    writer: asyncio.StreamWriter | None
    connected: bool = True
    team_name: str | None = None
    is_bot: bool = False
    bot_type: str | None = None


@dataclass
class SpectatorSession:
    """A seatless, read-only watcher of a table (A-spectate).

    A spectator receives every public game broadcast (bidding, plays, tricks,
    scores) via `Table.broadcast` and can participate in chat, but holds no
    seat, is never dealt a hand, and cannot bid/play. Multiple spectators can
    watch the same table at once, including while it is full or mid-game."""

    name: str
    writer: asyncio.StreamWriter | None
    connected: bool = True


class Table:
    """A single table's seats, connections, and game state."""

    def __init__(
        self,
        table_key: str,
        target_score: int = rules.DEFAULT_TARGET_SCORE,
        coinche_blocks_bidding: bool = True,
        score_mode: str = rules.DEFAULT_SCORE_MODE,
        bot_type: str = DEFAULT_BOT_TYPE,
        trick_pause_seconds: float = DEFAULT_TRICK_PAUSE_SECONDS,
        round_pause_seconds: float = DEFAULT_ROUND_PAUSE_SECONDS,
        bot_think_seconds: float = DEFAULT_BOT_THINK_SECONDS,
        turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    ) -> None:
        if not rules.is_supported_score_mode(score_mode):
            raise ValueError(f"Unknown score mode: {score_mode!r}")
        self.table_key = table_key
        self.target_score = target_score
        self.coinche_blocks_bidding = coinche_blocks_bidding
        self.score_mode = score_mode
        self.bot_type = bot_type
        # How long the server waits after broadcasting a trick's result before
        # moving play on (next play_request, or dealing the next round), so
        # every player has time to see the last card played (per user request).
        self.trick_pause_seconds = trick_pause_seconds
        # How long the server waits after broadcasting a round's final score
        # (ROUND_SCORE) before dealing the next round, so every player has
        # time to read the end-of-round recap (contract result, cumulative
        # score) shown by the client instead of it flashing by unseen.
        self.round_pause_seconds = round_pause_seconds
        # Minimum pause before each server-controlled bot decision. A further
        # random delay makes consecutive bot turns feel less mechanical.
        self.bot_think_seconds = bot_think_seconds
        self.turn_timeout_seconds = turn_timeout_seconds
        self.lock = asyncio.Lock()
        self.bot_task: asyncio.Task[None] | None = None
        self.turn_timer_task: asyncio.Task[None] | None = None
        self.turn_timer_seat: Seat | None = None
        self.turn_deadline: float | None = None
        # Visual pauses intentionally do not retain `lock`: chat and leaving
        # remain available while the completed trick / round recap is visible.
        # These tasks mark the game transition as pending so bot runners cannot
        # advance play before the corresponding pause has elapsed.
        self.trick_pause_task: asyncio.Task[None] | None = None
        self.round_pause_task: asyncio.Task[None] | None = None
        self.seats: dict[Seat, ClientSession | None] = {seat: None for seat in SEAT_ORDER}
        # Seatless watchers keyed by their (case-insensitive) chat name so a
        # spectator's own writer can be found for removal and duplicate names are
        # rejected. They receive every `broadcast` but never a per-seat `send_to`.
        self.spectators: dict[str, SpectatorSession] = {}
        self.game: Game | None = None
        self.discord_message_id: str | None = None
        self.discord_creator_name: str | None = None
        self.is_closed: bool = False

    def bot_think_delay(self) -> float:
        """Return a human-like delay before one bot decision.

        A zero minimum keeps automated tests and fast local demonstrations
        immediate. Otherwise each decision waits from the configured minimum
        through one additional second.
        """
        if self.bot_think_seconds <= 0:
            return 0
        return random.uniform(self.bot_think_seconds, self.bot_think_seconds + BOT_THINK_JITTER_SECONDS)

    def add_player(
        self,
        name: str,
        writer: asyncio.StreamWriter | None,
        team_name: str | None = None,
        preferred_seat: Seat | None = None,
    ) -> Seat:
        """Seat a new player (A14/A15). Raises TableError subclasses on rejection.

        `preferred_seat`, if given and still free, wins outright: the player is seated
        there directly (used by the web lobby, where clicking a specific empty chair
        should land you in that exact seat). If it's already taken, seating falls back
        to the `team_name` / normal ordering below.

        `team_name` is a free-text, optional label (e.g. "A"/"B" or any name) shared
        by teammates instead of naming each other directly. If it matches (case-
        insensitive, trimmed) another already-seated player's `team_name`, best-effort
        seat this player on the same team: the empty seat opposite that teammate (per
        `PARTNER_OF`) is tried first, falling back to normal seat-filling order (A17)
        when no match is found or that seat isn't free.
        """
        if self.game is not None:
            raise GameInProgressError(self.table_key)

        for session in self.seats.values():
            if session is not None and session.connected and session.name.lower() == name.lower():
                raise NameTakenError(name)

        if preferred_seat is not None and self.seats[preferred_seat] is None:
            self.seats[preferred_seat] = ClientSession(
                seat=preferred_seat, name=name, writer=writer, connected=True, team_name=team_name
            )
            if all(s is not None for s in self.seats.values()):
                self.game = Game(
                    target_score=self.target_score,
                    coinche_blocks_bidding=self.coinche_blocks_bidding,
                    score_mode=self.score_mode,
                )
            return preferred_seat

        normalized_team = team_name.strip().lower() if team_name else None
        if normalized_team:
            team_count = sum(
                1
                for s in self.seats.values()
                if s is not None and s.team_name is not None and s.team_name.strip().lower() == normalized_team
            )
            if team_count < 2:
                for seat, session in self.seats.items():
                    if (
                        session is not None
                        and session.team_name is not None
                        and session.team_name.strip().lower() == normalized_team
                    ):
                        partner_seat = PARTNER_OF[seat]
                        if self.seats[partner_seat] is None:
                            self.seats[partner_seat] = ClientSession(
                                seat=partner_seat, name=name, writer=writer, connected=True, team_name=team_name
                            )
                            if all(s is not None for s in self.seats.values()):
                                self.game = Game(
                                    target_score=self.target_score,
                                    coinche_blocks_bidding=self.coinche_blocks_bidding,
                                    score_mode=self.score_mode,
                                )
                            return partner_seat
                        break

        for seat in SEAT_ORDER:
            if self.seats[seat] is None:
                self.seats[seat] = ClientSession(
                    seat=seat, name=name, writer=writer, connected=True, team_name=team_name
                )
                if all(s is not None for s in self.seats.values()):
                    self.game = Game(
                        target_score=self.target_score,
                        coinche_blocks_bidding=self.coinche_blocks_bidding,
                        score_mode=self.score_mode,
                    )
                return seat

        raise TableFullError(self.table_key)

    def has_humans(self) -> bool:
        """True if any seat is still held by a human (bot/empty seats don't count).

        Disconnected humans still count: their seat holds real game state and
        they may reconnect. A table is only "abandoned" once every occupied
        seat is a bot (or the table is empty). Used to garbage-collect tables
        nobody is playing at anymore instead of leaving bot-only games running.
        """
        return any(s is not None and not s.is_bot for s in self.seats.values())

    def has_connected_humans(self) -> bool:
        """True if a human is connected and can observe or act at this table."""
        return any(s is not None and not s.is_bot and s.connected for s in self.seats.values())

    def find_disconnected_seat(self, name: str) -> Seat | None:
        """Case-insensitive lookup among disconnected seats, only when a game is live (A16)."""
        if self.game is None:
            return None
        for seat, session in self.seats.items():
            if session is not None and not session.connected and session.name.lower() == name.lower():
                return seat
        return None

    def mark_disconnected(self, seat: Seat) -> str:
        """Flag a seat as disconnected without clearing it or touching Game state (A16).

        Returns the disconnected player's name for the caller to broadcast.
        """
        session = self.seats[seat]
        assert session is not None
        session.connected = False
        return session.name

    def reconnect(self, seat: Seat, new_writer: asyncio.StreamWriter | None) -> dict:
        """Re-attach a new writer to a disconnected seat and return a resync snapshot (A16)."""
        session = self.seats[seat]
        assert session is not None
        session.writer = new_writer
        session.connected = True
        assert self.game is not None
        return self.game.snapshot_for(seat)

    def remove_player(self, seat: Seat) -> None:
        """Free a seat entirely. Pre-game only (game is None); not used for mid-game drops."""
        assert self.game is None
        self.seats[seat] = None

    def replace_with_bot(self, seat: Seat) -> str:
        """Hand a seated player's chair over to a server-controlled bot in place.

        Used when a player leaves mid-game: the seat can't simply be vacated
        (the Game holds four hands and expects four actors), so instead the
        session keeps its seat but becomes bot-driven -- `writer` is dropped
        (nothing more is pushed to the departed client) and `is_bot` flips on so
        `_run_bot_turns` will act for it. The seat is also given a fresh random
        bot name (drawn from `BOT_NAMES`, avoiding names already in use at the
        table), so the chair reads as a bot rather than keeping the departed
        human's name -- mirroring `fill_with_bots`. The remaining players are
        never left blocked waiting on an empty seat. Returns the new bot name.
        """
        session = self.seats[seat]
        assert session is not None
        used_names = {s.name.lower() for other, s in self.seats.items() if s is not None and other != seat}
        session.name = _pick_bot_name(used_names)
        session.writer = None
        session.connected = True
        session.is_bot = True
        session.team_name = None
        session.bot_type = self.bot_type
        return session.name

    def bot_seats(self) -> list[Seat]:
        """Seats currently held by a server-controlled bot, in table order.

        These are exactly the chairs a human can sit down in mid-game (a table
        with bots is one someone can join by replacing a bot), the inverse of
        `replace_with_bot`. Empty and human-held seats are excluded.
        """
        return [seat for seat in SEAT_ORDER if (s := self.seats[seat]) is not None and s.is_bot]

    def replace_bot(
        self,
        seat: Seat,
        name: str,
        writer: asyncio.StreamWriter | None,
        team_name: str | None = None,
    ) -> dict:
        """Sit a human down in a bot's chair mid-game and return a resync snapshot.

        The exact inverse of `replace_with_bot`: the seat keeps its hand and
        turn position (the Game is keyed by seat, not by session), but the
        session stops being bot-driven -- `is_bot` flips off, the human's
        `writer`/`name`/`team_name` take over, and `connected` is set. The
        returned `Game.snapshot_for(seat)` lets the caller send a RESYNC so the
        newcomer immediately sees the seat's hand and board, mirroring the
        reconnect path.
        """
        session = self.seats[seat]
        assert session is not None and session.is_bot
        assert self.game is not None
        session.name = name
        session.writer = writer
        session.connected = True
        session.is_bot = False
        session.team_name = team_name
        session.bot_type = None
        return self.game.snapshot_for(seat)

    def set_bot_type(self, seat: Seat, bot_type: str) -> None:
        """Set the strategy used by an occupied server-controlled bot seat."""
        session = self.seats[seat]
        assert session is not None and session.is_bot
        session.bot_type = bot_type

    def add_spectator(self, name: str, writer: asyncio.StreamWriter | None) -> str:
        """Register a seatless watcher and return the (possibly disambiguated) name.

        A spectator name must not collide with a connected seated player's name
        (they share the chat namespace) nor another current spectator; a numeric
        suffix is appended when it would. Unlike `add_player`, this never raises
        and is always allowed -- a full or in-progress table can still be watched.
        """
        seated = {s.name.lower() for s in self.seats.values() if s is not None and s.connected}
        watching = set(self.spectators.keys())
        taken = seated | watching
        unique = name
        if unique.lower() in taken:
            suffix = 2
            while f"{name} {suffix}".lower() in taken:
                suffix += 1
            unique = f"{name} {suffix}"
        self.spectators[unique.lower()] = SpectatorSession(name=unique, writer=writer)
        return unique

    def remove_spectator(self, name: str) -> None:
        """Drop a spectator by name (idempotent)."""
        self.spectators.pop(name.lower(), None)

    def fill_with_bots(self) -> list[Seat]:
        """Fill every open pre-game seat with a server-controlled bot."""
        if self.game is not None:
            raise GameInProgressError(self.table_key)

        used_names = {session.name.lower() for session in self.seats.values() if session is not None}
        added: list[Seat] = []
        for seat in SEAT_ORDER:
            if self.seats[seat] is None:
                name = _pick_bot_name(used_names)
                used_names.add(name.lower())
                self.seats[seat] = ClientSession(
                    seat=seat,
                    name=name,
                    writer=None,
                    connected=True,
                    is_bot=True,
                    bot_type=self.bot_type,
                )
                added.append(seat)
        if added:
            self.game = Game(
                target_score=self.target_score,
                coinche_blocks_bidding=self.coinche_blocks_bidding,
                score_mode=self.score_mode,
            )
        return added

    def restart_game(self) -> Game:
        """Start a brand-new game at this table once the previous one has ended
        (rematch). Resets cumulative scores/round number/dealer rotation back
        to a fresh `Game`, keeping the same seated players."""
        assert self.game is not None and self.game.game_over
        self.game = Game(
            target_score=self.target_score,
            coinche_blocks_bidding=self.coinche_blocks_bidding,
            score_mode=self.score_mode,
        )
        return self.game

    async def broadcast(self, msg_type: str, payload: dict, exclude: Seat | None = None) -> None:
        data = protocol.encode(msg_type, payload)
        for seat, session in list(self.seats.items()):
            if session is None or not session.connected or session.writer is None:
                continue
            if exclude is not None and seat == exclude:
                continue
            try:
                session.writer.write(data)
                await session.writer.drain()
            except (ConnectionError, OSError):
                self.mark_disconnected(seat)
        # Spectators receive every public broadcast (bidding/plays/tricks/scores/
        # chat) but never a per-seat `send_to` (so no hands leak, BR-U1-6). A
        # spectator whose socket errors is dropped outright -- there's no seat to
        # hold open for reconnection.
        await self._broadcast_to_spectators(data)

    async def _broadcast_to_spectators(self, data: bytes) -> None:
        for key, spectator in list(self.spectators.items()):
            if spectator.writer is None:
                continue
            try:
                spectator.writer.write(data)
                await spectator.writer.drain()
            except (ConnectionError, OSError):
                self.spectators.pop(key, None)

    async def send_to(self, seat: Seat, msg_type: str, payload: dict) -> None:
        session = self.seats.get(seat)
        if session is None or session.writer is None:
            return
        data = protocol.encode(msg_type, payload)
        try:
            session.writer.write(data)
            await session.writer.drain()
        except (ConnectionError, OSError):
            self.mark_disconnected(seat)

    @staticmethod
    async def send_to_writer(writer: asyncio.StreamWriter | None, msg_type: str, payload: dict) -> None:
        """Send one message directly to an arbitrary writer (a spectator, which
        holds no seat). Best-effort: a dropped socket is swallowed, matching
        `send_to` -- the connection loop notices the EOF and cleans up."""
        if writer is None:
            return
        try:
            writer.write(protocol.encode(msg_type, payload))
            await writer.drain()
        except (ConnectionError, OSError):
            pass


TABLES: dict[str, Table] = {}


def get_or_create_table(
    table_key: str,
    target_score: int = rules.DEFAULT_TARGET_SCORE,
    coinche_blocks_bidding: bool = True,
    score_mode: str = rules.DEFAULT_SCORE_MODE,
    bot_type: str = DEFAULT_BOT_TYPE,
    trick_pause_seconds: float = DEFAULT_TRICK_PAUSE_SECONDS,
    round_pause_seconds: float = DEFAULT_ROUND_PAUSE_SECONDS,
    bot_think_seconds: float = DEFAULT_BOT_THINK_SECONDS,
    turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
) -> Table:
    """Lazily create (on first join) or return the existing table for `table_key`."""
    if table_key not in TABLES:
        TABLES[table_key] = Table(
            table_key,
            target_score=target_score,
            coinche_blocks_bidding=coinche_blocks_bidding,
            score_mode=score_mode,
            bot_type=bot_type,
            trick_pause_seconds=trick_pause_seconds,
            round_pause_seconds=round_pause_seconds,
            bot_think_seconds=bot_think_seconds,
            turn_timeout_seconds=turn_timeout_seconds,
        )
    return TABLES[table_key]


def cancel_turn_timer(table: Table) -> None:
    """Cancel the active human-turn deadline, if any, and clear its identity."""
    task = table.turn_timer_task
    table.turn_timer_task = None
    table.turn_timer_seat = None
    table.turn_deadline = None
    if task is not None and task is not asyncio.current_task():
        task.cancel()


async def cancel_background_tasks(table: Table, include_turn_timer: bool = True) -> None:
    """Cancel and join background work that no longer has connected players."""
    current_task = asyncio.current_task()
    tasks = tuple(
        task
        for task in (
            table.bot_task,
            table.trick_pause_task,
            table.round_pause_task,
            table.turn_timer_task if include_turn_timer else None,
        )
        if task is not None and task is not current_task
    )
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if include_turn_timer:
        cancel_turn_timer(table)


async def remove_table(table_key: str) -> Table | None:
    """Drop an abandoned table and finish its background tasks."""
    table = TABLES.pop(table_key, None)
    if table is not None:
        table.is_closed = True
        await cancel_background_tasks(table)
    return table


LOBBY_SUBSCRIBERS: set[asyncio.StreamWriter] = set()


def tables_listing() -> list[dict]:
    """Snapshot of every table's lobby state (pre-join query).

    Read-only snapshot without locking -- data may be slightly stale
    (e.g. a player joined moments ago) but that's fine for the picker.
    """
    listing: list[dict] = []
    for _key, table in list(TABLES.items()):
        seats_filled = sum(1 for s in table.seats.values() if s is not None)
        listing.append(
            {
                "table_key": table.table_key,
                "in_progress": table.game is not None,
                "seats_filled": seats_filled,
                "spectators": len(table.spectators),
                "players": [
                    {
                        "seat": _seat_to_str(seat),
                        "name": s.name,
                        "team_name": s.team_name,
                        "connected": s.connected,
                        "is_bot": s.is_bot,
                    }
                    for seat, s in table.seats.items()
                    if s is not None
                ],
            }
        )
    return listing


def _seat_to_str(seat: Seat) -> str:
    return seat.value


async def notify_lobby_subscribers() -> None:
    """Push a TABLE_LISTING to every current lobby subscriber, dropping any that error."""
    data = protocol.encode(protocol.TABLE_LISTING, {"tables": tables_listing()})
    dead: list[asyncio.StreamWriter] = []
    for writer in list(LOBBY_SUBSCRIBERS):
        try:
            writer.write(data)
            await writer.drain()
        except (ConnectionError, OSError):
            dead.append(writer)
    for w in dead:
        LOBBY_SUBSCRIBERS.discard(w)
