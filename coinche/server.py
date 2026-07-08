"""Coinche TCP server: asyncio connection handling, join/reconnect, and dispatch.

Run with: python -m coinche.server [--host HOST] [--port PORT] [--target-score N]
"""

from __future__ import annotations

import argparse
import asyncio
import re

from coinche import protocol, rules
from coinche.cards import Card, Seat
from coinche.game import IllegalBidError, IllegalCardError, NotYourTurnError
from coinche.table import (
    GameInProgressError,
    NameTakenError,
    Table,
    TableFullError,
    get_or_create_table,
)

TABLE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{4,12}$")


def _seat_to_str(seat: Seat) -> str:
    return seat.value


def _card_to_wire(card: Card) -> str:
    return str(card)


def _wire_to_card(card_str: str) -> Card:
    return Card(rank=str(card_str)[:-1], suit=str(card_str)[-1])


def _trick_to_wire(trick: list[tuple[Seat, Card]]) -> list[dict]:
    return [{"seat": _seat_to_str(seat), "card": _card_to_wire(card)} for seat, card in trick]


def _players_summary(table: Table) -> list[dict]:
    return [
        {"seat": _seat_to_str(seat), "name": session.name}
        for seat, session in table.seats.items()
        if session is not None
    ]


def _bid_to_wire(bid: dict | None) -> dict | None:
    """Convert a `current_highest_bid` dict's `seat` (a Seat enum) to its wire string."""
    if bid is None:
        return None
    return {**bid, "seat": _seat_to_str(bid["seat"])}


def _snapshot_to_wire(snapshot: dict, table_key: str) -> dict:
    current_highest_bid = _bid_to_wire(snapshot["current_highest_bid"])
    bid_history = [{**entry, "seat": _seat_to_str(entry["seat"])} for entry in snapshot["bid_history"]]
    return {
        "table_key": table_key,
        "seat": _seat_to_str(snapshot["seat"]),
        "hand": [_card_to_wire(c) for c in snapshot["hand"]],
        "phase": snapshot["phase"],
        "current_highest_bid": current_highest_bid,
        "bid_history": bid_history,
        "current_trick": _trick_to_wire(snapshot["current_trick"]),
        "trump": snapshot["trump"],
        "whose_turn": _seat_to_str(snapshot["whose_turn"]),
        "cumulative_scores": snapshot["cumulative_scores"],
        "round_number": snapshot["round_number"],
        "dealer_seat": _seat_to_str(snapshot["dealer_seat"]),
    }


async def _send_error(writer: asyncio.StreamWriter, code: str, message: str) -> None:
    try:
        writer.write(protocol.encode(protocol.ERROR, {"code": code, "message": message}))
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _send_bid_request(table: Table, seat: Seat) -> None:
    assert table.game is not None
    options = table.game.bid_options_for(seat)
    await table.send_to(
        seat,
        protocol.BID_REQUEST,
        {
            "current_highest_bid": _bid_to_wire(options["current_highest_bid"]),
            "legal_actions": options["legal_actions"],
            "can_coinche": options["can_coinche"],
            "can_surcoinche": options["can_surcoinche"],
        },
    )


async def _send_play_request(table: Table, seat: Seat) -> None:
    assert table.game is not None
    options = table.game.play_options_for(seat)
    await table.send_to(
        seat,
        protocol.PLAY_REQUEST,
        {
            "legal_cards": [_card_to_wire(c) for c in options["legal_cards"]],
            "current_trick": _trick_to_wire(options["current_trick"]),
            "trump": options["trump"],
        },
    )


async def _broadcast_deal(table: Table) -> None:
    game = table.game
    assert game is not None
    for seat, session in table.seats.items():
        if session is None:
            continue
        await table.send_to(
            seat,
            protocol.DEAL,
            {
                "hand": [_card_to_wire(c) for c in game.get_hand(seat)],
                "dealer_seat": _seat_to_str(game.dealer),
                "first_bidder_seat": _seat_to_str(game.next_to_act),
                "round_number": game.round_number,
            },
        )


