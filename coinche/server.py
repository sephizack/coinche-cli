"""Coinche TCP server: asyncio connection handling, join/reconnect, and dispatch.

Run with: python -m coinche.server [--host HOST] [--port PORT] [--target-score N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from coinche import __version__, protocol, rules
from coinche.bot import DEFAULT_BOT_TYPE, choose_bid, choose_card, configure_samples, is_supported_bot_type
from coinche.cards import Card, Seat
from coinche.game import PARTNER_OF, TEAM_OF, Game, IllegalBidError, IllegalCardError, NotYourTurnError
from coinche.table import (
    LOBBY_SUBSCRIBERS,
    TABLES,
    GameInProgressError,
    NameTakenError,
    Table,
    TableFullError,
    cancel_background_tasks,
    cancel_turn_timer,
    get_or_create_table,
    notify_lobby_subscribers,
    remove_table,
    tables_listing,
)
from coinche.timeouts import (
    DEFAULT_BOT_THINK_SECONDS,
    DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS,
    DEFAULT_ROUND_PAUSE_SECONDS,
    DEFAULT_TRICK_PAUSE_SECONDS,
    DEFAULT_TURN_TIMEOUT_SECONDS,
    validate_timeout_order,
)

TABLE_KEY_PATTERN = re.compile(rf"^[A-Za-z0-9]{{{protocol.TABLE_KEY_MIN_LENGTH},{protocol.TABLE_KEY_MAX_LENGTH}}}$")

# Dedicated "game log" logger (per user request): records who bid/played what
# and which team took each trick/round/game, so results can be double-checked
# after the fact. Configured (handler/level) in `main()`; kept separate from
# ad-hoc `print()` startup messages.
logger = logging.getLogger("coinche.server")

DISCORD_NOTIF_CHANNEL_POST_URL = os.environ.get("DISCORD_NOTIF_CHANNEL_POST_URL")
COINCHE_PUBLIC_URL = os.environ.get("COINCHE_PUBLIC_URL", "").rstrip("/")
_DISCORD_WEBHOOK_TIMEOUT_SECONDS = 5.0
_DISCORD_TABLE_CREATED_COLOR = 0x57F287
_DISCORD_TABLE_CLOSED_COLOR = 0x95A5A6


def _table_join_url(table_key: str, preferred_seat: Seat) -> str | None:
    """Return a public meta-client deep link that requests a specific seat."""
    if not COINCHE_PUBLIC_URL:
        return None
    query = urllib.parse.urlencode({"table": table_key, "seat": preferred_seat.value})
    return f"{COINCHE_PUBLIC_URL}/?{query}"


def _table_spectate_url(table_key: str) -> str | None:
    """Return a public meta-client deep link that joins as a spectator."""
    if not COINCHE_PUBLIC_URL:
        return None
    query = urllib.parse.urlencode({"table": table_key, "spectate": "true"})
    return f"{COINCHE_PUBLIC_URL}/?{query}"


def _webhook_url_with_components(webhook_url: str) -> str:
    """Enable non-interactive components on a standard Discord webhook and wait for response."""
    parsed = urllib.parse.urlsplit(webhook_url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key not in ("with_components", "wait")
    ]
    query.append(("with_components", "true"))
    query.append(("wait", "true"))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def _webhook_message_url(webhook_url: str, message_id: str) -> str:
    """Return the Discord webhook endpoint URL for updating a specific message."""
    parsed = urllib.parse.urlsplit(webhook_url)
    base_path = parsed.path.rstrip("/")
    new_path = f"{base_path}/messages/{message_id}"
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key not in ("with_components", "wait")
    ]
    query.append(("with_components", "true"))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, new_path, urllib.parse.urlencode(query), ""))


def _post_discord_table_created(webhook_url: str, table_key: str, player_name: str, creator_seat: Seat) -> str | None:
    """Post a best-effort Discord notification without exposing the webhook URL."""
    description = f"La table **{table_key}** vient d'etre creee par **{player_name}**."
    teammate_url = _table_join_url(table_key, PARTNER_OF[creator_seat])
    opponent_url = _table_join_url(table_key, creator_seat.next())
    spectator_url = _table_spectate_url(table_key)
    body = {
        "username": "Coinche CLI",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "Nouvelle table !",
                "color": _DISCORD_TABLE_CREATED_COLOR,
                "description": description,
            }
        ],
    }
    if teammate_url and opponent_url and spectator_url:
        body["components"] = [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 5,
                        "label": f"Avec {player_name}",
                        "emoji": {"name": "🤝"},
                        "url": teammate_url,
                    },
                    {
                        "type": 2,
                        "style": 5,
                        "label": f"Contre {player_name}",
                        "emoji": {"name": "⚔️"},
                        "url": opponent_url,
                    },
                    {
                        "type": 2,
                        "style": 5,
                        "label": "Regarder la partie",
                        "emoji": {"name": "👁️"},
                        "url": spectator_url,
                    },
                ],
            }
        ]
    request = urllib.request.Request(
        _webhook_url_with_components(webhook_url),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "coinche-cli"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_DISCORD_WEBHOOK_TIMEOUT_SECONDS) as response:
            if response.status in (200, 204):
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    msg_id = data.get("id")
                    if isinstance(msg_id, str):
                        return msg_id
                return None
            logger.warning("[%s] Notification Discord rejetee (HTTP %s)", table_key, response.status)
    except (OSError, ValueError) as exc:
        logger.warning("[%s] Echec de la notification Discord: %s", table_key, exc)
    return None


def _patch_discord_table_closed(webhook_url: str, table_key: str, message_id: str, creator_name: str | None) -> None:
    """Update a Discord notification to indicate that the table is closed and remove components."""
    description = f"La table **{table_key}** est fermée."
    if creator_name:
        description = f"La table **{table_key}** créée par **{creator_name}** est fermée."
    body = {
        "embeds": [
            {
                "title": f"🔒 [Fermée] Table {table_key}",
                "color": _DISCORD_TABLE_CLOSED_COLOR,
                "description": description,
            }
        ],
        "components": [],
    }
    request = urllib.request.Request(
        _webhook_message_url(webhook_url, message_id),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "coinche-cli"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request, timeout=_DISCORD_WEBHOOK_TIMEOUT_SECONDS) as response:
            if response.status not in (200, 204):
                logger.warning("[%s] Mise a jour Discord rejetee (HTTP %s)", table_key, response.status)
    except (OSError, ValueError) as exc:
        logger.warning("[%s] Echec de la mise a jour Discord: %s", table_key, exc)


async def _notify_discord_table_created(webhook_url: str, table: Table, player_name: str, creator_seat: Seat) -> None:
    """Run the blocking webhook call away from the game server's event loop."""
    try:
        table.discord_creator_name = player_name
        msg_id = await asyncio.to_thread(
            _post_discord_table_created, webhook_url, table.table_key, player_name, creator_seat
        )
        if msg_id:
            table.discord_message_id = msg_id
            if table.is_closed or table.table_key not in TABLES:
                await _notify_discord_table_closed(webhook_url, table.table_key, msg_id, table.discord_creator_name)
    except Exception:  # noqa: BLE001 - a webhook failure must never affect a game session
        logger.exception("[%s] Echec inattendu de la notification Discord", table.table_key)


