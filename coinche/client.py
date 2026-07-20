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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from rich.live import Live
from rich.text import Text

from coinche import __version__, protocol, ui
from coinche.cards import Seat
from coinche.game import TEAM_OF
from coinche.rules import NONTRUMP_ORDER, TRUMP_ORDER

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BACKOFF_DELAYS = (1, 2, 4, 8, 16)

# Suit colors and canonical within-color order used only when displaying a
# hand, chosen so that black/red suits alternate left to right for
# readability. This is distinct from `cards.SUITS`, which stays in its
# original order for bid/trump enumeration elsewhere.
BLACK_SUITS: tuple[str, ...] = ("♠", "♣")
RED_SUITS: tuple[str, ...] = ("♥", "♦")

# Thread-safety flag: when set, `_read_single_key` returns "" promptly so the
# executor's worker thread frees up.  This allows shutdown_default_executor()
# to complete within ms rather than blocking for up to 5 minutes waiting on a
# thread stuck in os.read(stdin).
_key_interrupt = threading.Event()


@dataclass
class ClientState:
    seat: Seat | None = None
    table_key: str | None = None
    players: dict[Seat, str] = field(default_factory=dict)
    team_of: dict[Seat, str] = field(default_factory=dict)
    hand: list[str] = field(default_factory=list)
    legal_cards: list[str] = field(default_factory=list)
    current_trick: dict[Seat, str] = field(default_factory=dict)
    last_trick: dict[Seat, str] = field(default_factory=dict)
    # Holds the just-completed trick between TRICK_RESULT and the next
    # message that actually starts a fresh trick (PLAY_REQUEST). Kept out of
    # `last_trick` until then so the "Dernier pli" corner panel doesn't
    # update in lockstep with the main table -- while the completed trick's
    # 4 cards are still shown big on the main table during the post-trick
    # pause, the corner keeps showing the trick before that; both flip over
    # together only once the new trick actually begins.
    pending_last_trick: dict[Seat, str] | None = None
    whose_turn: Seat | None = None
    # Seat that dealt the current round, shown as a "(D)" marker on the table
    # so players can tell at a glance who bids first (dealer.next()) once the
    # bidding phase starts.
    dealer_seat: Seat | None = None
    trump: str | None = None
    contract_points: str | int | None = None
    contract_bidder: Seat | None = None
    # Coinche multiplier of the settled contract (1 = none, 2 = coinche,
    # 4 = surcoinche), shown next to the "Annonce : ..." reminder during play
    # so everyone can see the stakes were doubled/quadrupled. Set at
    # BIDDING_RESULT, reset each DEAL.
    coinche_level: int = 1
    # Highest bid still standing *while bidding is ongoing* (distinct from
    # `trump`/`contract_points`/`contract_bidder` above, which only get set
    # once bidding has settled into a final contract at BIDDING_RESULT).
    current_bid_trump: str | None = None
    current_bid_points: str | int | None = None
    current_bid_seat: Seat | None = None
    # Each seat's most recent bidding action (e.g. "90 ♥", "Passe", "Coinche"),
    # shown at that seat's position on the table in place of a played card —
    # there's nothing to play yet during bidding. Cleared once bidding ends.
    bid_marks: dict[Seat, str] = field(default_factory=dict)
    cumulative_scores: dict[str, int] = field(default_factory=lambda: {"NS": 0, "EW": 0})
    connection_status: dict[Seat, bool] = field(default_factory=dict)
    status_message: str = ""
    # Description of the most recently *completed* action (e.g. "Nord a
    # annoncé 90 Cœur"), kept separate from `status_message` so that a fresh
    # bid/play request for another seat never overwrites/hides what just
    # happened. Combined with `whose_turn` in the footer to show both
    # "what happened last" and "who we're waiting for now" at once.
    last_action: str = ""
    pending_bid_request: dict | None = None
    pending_play_request: dict | None = None
    # The bid request currently being answered, kept around (unlike
    # `pending_bid_request` above, which is consumed/cleared as soon as
    # `input_loop` picks it up) so `redraw()` can keep showing the stage-1
    # choice grid inline in the live view for as long as we're waiting on a
    # keypress. `active_bid_value_prompt` is (trump, legal_actions) once the
    # player has picked a trump and stage 2 (typing the point value) is on
    # screen instead; only one of the two is shown at a time.
    active_bid_request: dict | None = None
    active_bid_value_prompt: tuple[str, list] | None = None
    # Point value typed so far for the stage-2 prompt above, echoed inline by
    # `redraw()` instead of via the terminal's own line-input echo (see
    # `_handle_bid_value_key`): raw terminal echo happens outside Live's tracked
    # console and desyncs its cursor-position bookkeeping, which is what
    # caused stale table content to linger on screen.
    bid_value_buffer: str = ""
    bid_value_error: bool = False
    joined_once: bool = False
    # Every ERROR received from the server this session, as (timestamp, text)
    # pairs (e.g. a rejected join -- invalid table key, name taken, table
    # full -- or an in-game error like NOT_YOUR_TURN). Collected here so
    # `run_session` can dump them all at the end instead of the client just
    # vanishing silently when the server rejects the connection and hangs up.
    errors: list[tuple[float, str]] = field(default_factory=list)
    game_over: bool = False
    # Populated on GAME_OVER (final cumulative scores/winner) and by the last
    # ROUND_SCORE seen before it (per-team contract_result etc.), so the end-
    # of-game screen can show whether the last announcement was honored.
    final_scores: dict[str, int] = field(default_factory=lambda: {"NS": 0, "EW": 0})
    winning_team: str | None = None
    last_round_score: dict[str, dict] | None = None
    # Whether the end-of-round recap (score of the manche just finished, plus
    # whether the announced contract was honored) should currently be shown
    # in place of the normal table view. Set on ROUND_SCORE, cleared once the
    # next round's DEAL arrives (or the game ends, since GAME_OVER shows its
    # own final recap instead).
    round_over_screen: bool = False
    # Built alongside `last_round_score` (see `_build_last_round_contract`),
    # while `contract_bidder`/`trump`/`contract_points` still describe the
    # round that just ended (before the next DEAL resets them).
    last_round_contract: dict | None = None
    # Set once when the server reports a version different from ours (see
    # `_apply_message`'s JOINED/RESYNC handling); `update_notice_shown` guards
    # against printing the banner more than once per session.
    server_version: str | None = None
    update_notice_shown: bool = False
    # Team id ("NS"/"EW") -> the free-text label a player chose via `--team`,
    # if any (see `_team_names_from_wire`); shown in place of "Nous"/"Eux"
    # wherever a team is displayed.
    team_names: dict[str, str] = field(default_factory=dict)
    # Chat: split-pane state.
    active_pane: str = "game"  # "game" or "chat"
    chat_messages: deque[tuple[str, str, str | None, float]] = field(default_factory=lambda: deque(maxlen=20))
    chat_buffer: str = ""
    chat_error: bool = False
    chat_cursor: int = 0


