"""End-to-end integration tests: a real asyncio TCP server driven by scripted
socket clients (no coinche.ui/terminal involved).

Both scenarios below rely on two facts that hold for any freshly-created
table in this implementation:

- `Table.add_player` creates `Game(target_score=...)` with its default
  `initial_dealer=Seat.N`, so the first bidder (`dealer.next()`) is always
  seat "W" (rotation N -> W -> S -> E -> N, per A1).
- Seats are assigned in join order N, E, S, W (`table.SEAT_ORDER`).

The scripted strategy is deterministic-but-adaptive: the first bidder always
bids the *first* option in the server-provided `legal_actions` list, the
other three seats always pass (closing the auction after 3 consecutive
passes), and every card play always picks `legal_cards[0]` from the
server-provided `play_request`/resync-triggered request - never guessing at
card identities independently of what the server says is legal.
"""

from __future__ import annotations

import asyncio

import pytest

from coinche import protocol, server

HOST = "127.0.0.1"

# Fixed trick-play rotation (A1): N -> W -> S -> E -> N.
ROTATION_NEXT = {"N": "W", "W": "S", "S": "E", "E": "N"}

SEAT_JOIN_ORDER = ("N", "E", "S", "W")
NAMES_BY_SEAT = {"N": "Alice", "E": "Bob", "S": "Carol", "W": "Dave"}