async def _notify_discord_table_closed(
    webhook_url: str, table_key: str, message_id: str, creator_name: str | None
) -> None:
    """Run the blocking webhook patch call away from the game server's event loop."""
    try:
        await asyncio.to_thread(_patch_discord_table_closed, webhook_url, table_key, message_id, creator_name)
    except Exception:  # noqa: BLE001 - a webhook failure must never affect a game session
        logger.exception("[%s] Echec inattendu de la mise a jour Discord", table_key)


async def _remove_table_and_notify(table: Table) -> None:
    """Drop a table and update its Discord notification if one exists."""
    await remove_table(table.table_key)
    if DISCORD_NOTIF_CHANNEL_POST_URL and table.discord_message_id:
        asyncio.create_task(
            _notify_discord_table_closed(
                DISCORD_NOTIF_CHANNEL_POST_URL,
                table.table_key,
                table.discord_message_id,
                table.discord_creator_name,
            )
        )


def _seat_to_str(seat: Seat) -> str:
    return seat.value


def _player_label(table: Table, seat: Seat) -> str:
    """Human-readable "Name (seat/TEAM)" label for game-log lines."""
    session = table.seats.get(seat)
    name = session.name if session is not None else "?"
    if session is not None and session.is_bot:
        bot_type = session.bot_type or table.bot_type
        return f"{name} ({_seat_to_str(seat)}/{TEAM_OF[seat]}) (bot: {bot_type})"
    return f"{name} ({_seat_to_str(seat)}/{TEAM_OF[seat]})"


def _round_recap_chat_text(round_score: dict[str, dict]) -> str:
    """Build the system chat recap displayed once a round has been scored."""
    contract_result = round_score["NS"]["contract_result"]
    contract_status = "Contrat chuté ❌" if contract_result in {"failed", "capot_failed"} else "Contrat réussi ✅"

    def card_score(team: str) -> str:
        belote_bonus = round_score[team]["belote_bonus"]
        belote = f" (+{belote_bonus})" if belote_bonus else ""
        return f"{round_score[team]['card_points']}{belote}"

    return f"Fin de manche: {card_score('NS')} - {card_score('EW')}. {contract_status}."


def _positive_int(value: str) -> int:
    """Parse a strictly positive command-line integer."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be strictly positive")
    return parsed


def _card_to_wire(card: Card) -> str:
    return str(card)


def _wire_to_card(card_str: str) -> Card:
    return Card(rank=str(card_str)[:-1], suit=str(card_str)[-1])


def _trick_to_wire(trick: list[tuple[Seat, Card]]) -> list[dict]:
    return [{"seat": _seat_to_str(seat), "card": _card_to_wire(card)} for seat, card in trick]


def _players_summary(table: Table) -> list[dict]:
    return [
        {
            "seat": _seat_to_str(seat),
            "name": session.name,
            "team_name": session.team_name,
            "is_bot": session.is_bot,
            "bot_type": session.bot_type,
        }
        for seat, session in table.seats.items()
        if session is not None
    ]


def _table_options_to_wire(table: Table) -> dict:
    """Return the player-visible, immutable configuration of a table."""
    return {
        "target_score": table.target_score,
        "coinche_blocks_bidding": table.coinche_blocks_bidding,
        "score_mode": table.score_mode,
        "bot_type": table.bot_type,
        "trick_pause_seconds": table.trick_pause_seconds,
        "round_pause_seconds": table.round_pause_seconds,
        "bot_think_seconds": table.bot_think_seconds,
        "turn_timeout_seconds": table.turn_timeout_seconds,
    }


async def _broadcast_spectator_count(table: Table) -> None:
    """Tell every table participant how many spectators are currently watching."""
    await table.broadcast(protocol.SPECTATOR_COUNT, {"count": len(table.spectators)})


def _resolve_bot_seat(
    table: Table,
    bot_seats: list[Seat],
    preferred_seat: Seat | None,
    team_name: str | None,
) -> Seat:
    """Pick which bot chair a human replacing a bot should take.

    A requested `preferred_seat` wins outright when it's actually a bot seat
    (the web lobby lets you click a specific bot). Otherwise, if a `team_name`
    matches a seated human, prefer that human's partner seat when it's a bot so
    teammates end up on the same team. Falling back to the first bot seat in
    table order keeps the choice deterministic. `bot_seats` must be non-empty.
    """
    if preferred_seat is not None and preferred_seat in bot_seats:
        return preferred_seat

    normalized_team = team_name.strip().lower() if team_name else None
    if normalized_team:
        for seat, session in table.seats.items():
            if (
                session is not None
                and not session.is_bot
                and session.team_name is not None
                and session.team_name.strip().lower() == normalized_team
            ):
                partner_seat = PARTNER_OF[seat]
                if partner_seat in bot_seats:
                    return partner_seat
                break

    return bot_seats[0]


def _bid_to_wire(bid: dict | None) -> dict | None:
    """Convert a `current_highest_bid` dict's `seat` (a Seat enum) to its wire string."""
    if bid is None:
        return None
    return {**bid, "seat": _seat_to_str(bid["seat"])}


def _snapshot_to_wire(snapshot: dict, table_key: str, table: Table) -> dict:
    current_highest_bid = _bid_to_wire(snapshot["current_highest_bid"])
    bid_history = [{**entry, "seat": _seat_to_str(entry["seat"])} for entry in snapshot["bid_history"]]
    contract = snapshot.get("contract")
    return {
        "table_key": table_key,
        "table_options": _table_options_to_wire(table),
        "spectator_count": len(table.spectators),
        "seat": _seat_to_str(snapshot["seat"]),
        "players": _players_summary(table),
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
        "contract": ({**contract, "seat": _seat_to_str(contract["seat"])} if contract is not None else None),
        "server_version": __version__,
    }


def _public_snapshot_to_wire(snapshot: dict, table_key: str, table: Table, spectator_name: str) -> dict:
    """Wire form of `Game.public_snapshot` for the SPECTATING message: a seatless,
    hand-free view a spectator can render immediately on join (mid-bid or mid-play).

    Mirrors `_snapshot_to_wire`'s enum->string conversions but carries no `hand`
    (a spectator never sees cards) and adds `spectator_name` so the client knows
    it is watching, not seated, and can label its own chat lines."""
    current_highest_bid = _bid_to_wire(snapshot["current_highest_bid"])
    bid_history = [{**entry, "seat": _seat_to_str(entry["seat"])} for entry in snapshot["bid_history"]]
    contract = snapshot.get("contract")
    return {
        "table_key": table_key,
        "table_options": _table_options_to_wire(table),
        "spectator_count": len(table.spectators),
        "spectator_name": spectator_name,
        "players": _players_summary(table),
        "phase": snapshot["phase"],
        "current_highest_bid": current_highest_bid,
        "bid_history": bid_history,
        "current_trick": _trick_to_wire(snapshot["current_trick"]),
        "trump": snapshot["trump"],
        "whose_turn": _seat_to_str(snapshot["whose_turn"]) if snapshot["whose_turn"] is not None else None,
        "cumulative_scores": snapshot["cumulative_scores"],
        "round_number": snapshot["round_number"],
        "dealer_seat": _seat_to_str(snapshot["dealer_seat"]),
        "contract": ({**contract, "seat": _seat_to_str(contract["seat"])} if contract is not None else None),
        "target_score": table.target_score,
        "server_version": __version__,
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
            "turn_timeout_seconds": _turn_time_remaining(table, seat),
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
            "turn_timeout_seconds": _turn_time_remaining(table, seat),
        },
    )