def _players_from_wire(entries: list[dict]) -> dict[Seat, str]:
    return {Seat(p["seat"]): p["name"] for p in entries}


def _team_names_from_wire(entries: list[dict]) -> dict[str, str]:
    """Map team id ("NS"/"EW") to a player-chosen `team_name`, if any. When both
    teammates supplied one, the first one seen (seat order) wins -- best-effort,
    since they're expected to match (matched case-insensitively server-side in
    `Table.add_player`)."""
    team_names: dict[str, str] = {}
    for p in entries:
        name = p.get("team_name")
        if not name:
            continue
        team = TEAM_OF[Seat(p["seat"])]
        team_names.setdefault(team, name)
    return team_names


def _trick_from_wire(entries: list[dict]) -> dict[Seat, str]:
    return {Seat(t["seat"]): t["card"] for t in entries}


def _trump_label(trump: str) -> str:
    return trump


def _hand_suit_order(suits_present: set[str]) -> tuple[str, ...]:
    """Left-to-right suit display order for a hand containing `suits_present`,
    chosen so that black/red suits alternate for readability.

    With all 4 suits present, they interleave black/red/black/red. With only
    1-2 suits present, alternation is moot so they're just grouped black-
    before-red. But with exactly 3 suits present, one color necessarily has
    only one suit ("the lone suit") while the other has two — naively
    grouping black-then-red would put the two same-colored suits next to
    each other (e.g. pique, cœur, carreau puts the two reds together).
    Instead, the lone suit is placed in the middle, sandwiched between the
    two same-colored suits, so colors still alternate."""

    blacks = tuple(s for s in BLACK_SUITS if s in suits_present)
    reds = tuple(s for s in RED_SUITS if s in suits_present)
    if len(blacks) == 2 and len(reds) == 2:
        return (blacks[0], reds[0], blacks[1], reds[1])
    if len(blacks) == 2 and len(reds) == 1:
        return (blacks[0], reds[0], blacks[1])
    if len(reds) == 2 and len(blacks) == 1:
        return (reds[0], blacks[0], reds[1])
    return blacks + reds