async def _start_server(target_score: int = 1000) -> tuple[asyncio.AbstractServer, int]:
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # trick_pause_seconds=0/round_pause_seconds=0: these tests don't care
        # about the UX pauses after each trick/round (added per user request)
        # and would otherwise take much longer per round played (8 tricks *
        # 2.5s, plus 4s between rounds).
        await server.handle_connection(
            reader, writer, target_score, trick_pause_seconds=0, round_pause_seconds=0
        )

    srv = await asyncio.start_server(_handler, HOST, 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, port


async def _connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(HOST, port)


async def _send(writer: asyncio.StreamWriter, msg_type: str, payload: dict) -> None:
    writer.write(protocol.encode(msg_type, payload))
    await writer.drain()


async def _recv(reader: asyncio.StreamReader, timeout: float = 5.0) -> tuple[str, dict]:
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    assert line, "connection closed unexpectedly while awaiting a message"
    return protocol.decode(line)


async def _read_until(reader: asyncio.StreamReader, msg_type: str) -> dict:
    """Read from `reader` until a message of `msg_type` arrives, ignoring others."""
    while True:
        mtype, payload = await _recv(reader)
        if mtype == protocol.ERROR:
            raise AssertionError(f"unexpected error message while waiting for {msg_type!r}: {payload}")
        if mtype == msg_type:
            return payload


async def _read_any_until(reader: asyncio.StreamReader, msg_type: str) -> dict:
    """Like `_read_until`, but does not special-case `protocol.ERROR` -- used when
    the message being waited for is itself an expected `error` response."""
    while True:
        mtype, payload = await _recv(reader)
        if mtype == msg_type:
            return payload


async def _assert_no_message(reader: asyncio.StreamReader, timeout: float = 0.3) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(reader.readline(), timeout=timeout)


async def _join_all(port: int, table_key: str) -> dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    """Connect and join 4 scripted clients in seat order N, E, S, W."""
    conns: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
    for seat_key in SEAT_JOIN_ORDER:
        reader, writer = await _connect(port)
        await _send(writer, protocol.JOIN, {"table_key": table_key, "player_name": NAMES_BY_SEAT[seat_key]})
        mtype, payload = await _recv(reader)
        assert mtype == protocol.JOINED
        assert payload["seat"] == seat_key
        conns[seat_key] = (reader, writer)
    return conns


async def _bid_min_and_finalize_contract(conns: dict, observer_reader: asyncio.StreamReader) -> dict:
    """Scripted deterministic auction: W bids the minimum legal contract, the
    other three seats pass in rotation, closing the auction with W as both
    bidder and first trick leader."""
    bid_req = await _read_until(conns["W"][0], protocol.BID_REQUEST)
    assert bid_req["legal_actions"], "first bidder must have at least one legal bid"
    action = bid_req["legal_actions"][0]
    await _send(conns["W"][1], protocol.BID, {"action": "bid", "trump": action["trump"], "points": action["points"]})

    update = await _read_until(observer_reader, protocol.BID_UPDATE)
    assert update["seat"] == "W" and update["action"] == "bid"
    next_seat = update["next_to_act"]

    for _ in range(2):
        await _read_until(conns[next_seat][0], protocol.BID_REQUEST)
        await _send(conns[next_seat][1], protocol.BID, {"action": "pass"})
        update = await _read_until(observer_reader, protocol.BID_UPDATE)
        assert update["seat"] == next_seat and update["action"] == "pass"
        next_seat = update["next_to_act"]

    await _read_until(conns[next_seat][0], protocol.BID_REQUEST)
    await _send(conns[next_seat][1], protocol.BID, {"action": "pass"})
    result = await _read_until(observer_reader, protocol.BIDDING_RESULT)
    assert result["outcome"] == "contract"
    assert result["seat"] == "W"
    assert result["trump"] == action["trump"]
    assert result["points"] == action["points"]

    return {"trump": action["trump"], "points": action["points"], "first_leader": "W"}


def test_full_round_join_deal_bid_trick_score_flow():
    async def scenario() -> None:
        srv, port = await _start_server(target_score=1000)
        conns: dict = {}
        try:
            conns = await _join_all(port, "round01")
            observer_reader = conns["N"][0]

            deal_payload = await _read_until(conns["W"][0], protocol.DEAL)
            assert len(deal_payload["hand"]) == 8
            assert deal_payload["dealer_seat"] == "N"
            assert deal_payload["first_bidder_seat"] == "W"

            await _bid_min_and_finalize_contract(conns, observer_reader)

            current_actor = "W"
            for trick_num in range(1, 9):
                for _ in range(4):
                    req = await _read_until(conns[current_actor][0], protocol.PLAY_REQUEST)
                    assert req["legal_cards"], f"{current_actor} must have a legal card to play"
                    card = req["legal_cards"][0]
                    await _send(conns[current_actor][1], protocol.PLAY_CARD, {"card": card})
                    played = await _read_until(observer_reader, protocol.CARD_PLAYED)
                    assert played["seat"] == current_actor
                    current_actor = ROTATION_NEXT[current_actor]

                trick_result = await _read_until(observer_reader, protocol.TRICK_RESULT)
                assert trick_result["tricks_played"] == trick_num
                assert trick_result["tricks_remaining"] == 8 - trick_num
                current_actor = trick_result["winner_seat"]

            round_score = await _read_until(observer_reader, protocol.ROUND_SCORE)
            assert set(round_score["team_NS"]) >= {"card_points", "contract_result", "total"}
            assert set(round_score["team_EW"]) >= {"card_points", "contract_result", "total"}
            assert round_score["cumulative"]["NS"] == round_score["team_NS"]["total"]
            assert round_score["cumulative"]["EW"] == round_score["team_EW"]["total"]
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_disconnect_and_reconnect_mid_round():
    async def scenario() -> None:
        srv, port = await _start_server(target_score=1000)
        conns: dict = {}
        try:
            conns = await _join_all(port, "recon01")
            observer_reader = conns["N"][0]

            await _read_until(conns["W"][0], protocol.DEAL)
            await _bid_min_and_finalize_contract(conns, observer_reader)

            # Trick 1: W and S play normally.
            for seat in ("W", "S"):
                req = await _read_until(conns[seat][0], protocol.PLAY_REQUEST)
                card = req["legal_cards"][0]
                await _send(conns[seat][1], protocol.PLAY_CARD, {"card": card})
                played = await _read_until(observer_reader, protocol.CARD_PLAYED)
                assert played["seat"] == seat

            # It is now E's turn. E receives its play_request, then its
            # connection drops before responding (simulated real disconnect).
            await _read_until(conns["E"][0], protocol.PLAY_REQUEST)
            _, e_writer = conns["E"]
            e_writer.close()
            try:
                await e_writer.wait_closed()
            except (ConnectionError, OSError):
                pass

            status = await _read_until(observer_reader, protocol.CONNECTION_STATUS)
            assert status["seat"] == "E"
            assert status["status"] == "disconnected"

            # The game must not silently advance past E's turn: no further
            # broadcast (e.g. a card_played) should arrive while E is gone.
            await _assert_no_message(observer_reader)

            # A new connection re-joins with the same table_key + player_name.
            new_reader, new_writer = await _connect(port)
            await _send(new_writer, protocol.JOIN, {"table_key": "recon01", "player_name": NAMES_BY_SEAT["E"]})

            resync = await _read_until(new_reader, protocol.RESYNC)
            assert resync["seat"] == "E"
            assert resync["phase"] == "trick_play"
            assert resync["whose_turn"] == "E"
            assert len(resync["hand"]) == 8  # E had not played any card yet this trick
            assert len(resync["current_trick"]) == 2  # W's and S's cards already on the table

            reconnected_status = await _read_until(observer_reader, protocol.CONNECTION_STATUS)
            assert reconnected_status["seat"] == "E"
            assert reconnected_status["status"] == "reconnected"

            conns["E"] = (new_reader, new_writer)

            # It's still E's turn: the server follows resync with a fresh play_request.
            play_req = await _read_until(new_reader, protocol.PLAY_REQUEST)
            assert play_req["legal_cards"]
            card = play_req["legal_cards"][0]
            await _send(new_writer, protocol.PLAY_CARD, {"card": card})
            played = await _read_until(observer_reader, protocol.CARD_PLAYED)
            assert played["seat"] == "E"

            # Finish trick 1 with N's final play (W, S, E already played above).
            req = await _read_until(conns["N"][0], protocol.PLAY_REQUEST)
            card = req["legal_cards"][0]
            await _send(conns["N"][1], protocol.PLAY_CARD, {"card": card})
            played = await _read_until(observer_reader, protocol.CARD_PLAYED)
            assert played["seat"] == "N"

            trick_result = await _read_until(observer_reader, protocol.TRICK_RESULT)
            assert trick_result["tricks_played"] == 1
            current_actor = trick_result["winner_seat"]

            # Drive the remaining 7 tricks to completion, confirming play
            # resumes correctly after the reconnect.
            for trick_num in range(2, 9):
                for _ in range(4):
                    req = await _read_until(conns[current_actor][0], protocol.PLAY_REQUEST)
                    card = req["legal_cards"][0]
                    await _send(conns[current_actor][1], protocol.PLAY_CARD, {"card": card})
                    played = await _read_until(observer_reader, protocol.CARD_PLAYED)
                    assert played["seat"] == current_actor
                    current_actor = ROTATION_NEXT[current_actor]

                trick_result = await _read_until(observer_reader, protocol.TRICK_RESULT)
                assert trick_result["tricks_played"] == trick_num
                current_actor = trick_result["winner_seat"]

            round_score = await _read_until(observer_reader, protocol.ROUND_SCORE)
            assert round_score["cumulative"]["NS"] == round_score["team_NS"]["total"]
            assert round_score["cumulative"]["EW"] == round_score["team_EW"]["total"]
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_out_of_turn_bid_rejected_with_error_and_game_unaffected():
    async def scenario() -> None:
        srv, port = await _start_server()
        conns: dict = {}
        try:
            conns = await _join_all(port, "turn01")
            observer_reader = conns["N"][0]

            await _read_until(conns["W"][0], protocol.DEAL)
            bid_req = await _read_until(conns["W"][0], protocol.BID_REQUEST)

            # S attempts to bid before it is their turn (W is first bidder).
            await _send(conns["S"][1], protocol.BID, {"action": "pass"})
            error_payload = await _read_any_until(conns["S"][0], protocol.ERROR)
            assert error_payload["code"] == protocol.NOT_YOUR_TURN

            # Game state must be unaffected: W can still bid normally afterward.
            action = bid_req["legal_actions"][0]
            await _send(
                conns["W"][1], protocol.BID, {"action": "bid", "trump": action["trump"], "points": action["points"]}
            )
            update = await _read_until(observer_reader, protocol.BID_UPDATE)
            assert update["seat"] == "W" and update["action"] == "bid"
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_out_of_turn_card_play_rejected_with_error_and_game_unaffected():
    async def scenario() -> None:
        srv, port = await _start_server()
        conns: dict = {}
        try:
            conns = await _join_all(port, "turn02")
            observer_reader = conns["N"][0]

            await _read_until(conns["W"][0], protocol.DEAL)
            await _bid_min_and_finalize_contract(conns, observer_reader)

            play_req = await _read_until(conns["W"][0], protocol.PLAY_REQUEST)

            # S attempts to play before it is their turn (W is first leader).
            await _send(conns["S"][1], protocol.PLAY_CARD, {"card": "7♠"})
            error_payload = await _read_any_until(conns["S"][0], protocol.ERROR)
            assert error_payload["code"] == protocol.NOT_YOUR_TURN

            # Game state must be unaffected: W can still lead normally afterward.
            card = play_req["legal_cards"][0]
            await _send(conns["W"][1], protocol.PLAY_CARD, {"card": card})
            played = await _read_until(observer_reader, protocol.CARD_PLAYED)
            assert played["seat"] == "W"
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_illegal_card_play_rejected_with_error_and_state_unchanged(monkeypatch):
    # Disable shuffling so the deal is fully deterministic: with dealer=N and
    # the 3-2-3 packet split, W (first to act) is dealt 7♠,8♠,9♠,V♥,D♥,V♦,D♦,R♦
    # and S is dealt 10♠,V♠,D♠,R♥,A♥,A♦,7♣,8♣ (in unshuffled build_deck() order).
    monkeypatch.setattr("coinche.cards.random.shuffle", lambda seq: None)

    async def scenario() -> None:
        srv, port = await _start_server()
        conns: dict = {}
        try:
            conns = await _join_all(port, "illeg01")
            observer_reader = conns["N"][0]

            await _read_until(conns["W"][0], protocol.DEAL)
            await _bid_min_and_finalize_contract(conns, observer_reader)  # W bids 80 ♠

            w_play_req = await _read_until(conns["W"][0], protocol.PLAY_REQUEST)
            w_card = w_play_req["legal_cards"][0]
            assert w_card == "7♠"
            await _send(conns["W"][1], protocol.PLAY_CARD, {"card": w_card})
            await _read_until(observer_reader, protocol.CARD_PLAYED)

            # S must follow/overtrump in spades; R♥ (a heart S holds) is illegal.
            s_play_req = await _read_until(conns["S"][0], protocol.PLAY_REQUEST)
            assert "R♥" not in s_play_req["legal_cards"]
            await _send(conns["S"][1], protocol.PLAY_CARD, {"card": "R♥"})
            error_payload = await _read_any_until(conns["S"][0], protocol.ERROR)
            assert error_payload["code"] == protocol.ILLEGAL_CARD

            # State unchanged: no card_played broadcast for the illegal attempt;
            # S can still play a legal card afterward.
            legal_card = s_play_req["legal_cards"][0]
            await _send(conns["S"][1], protocol.PLAY_CARD, {"card": legal_card})
            played = await _read_until(observer_reader, protocol.CARD_PLAYED)
            assert played["seat"] == "S"
            assert played["card"] == legal_card
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_empty_card_string_rejected_with_error_not_a_crash():
    """Regression test for a bug found during QA: an empty `card` string used
    to crash the connection handler with an unhandled IndexError (`""[-1]`)
    instead of a clean ILLEGAL_CARD error. See discoveries.md DISC-009."""

    async def scenario() -> None:
        srv, port = await _start_server()
        conns: dict = {}
        try:
            conns = await _join_all(port, "emptycard01")
            observer_reader = conns["N"][0]

            await _read_until(conns["W"][0], protocol.DEAL)
            await _bid_min_and_finalize_contract(conns, observer_reader)

            play_req = await _read_until(conns["W"][0], protocol.PLAY_REQUEST)
            await _send(conns["W"][1], protocol.PLAY_CARD, {"card": ""})
            error_payload = await _read_any_until(conns["W"][0], protocol.ERROR)
            assert error_payload["code"] == protocol.ILLEGAL_CARD

            # Connection must still be usable afterward (no crash/hang).
            legal_card = play_req["legal_cards"][0]
            await _send(conns["W"][1], protocol.PLAY_CARD, {"card": legal_card})
            played = await _read_until(observer_reader, protocol.CARD_PLAYED)
            assert played["seat"] == "W"
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_malformed_json_line_rejected_without_crashing_connection():
    async def scenario() -> None:
        srv, port = await _start_server()
        conns: dict = {}
        try:
            conns = await _join_all(port, "malf01")
            reader, writer = conns["N"]
            other_reader = conns["E"][0]

            await _read_until(reader, protocol.DEAL)
            await _read_until(other_reader, protocol.DEAL)

            writer.write(b"not valid json at all\n")
            await writer.drain()
            error_payload = await _read_any_until(reader, protocol.ERROR)
            assert error_payload["code"] == protocol.MALFORMED_MESSAGE

            # Connection must still be usable afterward.
            await _send(writer, protocol.CHAT, {"text": "still alive"})
            chat_seen = await _read_until(other_reader, protocol.CHAT)
            assert chat_seen["text"] == "still alive"
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_oversized_line_is_rejected_gracefully_not_a_server_crash():
    """Regression test: a client line exceeding asyncio's default 64 KiB
    StreamReader limit used to raise an unhandled ValueError from readline(),
    crashing the connection task instead of being rejected cleanly. See
    discoveries.md DISC-009."""

    async def scenario() -> None:
        srv, port = await _start_server()
        try:
            reader, writer = await _connect(port)
            huge_name = "A" * 70_000  # exceeds the ~64 KiB readline() limit
            await _send(writer, protocol.JOIN, {"table_key": "oversz1", "player_name": huge_name})

            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if line:
                mtype, payload = protocol.decode(line)
                assert mtype == protocol.ERROR
                assert payload["code"] == protocol.MALFORMED_MESSAGE
            writer.close()

            # The server itself must still be healthy afterward.
            reader2, writer2 = await _connect(port)
            await _send(writer2, protocol.JOIN, {"table_key": "oversz2", "player_name": "Zoe"})
            mtype2, payload2 = await _recv(reader2)
            assert mtype2 == protocol.JOINED
            writer2.close()
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_trick_completion_pauses_before_next_play_request():
    """Per user request: after a trick completes, the server must wait
    `trick_pause_seconds` before letting play continue (next play_request),
    so every player has time to see the last card played."""

    async def scenario() -> None:
        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await server.handle_connection(reader, writer, 1000, trick_pause_seconds=0.3, round_pause_seconds=0)

        srv = await asyncio.start_server(_handler, HOST, 0)
        port = srv.sockets[0].getsockname()[1]
        conns: dict = {}
        try:
            conns = await _join_all(port, "pause01")
            observer_reader = conns["N"][0]

            await _read_until(conns["W"][0], protocol.DEAL)
            await _bid_min_and_finalize_contract(conns, observer_reader)

            current_actor = "W"
            for _ in range(4):
                req = await _read_until(conns[current_actor][0], protocol.PLAY_REQUEST)
                card = req["legal_cards"][0]
                await _send(conns[current_actor][1], protocol.PLAY_CARD, {"card": card})
                await _read_until(observer_reader, protocol.CARD_PLAYED)
                current_actor = ROTATION_NEXT[current_actor]

            start = asyncio.get_event_loop().time()
            trick_result = await _read_until(observer_reader, protocol.TRICK_RESULT)
            winner = trick_result["winner_seat"]
            await _read_until(conns[winner][0], protocol.PLAY_REQUEST)
            elapsed = asyncio.get_event_loop().time() - start
            assert elapsed >= 0.25, f"next play_request arrived too early ({elapsed:.3f}s < ~0.3s pause)"
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_round_completion_pauses_before_next_deal():
    """Per user request: after a round (manche) completes without ending the
    game, the server must wait `round_pause_seconds` after broadcasting
    ROUND_SCORE before dealing the next round, so every player has time to
    read the end-of-round recap shown by the client."""

    async def scenario() -> None:
        async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await server.handle_connection(reader, writer, 1000, trick_pause_seconds=0, round_pause_seconds=0.3)

        srv = await asyncio.start_server(_handler, HOST, 0)
        port = srv.sockets[0].getsockname()[1]
        conns: dict = {}
        try:
            conns = await _join_all(port, "pause02")
            observer_reader = conns["N"][0]

            await _read_until(conns["W"][0], protocol.DEAL)
            await _bid_min_and_finalize_contract(conns, observer_reader)

            current_actor = "W"
            for _ in range(8):
                for _ in range(4):
                    req = await _read_until(conns[current_actor][0], protocol.PLAY_REQUEST)
                    card = req["legal_cards"][0]
                    await _send(conns[current_actor][1], protocol.PLAY_CARD, {"card": card})
                    await _read_until(observer_reader, protocol.CARD_PLAYED)
                    current_actor = ROTATION_NEXT[current_actor]
                trick_result = await _read_until(observer_reader, protocol.TRICK_RESULT)
                current_actor = trick_result["winner_seat"]

            start = asyncio.get_event_loop().time()
            await _read_until(observer_reader, protocol.ROUND_SCORE)
            await _read_until(observer_reader, protocol.DEAL)
            elapsed = asyncio.get_event_loop().time() - start
            assert elapsed >= 0.25, f"next deal arrived too early ({elapsed:.3f}s < ~0.3s pause)"
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_rematch_after_game_over_restarts_with_fresh_scores():
    """Per user request: once GAME_OVER fires, any seated player can request a
    rematch (REMATCH); the server resets cumulative scores/round number and
    kicks off a brand-new game (NEW_GAME, then a normal deal/bid_request)
    without requiring a fresh connection/join."""

    async def scenario() -> None:
        srv, port = await _start_server(target_score=1)  # any round finishes the game
        conns: dict = {}
        try:
            conns = await _join_all(port, "rematch1")
            observer_reader = conns["N"][0]

            await _read_until(conns["W"][0], protocol.DEAL)
            await _bid_min_and_finalize_contract(conns, observer_reader)

            current_actor = "W"
            for _ in range(8):
                for _ in range(4):
                    req = await _read_until(conns[current_actor][0], protocol.PLAY_REQUEST)
                    card = req["legal_cards"][0]
                    await _send(conns[current_actor][1], protocol.PLAY_CARD, {"card": card})
                    await _read_until(observer_reader, protocol.CARD_PLAYED)
                    current_actor = ROTATION_NEXT[current_actor]
                trick_result = await _read_until(observer_reader, protocol.TRICK_RESULT)
                current_actor = trick_result["winner_seat"]

            await _read_until(observer_reader, protocol.ROUND_SCORE)
            game_over = await _read_until(observer_reader, protocol.GAME_OVER)
            assert game_over["winning_team"] in ("NS", "EW")

            # N asks for a rematch.
            await _send(conns["N"][1], protocol.REMATCH, {})

            new_game = await _read_until(observer_reader, protocol.NEW_GAME)
            assert new_game["target_score"] == 1

            deal_payload = await _read_until(conns["W"][0], protocol.DEAL)
            assert deal_payload["round_number"] == 1
            assert deal_payload["dealer_seat"] == "N"
            assert deal_payload["first_bidder_seat"] == "W"

            bid_req = await _read_until(conns["W"][0], protocol.BID_REQUEST)
            assert bid_req["legal_actions"]
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_fifth_player_join_rejected_game_in_progress():
    async def scenario() -> None:
        srv, port = await _start_server()
        conns: dict = {}
        try:
            conns = await _join_all(port, "full01")

            reader5, writer5 = await _connect(port)
            await _send(writer5, protocol.JOIN, {"table_key": "full01", "player_name": "Eve"})
            mtype, payload = await _recv(reader5)
            assert mtype == protocol.ERROR
            assert payload["code"] == protocol.GAME_IN_PROGRESS
            writer5.close()
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_duplicate_name_join_rejected_name_taken_case_insensitive():
    async def scenario() -> None:
        srv, port = await _start_server()
        conns: dict = {}
        try:
            reader1, writer1 = await _connect(port)
            await _send(writer1, protocol.JOIN, {"table_key": "dup01", "player_name": "Alice"})
            mtype1, _ = await _recv(reader1)
            assert mtype1 == protocol.JOINED
            conns["N"] = (reader1, writer1)

            reader2, writer2 = await _connect(port)
            await _send(writer2, protocol.JOIN, {"table_key": "dup01", "player_name": "alice"})
            mtype2, payload2 = await _recv(reader2)
            assert mtype2 == protocol.ERROR
            assert payload2["code"] == protocol.NAME_TAKEN
            writer2.close()
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())