def _turn_time_remaining(table: Table, seat: Seat) -> float:
    if table.turn_timer_seat != seat or table.turn_deadline is None:
        return table.turn_timeout_seconds
    return max(0.0, table.turn_deadline - asyncio.get_running_loop().time())


async def _ensure_turn_timer(table: Table, seat: Seat) -> float | None:
    """Keep one deadline for the active human seat; bots never receive one."""
    session = table.seats.get(seat)
    if session is None or session.is_bot:
        return None
    loop = asyncio.get_running_loop()
    if (
        table.turn_timer_task is not None
        and not table.turn_timer_task.done()
        and table.turn_timer_seat == seat
        and table.turn_deadline is not None
        and table.turn_deadline > loop.time()
    ):
        return table.turn_deadline - loop.time()

    cancel_turn_timer(table)
    game = table.game
    assert game is not None
    deadline = loop.time() + table.turn_timeout_seconds
    table.turn_timer_seat = seat
    table.turn_deadline = deadline
    table.turn_timer_task = asyncio.create_task(_handle_turn_timeout(table, game, seat, deadline))
    return table.turn_timeout_seconds


async def _request_turn(table: Table, seat: Seat) -> None:
    """Arm the active human's deadline, then send its phase-specific request."""
    if await _ensure_turn_timer(table, seat) is None:
        _schedule_bot_turns(table)
        return
    assert table.game is not None
    if table.game.phase == "bidding":
        await _send_bid_request(table, seat)
    else:
        await _send_play_request(table, seat)


async def _kick_timed_out_player(table: Table, seat: Seat, writer: asyncio.StreamWriter | None, name: str) -> None:
    """Replace the expired human under the table lock and close only their socket."""
    bot_name = table.replace_with_bot(seat)
    logger.info("[%s] TIMEOUT %s (%s) -> bot %s", table.table_key, name, _seat_to_str(seat), bot_name)
    await table.broadcast(
        protocol.CONNECTION_STATUS,
        {"seat": _seat_to_str(seat), "name": name, "bot_name": bot_name, "status": "replaced_by_bot"},
        exclude=seat,
    )
    await notify_lobby_subscribers()
    await table.send_to_writer(
        writer,
        protocol.TURN_TIMEOUT,
        {"message": "Vous n'avez pas joué à temps : un bot reprend votre place."},
    )
    if writer is not None:
        writer.close()
    if not table.has_humans():
        await _remove_table_and_notify(table)
        logger.info("[%s] table abandonnee (plus aucun joueur humain) -> supprimee", table.table_key)
        await notify_lobby_subscribers()
        return
    _schedule_bot_turns(table)


async def _handle_turn_timeout(table: Table, game: Game, seat: Seat, deadline: float) -> None:
    """Wait for one deadline and replace its still-idle human seat."""
    task = asyncio.current_task()
    try:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(remaining)
        async with table.lock:
            session = table.seats.get(seat)
            if (
                table.game is not game
                or game.game_over
                or game.next_to_act != seat
                or table.turn_timer_task is not task
                or table.turn_timer_seat != seat
                or table.turn_deadline != deadline
                or session is None
                or session.is_bot
            ):
                return
            writer = session.writer
            name = session.name
            table.turn_timer_task = None
            table.turn_timer_seat = None
            table.turn_deadline = None
            await _kick_timed_out_player(table, seat, writer, name)
    except asyncio.CancelledError:
        raise
    finally:
        if table.turn_timer_task is task:
            table.turn_timer_task = None
            table.turn_timer_seat = None
            table.turn_deadline = None


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
    # Spectators get the same new-round signal (dealer, first bidder, round
    # number) so they reset the board and start following the auction, but with
    # NO hand -- a spectator is never dealt cards (BR-U1-6/NFR4).
    if table.spectators:
        deal_data = protocol.encode(
            protocol.DEAL,
            {
                "hand": [],
                "dealer_seat": _seat_to_str(game.dealer),
                "first_bidder_seat": _seat_to_str(game.next_to_act),
                "round_number": game.round_number,
            },
        )
        await table._broadcast_to_spectators(deal_data)


async def _handle_bid_result(table: Table, seat: Seat, result: dict) -> None:
    game = table.game
    assert game is not None
    outcome = result["outcome"]

    if outcome == "continue":
        action = result["action"]
        if action == "bid":
            logger.info(
                "[%s] R%d ANNONCE %s -> %s %s",
                table.table_key,
                game.round_number,
                _player_label(table, seat),
                result.get("points"),
                result.get("trump"),
            )
        else:
            logger.info(
                "[%s] R%d %s -> %s",
                table.table_key,
                game.round_number,
                _player_label(table, seat),
                action.upper(),
            )
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
        await _request_turn(table, result["next_to_act"])

    elif outcome == "redeal":
        logger.info(
            "[%s] R%d REDONNE (tout le monde a passé), nouveau donneur %s",
            table.table_key,
            game.round_number,
            _player_label(table, result["dealer_seat"]),
        )
        await _announce_bot_starting_hands(table, result["completed_round_hands"])
        await table.broadcast(
            protocol.BIDDING_RESULT,
            {"outcome": "redeal", "dealer_seat": _seat_to_str(result["dealer_seat"])},
        )
        await _broadcast_deal(table)
        await _request_turn(table, game.next_to_act)

    elif outcome == "contract":
        logger.info(
            "[%s] R%d CONTRAT %s %s par %s (equipe %s) coinche_level=%d",
            table.table_key,
            game.round_number,
            result["points"],
            result["trump"],
            _player_label(table, result["seat"]),
            result["attacking_team"],
            result["coinche_level"],
        )
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
                "final_bid_action": result.get("final_bid_action"),
                "final_bid_seat": (
                    _seat_to_str(result["final_bid_seat"]) if result.get("final_bid_seat") is not None else None
                ),
            },
        )
        await _request_turn(table, result["first_leader"])