def _sort_hand(hand: list[str], trump: str | None) -> list[str]:
    """Sort a hand strongest-to-weakest, left to right (grouped by suit,
    ordered by `_hand_suit_order` so black/red suits alternate for
    readability — this ordering is purely a client-side display concern,
    computed fresh from `hand`'s own suits each call, and never sent back to
    the server, so it can't affect gameplay).
    Before a trump is known (`trump=None`, i.e. during the bidding phase),
    every suit is ordered using the *trump* strength order (J, 9, A, 10, K,
    Q, 8, 7) rather than the plain-suit order — this lets the player evaluate
    each suit as a candidate trump while bidding. Once a trump is declared,
    only that suit keeps the trump strength order and the others fall back
    to the non-trump order."""

    def rank_strength(rank: str, suit: str) -> int:
        if trump is None or suit == trump:
            return TRUMP_ORDER.index(rank)
        return NONTRUMP_ORDER.index(rank)

    suit_order = _hand_suit_order({card[-1] for card in hand})

    def sort_key(card: str) -> tuple[int, int]:
        rank, suit = card[:-1], card[-1]
        return (suit_order.index(suit), -rank_strength(rank, suit))

    return sorted(hand, key=sort_key)


def _bid_action_label(payload: dict) -> str:
    """Readable French description of a single bid action for the "last action" line."""
    action = payload["action"]
    if action == "pass":
        return "a passé"
    if action == "coinche":
        return "a coinché"
    if action == "surcoinche":
        return "a surcoinché"
    points = "Capot" if payload.get("points") == "capot" else payload.get("points")
    return f"a annoncé {points} {_trump_label(payload['trump'])}"


def _bid_mark_label(entry: dict) -> str:
    """Compact label for a single bid action, shown at the acting seat's
    position on the table (e.g. "90 ♥", "Passe", "Coinche", "Surcoinche")."""
    action = entry["action"]
    if action == "pass":
        return "Passe"
    if action == "coinche":
        return "Coinche"
    if action == "surcoinche":
        return "Surcoinche"
    points = "Capot" if entry.get("points") == "capot" else entry.get("points")
    return f"{points} {_trump_label(entry['trump'])}"


def _build_last_round_contract(state: ClientState) -> dict | None:
    """Build the {trump, points, bidder_name, attacking_team, result} summary of
    the round that just ended, from the contract fields BIDDING_RESULT set
    (still valid at ROUND_SCORE/GAME_OVER time, before the next DEAL resets
    them) plus `state.last_round_score`'s per-team `contract_result`. Returns
    None if there's nothing to show yet (e.g. `last_round_score` hasn't been
    populated)."""
    if state.contract_bidder is None or state.last_round_score is None:
        return None
    attacking_team = state.team_of.get(state.contract_bidder, "NS")
    round_score_for_attacker = state.last_round_score.get(attacking_team)
    if round_score_for_attacker is None:
        return None
    return {
        "trump": state.trump,
        "points": state.contract_points,
        "bidder_name": state.players.get(state.contract_bidder, state.contract_bidder.value),
        "attacking_team": attacking_team,
        "result": round_score_for_attacker["contract_result"],
    }


