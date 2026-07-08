"""Coinche TCP client: connection, live-redraw table view, keyboard-menu prompts.

Run with: python -m coinche.client [--host HOST] [--port PORT] [--table KEY] [--name NAME]
                                    [--team TEAM_NAME]
Any omitted value falls back to an interactive input() prompt. `--team` is
optional: it's a free-text label (e.g. "A"/"B") shared with a teammate to try
to be seated on the same team (best-effort; the server falls back to normal
seating if that isn't possible).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from rich.live import Live
from rich.text import Text

from coinche import protocol, ui
from coinche.cards import Seat
from coinche.game import TEAM_OF
from coinche.rules import NONTRUMP_ORDER, TRUMP_ORDER

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BACKOFF_DELAYS = (1, 2, 4, 8, 16)

# Suit order used only when displaying a hand, chosen so that black/red
# suits alternate left to right (pique, cœur, trèfle, carreau) for
# readability. This is distinct from `cards.SUITS`, which stays in its
# original order for bid/trump enumeration elsewhere.
HAND_SUIT_ORDER: tuple[str, ...] = ("♠", "♥", "♣", "♦")


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
    whose_turn: Seat | None = None
    trump: str | None = None
    contract_points: str | int | None = None
    contract_bidder: Seat | None = None
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
    joined_once: bool = False
    game_over: bool = False


def _players_from_wire(entries: list[dict]) -> dict[Seat, str]:
    return {Seat(p["seat"]): p["name"] for p in entries}


def _trick_from_wire(entries: list[dict]) -> dict[Seat, str]:
    return {Seat(t["seat"]): t["card"] for t in entries}


def _trump_label(trump: str) -> str:
    return trump


def _sort_hand(hand: list[str], trump: str | None) -> list[str]:
    """Sort a hand strongest-to-weakest, left to right (grouped by suit, in
    `HAND_SUIT_ORDER`, which alternates black/red suits for readability).
    Before a trump is known (`trump=None`), every suit is ordered by its
    non-trump strength. Once a trump is declared, that suit's cards use the
    trump strength order instead, so the ranking of e.g. the Jack/9 changes
    accordingly."""

    def rank_strength(rank: str, suit: str) -> int:
        if suit == trump:
            return TRUMP_ORDER.index(rank)
        return NONTRUMP_ORDER.index(rank)

    def sort_key(card: str) -> tuple[int, int]:
        rank, suit = card[:-1], card[-1]
        return (HAND_SUIT_ORDER.index(suit), -rank_strength(rank, suit))

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
        state.status_message = f"En attente de joueurs ({len(state.players)}/4)..."

    elif msg_type == protocol.LOBBY_UPDATE:
        state.players = _players_from_wire(payload["players"])
        state.team_of = {s: TEAM_OF[s] for s in state.players}
        state.status_message = f"En attente de joueurs ({payload['seats_filled']}/4)..."

    elif msg_type == protocol.DEAL:
        state.hand = _sort_hand(payload["hand"], None)
        state.legal_cards = []
        state.current_trick = {}
        state.last_trick = {}
        state.trump = None
        state.contract_points = None
        state.contract_bidder = None
        state.current_bid_trump = None
        state.current_bid_points = None
        state.current_bid_seat = None
        state.bid_marks = {}
        state.whose_turn = Seat(payload["first_bidder_seat"])
        state.last_action = f"Nouvelle donne #{payload['round_number']} (donneur {payload['dealer_seat']})"

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
        state.current_trick = {}
        state.last_trick = _trick_from_wire(payload["trick"])
        winner = Seat(payload["winner_seat"])
        who = state.players.get(winner, winner.value)
        state.last_action = f"Pli remporté par {who} (+{payload['points_won']} pts)"
        state.whose_turn = winner

    elif msg_type == protocol.ROUND_SCORE:
        state.cumulative_scores = payload["cumulative"]
        state.last_action = "Score de la manche"
        state.whose_turn = None

    elif msg_type == protocol.GAME_OVER:
        state.game_over = True
        state.last_action = f"Partie terminée — vainqueur : {payload['winning_team']}"
        state.whose_turn = None

    elif msg_type == protocol.RESYNC:
        state.joined_once = True
        state.table_key = payload["table_key"]
        state.seat = Seat(payload["seat"])
        state.trump = payload["trump"]
        state.hand = _sort_hand(payload["hand"], state.trump)
        state.legal_cards = []
        state.current_trick = _trick_from_wire(payload["current_trick"])
        state.cumulative_scores = payload["cumulative_scores"]
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
        state.last_action = f"[chat] {who}: {payload['text']}"

    elif msg_type == protocol.ERROR:
        state.last_action = f"Erreur : {payload.get('message', payload.get('code'))}"


async def run_session(
    host: str, port: int, table_key: str, player_name: str, team_name: str | None = None
) -> str:
    """Run one connection attempt end-to-end.

    `team_name`, if given, is a free-text label (e.g. "A"/"B") shared with a
    teammate to try to be seated on the same team (best-effort; the server
    falls back to normal seating if no other player joined with the same
    label yet, or their team is already full).

    Returns "not_joined" if the session never completed a join/resync,
    "game_over" if the game concluded normally, or "disconnected" if the
    connection dropped mid-session after having joined (worth retrying).
    """
    state = ClientState()

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
    live = Live(auto_refresh=False, screen=False)
    live.start()

    def redraw() -> None:
        if state.seat is None or not state.players:
            live.update(Text(state.status_message))
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
                legal_cards=state.legal_cards or None,
                trump=display_trump,
                contract_points=display_points,
                contract_bidder_name=contract_bidder_name,
                last_trick=state.last_trick,
            )
            live.update(view)
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
        while True:
            await action_event.wait()
            action_event.clear()

            if state.game_over:
                return
            if reader.at_eof():
                return

            if state.pending_bid_request is not None:
                req = state.pending_bid_request
                state.pending_bid_request = None
                live.stop()
                menu_text, tokens = ui.render_bid_menu(
                    req["legal_actions"], req["current_highest_bid"], req["can_coinche"], req["can_surcoinche"]
                )
                print(menu_text)
                choice = await _prompt_key_choice(tokens)

                bid_payload: dict | None = None
                if choice is not None and choice["action"] == "select_trump":
                    trump = choice["trump"]
                    prompt_text, valid_points = ui.render_bid_value_prompt(trump, req["legal_actions"])
                    print(prompt_text)
                    points = await _prompt_bid_value(valid_points)
                    bid_payload = {"action": "bid", "trump": trump, "points": points}
                elif choice is not None:
                    bid_payload = choice

                live.start()
                if bid_payload is None:
                    continue
                try:
                    writer.write(protocol.encode(protocol.BID, bid_payload))
                    await writer.drain()
                except (ConnectionError, OSError):
                    return

            elif state.pending_play_request is not None:
                state.pending_play_request = None
                # Use state.legal_cards (already sorted to match the hand
                # display order) so menu numbers line up with build_hand's.
                _, tokens = ui.render_play_menu(state.legal_cards)
                choice = await _prompt_key_choice(tokens)
                state.legal_cards = []
                redraw()
                if choice is None:
                    continue
                try:
                    writer.write(protocol.encode(protocol.PLAY_CARD, {"card": choice}))
                    await writer.drain()
                except (ConnectionError, OSError):
                    return

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

    if state.game_over:
        return "game_over"
    if state.joined_once:
        return "disconnected"
    return "not_joined"


def _read_single_key() -> str:
    """Read one keystroke with no Enter needed (POSIX raw mode; falls back to a line read
    when stdin isn't a real terminal, e.g. piped input)."""
    try:
        import termios
        import tty
    except ImportError:
        return sys.stdin.readline()[:1]

    fd = sys.stdin.fileno()
    try:
        old_settings = termios.tcgetattr(fd)
    except termios.error:
        return sys.stdin.readline()[:1]

    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


async def _prompt_key_choice(tokens: dict[str, object]) -> object | None:
    """Read single keystrokes (no Enter) until a valid numbered token is pressed."""
    while True:
        key = await asyncio.to_thread(_read_single_key)
        if not key:
            return None
        if key in tokens:
            return tokens[key]


async def _prompt_bid_value(valid_points: list[int | str]) -> int | str:
    """Read the announced point value typed by hand (needs Enter), re-prompting on an
    invalid value."""
    valid_tokens = {str(p) for p in valid_points}
    while True:
        raw = await asyncio.to_thread(input, "> ")
        token = raw.strip().lower()
        if token in valid_tokens:
            return int(token) if token.isdigit() else token
        print("Valeur invalide, réessayez.")


def _prompt_missing(args: argparse.Namespace) -> tuple[str, int, str, str, str | None]:
    host = args.host or input(f"Adresse du serveur [{DEFAULT_HOST}]: ").strip() or DEFAULT_HOST
    if args.port is not None:
        port = args.port
    else:
        raw_port = input(f"Port [{DEFAULT_PORT}]: ").strip()
        port = int(raw_port) if raw_port else DEFAULT_PORT
    table_key = args.table or input("Clé de table : ").strip()
    player_name = args.name or input("Votre nom : ").strip()
    if args.team is not None:
        team_name = args.team.strip() or None
    else:
        team_name = input("Nom d'équipe (optionnel, ex: A/B) : ").strip() or None
    return host, port, table_key, player_name, team_name


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coinche network game client")
    parser.add_argument("--host", help="Server host/IP")
    parser.add_argument("--port", type=int, help="Server port")
    parser.add_argument("--table", help="Table key")
    parser.add_argument("--name", help="Player name")
    parser.add_argument(
        "--team", help="Team label (e.g. 'A'/'B') shared with a teammate to try to be seated together (best-effort)"
    )
    return parser


async def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    host, port, table_key, player_name, team_name = _prompt_missing(args)

    result = await run_session(host, port, table_key, player_name, team_name)
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
    asyncio.run(main())


if __name__ == "__main__":
    cli()