def test_disconnect_and_reconnect_during_bidding_phase():
    async def scenario() -> None:
        srv, port = await _start_server()
        conns: dict = {}
        try:
            conns = await _join_all(port, "reconbid01")
            observer_reader = conns["N"][0]

            await _read_until(conns["W"][0], protocol.DEAL)
            bid_req = await _read_until(conns["W"][0], protocol.BID_REQUEST)
            action = bid_req["legal_actions"][0]
            await _send(
                conns["W"][1], protocol.BID, {"action": "bid", "trump": action["trump"], "points": action["points"]}
            )
            update = await _read_until(observer_reader, protocol.BID_UPDATE)
            next_seat = update["next_to_act"]  # "S" per rotation

            # `next_seat` receives its bid_request, then disconnects before responding.
            await _read_until(conns[next_seat][0], protocol.BID_REQUEST)
            _, dropped_writer = conns[next_seat]
            dropped_writer.close()
            try:
                await dropped_writer.wait_closed()
            except (ConnectionError, OSError):
                pass

            status = await _read_until(observer_reader, protocol.CONNECTION_STATUS)
            assert status["seat"] == next_seat
            assert status["status"] == "disconnected"

            # The auction must not silently advance past the disconnected seat's turn.
            await _assert_no_message(observer_reader)

            # A new connection re-joins mid-bidding with the same table_key + name.
            new_reader, new_writer = await _connect(port)
            await _send(new_writer, protocol.JOIN, {"table_key": "reconbid01", "player_name": NAMES_BY_SEAT[next_seat]})

            resync = await _read_until(new_reader, protocol.RESYNC)
            assert resync["seat"] == next_seat
            assert resync["phase"] == "bidding"
            assert resync["whose_turn"] == next_seat
            assert resync["current_highest_bid"]["trump"] == action["trump"]
            assert resync["current_highest_bid"]["points"] == action["points"]
            assert len(resync["bid_history"]) == 1
            assert len(resync["hand"]) == 8

            reconnected_status = await _read_until(observer_reader, protocol.CONNECTION_STATUS)
            assert reconnected_status["seat"] == next_seat
            assert reconnected_status["status"] == "reconnected"

            conns[next_seat] = (new_reader, new_writer)

            # It's still next_seat's turn: the server follows resync with a bid_request.
            bid_req2 = await _read_until(new_reader, protocol.BID_REQUEST)
            assert bid_req2["legal_actions"]

            # Finish the auction (S, E, N all pass) to confirm resumed bidding works.
            await _send(new_writer, protocol.BID, {"action": "pass"})
            update2 = await _read_until(observer_reader, protocol.BID_UPDATE)
            seat_ptr = update2["next_to_act"]  # "E"

            await _read_until(conns[seat_ptr][0], protocol.BID_REQUEST)
            await _send(conns[seat_ptr][1], protocol.BID, {"action": "pass"})
            update3 = await _read_until(observer_reader, protocol.BID_UPDATE)
            seat_ptr = update3["next_to_act"]  # "N"

            await _read_until(conns[seat_ptr][0], protocol.BID_REQUEST)
            await _send(conns[seat_ptr][1], protocol.BID, {"action": "pass"})
            bidding_result = await _read_until(observer_reader, protocol.BIDDING_RESULT)
            assert bidding_result["outcome"] == "contract"
            assert bidding_result["seat"] == "W"
        finally:
            for _reader, writer in conns.values():
                writer.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(scenario())