def _apply_current_highest_bid(state: ClientState, current_highest_bid: dict | None) -> None:
    if current_highest_bid is None:
        state.current_bid_trump = None
        state.current_bid_points = None
        state.current_bid_seat = None
    else:
        state.current_bid_trump = current_highest_bid["trump"]
        state.current_bid_points = current_highest_bid["points"]
        state.current_bid_seat = Seat(current_highest_bid["seat"])


def _apply_message(state: ClientState, msg_type: str, payload: dict, action_event: asyncio.Event) -> None:
    if msg_type == protocol.JOINED:
        state.joined_once = True
        state.table_key = payload["table_key"]
        state.seat = Seat(payload["seat"])
        state.players = _players_from_wire(payload["players"])
        state.team_of = {s: TEAM_OF[s] for s in state.players}
        state.team_names = _team_names_from_wire(payload["players"])
        state.status_message = f"En attente de joueurs ({len(state.players)}/4)..."
        state.server_version = payload.get("server_version")

    elif msg_type == protocol.LOBBY_UPDATE:
        state.players = _players_from_wire(payload["players"])
        state.team_of = {s: TEAM_OF[s] for s in state.players}
        state.team_names = _team_names_from_wire(payload["players"])
        state.status_message = f"En attente de joueurs ({payload['seats_filled']}/4)..."

    elif msg_type == protocol.DEAL:
        state.hand = _sort_hand(payload["hand"], None)
        state.legal_cards = []
        state.current_trick = {}
        state.last_trick = {}
        state.pending_last_trick = None
        state.trump = None
        state.contract_points = None
        state.contract_bidder = None
        state.coinche_level = 1
        state.current_bid_trump = None
        state.current_bid_points = None
        state.current_bid_seat = None
        state.bid_marks = {}
        state.whose_turn = Seat(payload["first_bidder_seat"])
        state.dealer_seat = Seat(payload["dealer_seat"])
        state.last_action = f"Nouvelle donne #{payload['round_number']} (donneur {payload['dealer_seat']})"
        # Deliberately do NOT clear `round_over_screen` here: the end-of-round
        # recap stays on screen (holding this freshly-dealt state underneath it)
        # until the local player presses a key to dismiss it (see input_loop),
        # so the recap is never auto-hidden by the next deal arriving.

    elif msg_type == protocol.BID_REQUEST:
        state.pending_bid_request = payload
        _apply_current_highest_bid(state, payload["current_highest_bid"])
        state.whose_turn = state.seat
        action_event.set()

    elif msg_type == protocol.BID_UPDATE:
        seat = Seat(payload["seat"])
        who = state.players.get(seat, seat.value)
        state.last_action = f"{who} {_bid_action_label(payload)}"
        state.bid_marks[seat] = _bid_mark_label(payload)
        if payload["action"] == "bid":
            state.current_bid_trump = payload["trump"]
            state.current_bid_points = payload["points"]
            state.current_bid_seat = seat
        state.whose_turn = Seat(payload["next_to_act"])

    elif msg_type == protocol.BIDDING_RESULT:
        state.bid_marks = {}
        if payload["outcome"] == "redeal":
            state.last_action = "Tout le monde a passé — nouvelle donne"
            state.whose_turn = None
        else:
            state.trump = payload["trump"]
            state.hand = _sort_hand(state.hand, state.trump)
            state.contract_points = payload["points"]
            state.contract_bidder = Seat(payload["seat"])
            state.coinche_level = payload.get("coinche_level", 1)
            who = state.players.get(Seat(payload["seat"]), payload["seat"])
            points = "Capot" if payload["points"] == "capot" else payload["points"]
            state.last_action = f"Contrat retenu : {points} {_trump_label(payload['trump'])} par {who}"
            state.whose_turn = Seat(payload["first_leader"])

    elif msg_type == protocol.PLAY_REQUEST:
        state.pending_play_request = payload
        # Sort legal cards the same way the hand is displayed, so the
        # numbered choices shown under the hand read 1, 2, 3... left to
        # right instead of following the server's (unsorted) hand order.
        state.legal_cards = _sort_hand(payload["legal_cards"], payload["trump"])
        state.trump = payload["trump"]
        state.current_trick = _trick_from_wire(payload["current_trick"])
        # A new trick is actually starting now: promote the previously-held
        # completed trick into `last_trick` at the same moment the main
        # table's `current_trick` moves on, so both panels flip together.
        if state.pending_last_trick is not None:
            state.last_trick = state.pending_last_trick
            state.pending_last_trick = None
        state.whose_turn = state.seat
        action_event.set()

    elif msg_type == protocol.CARD_PLAYED:
        state.current_trick = _trick_from_wire(payload["current_trick"])
        played_seat = Seat(payload["seat"])
        who = state.players.get(played_seat, played_seat.value)
        state.last_action = f"{who} a joué {payload['card']}"
        announcement = payload.get("belote_announcement")
        if announcement == "belote":
            state.last_action += " — Belote !"
        elif announcement == "rebelote":
            state.last_action += " — Rebelote !"
        next_to_act = payload.get("next_to_act")
        state.whose_turn = Seat(next_to_act) if next_to_act is not None else None
        if played_seat == state.seat and payload["card"] in state.hand:
            state.hand.remove(payload["card"])

    elif msg_type == protocol.TRICK_RESULT:
        # Deliberately do NOT clear `current_trick` here (it already holds
        # all 4 cards, via CARD_PLAYED's `completed_trick` broadcast below)
        # and do NOT update `last_trick` yet either: the server holds off
        # sending the next PLAY_REQUEST for `trick_pause_seconds` after
        # broadcasting this message specifically so players can see the
        # completed trick big on the main table during that pause. Stash it
        # in `pending_last_trick` instead -- it's promoted to `last_trick`
        # once `TRICK_CLEARED` (or the next `PLAY_REQUEST`) arrives, so the
        # "Dernier pli" corner doesn't jump to this trick while it's still
        # on display in the middle of the table.
        state.pending_last_trick = _trick_from_wire(payload["trick"])
        winner = Seat(payload["winner_seat"])
        who = state.players.get(winner, winner.value)
        state.last_action = f"Pli remporté par {who} (+{payload['points_won']} pts)"
        state.whose_turn = winner

    elif msg_type == protocol.TRICK_CLEARED:
        # Sent to every seat (not just whoever plays next) right after the
        # post-trick pause ends, so all four players clear the table and
        # promote `last_trick` at the same moment -- otherwise only the next
        # player to act (via PLAY_REQUEST below) would update, leaving the
        # other three looking at stale cards until their own next turn.
        state.current_trick = {}
        if state.pending_last_trick is not None:
            state.last_trick = state.pending_last_trick
            state.pending_last_trick = None

    elif msg_type == protocol.ROUND_SCORE:
        state.cumulative_scores = payload["cumulative"]
        state.last_round_score = {"NS": payload["team_NS"], "EW": payload["team_EW"]}
        state.last_round_contract = _build_last_round_contract(state)
        # Shown in place of the normal table view until the local player
        # presses a key to dismiss it (see input_loop) -- NOT auto-cleared by
        # the next DEAL, so the recap never flashes by unread. Skipped entirely
        # if this round also ended the game (see GAME_OVER below), whose own
        # recap screen already covers this same information.
        state.round_over_screen = True
        state.last_action = "Score de la manche"
        state.whose_turn = None
        # Wake input_loop so it can block on a keypress to dismiss the recap.
        action_event.set()

    elif msg_type == protocol.GAME_OVER:
        state.game_over = True
        state.round_over_screen = False
        state.final_scores = payload["final_scores"]
        state.winning_team = payload["winning_team"]
        state.last_action = f"Partie terminée — vainqueur : {payload['winning_team']}"
        state.whose_turn = None
        action_event.set()

    elif msg_type == protocol.NEW_GAME:
        state.game_over = False
        state.round_over_screen = False
        state.final_scores = {"NS": 0, "EW": 0}
        state.winning_team = None
        state.last_round_score = None
        state.last_round_contract = None
        state.cumulative_scores = {"NS": 0, "EW": 0}
        state.last_action = "Nouvelle partie !"

    elif msg_type == protocol.RESYNC:
        state.joined_once = True
        state.table_key = payload["table_key"]
        state.seat = Seat(payload["seat"])
        state.trump = payload["trump"]
        state.hand = _sort_hand(payload["hand"], state.trump)
        state.legal_cards = []
        state.current_trick = _trick_from_wire(payload["current_trick"])
        state.pending_last_trick = None
        state.cumulative_scores = payload["cumulative_scores"]
        state.server_version = payload.get("server_version")
        # A resync always drops the player back onto the live table (mid-deal
        # or mid-bid), never into an in-between-rounds pause, so any stale
        # round-over recap must be cleared here too.
        state.round_over_screen = False
        if payload.get("players"):
            state.players = _players_from_wire(payload["players"])
            state.team_names = _team_names_from_wire(payload["players"])
        if state.seat not in state.players:
            state.players[state.seat] = state.players.get(state.seat, "Moi")
        state.team_of = {s: TEAM_OF[s] for s in Seat}
        state.whose_turn = Seat(payload["whose_turn"]) if payload.get("whose_turn") else None
        state.bid_marks = {}
        if payload.get("phase") == "bidding":
            _apply_current_highest_bid(state, payload.get("current_highest_bid"))
            for entry in payload.get("bid_history", []):
                state.bid_marks[Seat(entry["seat"])] = _bid_mark_label(entry)
        else:
            _apply_current_highest_bid(state, None)
        state.status_message = "Reconnecté — synchronisation effectuée"
        state.last_action = "Reconnecté — synchronisation effectuée"

    elif msg_type == protocol.CONNECTION_STATUS:
        seat = Seat(payload["seat"])
        state.connection_status[seat] = payload["status"] != "disconnected"
        banner = ui.render_connection_banner(payload["name"], payload["status"])
        state.last_action = banner.plain

    elif msg_type == protocol.CHAT:
        seat = Seat(payload["seat"])
        who = state.players.get(seat, seat.value)
        team = state.team_of.get(seat)
        state.chat_messages.append((who, payload["text"], team, time.time()))

    elif msg_type == protocol.ERROR:
        text = payload.get("message") or payload.get("code") or "Erreur inconnue"
        state.errors.append((time.time(), text))
        state.last_action = f"Erreur : {text}"


