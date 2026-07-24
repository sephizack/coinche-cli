"""I/O-free session-state core for the Coinche client.

Holds `ClientState` (the per-session, player-visible game state) plus the pure
reducer `apply_message` and the `snapshot_to_dict` projection. This module is
the **mirror contract**: both the terminal renderer (`client.py`) and the web
bridge (later units) project their view from the one `ClientState` object.

Hard constraints (BR-U1-1 / NFR1): this module performs NO I/O — no `rich`, no
`asyncio`, no sockets, no `await`. It consumes already-decoded wire values and
never re-implements enum<->JSON conversion (that stays in `protocol.py`,
BR-U1-2). The only wall-clock source is `time.time()` (BR-U1-8, IN2).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from coinche import protocol
from coinche.cards import Seat
from coinche.game import TEAM_OF
from coinche.rules import NONTRUMP_ORDER, TRUMP_ORDER

# Suit colors and canonical within-color order used only when displaying a
# hand, chosen so that black/red suits alternate left to right for
# readability. This is distinct from `cards.SUITS`, which stays in its
# original order for bid/trump enumeration elsewhere.
BLACK_SUITS: tuple[str, ...] = ("♠", "♣")
RED_SUITS: tuple[str, ...] = ("♥", "♦")


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
    # Set on BID_REQUEST while it's this seat's turn to bid; both UIs show the
    # bid prompt/panel directly from it (the CLI stage-1 choice grid inline in
    # the live view, the web bid panel). Cleared once the turn ends — locally
    # when the player acts, and by the reducer on BID_UPDATE/BIDDING_RESULT/
    # DEAL/RESYNC so the prompt never lingers past our turn.
    pending_bid_request: dict | None = None
    pending_play_request: dict | None = None
    # `active_bid_value_prompt` is (trump, legal_actions) once the player has
    # picked a trump and stage 2 (typing the point value) is on screen; it takes
    # display/dispatch priority over the stage-1 grid while set.
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
    # `apply_message`'s JOINED/RESYNC handling); `update_notice_shown` guards
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


@dataclass
class ApplyResult:
    """Return value of `apply_message`, replacing the removed `asyncio.Event`
    parameter (ADR-3). The caller (terminal `receiver_loop` or the web bridge)
    sets its own event / notifies browsers from this flag."""

    action_requested: bool = False


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


def _connection_banner_text(name: str, status: str) -> str:
    """Plain-text connection-status notice for `last_action`.

    Kept byte-identical to `ui.render_connection_banner(name, status).plain` so
    the terminal view is unchanged — but built here without importing `rich`
    (BR-U1-7 / IN1: this module stays I/O-free). The terminal side is free to
    re-render a styled banner in its redraw path if it wants to; `last_action`
    itself only ever carries plain text."""
    if status == "disconnected":
        return f"⚠ En attente de {name} (reconnexion...)"
    return f"✓ {name} reconnecté"


def apply_message(state: ClientState, msg_type: str, payload: dict) -> ApplyResult:
    """Reduce one decoded server message into `state` (mutating in place) and
    return an `ApplyResult`.

    `action_requested` is True for exactly the messages that previously called
    `action_event.set()` inside the reducer — BID_REQUEST, PLAY_REQUEST,
    ROUND_SCORE, GAME_OVER ("it's now your turn / a screen the input loop must
    react to"). The `action_event.set()` in `receiver_loop`'s finally (on
    connection close) is NOT part of the reducer and is unaffected. Every
    per-message state transition is identical to the former `_apply_message`
    (BR-U1-3)."""
    action_requested = False

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
        state.pending_bid_request = None
        state.pending_play_request = None
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
        action_requested = True

    elif msg_type == protocol.BID_UPDATE:
        # A bid action was registered. The server only ever holds one open
        # bid request at a time (the seat whose turn it is), so any BID_UPDATE
        # arriving after our BID_REQUEST is the result of our own action —
        # the standing request is consumed. Clear it so neither UI keeps the
        # bid prompt/panel open past our turn (this is the field the web bid
        # panel is driven from).
        state.pending_bid_request = None
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
        state.pending_bid_request = None  # bidding settled — no open request
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
        action_requested = True

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
        action_requested = True

    elif msg_type == protocol.GAME_OVER:
        state.game_over = True
        state.round_over_screen = False
        state.final_scores = payload["final_scores"]
        state.winning_team = payload["winning_team"]
        state.last_action = f"Partie terminée — vainqueur : {payload['winning_team']}"
        state.whose_turn = None
        action_requested = True

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
        # Resync rebuilds from the server's authoritative snapshot; any stale
        # local bid/play prompt is dropped (the server will re-send a fresh
        # BID_REQUEST / PLAY_REQUEST if it's still this seat's turn).
        state.pending_bid_request = None
        state.pending_play_request = None
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
        # IN1/BR-U1-7: build the plain notice here (no `rich`); the terminal
        # side may re-render it styled from `last_action` in its redraw path.
        state.last_action = _connection_banner_text(payload["name"], payload["status"])

    elif msg_type == protocol.CHAT:
        seat = Seat(payload["seat"])
        who = state.players.get(seat, seat.value)
        team = state.team_of.get(seat)
        state.chat_messages.append((who, payload["text"], team, time.time()))

    elif msg_type == protocol.ERROR:
        text = payload.get("message") or payload.get("code") or "Erreur inconnue"
        state.errors.append((time.time(), text))
        state.last_action = f"Erreur : {text}"

    return ApplyResult(action_requested=action_requested)


def snapshot_to_dict(state: ClientState) -> dict:
    """Project `ClientState` into a JSON-serializable dict for a browser push.

    Pure — never mutates `state`, and calling it twice on unchanged state
    yields equal dicts (INV-2). All values are str/int/bool/list/dict; enum
    keys/values are emitted as their already-decoded `.value` strings (no enum
    re-mapping — BR-U1-2). `hand` is the LOCAL seat's cards only; the
    projection never includes or synthesizes any other seat's cards
    (BR-U1-6 / NFR4)."""
    return {
        "seat": state.seat.value if state.seat is not None else None,
        "table_key": state.table_key,
        "players": {seat.value: name for seat, name in state.players.items()},
        "team_of": {seat.value: team for seat, team in state.team_of.items()},
        "team_names": dict(state.team_names),
        "hand": list(state.hand),  # LOCAL seat only — never other hands
        "legal_cards": list(state.legal_cards),
        "current_trick": {seat.value: card for seat, card in state.current_trick.items()},
        "last_trick": {seat.value: card for seat, card in state.last_trick.items()},
        "whose_turn": state.whose_turn.value if state.whose_turn is not None else None,
        "dealer_seat": state.dealer_seat.value if state.dealer_seat is not None else None,
        "trump": state.trump,
        "contract_points": state.contract_points,
        "contract_bidder": state.contract_bidder.value if state.contract_bidder is not None else None,
        "coinche_level": state.coinche_level,
        "current_bid": {
            "trump": state.current_bid_trump,
            "points": state.current_bid_points,
            "seat": state.current_bid_seat.value if state.current_bid_seat is not None else None,
        },
        "bid_marks": {seat.value: label for seat, label in state.bid_marks.items()},
        "cumulative_scores": dict(state.cumulative_scores),
        "final_scores": dict(state.final_scores),
        "winning_team": state.winning_team,
        "last_round_score": state.last_round_score,
        "last_round_contract": state.last_round_contract,  # round-recap parity (FR3.2/3.3)
        "connection_status": {seat.value: status for seat, status in state.connection_status.items()},
        "status_message": state.status_message,
        "last_action": state.last_action,
        "pending_bid_request": state.pending_bid_request,  # so the web can show the bid panel
        "pending_play_request": bool(state.legal_cards),
        "chat_messages": [
            {"name": name, "text": text, "team": team, "ts": ts} for name, text, team, ts in state.chat_messages
        ],
        "flags": {
            "game_over": state.game_over,
            "round_over_screen": state.round_over_screen,
            "joined_once": state.joined_once,
        },
        "server_version": state.server_version,
    }
