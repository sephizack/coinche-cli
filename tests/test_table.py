"""Tests for coinche.table: seat assignment, disconnection, reconnection (A14-A16)."""

from __future__ import annotations

import asyncio

import pytest

from coinche.bot import DEFAULT_BOT_TYPE
import coinche.table as table_mod
from coinche.game import Seat
from coinche.table import BOT_NAMES, GameInProgressError, NameTakenError, Table, TableFullError


class FakeWriter:
    """Minimal StreamWriter stand-in: records writes, no real socket."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None


def test_add_player_fills_seats_in_order():
    table = Table("abcd")
    seat1 = table.add_player("Alice", FakeWriter())
    seat2 = table.add_player("Bob", FakeWriter())
    seat3 = table.add_player("Carol", FakeWriter())
    seat4 = table.add_player("Dave", FakeWriter())
    assert [seat1, seat2, seat3, seat4] == [Seat.N, Seat.E, Seat.S, Seat.W]
    assert table.game is not None  # auto-starts once the 4th seat fills


def test_table_passes_non_blocking_coinche_rule_to_games():
    table = Table("abcd", coinche_blocks_bidding=False, bot_type=DEFAULT_BOT_TYPE)
    for name in ("Alice", "Bob", "Carol", "Dave"):
        table.add_player(name, FakeWriter())

    assert table.game is not None
    assert table.game.coinche_blocks_bidding is False
    assert table.bot_type == DEFAULT_BOT_TYPE


def test_fill_with_bots_occupies_open_seats_and_starts_game():
    table = Table("abcd", bot_type="noob")
    table.add_player("Alice", FakeWriter())

    added = table.fill_with_bots()

    assert added == [Seat.E, Seat.S, Seat.W]
    assert table.game is not None
    assert table.seats[Seat.N].is_bot is False
    assert all(table.seats[seat].is_bot for seat in added)
    assert {table.seats[seat].bot_type for seat in added} == {"noob"}
    bot_names = [table.seats[seat].name for seat in added]
    assert all(name in BOT_NAMES for name in bot_names)
    assert len(set(bot_names)) == len(bot_names)


def test_bot_type_is_held_by_each_bot_seat():
    table = Table("abcd", bot_type="noob")
    table.add_player("Alice", FakeWriter())
    table.fill_with_bots()

    table.set_bot_type(Seat.E, "maestro")

    assert table.seats[Seat.E].bot_type == "maestro"
    assert table.seats[Seat.S].bot_type == "noob"


def test_bot_seats_lists_only_bot_held_seats_in_order():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter())  # N, human
    table.fill_with_bots()  # E, S, W bots
    assert table.bot_seats() == [Seat.E, Seat.S, Seat.W]


def test_replace_bot_swaps_a_bot_seat_to_a_human_keeping_the_hand():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter())  # N, human
    table.fill_with_bots()  # E, S, W bots
    assert table.game is not None
    bot_hand_before = list(table.game.get_hand(Seat.E))

    writer = FakeWriter()
    snapshot = table.replace_bot(Seat.E, "Bob", writer, team_name="Equipe 2")

    session = table.seats[Seat.E]
    assert session is not None
    assert session.is_bot is False
    assert session.name == "Bob"
    assert session.writer is writer
    assert session.connected is True
    assert session.team_name == "Equipe 2"
    # The Game is keyed by seat, so taking over the chair inherits its hand.
    assert snapshot["seat"] == Seat.E
    assert snapshot["hand"] == bot_hand_before
    # Bob's seat no longer counts as a replaceable bot; the others still do.
    assert table.bot_seats() == [Seat.S, Seat.W]


def test_replace_bot_rejects_a_non_bot_seat():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter())  # N, human
    table.fill_with_bots()
    with pytest.raises(AssertionError):
        table.replace_bot(Seat.N, "Mallory", FakeWriter())


def test_replace_with_bot_renames_seat_to_a_fresh_bot_name():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter())  # N, human
    table.add_player("Bob", FakeWriter())  # E, human
    table.fill_with_bots()  # S, W bots
    assert table.game is not None
    bot_names_in_use = {table.seats[Seat.S].name, table.seats[Seat.W].name}

    new_name = table.replace_with_bot(Seat.N)

    session = table.seats[Seat.N]
    assert session is not None
    # The seat is now a bot, driven by the server (no writer), and no longer
    # carries the departed human's name/team.
    assert session.is_bot is True
    assert session.writer is None
    assert session.connected is True
    assert session.team_name is None
    assert session.name != "Alice"
    assert new_name == session.name
    # The fresh name is drawn from the bot pool and doesn't collide with the
    # other bots already at the table.
    assert new_name in BOT_NAMES
    assert new_name not in bot_names_in_use


def test_bot_think_delay_adds_one_second_of_random_jitter(monkeypatch):
    table = Table("abcd", bot_think_seconds=1.0)
    bounds: list[tuple[float, float]] = []

    def uniform(minimum: float, maximum: float) -> float:
        bounds.append((minimum, maximum))
        return 1.6

    monkeypatch.setattr(table_mod.random, "uniform", uniform)

    assert table.bot_think_delay() == 1.6
    assert bounds == [(1.0, 2.0)]


def test_bot_think_delay_zero_disables_waiting(monkeypatch):
    table = Table("abcd", bot_think_seconds=0)

    def unexpected_random_delay(_minimum: float, _maximum: float) -> float:
        raise AssertionError("zero bot thinking time must not be randomized")

    monkeypatch.setattr(table_mod.random, "uniform", unexpected_random_delay)

    assert table.bot_think_delay() == 0


def test_remove_table_cancels_and_awaits_background_tasks():
    async def scenario() -> None:
        table = table_mod.get_or_create_table("cleanup")
        bot_task = asyncio.create_task(asyncio.sleep(60))
        table.bot_task = bot_task

        await table_mod.remove_table("cleanup")

        assert "cleanup" not in table_mod.TABLES
        assert bot_task.cancelled()

    asyncio.run(scenario())


def test_add_player_rejects_fifth_join():
    table = Table("abcd")
    for name in ("Alice", "Bob", "Carol", "Dave"):
        table.add_player(name, FakeWriter())
    with pytest.raises(GameInProgressError):
        table.add_player("Eve", FakeWriter())


def test_add_player_rejects_duplicate_connected_name_case_insensitive():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter())
    with pytest.raises(NameTakenError):
        table.add_player("alice", FakeWriter())


def test_add_player_rejects_when_table_somehow_full_without_game():
    # Defensive edge case: all 4 seats occupied but self.game is still None
    # (shouldn't happen via the normal add_player path, but add_player must
    # not silently overwrite an occupied seat).
    from coinche.table import ClientSession

    table = Table("abcd")
    for seat in table.seats:
        table.seats[seat] = ClientSession(seat=seat, name=f"Player-{seat.value}", writer=FakeWriter())
    table.game = None

    with pytest.raises(TableFullError):
        table.add_player("Eve", FakeWriter())


def test_add_player_with_matching_team_name_seats_on_the_opposite_seat():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter(), team_name="A")  # seated at N
    seat = table.add_player("Bob", FakeWriter(), team_name="A")
    assert seat == Seat.S  # PARTNER_OF[N] == S


def test_add_player_with_matching_team_name_is_case_insensitive_and_trims_whitespace():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter(), team_name="Team Rocket")  # seated at N
    seat = table.add_player("Bob", FakeWriter(), team_name="  team rocket  ")
    assert seat == Seat.S


def test_add_player_with_unmatched_team_name_falls_back_to_normal_order():
    table = Table("abcd")
    seat = table.add_player("Bob", FakeWriter(), team_name="B")
    assert seat == Seat.N


def test_add_player_honours_preferred_seat_over_normal_order():
    table = Table("abcd")
    # First joiner asking for W must land on W, not the default N.
    seat = table.add_player("Alice", FakeWriter(), preferred_seat=Seat.W)
    assert seat == Seat.W


def test_add_player_preferred_seat_falls_back_when_taken():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter(), preferred_seat=Seat.N)
    # N is taken now; a request for N falls back to normal fill order (E).
    seat = table.add_player("Bob", FakeWriter(), preferred_seat=Seat.N)
    assert seat == Seat.E


def test_add_player_preferred_seat_wins_over_team_matching():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter(), team_name="A")  # seated at N
    # Team "A" would normally seat Bob at S (opposite Alice), but an explicit
    # free preferred seat takes priority.
    seat = table.add_player("Bob", FakeWriter(), team_name="A", preferred_seat=Seat.E)
    assert seat == Seat.E


def test_add_player_with_matching_team_name_whose_seat_is_taken_falls_back_to_normal_order():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter(), team_name="A")  # N
    table.add_player("Zoe", FakeWriter())  # E
    table.add_player("Carol", FakeWriter())  # S (Alice's partner seat, taken first)
    seat = table.add_player("Bob", FakeWriter(), team_name="A")
    assert seat == Seat.W  # partner seat (S) already taken: normal seat-filling order


def test_add_player_team_name_full_falls_back_to_no_label_seating():
    """When a team_name already has 2 seated players, a third joining with the
    same label should fall back to normal seat-filling with the label ignored
    (not inherit the saturated label on a wrong-side seat)."""
    table = Table("abcd")
    table.add_player("Alice", FakeWriter(), team_name="Equipe 1")  # N
    table.add_player("Bob", FakeWriter(), team_name="Equipe 1")  # S (opposite Alice)
    # Equipe 1 is now full; Carol uses the same label but gets normal seat-filling
    seat = table.add_player("Carol", FakeWriter(), team_name="Equipe 1")
    assert seat == Seat.E  # next seat in SEAT_ORDER after N, S
    session = table.seats[Seat.E]
    # team_name is still recorded on the session (the fallback path stores it),
    # but the pairing invariant holds: N and S are the real Equipe 1 pair.
    assert session is not None
    assert session.team_name == "Equipe 1"


def test_mark_disconnected_flips_flag_without_clearing_seat_or_game():
    table = Table("abcd")
    seats = [table.add_player(name, FakeWriter()) for name in ("Alice", "Bob", "Carol", "Dave")]
    game_before = table.game
    seat = seats[0]
    name = table.mark_disconnected(seat)
    assert name == "Alice"
    assert table.seats[seat] is not None
    assert table.seats[seat].connected is False
    assert table.game is game_before  # untouched


def test_find_disconnected_seat_case_insensitive_and_only_with_game():
    table = Table("abcd")
    table.add_player("Alice", FakeWriter())
    # No game yet (only 1/4 seated): find_disconnected_seat must return None.
    assert table.find_disconnected_seat("Alice") is None

    for name in ("Bob", "Carol", "Dave"):
        table.add_player(name, FakeWriter())
    assert table.game is not None

    seat = Seat.N
    table.mark_disconnected(seat)
    assert table.find_disconnected_seat("ALICE") == seat
    assert table.find_disconnected_seat("Bob") is None  # still connected


def test_reconnect_reattaches_writer_and_returns_snapshot():
    table = Table("abcd")
    for name in ("Alice", "Bob", "Carol", "Dave"):
        table.add_player(name, FakeWriter())
    seat = Seat.N
    table.mark_disconnected(seat)
    assert table.seats[seat].connected is False

    new_writer = FakeWriter()
    snapshot = table.reconnect(seat, new_writer)

    assert table.seats[seat].connected is True
    assert table.seats[seat].writer is new_writer
    assert snapshot["seat"] == seat
    assert len(snapshot["hand"]) == 8
    assert snapshot["phase"] == "bidding"
    assert "cumulative_scores" in snapshot


def test_tables_listing_exposes_connected_flag():
    """The lobby listing must report each seated player's connected state so the
    picker can tell a reconnectable table from a genuinely unavailable one."""
    import coinche.table as table_mod
    from coinche.table import get_or_create_table, tables_listing

    table_mod.TABLES.clear()
    try:
        table = get_or_create_table("recon")
        for name in ("Alice", "Bob", "Carol", "Dave"):
            table.add_player(name, FakeWriter())
        table.mark_disconnected(Seat.N)  # Alice

        listing = tables_listing()
        entry = next(t for t in listing if t["table_key"] == "recon")
        by_name = {p["name"]: p for p in entry["players"]}
        assert by_name["Alice"]["connected"] is False
        assert by_name["Bob"]["connected"] is True
        assert by_name["Alice"]["is_bot"] is False
    finally:
        table_mod.TABLES.clear()


def test_broadcast_and_send_to_write_encoded_json():
    async def run() -> None:
        table = Table("abcd")
        writers = {name: FakeWriter() for name in ("Alice", "Bob", "Carol", "Dave")}
        for name, writer in writers.items():
            table.add_player(name, writer)

        await table.broadcast("chat", {"seat": "N", "text": "hi"})
        for writer in writers.values():
            assert len(writer.written) == 1

        await table.send_to(Seat.N, "chat", {"seat": "N", "text": "private"})
        assert len(writers["Alice"].written) == 2

    asyncio.run(run())


def test_broadcast_write_failure_marks_disconnected():
    async def run() -> None:
        table = Table("abcd")
        for name in ("Alice", "Bob", "Carol", "Dave"):
            table.add_player(name, FakeWriter())

        class BrokenWriter(FakeWriter):
            def write(self, data: bytes) -> None:
                raise ConnectionResetError("peer gone")

        table.seats[Seat.N].writer = BrokenWriter()
        await table.broadcast("chat", {"seat": "E", "text": "hi"})
        assert table.seats[Seat.N].connected is False

    asyncio.run(run())


def test_add_spectator_allowed_on_full_table_and_gets_unique_name():
    table = Table("abcd")
    for name in ("Alice", "Bob", "Carol", "Dave"):
        table.add_player(name, FakeWriter())
    assert table.game is not None  # table is full / game started

    # Spectating a full table is always allowed (no TableFullError).
    n1 = table.add_spectator("Eve", FakeWriter())
    assert n1 == "Eve"
    assert len(table.spectators) == 1

    # A colliding spectator name (vs a seated player) is disambiguated.
    n2 = table.add_spectator("Alice", FakeWriter())
    assert n2 == "Alice 2"
    # And a colliding spectator name (vs another spectator) too.
    n3 = table.add_spectator("Eve", FakeWriter())
    assert n3 == "Eve 2"


def test_broadcast_reaches_spectators():
    async def run() -> None:
        table = Table("abcd")
        for name in ("Alice", "Bob", "Carol", "Dave"):
            table.add_player(name, FakeWriter())
        spec_writer = FakeWriter()
        table.add_spectator("Eve", spec_writer)

        await table.broadcast("chat", {"seat": "N", "name": "Alice", "text": "hi"})
        assert spec_writer.written, "spectator should receive broadcasts"

    asyncio.run(run())


def test_remove_spectator_is_idempotent():
    table = Table("abcd")
    table.add_spectator("Eve", FakeWriter())
    assert len(table.spectators) == 1
    table.remove_spectator("Eve")
    assert len(table.spectators) == 0
    table.remove_spectator("Eve")  # no error the second time
    assert len(table.spectators) == 0


def test_broadcast_drops_a_broken_spectator_without_touching_seats():
    async def run() -> None:
        table = Table("abcd")
        for name in ("Alice", "Bob", "Carol", "Dave"):
            table.add_player(name, FakeWriter())

        class BrokenWriter(FakeWriter):
            def write(self, data: bytes) -> None:
                raise ConnectionResetError("peer gone")

        table.add_spectator("Eve", BrokenWriter())
        await table.broadcast("chat", {"seat": "N", "name": "Alice", "text": "hi"})
        # The broken spectator is dropped; seated players are unaffected.
        assert len(table.spectators) == 0
        assert all(s is not None and s.connected for s in table.seats.values())

    asyncio.run(run())
