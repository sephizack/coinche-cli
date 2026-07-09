"""Tests for coinche.table: seat assignment, disconnection, reconnection (A14-A16)."""

import asyncio

import pytest

from coinche.game import Seat
from coinche.table import GameInProgressError, NameTakenError, Table, TableFullError


class FakeWebSocket:
    """Minimal WebSocket connection stand-in: records sends, no real socket."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    async def send(self, data: bytes) -> None:
        self.written.append(data)


def test_add_player_fills_seats_in_order():
    table = Table("abcd")
    seat1 = table.add_player("Alice", FakeWebSocket())
    seat2 = table.add_player("Bob", FakeWebSocket())
    seat3 = table.add_player("Carol", FakeWebSocket())
    seat4 = table.add_player("Dave", FakeWebSocket())
    assert [seat1, seat2, seat3, seat4] == [Seat.N, Seat.E, Seat.S, Seat.W]
    assert table.game is not None  # auto-starts once the 4th seat fills


def test_add_player_rejects_fifth_join():
    table = Table("abcd")
    for name in ("Alice", "Bob", "Carol", "Dave"):
        table.add_player(name, FakeWebSocket())
    with pytest.raises(GameInProgressError):
        table.add_player("Eve", FakeWebSocket())


def test_add_player_rejects_duplicate_connected_name_case_insensitive():
    table = Table("abcd")
    table.add_player("Alice", FakeWebSocket())
    with pytest.raises(NameTakenError):
        table.add_player("alice", FakeWebSocket())


def test_add_player_rejects_when_table_somehow_full_without_game():
    # Defensive edge case: all 4 seats occupied but self.game is still None
    # (shouldn't happen via the normal add_player path, but add_player must
    # not silently overwrite an occupied seat).
    from coinche.table import ClientSession

    table = Table("abcd")
    for seat in table.seats:
        table.seats[seat] = ClientSession(seat=seat, name=f"Player-{seat.value}", websocket=FakeWebSocket())
    table.game = None

    with pytest.raises(TableFullError):
        table.add_player("Eve", FakeWebSocket())


def test_add_player_with_matching_team_name_seats_on_the_opposite_seat():
    table = Table("abcd")
    table.add_player("Alice", FakeWebSocket(), team_name="A")  # seated at N
    seat = table.add_player("Bob", FakeWebSocket(), team_name="A")
    assert seat == Seat.S  # PARTNER_OF[N] == S


def test_add_player_with_matching_team_name_is_case_insensitive_and_trims_whitespace():
    table = Table("abcd")
    table.add_player("Alice", FakeWebSocket(), team_name="Team Rocket")  # seated at N
    seat = table.add_player("Bob", FakeWebSocket(), team_name="  team rocket  ")
    assert seat == Seat.S


def test_add_player_with_unmatched_team_name_falls_back_to_normal_order():
    table = Table("abcd")
    seat = table.add_player("Bob", FakeWebSocket(), team_name="B")
    assert seat == Seat.N


def test_add_player_with_matching_team_name_whose_seat_is_taken_falls_back_to_normal_order():
    table = Table("abcd")
    table.add_player("Alice", FakeWebSocket(), team_name="A")  # N
    table.add_player("Zoe", FakeWebSocket())  # E
    table.add_player("Carol", FakeWebSocket())  # S (Alice's partner seat, taken first)
    seat = table.add_player("Bob", FakeWebSocket(), team_name="A")
    assert seat == Seat.W  # partner seat (S) already taken: normal seat-filling order


def test_mark_disconnected_flips_flag_without_clearing_seat_or_game():
    table = Table("abcd")
    seats = [table.add_player(name, FakeWebSocket()) for name in ("Alice", "Bob", "Carol", "Dave")]
    game_before = table.game
    seat = seats[0]
    name = table.mark_disconnected(seat)
    assert name == "Alice"
    assert table.seats[seat] is not None
    assert table.seats[seat].connected is False
    assert table.game is game_before  # untouched


def test_find_disconnected_seat_case_insensitive_and_only_with_game():
    table = Table("abcd")
    table.add_player("Alice", FakeWebSocket())
    # No game yet (only 1/4 seated): find_disconnected_seat must return None.
    assert table.find_disconnected_seat("Alice") is None

    for name in ("Bob", "Carol", "Dave"):
        table.add_player(name, FakeWebSocket())
    assert table.game is not None

    seat = Seat.N
    table.mark_disconnected(seat)
    assert table.find_disconnected_seat("ALICE") == seat
    assert table.find_disconnected_seat("Bob") is None  # still connected


def test_reconnect_reattaches_websocket_and_returns_snapshot():
    table = Table("abcd")
    for name in ("Alice", "Bob", "Carol", "Dave"):
        table.add_player(name, FakeWebSocket())
    seat = Seat.N
    table.mark_disconnected(seat)
    assert table.seats[seat].connected is False

    new_websocket = FakeWebSocket()
    snapshot = table.reconnect(seat, new_websocket)

    assert table.seats[seat].connected is True
    assert table.seats[seat].websocket is new_websocket
    assert snapshot["seat"] == seat
    assert len(snapshot["hand"]) == 8
    assert snapshot["phase"] == "bidding"
    assert "cumulative_scores" in snapshot


def test_broadcast_and_send_to_write_encoded_json():
    async def run() -> None:
        table = Table("abcd")
        websockets_ = {name: FakeWebSocket() for name in ("Alice", "Bob", "Carol", "Dave")}
        for name, websocket in websockets_.items():
            table.add_player(name, websocket)

        await table.broadcast("chat", {"seat": "N", "text": "hi"})
        for websocket in websockets_.values():
            assert len(websocket.written) == 1

        await table.send_to(Seat.N, "chat", {"seat": "N", "text": "private"})
        assert len(websockets_["Alice"].written) == 2

    asyncio.run(run())


def test_broadcast_write_failure_marks_disconnected():
    async def run() -> None:
        table = Table("abcd")
        for name in ("Alice", "Bob", "Carol", "Dave"):
            table.add_player(name, FakeWebSocket())

        class BrokenWebSocket(FakeWebSocket):
            async def send(self, data: bytes) -> None:
                raise ConnectionResetError("peer gone")

        table.seats[Seat.N].websocket = BrokenWebSocket()
        await table.broadcast("chat", {"seat": "E", "text": "hi"})
        assert table.seats[Seat.N].connected is False

    asyncio.run(run())