async def _handle_play_result(table: Table, result: dict) -> None:
    game = table.game
    assert game is not None

    belote = result.get("belote_announcement")
    logger.info(
        "[%s] R%d JOUE %s -> %s%s",
        table.table_key,
        game.round_number,
        _player_label(table, result["seat"]),
        _card_to_wire(result["card"]),
        f" ({belote} !)" if belote else "",
    )

    next_actor = result.get("next_to_act")
    # When this card completes the trick, `result["current_trick"]` is
    # already `[]` (game.py resets it as soon as the trick is resolved) --
    # broadcast `completed_trick` (the full 4 cards) instead so the client
    # still shows all four cards on the table during the post-trick pause,
    # rather than clearing them the instant the 4th card lands.
    trick_for_broadcast = result["completed_trick"] if result["trick_complete"] else result["current_trick"]
    await table.broadcast(
        protocol.CARD_PLAYED,
        {
            "seat": _seat_to_str(result["seat"]),
            "card": _card_to_wire(result["card"]),
            "current_trick": _trick_to_wire(trick_for_broadcast),
            "next_to_act": _seat_to_str(next_actor) if next_actor is not None else None,
            "belote_announcement": belote,
        },
    )

    if not result["trick_complete"]:
        await _request_turn(table, result["next_to_act"])
        return

    logger.info(
        "[%s] R%d PLI #%d gagne par %s (%d pts, %d restants)",
        table.table_key,
        game.round_number,
        result["tricks_played"],
        _player_label(table, result["winner_seat"]),
        result["points_won"],
        result["tricks_remaining"],
    )
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

    game = table.game
    assert game is not None
    table.trick_pause_task = asyncio.create_task(_finish_trick_pause(table, game, result))


async def _finish_trick_pause(table: Table, game: Game, result: dict) -> None:
    """Complete a trick's visual pause without retaining the table lock."""
    try:
        await asyncio.sleep(table.trick_pause_seconds)
        async with table.lock:
            # The table can be abandoned while the pause is visible.
            if table.game is not game or not table.has_humans():
                return
            await _continue_after_trick_pause(table, result)
    finally:
        if table.trick_pause_task is asyncio.current_task():
            table.trick_pause_task = None
        if table.round_pause_task is None and table.game is game and table.has_connected_humans():
            _schedule_bot_turns(table)


async def _continue_after_trick_pause(table: Table, result: dict) -> None:
    """Advance after a completed-trick pause. Caller must hold `table.lock`."""

    # Tell every player the trick is over now, not just whoever acts next
    # (per user request): `_send_play_request` below only targets the single
    # seat leading the next trick, so without this broadcast the other three
    # players would keep staring at the finished trick's four cards -- and
    # their "Dernier pli" corner would stay stale -- until their own next
    # turn, which can be several tricks later. Sending this to everyone lets
    # all clients clear the table / promote `last_trick` in lockstep.
    await table.broadcast(protocol.TRICK_CLEARED, {})

    if not result["round_complete"]:
        await _request_turn(table, result["next_to_act"])
        return

    await _handle_round_completion(table, result)


async def _handle_round_completion(table: Table, result: dict) -> None:
    """Publish a completed round and start its optional visual pause.

    Caller must hold `table.lock`. The delay before the next deal runs in its
    own task so CHAT and LEAVE messages remain responsive.
    """
    game = table.game
    assert game is not None
    next_dealer_seat = result["next_dealer_seat"]
    logger.info(
        "[%s] R%d FIN DE MANCHE score_manche NS=%d EW=%d cumul NS=%d EW=%d",
        table.table_key,
        game.round_number,
        result["round_score"]["NS"]["total"],
        result["round_score"]["EW"]["total"],
        result["cumulative_scores"]["NS"],
        result["cumulative_scores"]["EW"],
    )
    await table.broadcast(
        protocol.ROUND_SCORE,
        {
            "team_NS": result["round_score"]["NS"],
            "team_EW": result["round_score"]["EW"],
            "cumulative": result["cumulative_scores"],
            "next_dealer_seat": _seat_to_str(next_dealer_seat) if next_dealer_seat is not None else None,
        },
    )
    await table.broadcast(
        protocol.CHAT,
        {"seat": None, "name": "Système", "text": _round_recap_chat_text(result["round_score"]), "system": True},
    )
    await _announce_bot_starting_hands(table, result["completed_round_hands"], result.get("contract_trump"))

    if result["game_over"]:
        logger.info(
            "[%s] FIN DE PARTIE equipe gagnante=%s scores finaux NS=%d EW=%d",
            table.table_key,
            result["winning_team"],
            result["cumulative_scores"]["NS"],
            result["cumulative_scores"]["EW"],
        )
        await table.broadcast(
            protocol.GAME_OVER,
            {"final_scores": result["cumulative_scores"], "winning_team": result["winning_team"]},
        )
    else:
        table.round_pause_task = asyncio.create_task(_finish_round_pause(table, game))


async def _finish_round_pause(table: Table, game: Game) -> None:
    """Deal the next round after its recap delay without holding `table.lock`."""
    try:
        await asyncio.sleep(table.round_pause_seconds)
        async with table.lock:
            if table.game is not game or not table.has_humans():
                return
            await _broadcast_deal(table)
            await _request_turn(table, game.next_to_act)
    finally:
        if table.round_pause_task is asyncio.current_task():
            table.round_pause_task = None
        if table.game is game and table.has_connected_humans():
            _schedule_bot_turns(table)


def _sort_hand_for_display(hand: list[Card], trump: str | None) -> list[Card]:
    """Sort a hand for readable chat logs: grouped by suit, strongest first.

    The trump suit (when known) leads, then the other suits in their canonical
    order; within each suit cards run strongest -> weakest so the log reads the
    way a player would fan their hand.
    """

    def rank_strength(card: Card) -> int:
        order = rules.TRUMP_ORDER if trump is not None and card.suit == trump else rules.NONTRUMP_ORDER
        return order.index(card.rank)

    def suit_priority(suit: str) -> int:
        # Trump first, then the remaining suits in their canonical order.
        return (-1, suit) if suit == trump else (rules.SUITS.index(suit), suit)

    return sorted(hand, key=lambda c: (suit_priority(c.suit), -rank_strength(c)))


async def _announce_bot_starting_hands(table: Table, hands: dict[Seat, list[Card]], trump: str | None = None) -> None:
    """Publish each bot's completed-round hand after scoring, never during play."""
    for seat, session in table.seats.items():
        if session is None or not session.is_bot:
            continue
        sorted_hand = _sort_hand_for_display(hands[seat], trump)
        cards = " ".join(_card_to_wire(card) for card in sorted_hand)
        await table.broadcast(
            protocol.CHAT,
            {
                "seat": _seat_to_str(seat),
                "name": session.name,
                "text": f"Ma main de départ était : {cards}",
                "system": True,
            },
        )


async def _run_bot_turns(table: Table) -> None:
    """Advance bot turns without retaining the table lock while a bot thinks."""
    try:
        while True:
            async with table.lock:
                game = table.game
                if game is None or game.game_over:
                    return
                if not table.has_connected_humans():
                    return
                if table.trick_pause_task is not None or table.round_pause_task is not None:
                    return
                seat = game.next_to_act
                session = table.seats.get(seat)
                if session is None or not session.is_bot:
                    return
                phase = game.phase
                bot_type = session.bot_type or table.bot_type
                target = table.bot_think_delay()

            # The Monte-Carlo decision is CPU-bound, so put it in a worker
            # thread. Crucially, neither the calculation nor its visual delay
            # retains `table.lock`: CHAT and LEAVE can be handled meanwhile.
            started = time.monotonic()
            loop = asyncio.get_running_loop()
            if phase == "bidding":
                bid_action = await loop.run_in_executor(None, choose_bid, game, seat, bot_type)
            elif phase == "trick_play":
                card = await loop.run_in_executor(None, choose_card, game, seat, bot_type)
            else:
                return

            elapsed = time.monotonic() - started
            if elapsed < target:
                await asyncio.sleep(target - elapsed)

            async with table.lock:
                # A player may have taken over this bot chair while it was
                # thinking. Never apply a stale bot decision in that case.
                if table.game is not game or game.next_to_act != seat:
                    continue
                session = table.seats.get(seat)
                if session is None or not session.is_bot:
                    continue
                if phase == "bidding":
                    result = game.submit_bid(
                        seat,
                        bid_action["action"],
                        trump=bid_action.get("trump"),
                        points=bid_action.get("points"),
                    )
                    await _handle_bid_result(table, seat, result)
                else:
                    result = game.submit_card(seat, card)
                    await _handle_play_result(table, result)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- a bot fault must not kill the server task
        logger.exception("[%s] erreur pendant le tour d'un bot", table.table_key)
    finally:
        if table.bot_task is asyncio.current_task():
            table.bot_task = None