async def run_session(
    host: str,
    port: int,
    table_key: str,
    player_name: str,
    team_name: str | None = None,
    connection: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None,
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

    try:
        writer.write(
            protocol.encode(
                protocol.JOIN,
                {"table_key": table_key, "player_name": player_name, "team_name": team_name},
            )
        )
        await writer.drain()
    except (ConnectionError, OSError):
        return "not_joined"

    action_event = asyncio.Event()
    live = Live(auto_refresh=False, screen=True)
    live.start()

    def redraw() -> None:
        if state.server_version is not None and state.server_version != __version__ and not state.update_notice_shown:
            state.update_notice_shown = True
            live.console.print(ui.render_update_notice(__version__, state.server_version))
        if state.seat is None or not state.players:
            live.update(Text(state.status_message))
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
            elif state.active_bid_request is not None:
                req = state.active_bid_request
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
                _apply_message(state, msg_type, payload, action_event)
                redraw()
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
                    try:
                        writer.write(protocol.encode(protocol.REMATCH, {}))
                        await writer.drain()
                    except (ConnectionError, OSError):
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
                    await _handle_chat_key(state, key, writer)
                    redraw()
                elif state.active_bid_request is not None:
                    await _handle_bid_key(state, key, writer, redraw)
                elif state.active_bid_value_prompt is not None:
                    await _handle_bid_value_key(state, key, writer, redraw)
                elif state.pending_bid_request is not None:
                    req = state.pending_bid_request
                    state.pending_bid_request = None
                    state.active_bid_request = req
                    redraw()
                elif state.pending_play_request is not None:
                    state.pending_play_request = None
                    redraw()
                elif state.legal_cards:
                    await _handle_play_key(state, key, writer, redraw)

                key_task = asyncio.ensure_future(asyncio.to_thread(_read_single_key))
        finally:
            key_task.cancel()

    try:
        redraw()
        await asyncio.gather(receiver_loop(), input_loop())
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


async def _handle_chat_key(state: ClientState, key: str, writer: asyncio.StreamWriter) -> None:
    """Per-key dispatch for the chat pane (called from input_loop)."""
    if key in ("\r", "\n"):
        text = state.chat_buffer.strip()
        if text:
            try:
                writer.write(protocol.encode(protocol.CHAT, {"text": text}))
                await writer.drain()
            except (ConnectionError, OSError):
                pass
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


async def _handle_bid_key(
    state: ClientState, key: str, writer: asyncio.StreamWriter, redraw: Callable[[], None]
) -> bool:
    """Per-key dispatch for stage-1 bid menu. Returns True if the key was consumed."""
    req = state.active_bid_request
    if req is None:
        return False
    _, tokens = ui.render_bid_menu(
        req["legal_actions"], req["current_highest_bid"], req["can_coinche"], req["can_surcoinche"]
    )
    if key not in tokens:
        return False
    choice = tokens[key]
    bid_payload: dict | None = None
    if choice["action"] == "select_trump":
        trump = choice["trump"]
        state.active_bid_value_prompt = (trump, req["legal_actions"])
        state.bid_value_buffer = ""
        state.bid_value_error = False
        redraw()
        return True
    bid_payload = choice
    state.active_bid_request = None
    state.active_bid_value_prompt = None
    redraw()
    if bid_payload is not None:
        try:
            writer.write(protocol.encode(protocol.BID, bid_payload))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
    return True


async def _handle_bid_value_key(
    state: ClientState, key: str, writer: asyncio.StreamWriter, redraw: Callable[[], None]
) -> bool:
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
            bid_payload = {"action": "bid", "trump": trump, "points": points}
            state.active_bid_request = None
            state.active_bid_value_prompt = None
            redraw()
            try:
                writer.write(protocol.encode(protocol.BID, bid_payload))
                await writer.drain()
            except (ConnectionError, OSError):
                pass
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


async def _handle_play_key(
    state: ClientState, key: str, writer: asyncio.StreamWriter, redraw: Callable[[], None]
) -> None:
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
    try:
        writer.write(protocol.encode(protocol.PLAY_CARD, {"card": choice}))
        await writer.drain()
    except (ConnectionError, OSError):
        pass


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


async def _lobby_picker(
    host: str,
    port: int,
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

    try:
        writer.write(protocol.encode(protocol.SUBSCRIBE_LOBBY, {}))
        await writer.drain()
    except (ConnectionError, OSError):
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
        ui.render_lobby(latest_tables, table_cursor, error=lobby_error),
        auto_refresh=False,
        screen=True,
    )
    live.start()

    def redraw() -> None:
        if step == "table":
            live.update(ui.render_lobby(latest_tables, table_cursor, error=lobby_error))
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
        result = await _lobby_picker(host, port)
        if result is None:
            return
        table_key, team_name, conn_reader, conn_writer = result
        connection = (conn_reader, conn_writer)

    result = await run_session(host, port, table_key, player_name, team_name, connection=connection)
    if result == "not_joined":
        return
    if result == "game_over":
        print("Partie terminée. Au revoir !")
        return

    for delay in BACKOFF_DELAYS:
        print(f"Connexion perdue. Nouvelle tentative dans {delay}s...")
        await asyncio.sleep(delay)
        result = await run_session(host, port, table_key, player_name, team_name)
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
