"""Table/session registry: seat assignment, disconnection, and reconnection (A14-A16)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from coinche import protocol, rules
from coinche.game import PARTNER_OF, Game, Seat

SEAT_ORDER: tuple[Seat, ...] = (Seat.N, Seat.E, Seat.S, Seat.W)


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


class Table:
    """A single table's seats, connections, and game state."""

    def __init__(self, table_key: str, target_score: int = rules.DEFAULT_TARGET_SCORE) -> None:
        self.table_key = table_key
        self.target_score = target_score
        self.lock = asyncio.Lock()
        self.seats: dict[Seat, ClientSession | None] = {seat: None for seat in SEAT_ORDER}
        self.game: Game | None = None

    def add_player(
        self, name: str, writer: asyncio.StreamWriter | None, preferred_partner: str | None = None
    ) -> Seat:
        """Seat a new player (A14/A15). Raises TableError subclasses on rejection.

        If `preferred_partner` names another already-seated player (case-insensitive),
        best-effort seat this player on the same team: the empty seat opposite that
        partner (per `PARTNER_OF`) is tried first, falling back to normal seat-filling
        order (A17) when the partner isn't found or their partner seat isn't free.
        """
        if self.game is not None:
            raise GameInProgressError(self.table_key)

        for session in self.seats.values():
            if session is not None and session.connected and session.name.lower() == name.lower():
                raise NameTakenError(name)

        if preferred_partner:
            for seat, session in self.seats.items():
                if session is not None and session.name.lower() == preferred_partner.strip().lower():
                    partner_seat = PARTNER_OF[seat]
                    if self.seats[partner_seat] is None:
                        self.seats[partner_seat] = ClientSession(
                            seat=partner_seat, name=name, writer=writer, connected=True
                        )
                        if all(s is not None for s in self.seats.values()):
                            self.game = Game(target_score=self.target_score)
                        return partner_seat
                    break

        for seat in SEAT_ORDER:
            if self.seats[seat] is None:
                self.seats[seat] = ClientSession(seat=seat, name=name, writer=writer, connected=True)
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


def get_or_create_table(table_key: str, target_score: int = rules.DEFAULT_TARGET_SCORE) -> Table:
    """Lazily create (on first join) or return the existing table for `table_key`."""
    if table_key not in TABLES:
        TABLES[table_key] = Table(table_key, target_score=target_score)
    return TABLES[table_key]