def _schedule_bot_turns(table: Table) -> None:
    """Ensure this table has at most one background bot-turn runner."""
    if not table.has_connected_humans():
        return
    if table.bot_task is None or table.bot_task.done():
        table.bot_task = asyncio.create_task(_run_bot_turns(table))


async def _dispatch(table: Table, seat: Seat, msg_type: str, payload: dict) -> None:
    # Chat works in the lobby as well as mid-game; handle it before the
    # game-is-None guard that otherwise drops all game-phase messages.
    if msg_type == protocol.CHAT:
        session = table.seats.get(seat)
        name = session.name if session is not None else _seat_to_str(seat)
        await table.broadcast(protocol.CHAT, {"seat": _seat_to_str(seat), "name": name, "text": payload["text"]})
        return

    if msg_type == protocol.FILL_BOTS:
        if table.game is not None:
            await table.send_to(
                seat,
                protocol.ERROR,
                {"code": protocol.GAME_IN_PROGRESS, "message": "La partie a déjà commencé"},
            )
            return
        added = table.fill_with_bots()
        if not added:
            await table.send_to(
                seat,
                protocol.ERROR,
                {"code": protocol.TABLE_FULL, "message": "La table est déjà pleine"},
            )
            return
        players = _players_summary(table)
        await table.broadcast(
            protocol.LOBBY_UPDATE,
            {"players": players, "seats_filled": len(players), "waiting_for": 0},
        )
        await notify_lobby_subscribers()
        await _broadcast_deal(table)
        assert table.game is not None
        await _request_turn(table, table.game.next_to_act)
        _schedule_bot_turns(table)
        return

    if msg_type == protocol.SET_BOT_TYPE:
        target_seat = Seat(payload["seat"])
        target = table.seats.get(target_seat)
        if target is None or not target.is_bot:
            await table.send_to(
                seat,
                protocol.ERROR,
                {"code": protocol.MALFORMED_MESSAGE, "message": "Ce siège n'est pas occupé par un bot"},
            )
            return
        table.set_bot_type(target_seat, payload["bot_type"])
        logger.info("[%s] TYPE BOT %s -> %s", table.table_key, _seat_to_str(target_seat), payload["bot_type"])
        await table.broadcast(
            protocol.BOT_TYPE_CHANGED,
            {"seat": _seat_to_str(target_seat), "bot_type": payload["bot_type"]},
        )
        return

    game = table.game
    if game is None:
        return  # ignore game-phase messages while still in the lobby

    # A completed trick/round is being displayed. There is no legitimate move
    # request during that interval, so do not let a stale or malicious action
    # skip the visual pause; CHAT and LEAVE were handled before this point.
    if table.trick_pause_task is not None or table.round_pause_task is not None:
        if msg_type in {protocol.BID, protocol.PLAY_CARD}:
            return

    if msg_type == protocol.BID:
        try:
            result = game.submit_bid(seat, payload["action"], trump=payload.get("trump"), points=payload.get("points"))
        except NotYourTurnError:
            await table.send_to(
                seat, protocol.ERROR, {"code": protocol.NOT_YOUR_TURN, "message": "Ce n'est pas encore votre tour"}
            )
            return
        except IllegalBidError as exc:
            await table.send_to(seat, protocol.ERROR, {"code": protocol.ILLEGAL_BID, "message": str(exc)})
            return
        cancel_turn_timer(table)
        await _handle_bid_result(table, seat, result)
        _schedule_bot_turns(table)

    elif msg_type == protocol.PLAY_CARD:
        card_str = payload["card"]
        if not isinstance(card_str, str) or len(card_str) < 2:
            await table.send_to(seat, protocol.ERROR, {"code": protocol.ILLEGAL_CARD, "message": "Carte invalide"})
            return
        card = _wire_to_card(card_str)
        try:
            result = game.submit_card(seat, card)
        except NotYourTurnError:
            await table.send_to(
                seat, protocol.ERROR, {"code": protocol.NOT_YOUR_TURN, "message": "Ce n'est pas encore votre tour"}
            )
            return
        except IllegalCardError as exc:
            await table.send_to(seat, protocol.ERROR, {"code": protocol.ILLEGAL_CARD, "message": str(exc)})
            return
        cancel_turn_timer(table)
        await _handle_play_result(table, result)
        _schedule_bot_turns(table)

    elif msg_type == protocol.REMATCH:
        # Only meaningful once the previous game has actually ended; a stray/
        # duplicate rematch request (e.g. several players pressing it, or one
        # arriving after another player's rematch already restarted the table)
        # is silently ignored rather than restarting an in-progress game.
        if game is None or not game.game_over:
            return
        logger.info("[%s] NOUVELLE PARTIE demandee par %s", table.table_key, _player_label(table, seat))
        table.restart_game()
        await table.broadcast(protocol.NEW_GAME, {"target_score": table.target_score})
        await _broadcast_deal(table)
        assert table.game is not None
        await _request_turn(table, table.game.next_to_act)
        _schedule_bot_turns(table)


