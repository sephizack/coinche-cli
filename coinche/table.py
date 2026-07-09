"""Table/session registry: seat assignment, disconnection, and reconnection (A14-A16)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

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
    websocket: ServerConnection | None
    connected: bool = True
    team_name: str | None = None


class Table:
    """A single table's seats, connections, and game state."""

    def __init__(
        self,
        table_key: str,
        target_score: int = rules.DEFAULT_TARGET_SCORE,
        trick_pause_seconds: float = 2.5,
        round_pause_seconds: float = 4.0,
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
        self.lock = asyncio.Lock()
        self.seats: dict[Seat, ClientSession | None] = {seat: None for seat in SEAT_ORDER}
        self.game: Game | None = None

    def add_player(
        self, name: str, websocket: ServerConnection | None, team_name: str | None = None
    ) -> Seat:
        """Seat a new player (A14/A15). Raises TableError subclasses on rejection.

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

        normalized_team = team_name.strip().lower() if team_name else None
        if normalized_team:
            for seat, session in self.seats.items():
                if (
                    session is not None
                    and session.team_name is not None
                    and session.team_name.strip().lower() == normalized_team
                ):
                    partner_seat = PARTNER_OF[seat]
                    if self.seats[partner_seat] is None:
                        self.seats[partner_seat] = ClientSession(
                            seat=partner_seat, name=name, websocket=websocket, connected=True, team_name=team_name
                        )
                        if all(s is not None for s in self.seats.values()):
                            self.game = Game(target_score=self.target_score)
                        return partner_seat
                    break

        for seat in SEAT_ORDER:
            if self.seats[seat] is None:
                self.seats[seat] = ClientSession(seat=seat, name=name, websocket=websocket, connected=True, team_name=team_name)
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

    def reconnect(self, seat: Seat, new_websocket: ServerConnection | None) -> dict:
        """Re-attach a new websocket connection to a disconnected seat and return a resync snapshot (A16)."""
        session = self.seats[seat]
        assert session is not None
        session.websocket = new_websocket
        session.connected = True
        assert self.game is not None
        return self.game.snapshot_for(seat)

    def remove_player(self, seat: Seat) -> None:
        """Free a seat entirely. Pre-game only (game is None); not used for mid-game drops."""
        assert self.game is None
        self.seats[seat] = None

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
            if session is None or not session.connected or session.websocket is None:
                continue
            if exclude is not None and seat == exclude:
                continue
            try:
                await session.websocket.send(data)
            except (ConnectionClosed, OSError):
                self.mark_disconnected(seat)

    async def send_to(self, seat: Seat, msg_type: str, payload: dict) -> None:
        session = self.seats.get(seat)
        if session is None or session.websocket is None:
            return
        data = protocol.encode(msg_type, payload)
        try:
            await session.websocket.send(data)
        except (ConnectionClosed, OSError):
            self.mark_disconnected(seat)


TABLES: dict[str, Table] = {}


def get_or_create_table(
    table_key: str,
    target_score: int = rules.DEFAULT_TARGET_SCORE,
    trick_pause_seconds: float = 2.5,
    round_pause_seconds: float = 4.0,
) -> Table:
    """Lazily create (on first join) or return the existing table for `table_key`."""
    if table_key not in TABLES:
        TABLES[table_key] = Table(
            table_key,
            target_score=target_score,
            trick_pause_seconds=trick_pause_seconds,
            round_pause_seconds=round_pause_seconds,
        )
    return TABLES[table_key]