async def _handle_bid_result(table: Table, seat: Seat, result: dict) -> None:
    game = table.game
    assert game is not None
    outcome = result["outcome"]

    if outcome == "continue":
        await table.broadcast(
            protocol.BID_UPDATE,
            {
                "seat": _seat_to_str(seat),
                "action": result["action"],
                "trump": result.get("trump"),
                "points": result.get("points"),
                "next_to_act": _seat_to_str(result["next_to_act"]),
            },
        )
        await _send_bid_request(table, result["next_to_act"])

    elif outcome == "redeal":
        await table.broadcast(
            protocol.BIDDING_RESULT,
            {"outcome": "redeal", "dealer_seat": _seat_to_str(result["dealer_seat"])},
        )
        await _broadcast_deal(table)
        await _send_bid_request(table, game.next_to_act)

    elif outcome == "contract":
        await table.broadcast(
            protocol.BIDDING_RESULT,
            {
                "outcome": "contract",
                "attacking_team": result["attacking_team"],
                "seat": _seat_to_str(result["seat"]),
                "trump": result["trump"],
                "points": result["points"],
                "coinche_level": result["coinche_level"],
                "first_leader": _seat_to_str(result["first_leader"]),
            },
        )
        await _send_play_request(table, result["first_leader"])


async def _handle_play_result(table: Table, result: dict) -> None:
    game = table.game
    assert game is not None

    next_actor = result.get("next_to_act")
    await table.broadcast(
        protocol.CARD_PLAYED,
        {
            "seat": _seat_to_str(result["seat"]),
            "card": _card_to_wire(result["card"]),
            "current_trick": _trick_to_wire(result["current_trick"]),
            "next_to_act": _seat_to_str(next_actor) if next_actor is not None else None,
        },
    )

    if not result["trick_complete"]:
        await _send_play_request(table, result["next_to_act"])
        return

    await table.broadcast(
        protocol.TRICK_RESULT,
        {
            "winner_seat": _seat_to_str(result["winner_seat"]),
            "trick": _trick_to_wire(result["completed_trick"]),
            "points_won": result["points_won"],
            "tricks_played": result["tricks_played"],
            "tricks_remaining": result["tricks_remaining"],
        },
    )

    if not result["round_complete"]:
        await _send_play_request(table, result["next_to_act"])
        return

    next_dealer_seat = result["next_dealer_seat"]
    await table.broadcast(
        protocol.ROUND_SCORE,
        {
            "team_NS": result["round_score"]["NS"],
            "team_EW": result["round_score"]["EW"],
            "cumulative": result["cumulative_scores"],
            "next_dealer_seat": _seat_to_str(next_dealer_seat) if next_dealer_seat is not None else None,
        },
    )

    if result["game_over"]:
        await table.broadcast(
            protocol.GAME_OVER,
            {"final_scores": result["cumulative_scores"], "winning_team": result["winning_team"]},
        )
    else:
        await _broadcast_deal(table)
        await _send_bid_request(table, game.next_to_act)


async def _dispatch(table: Table, seat: Seat, msg_type: str, payload: dict) -> None:
    game = table.game
    if game is None:
        return  # ignore game-phase messages while still in the lobby

    if msg_type == protocol.BID:
        try:
            result = game.submit_bid(seat, payload["action"], trump=payload.get("trump"), points=payload.get("points"))
        except NotYourTurnError:
            await table.send_to(seat, protocol.ERROR, {"code": protocol.NOT_YOUR_TURN, "message": "Not your turn"})
            return
        except IllegalBidError as exc:
            await table.send_to(seat, protocol.ERROR, {"code": protocol.ILLEGAL_BID, "message": str(exc)})
            return
        await _handle_bid_result(table, seat, result)

    elif msg_type == protocol.PLAY_CARD:
        card_str = payload["card"]
        if not isinstance(card_str, str) or len(card_str) < 2:
            await table.send_to(
                seat, protocol.ERROR, {"code": protocol.ILLEGAL_CARD, "message": f"Malformed card: {card_str!r}"}
            )
            return
        card = _wire_to_card(card_str)
        try:
            result = game.submit_card(seat, card)
        except NotYourTurnError:
            await table.send_to(seat, protocol.ERROR, {"code": protocol.NOT_YOUR_TURN, "message": "Not your turn"})
            return
        except IllegalCardError as exc:
            await table.send_to(seat, protocol.ERROR, {"code": protocol.ILLEGAL_CARD, "message": str(exc)})
            return
        await _handle_play_result(table, result)

    elif msg_type == protocol.CHAT:
        await table.broadcast(protocol.CHAT, {"seat": _seat_to_str(seat), "text": payload["text"]})