async def _handle_leave(table: Table, seat: Seat, writer: asyncio.StreamWriter) -> bool:
    """Let a player leave their table and return the connection to the lobby.

    Pre-game the seat is freed outright. Mid-game the seat can't be vacated
    (the running Game expects four actors), so it's handed over to a bot that
    plays out the rest of the game for the remaining players -- nobody is left
    blocked. Either way the leaver gets a LEFT confirmation and is re-subscribed
    to the live lobby listing so it can immediately pick another table.

    Always returns True (the caller returns the leaver to the join handshake).
    """
    session = table.seats.get(seat)
    name = session.name if session is not None else "?"

    if table.game is not None:
        # Mid-game: convert the seat to a bot rather than removing it, so the
        # other three players' game keeps running instead of stalling on an
        # empty chair.
        cancel_turn_timer(table)
        bot_name = table.replace_with_bot(seat)
        logger.info(
            "[%s] DEPART %s (%s) -> repris par un bot (%s)",
            table.table_key,
            name,
            _seat_to_str(seat),
            bot_name,
        )
        # Let the table (and any lobby watchers) see the seat is now a bot. `name`
        # is the departed human (for the "a quitté" banner); `bot_name` is the
        # fresh bot identity now holding the seat, so clients relabel the chair.
        await table.broadcast(
            protocol.CONNECTION_STATUS,
            {
                "seat": _seat_to_str(seat),
                "name": name,
                "bot_name": bot_name,
                "bot_type": table.seats[seat].bot_type,
                "status": "replaced_by_bot",
            },
            exclude=seat,
        )
    else:
        table.remove_player(seat)
        logger.info("[%s] DEPART %s (%s)", table.table_key, name, _seat_to_str(seat))
        players = _players_summary(table)
        await table.broadcast(
            protocol.LOBBY_UPDATE,
            {"players": players, "seats_filled": len(players), "waiting_for": 4 - len(players)},
        )

    # If nobody human is left at the table, it's abandoned: tear it down instead
    # of leaving a bot-only game (or an empty table) lingering in the registry
    # and the lobby listing forever, burning CPU driving bot turns for an
    # audience of nobody.
    removed = not table.has_humans()
    if removed:
        await _remove_table_and_notify(table)
        logger.info("[%s] table abandonnee (plus aucun joueur humain) -> supprimee", table.table_key)

    # Confirm the departure to the leaver and immediately start streaming lobby
    # updates so its table picker is live again without an extra round trip.
    # (The next JOIN's `_resolve_join` will discard this writer from the set.)
    LOBBY_SUBSCRIBERS.add(writer)
    try:
        writer.write(protocol.encode(protocol.LEFT, {}))
        writer.write(protocol.encode(protocol.TABLE_LISTING, {"tables": tables_listing()}))
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    await notify_lobby_subscribers()

    # If we just handed the seat to a bot and it's that bot's turn, drive it
    # (and any following bot turns) so play resumes without waiting for a human.
    # A removed (abandoned) table has no one left to play for, so leave it idle.
    if not removed and table.game is not None:
        _schedule_bot_turns(table)
    return True


async def _spectator_snapshot_payload(table: Table, spectator_name: str) -> dict:
    """Build the SPECTATING payload for a freshly attached spectator.

    When a game is under way, this is the hand-free public snapshot (mid-bid or
    mid-play) so the watcher's board is immediately in sync; before the game
    starts there is no `Game` yet, so a minimal waiting-room payload (just the
    seated players) is sent and the spectator catches up on the first DEAL."""
    if table.game is not None:
        return _public_snapshot_to_wire(table.game.public_snapshot(), table.table_key, table, spectator_name)
    return {
        "table_key": table.table_key,
        "table_options": _table_options_to_wire(table),
        "spectator_count": len(table.spectators),
        "spectator_name": spectator_name,
        "players": _players_summary(table),
        "phase": "waiting",
        "current_highest_bid": None,
        "bid_history": [],
        "current_trick": [],
        "trump": None,
        "whose_turn": None,
        "cumulative_scores": {"NS": 0, "EW": 0},
        "round_number": 0,
        "dealer_seat": None,
        "contract": None,
        "target_score": table.target_score,
        "server_version": __version__,
    }


async def _handle_spectator_leave(table: Table, spectator_name: str, writer: asyncio.StreamWriter) -> None:
    """Drop a spectator off a table and return its connection to the live lobby.

    Symmetric to `_handle_leave` for a seated player but simpler: there is no
    seat to free and never a game-in-progress guard (leaving as a spectator is
    always fine). Confirms with LEFT + a fresh listing and re-subscribes the
    writer so its table picker is live again on the same socket."""
    table.remove_spectator(spectator_name)
    logger.info("[%s] DEPART SPECTATEUR %s", table.table_key, spectator_name)

    removed = not table.has_humans() and not table.spectators
    if removed:
        await _remove_table_and_notify(table)
        logger.info("[%s] table abandonnee (plus aucun occupant) -> supprimee", table.table_key)
    else:
        await _broadcast_spectator_count(table)

    LOBBY_SUBSCRIBERS.add(writer)
    try:
        writer.write(protocol.encode(protocol.LEFT, {}))
        writer.write(protocol.encode(protocol.TABLE_LISTING, {"tables": tables_listing()}))
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    await notify_lobby_subscribers()


async def _resolve_join(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_score: int,
    trick_pause_seconds: float,
    round_pause_seconds: float,
    bot_think_seconds: float,
    turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
) -> tuple[Table, Seat | None, str | None] | None:
    """Read messages from a fresh client connection until a JOIN arrives.

    LIST_TABLES is served inline (TABLE_LISTING reply) and the loop continues
    so the same connection can then send JOIN -- no extra round trip needed.
    SUBSCRIBE_LOBBY registers the writer for live push TABLE_LISTING updates.

    Returns `(table, seat, None)` for a seated player, `(table, None, name)` for
    a spectator, or None if the connection dropped/was rejected before joining.
    """
    try:
        return await _resolve_join_inner(
            reader,
            writer,
            target_score,
            trick_pause_seconds,
            round_pause_seconds,
            bot_think_seconds,
            turn_timeout_seconds,
        )
    finally:
        LOBBY_SUBSCRIBERS.discard(writer)


