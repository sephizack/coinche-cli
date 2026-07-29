"""Table/session registry: seat assignment, disconnection, and reconnection (A14-A16)."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from coinche import protocol, rules
from coinche.game import PARTNER_OF, Game, Seat

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
    "Rinoa",
    "Zell",
    "Tidus",
    "Yuna",
    "Auron",
    "Lightning",
    "Noctis",
    "Gladiolus",
    "Ignis",
    "Prompto",
    # Metal Gear Solid
    "Solid Snake",
    "Liquid Snake",
    "Meryl",
    "Otacon",
    "Raiden",
    "Revolver Ocelot",
    "Grey Fox",
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
)
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


class Table:
    """A single table's seats, connections, and game state."""

    def __init__(
        self,
        table_key: str,
        target_score: int = rules.DEFAULT_TARGET_SCORE,
        trick_pause_seconds: float = 2.5,
        round_pause_seconds: float = 4.0,
        bot_think_seconds: float = 1.0,
    ) -> None:
        self.table_key = table_key
        self.target_score = target_score
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
        self.lock = asyncio.Lock()
        self.seats: dict[Seat, ClientSession | None] = {seat: None for seat in SEAT_ORDER}
        self.game: Game | None = None

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
                self.game = Game(target_score=self.target_score)
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
                                self.game = Game(target_score=self.target_score)
                            return partner_seat
                        break

        for seat in SEAT_ORDER:
            if self.seats[seat] is None:
                self.seats[seat] = ClientSession(
                    seat=seat, name=name, writer=writer, connected=True, team_name=team_name
                )
                if all(s is not None for s in self.seats.values()):
                    self.game = Game(target_score=self.target_score)
                return seat

        raise TableFullError(self.table_key)

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
        session keeps its seat/name but becomes bot-driven -- `writer` is
        dropped (nothing more is pushed to the departed client) and `is_bot`
        flips on so `_run_bot_turns` will act for it. The remaining players are
        never left blocked waiting on an empty seat. Returns the departing name.
        """
        session = self.seats[seat]
        assert session is not None
        name = session.name
        session.writer = None
        session.connected = True
        session.is_bot = True
        return name

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
                )
                added.append(seat)
        if added:
            self.game = Game(target_score=self.target_score)
        return added

    def restart_game(self) -> Game:
        """Start a brand-new game at this table once the previous one has ended
        (rematch). Resets cumulative scores/round number/dealer rotation back
        to a fresh `Game`, keeping the same seated players."""
        assert self.game is not None and self.game.game_over
        self.game = Game(target_score=self.target_score)
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


TABLES: dict[str, Table] = {}


def get_or_create_table(
    table_key: str,
    target_score: int = rules.DEFAULT_TARGET_SCORE,
    trick_pause_seconds: float = 2.5,
    round_pause_seconds: float = 4.0,
    bot_think_seconds: float = 1.0,
) -> Table:
    """Lazily create (on first join) or return the existing table for `table_key`."""
    if table_key not in TABLES:
        TABLES[table_key] = Table(
            table_key,
            target_score=target_score,
            trick_pause_seconds=trick_pause_seconds,
            round_pause_seconds=round_pause_seconds,
            bot_think_seconds=bot_think_seconds,
        )
    return TABLES[table_key]


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