async def _resolve_join(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target_score: int
) -> tuple[Table, Seat] | None:
    try:
        line = await reader.readline()
    except ValueError:
        # Line exceeded the StreamReader's length limit (oversized/malformed input).
        await _send_error(writer, protocol.MALFORMED_MESSAGE, "Message too large")
        return None
    if not line:
        return None

    try:
        msg_type, payload = protocol.decode(line)
    except protocol.ProtocolError:
        await _send_error(writer, protocol.MALFORMED_MESSAGE, "Expected a join message")
        return None

    if msg_type != protocol.JOIN:
        await _send_error(writer, protocol.MALFORMED_MESSAGE, "First message must be 'join'")
        return None

    table_key = str(payload["table_key"]).lower()
    player_name = str(payload["player_name"]).strip()
    preferred_partner = str(payload["preferred_partner"]).strip() if payload.get("preferred_partner") else None

    if not TABLE_KEY_PATTERN.match(table_key):
        await _send_error(writer, protocol.MALFORMED_MESSAGE, "table_key must be 4-12 alphanumeric characters")
        return None
    if not player_name:
        await _send_error(writer, protocol.MALFORMED_MESSAGE, "player_name must not be empty")
        return None

    table = get_or_create_table(table_key, target_score=target_score)

    async with table.lock:
        reconnect_seat = table.find_disconnected_seat(player_name) if table.game is not None else None

        if reconnect_seat is not None:
            seat = reconnect_seat
            snapshot = table.reconnect(seat, writer)
            await table.send_to(seat, protocol.RESYNC, _snapshot_to_wire(snapshot, table_key))
            await table.broadcast(
                protocol.CONNECTION_STATUS,
                {"seat": _seat_to_str(seat), "name": player_name, "status": "reconnected"},
                exclude=seat,
            )
            # resync intentionally omits legal_actions/legal_cards; if it's this
            # seat's turn, follow up with a normal request so it can resume acting.
            assert table.game is not None
            if table.game.next_to_act == seat:
                if table.game.phase == "bidding":
                    await _send_bid_request(table, seat)
                elif table.game.phase == "trick_play":
                    await _send_play_request(table, seat)
            return table, seat

        try:
            seat = table.add_player(player_name, writer, preferred_partner=preferred_partner)
        except NameTakenError:
            await _send_error(writer, protocol.NAME_TAKEN, f"Name already taken: {player_name}")
            return None
        except GameInProgressError:
            await _send_error(writer, protocol.GAME_IN_PROGRESS, "Game already in progress")
            return None
        except TableFullError:
            await _send_error(writer, protocol.TABLE_FULL, "Table is full")
            return None

        players = _players_summary(table)
        await table.send_to(
            seat,
            protocol.JOINED,
            {"table_key": table_key, "seat": _seat_to_str(seat), "players": players, "target_score": table.target_score},
        )
        await table.broadcast(
            protocol.LOBBY_UPDATE,
            {"players": players, "seats_filled": len(players), "waiting_for": 4 - len(players)},
            exclude=seat,
        )
        if table.game is not None:
            await _broadcast_deal(table)
            await _send_bid_request(table, table.game.next_to_act)

        return table, seat


async def handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target_score: int
) -> None:
    table: Table | None = None
    seat: Seat | None = None
    try:
        joined = await _resolve_join(reader, writer, target_score)
        if joined is None:
            return
        table, seat = joined

        while True:
            try:
                line = await reader.readline()
            except ValueError:
                # Line exceeded the StreamReader's length limit (oversized/malformed
                # input) -- reject and drop the connection rather than crash the task.
                await _send_error(writer, protocol.MALFORMED_MESSAGE, "Message too large")
                break
            if not line:
                break
            try:
                msg_type, payload = protocol.decode(line)
            except protocol.ProtocolError as exc:
                await _send_error(writer, protocol.MALFORMED_MESSAGE, str(exc))
                continue

            async with table.lock:
                await _dispatch(table, seat, msg_type, payload)

    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        if table is not None and seat is not None:
            async with table.lock:
                if table.game is None:
                    table.remove_player(seat)
                    players = _players_summary(table)
                    await table.broadcast(
                        protocol.LOBBY_UPDATE,
                        {"players": players, "seats_filled": len(players), "waiting_for": 4 - len(players)},
                    )
                else:
                    name = table.mark_disconnected(seat)
                    await table.broadcast(
                        protocol.CONNECTION_STATUS,
                        {"seat": _seat_to_str(seat), "name": name, "status": "disconnected"},
                    )
        try:
            writer.close()
        except Exception:
            pass


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coinche network game server")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument(
        "--target-score",
        type=int,
        default=rules.DEFAULT_TARGET_SCORE,
        help=f"Cumulative score to win the game (default: {rules.DEFAULT_TARGET_SCORE})",
    )
    return parser


async def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(reader, writer, args.target_score)

    server = await asyncio.start_server(_handler, args.host, args.port)
    bound = server.sockets[0].getsockname() if server.sockets else (args.host, args.port)
    print(f"Coinche server listening on {bound[0]}:{bound[1]} (target score {args.target_score})")
    async with server:
        await server.serve_forever()


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