async def _resolve_join_inner(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_score: int,
    trick_pause_seconds: float,
    round_pause_seconds: float,
    bot_think_seconds: float,
    turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
) -> tuple[Table, Seat | None, str | None] | None:
    while True:
        try:
            line = await reader.readline()
        except ValueError:
            await _send_error(writer, protocol.MALFORMED_MESSAGE, "Message too large")
            return None
        if not line:
            return None

        try:
            msg_type, payload = protocol.decode(line)
        except protocol.ProtocolError:
            await _send_error(writer, protocol.MALFORMED_MESSAGE, "Expected a join message")
            return None

        if msg_type == protocol.LIST_TABLES:
            try:
                writer.write(protocol.encode(protocol.TABLE_LISTING, {"tables": tables_listing()}))
                await writer.drain()
            except (ConnectionError, OSError):
                return None
            continue

        if msg_type == protocol.SUBSCRIBE_LOBBY:
            LOBBY_SUBSCRIBERS.add(writer)
            try:
                writer.write(protocol.encode(protocol.TABLE_LISTING, {"tables": tables_listing()}))
                await writer.drain()
            except (ConnectionError, OSError):
                return None
            continue

        if msg_type != protocol.JOIN:
            await _send_error(writer, protocol.MALFORMED_MESSAGE, "First message must be 'join'")
            return None

        break

    requested_table_key = str(payload["table_key"]).strip()
    player_name = str(payload["player_name"]).strip()
    team_name = str(payload["team_name"]).strip() if payload.get("team_name") else None
    spectate = bool(payload.get("spectate"))
    suppress_discord_notification = payload.get("suppress_discord_notification", False)
    coinche_blocks_bidding = payload.get("coinche_blocks_bidding", True)
    score_mode = payload.get("score_mode", rules.DEFAULT_SCORE_MODE)
    bot_type = payload.get("bot_type", DEFAULT_BOT_TYPE)
    if not isinstance(score_mode, str) or not rules.is_supported_score_mode(score_mode):
        await _send_error(writer, protocol.MALFORMED_MESSAGE, "Mode de comptage inconnu.")
        return None
    if not isinstance(bot_type, str) or not is_supported_bot_type(bot_type):
        await _send_error(writer, protocol.MALFORMED_MESSAGE, "Type de bot inconnu.")
        return None

    preferred_seat: Seat | None = None
    if payload.get("seat"):
        try:
            preferred_seat = Seat(str(payload["seat"]).strip().upper())
        except ValueError:
            await _send_error(writer, protocol.MALFORMED_MESSAGE, "seat must be one of N/E/S/W")
            return None

    if not TABLE_KEY_PATTERN.fullmatch(requested_table_key):
        await _send_error(
            writer,
            protocol.MALFORMED_MESSAGE,
            (
                f"table_key must be {protocol.TABLE_KEY_MIN_LENGTH}-"
                f"{protocol.TABLE_KEY_MAX_LENGTH} alphanumeric characters"
            ),
        )
        return None
    if not player_name:
        await _send_error(writer, protocol.MALFORMED_MESSAGE, "player_name must not be empty")
        return None

    table_key = next(
        (existing_key for existing_key in TABLES if existing_key.lower() == requested_table_key.lower()),
        requested_table_key,
    )

    if spectate:
        # Spectators cannot resurrect a table that was torn down because all
        # players left. Only look up existing tables — never create one.
        if table_key not in TABLES:
            await _send_error(writer, protocol.TABLE_FULL, "Table introuvable ou abandonnée")
            return None
        table = TABLES[table_key]
        async with table.lock:
            # A spectator joins without a seat and is always accepted (full or
            # in-progress tables are exactly what one wants to watch). Send the
            # current public snapshot so the board is immediately in sync, then
            # let `broadcast` keep it live; chat reaches spectators too.
            unique_name = table.add_spectator(player_name, writer)
            logger.info("[%s] SPECTATEUR %s", table_key, unique_name)
            await table.send_to_writer(
                writer, protocol.SPECTATING, await _spectator_snapshot_payload(table, unique_name)
            )
            await _broadcast_spectator_count(table)
            await notify_lobby_subscribers()
            return table, None, unique_name

    table_was_created = table_key not in TABLES
    table = get_or_create_table(
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

    async with table.lock:
        reconnect_seat = table.find_disconnected_seat(player_name) if table.game is not None else None

        if reconnect_seat is not None:
            seat = reconnect_seat
            logger.info("[%s] RECONNEXION %s (%s)", table_key, player_name, _seat_to_str(seat))
            snapshot = table.reconnect(seat, writer)
            await table.send_to(seat, protocol.RESYNC, _snapshot_to_wire(snapshot, table_key, table))
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
                    await _request_turn(table, seat)
                elif table.game.phase == "trick_play":
                    await _request_turn(table, seat)
            _schedule_bot_turns(table)
            await notify_lobby_subscribers()
            return table, seat, None

        # Replace-a-bot branch: a table with bots is one a human can sit down at
        # mid-game by taking over a bot's chair (the inverse of a leaver being
        # replaced by a bot). Only reached when the game is already running and
        # at least one seat is bot-driven; a fresh/empty table falls through to
        # normal seating below.
        if table.game is not None:
            bot_seats = table.bot_seats()
            if bot_seats:
                for session in table.seats.values():
                    if (
                        session is not None
                        and not session.is_bot
                        and session.connected
                        and session.name.lower() == player_name.lower()
                    ):
                        await _send_error(writer, protocol.NAME_TAKEN, f"Name already taken: {player_name}")
                        return None
                target_seat = _resolve_bot_seat(table, bot_seats, preferred_seat, team_name)
                bot_name = table.seats[target_seat].name  # type: ignore[union-attr]
                snapshot = table.replace_bot(target_seat, player_name, writer, team_name=team_name)
                logger.info(
                    "[%s] REMPLACEMENT BOT %s -> %s (%s)%s",
                    table_key,
                    bot_name,
                    player_name,
                    _seat_to_str(target_seat),
                    f" equipe={team_name}" if team_name else "",
                )
                await table.send_to(target_seat, protocol.RESYNC, _snapshot_to_wire(snapshot, table_key, table))
                await table.broadcast(
                    protocol.CONNECTION_STATUS,
                    {"seat": _seat_to_str(target_seat), "name": player_name, "status": "bot_replaced"},
                    exclude=target_seat,
                )
                # resync omits legal_actions/legal_cards; if it's this seat's turn,
                # follow up with a normal request so the newcomer can act right away.
                if table.game.next_to_act == target_seat:
                    if table.game.phase == "bidding":
                        await _request_turn(table, target_seat)
                    elif table.game.phase == "trick_play":
                        await _request_turn(table, target_seat)
                _schedule_bot_turns(table)
                await notify_lobby_subscribers()
                return table, target_seat, None

        try:
            seat = table.add_player(player_name, writer, team_name=team_name, preferred_seat=preferred_seat)
        except NameTakenError:
            await _send_error(writer, protocol.NAME_TAKEN, f"Name already taken: {player_name}")
            return None
        except GameInProgressError:
            await _send_error(writer, protocol.GAME_IN_PROGRESS, "Game already in progress")
            return None
        except TableFullError:
            await _send_error(writer, protocol.TABLE_FULL, "Table is full")
            return None

        if table_was_created and DISCORD_NOTIF_CHANNEL_POST_URL and not suppress_discord_notification:
            asyncio.create_task(_notify_discord_table_created(DISCORD_NOTIF_CHANNEL_POST_URL, table, player_name, seat))
        logger.info(
            "[%s] CONNEXION %s (%s)%s",
            table_key,
            player_name,
            _seat_to_str(seat),
            f" equipe={team_name}" if team_name else "",
        )
        players = _players_summary(table)
        await table.send_to(
            seat,
            protocol.JOINED,
            {
                "table_key": table_key,
                "table_options": _table_options_to_wire(table),
                "spectator_count": len(table.spectators),
                "seat": _seat_to_str(seat),
                "players": players,
                "target_score": table.target_score,
                "server_version": __version__,
            },
        )
        await table.broadcast(
            protocol.LOBBY_UPDATE,
            {"players": players, "seats_filled": len(players), "waiting_for": 4 - len(players)},
            exclude=seat,
        )
        # Notify seated players of the new arrival so the join effect fires.
        await table.broadcast(
            protocol.CONNECTION_STATUS,
            {"seat": _seat_to_str(seat), "name": player_name, "status": "joined"},
            exclude=seat,
        )
        if table.game is not None:
            await _broadcast_deal(table)
            await _request_turn(table, table.game.next_to_act)

        await notify_lobby_subscribers()
        return table, seat, None


async def handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_score: int,
    trick_pause_seconds: float = DEFAULT_TRICK_PAUSE_SECONDS,
    round_pause_seconds: float = DEFAULT_ROUND_PAUSE_SECONDS,
    bot_think_seconds: float = DEFAULT_BOT_THINK_SECONDS,
    turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
) -> None:
    table: Table | None = None
    seat: Seat | None = None
    spectator_name: str | None = None
    try:
        # Outer loop: a connection can join a table, LEAVE it (pre-game) to
        # return to the lobby, and then join another -- all on the same socket,
        # so the browser/terminal session and its web overlay survive a table
        # switch. Each pass re-runs the join handshake (which serves the lobby
        # picker) and then pumps that table's messages until leave/drop.
        while True:
            joined = await _resolve_join(
                reader,
                writer,
                target_score,
                trick_pause_seconds,
                round_pause_seconds,
                bot_think_seconds,
                turn_timeout_seconds,
            )
            if joined is None:
                return
            table, seat, spectator_name = joined

            left = False
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

                # Spectator branch: no seat, so game actions don't apply. A
                # spectator can chat and can LEAVE to return to the lobby; every
                # other action is silently ignored (the client never sends them).
                if spectator_name is not None:
                    if msg_type == protocol.LEAVE:
                        async with table.lock:
                            await _handle_spectator_leave(table, spectator_name, writer)
                        table = None
                        spectator_name = None
                        left = True
                        break
                    if msg_type == protocol.CHAT:
                        async with table.lock:
                            await table.broadcast(
                                protocol.CHAT, {"seat": None, "name": spectator_name, "text": payload["text"]}
                            )
                    continue

                if msg_type == protocol.LEAVE:
                    async with table.lock:
                        left = await _handle_leave(table, seat, writer)
                    if left:
                        # Seat already vacated by _handle_leave; forget it so the
                        # drop-cleanup below doesn't touch a now-empty seat, then
                        # loop back to the lobby/join handshake.
                        table = None
                        seat = None
                        break
                    continue

                async with table.lock:
                    await _dispatch(table, seat, msg_type, payload)

            if left:
                continue  # returned to lobby; re-resolve join on the same socket
            break  # connection dropped -- fall through to cleanup

    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        if table is not None and spectator_name is not None:
            async with table.lock:
                table.remove_spectator(spectator_name)
                logger.info("[%s] DEPART SPECTATEUR %s", table.table_key, spectator_name)
                if not table.has_humans() and not table.spectators:
                    await _remove_table_and_notify(table)
                    logger.info("[%s] table abandonnee (plus aucun occupant) -> supprimee", table.table_key)
                else:
                    await _broadcast_spectator_count(table)
                await notify_lobby_subscribers()
        elif table is not None and seat is not None:
            async with table.lock:
                if table.game is None:
                    table.remove_player(seat)
                    players = _players_summary(table)
                    await table.broadcast(
                        protocol.LOBBY_UPDATE,
                        {"players": players, "seats_filled": len(players), "waiting_for": 4 - len(players)},
                    )
                    if not table.has_humans() and not table.spectators:
                        await _remove_table_and_notify(table)
                        logger.info("[%s] table abandonnee (plus aucun occupant) -> supprimee", table.table_key)
                    await notify_lobby_subscribers()
                else:
                    session = table.seats.get(seat)
                    if session is not None and session.writer is writer:
                        name = table.mark_disconnected(seat)
                        logger.info("[%s] DECONNEXION %s (%s)", table.table_key, name, _seat_to_str(seat))
                        await table.broadcast(
                            protocol.CONNECTION_STATUS,
                            {"seat": _seat_to_str(seat), "name": name, "status": "disconnected"},
                        )
                        # Keep an active deadline while a player reconnects: it
                        # still owns the same turn and must not stall the game.
                        if not table.has_connected_humans():
                            await cancel_background_tasks(table, include_turn_timer=False)
                        await notify_lobby_subscribers()
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
    parser.add_argument(
        "--trick-pause",
        type=float,
        default=DEFAULT_TRICK_PAUSE_SECONDS,
        help=(
            "Seconds to pause after each completed trick so players can see the last card played "
            f"(default: {DEFAULT_TRICK_PAUSE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--round-pause",
        type=float,
        default=DEFAULT_ROUND_PAUSE_SECONDS,
        help=(
            "Seconds to pause after each completed round (manche) so players can read the "
            f"end-of-round score recap before the next deal starts (default: {DEFAULT_ROUND_PAUSE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--bot-think",
        type=float,
        default=DEFAULT_BOT_THINK_SECONDS,
        help=(
            "Minimum seconds each bot waits before bidding or playing; up to one random extra second "
            f"(default: {DEFAULT_BOT_THINK_SECONDS})"
        ),
    )
    parser.add_argument(
        "--turn-timeout",
        type=_positive_float,
        default=DEFAULT_TURN_TIMEOUT_SECONDS,
        help=f"Seconds a human may hold a turn before a bot replaces them (default: {DEFAULT_TURN_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--bot-samples",
        type=_positive_int,
        default=100,
        help="Monte Carlo hidden-hand samples evaluated for each bot card choice (default: 100)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Game-log verbosity: who bid/played what, trick/round/game winners (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional file path to also write the game log to (in addition to stdout)",
    )
    return parser


def _detect_lan_ip() -> str | None:
    """Best-effort local (LAN) IP, without any external call.

    Opens a UDP socket "towards" a public address: no packet is actually sent,
    but the OS picks the outbound interface, whose address we can read back.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # A timeout so a stuck routing lookup (VPN, no route) can never wedge
            # the executor thread — otherwise `asyncio.run` hangs at shutdown
            # waiting to join it. `socket.timeout` subclasses `OSError`.
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def _detect_public_ip(timeout: float = 3.0) -> str | None:
    """Best-effort public (internet) IP via an external HTTP query."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as resp:
            return resp.read().decode("utf-8").strip() or None
    except Exception:
        return None


async def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    try:
        validate_timeout_order(args.turn_timeout, DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    configure_samples(args.bot_samples)
    logger.info("Bot Monte Carlo samples: %d", args.bot_samples)
    logger.info(
        "Timeouts validated: turn=%.1fs global-kick=%.1fs",
        args.turn_timeout,
        DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS,
    )

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(
            reader,
            writer,
            args.target_score,
            trick_pause_seconds=args.trick_pause,
            round_pause_seconds=args.round_pause,
            bot_think_seconds=args.bot_think,
            turn_timeout_seconds=args.turn_timeout,
        )

    server = await asyncio.start_server(_handler, args.host, args.port)
    bound = server.sockets[0].getsockname() if server.sockets else (args.host, args.port)
    port = bound[1]
    print(f"Coinche server listening on {bound[0]}:{port} (target score {args.target_score})")

    # Detect reachable addresses so players know where to connect. Both lookups
    # are best-effort and run in an executor to avoid blocking the event loop.
    # (run_in_executor rather than asyncio.to_thread, which needs Python 3.9+.)
    loop = asyncio.get_event_loop()
    lan_ip = await loop.run_in_executor(None, _detect_lan_ip)
    public_ip = await loop.run_in_executor(None, _detect_public_ip)
    if lan_ip:
        print(f"  LAN (same network) : {lan_ip}:{port}")
        print(f"    -> ./run_client.sh --host {lan_ip} --port {port}")
    if public_ip:
        print(f"  Internet (public)  : {public_ip}:{port}")
        print(f"    -> ./run_client.sh --host {public_ip} --port {port}")
        print("  (forward this port on your router for remote players)")
    elif not lan_ip:
        print("  (could not detect a network address)")
    if not lan_ip and not public_ip:
        print(f"  (clients can connect with: ./run_client.sh --host <IP> --port {port})")
    async with server:
        await server.serve_forever()


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
