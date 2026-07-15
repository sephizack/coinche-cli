"""Game orchestration: auction state machine, trick play, and round/game lifecycle.

Transport-agnostic and I/O-free: all methods return plain event/result dicts
(or raise a GameError subclass) that the server layer translates into protocol
messages (join/bid_request/play_request/etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coinche import rules
from coinche.cards import Card, Seat, build_deck
from coinche.cards import deal as deal_cards

# Re-exported so callers can `from coinche.game import Seat`.
__all__ = [
    "Seat",
    "TEAM_OF",
    "PARTNER_OF",
    "BidState",
    "RoundState",
    "Game",
    "GameError",
    "NotYourTurnError",
    "IllegalBidError",
    "IllegalCardError",
]

TEAM_OF: dict[Seat, str] = {
    Seat.N: "NS",
    Seat.S: "NS",
    Seat.E: "EW",
    Seat.W: "EW",
}

PARTNER_OF: dict[Seat, Seat] = {
    Seat.N: Seat.S,
    Seat.S: Seat.N,
    Seat.E: Seat.W,
    Seat.W: Seat.E,
}


class GameError(Exception):
    """Base class for all game-state validation errors."""


class NotYourTurnError(GameError):
    pass


class IllegalBidError(GameError):
    pass


class IllegalCardError(GameError):
    pass


@dataclass
class BidState:
    next_to_act: Seat
    history: list[dict] = field(default_factory=list)
    current_highest_bid: dict | None = None
    coinche_level: int = 1
    pass_streak: int = 0
    any_bid_made: bool = False


@dataclass
class RoundState:
    dealer: Seat
    dealt_hands: dict[Seat, list[Card]]
    hands: dict[Seat, list[Card]]
    trump: str | None = None  # suit glyph, set once bidding settles
    leader: Seat | None = None
    current_trick: list[tuple[Seat, Card]] = field(default_factory=list)
    trick_history: list[dict] = field(default_factory=list)
    captured_points: dict[str, int] = field(default_factory=lambda: {"NS": 0, "EW": 0})
    belote_holder: str | None = None
    belote_seat: Seat | None = None
    belote_announced: int = 0  # 0=none yet, 1="Belote" said, 2="Rebelote" said
    tricks_played: int = 0


class Game:
    """Orchestrates one Coinche game (a sequence of rounds to a target score)."""

    def __init__(
        self,
        target_score: int = rules.DEFAULT_TARGET_SCORE,
        initial_dealer: Seat = Seat.N,
    ) -> None:
        self.target_score = target_score
        self.dealer = initial_dealer
        self.round_number = 0
        self.cumulative_scores: dict[str, int] = {"NS": 0, "EW": 0}
        self.phase = "bidding"
        self.next_to_act: Seat = initial_dealer.next()
        self.game_over = False
        self.winning_team: str | None = None
        self.bid_state: BidState | None = None
        self.round_state: RoundState | None = None
        self.start_round()

    # --- Round lifecycle ------------------------------------------------

    def start_round(self) -> dict:
        """Deal a new hand and open bidding. Returns a round-start event."""
        self.round_number += 1
        hands = deal_cards(build_deck(), self.dealer)
        self.round_state = RoundState(
            dealer=self.dealer,
            dealt_hands={s: list(h) for s, h in hands.items()},
            hands=hands,
        )
        first_bidder = self.dealer.next()
        self.bid_state = BidState(next_to_act=first_bidder)
        self.next_to_act = first_bidder
        self.phase = "bidding"
        return {
            "outcome": "round_started",
            "round_number": self.round_number,
            "dealer_seat": self.dealer,
            "first_bidder_seat": first_bidder,
        }

    def get_hand(self, seat: Seat) -> list[Card]:
        assert self.round_state is not None
        return list(self.round_state.hands[seat])

    # --- Bidding ----------------------------------------------------------

    def bid_options_for(self, seat: Seat) -> dict:
        assert self.bid_state is not None
        bid = self.bid_state
        if seat != bid.next_to_act:
            return {
                "current_highest_bid": bid.current_highest_bid,
                "legal_actions": [],
                "can_coinche": False,
                "can_surcoinche": False,
            }
        current = bid.current_highest_bid
        can_coinche = (
            current is not None
            and bid.coinche_level == 1
            and TEAM_OF[seat] != current["team"]
        )
        can_surcoinche = (
            current is not None
            and bid.coinche_level == 2
            and TEAM_OF[seat] == current["team"]
        )
        # Once a coinche is on the table (coinche_level >= 2), the auction is
        # closed to new point bids: the only moves left are passing or
        # surcoinching (A6). Offering fresh bids would let the auction climb
        # past the coinche, which isn't allowed.
        legal_actions = rules.legal_bid_actions(current) if bid.coinche_level == 1 else []
        return {
            "current_highest_bid": current,
            "legal_actions": legal_actions,
            "can_coinche": can_coinche,
            "can_surcoinche": can_surcoinche,
        }

    def submit_bid(
        self,
        seat: Seat,
        action: str,
        trump: str | None = None,
        points: int | str | None = None,
    ) -> dict:
        """Validate and apply a bid action. Raises GameError on invalid input."""
        assert self.bid_state is not None
        bid = self.bid_state

        if self.phase != "bidding":
            raise IllegalBidError("Bidding is not currently open")
        if seat != bid.next_to_act:
            raise NotYourTurnError(f"It is not {seat}'s turn to bid")

        if action == "pass":
            bid.history.append({"seat": seat, "action": "pass"})
            bid.pass_streak += 1
            if not bid.any_bid_made and bid.pass_streak == 4:
                return self._redeal()
            if bid.any_bid_made and bid.pass_streak == 3:
                return self._finalize_contract()
            bid.next_to_act = seat.next()
            self.next_to_act = bid.next_to_act
            return {"outcome": "continue", "seat": seat, "action": "pass", "next_to_act": bid.next_to_act}

        if action == "bid":
            if bid.coinche_level != 1:
                raise IllegalBidError("Cannot bid once a coinche has been declared")
            new_bid = {"trump": trump, "points": points}
            if not rules.is_valid_bid(new_bid, bid.current_highest_bid):
                raise IllegalBidError(f"Illegal bid: {new_bid} over {bid.current_highest_bid}")
            bid.current_highest_bid = {"team": TEAM_OF[seat], "seat": seat, "trump": trump, "points": points}
            bid.coinche_level = 1
            bid.pass_streak = 0
            bid.any_bid_made = True
            bid.history.append({"seat": seat, "action": "bid", "trump": trump, "points": points})
            bid.next_to_act = seat.next()
            self.next_to_act = bid.next_to_act
            return {
                "outcome": "continue",
                "seat": seat,
                "action": "bid",
                "trump": trump,
                "points": points,
                "next_to_act": bid.next_to_act,
            }

        if action == "coinche":
            current = bid.current_highest_bid
            if current is None or bid.coinche_level != 1 or TEAM_OF[seat] == current["team"]:
                raise IllegalBidError("Coinche is not currently available to this seat")
            bid.coinche_level = 2
            bid.pass_streak = 0
            bid.history.append({"seat": seat, "action": "coinche"})
            bid.next_to_act = seat.next()
            self.next_to_act = bid.next_to_act
            return {"outcome": "continue", "seat": seat, "action": "coinche", "next_to_act": bid.next_to_act}

        if action == "surcoinche":
            current = bid.current_highest_bid
            if current is None or bid.coinche_level != 2 or TEAM_OF[seat] != current["team"]:
                raise IllegalBidError("Surcoinche is not currently available to this seat")
            bid.coinche_level = 4
            bid.pass_streak = 0
            bid.history.append({"seat": seat, "action": "surcoinche"})
            # A surcoinche ends the auction immediately: nothing outranks it,
            # so play starts right away with the surcoinched contract.
            return self._finalize_contract()

        raise IllegalBidError(f"Unknown bid action: {action!r}")

    def _redeal(self) -> dict:
        assert self.round_state is not None
        self.dealer = self.dealer.next()
        event = self.start_round()
        return {"outcome": "redeal", "dealer_seat": event["dealer_seat"], "first_bidder_seat": event["first_bidder_seat"], "round_number": event["round_number"]}

    def _finalize_contract(self) -> dict:
        assert self.bid_state is not None
        assert self.round_state is not None
        bid = self.bid_state
        contract = bid.current_highest_bid
        assert contract is not None

        trump_glyph = contract["trump"]
        self.round_state.trump = trump_glyph
        self.round_state.belote_holder = self._detect_belote(trump_glyph)

        first_leader = self.dealer.next()
        self.round_state.leader = first_leader
        self.phase = "trick_play"
        self.next_to_act = first_leader

        return {
            "outcome": "contract",
            "attacking_team": contract["team"],
            "seat": contract["seat"],
            "trump": contract["trump"],
            "points": contract["points"],
            "coinche_level": bid.coinche_level,
            "first_leader": first_leader,
        }

    def _detect_belote(self, trump_suit: str) -> str | None:
        assert self.round_state is not None
        for seat, hand in self.round_state.dealt_hands.items():
            ranks = {c.rank for c in hand if c.suit == trump_suit}
            if {"R", "D"}.issubset(ranks):
                self.round_state.belote_seat = seat
                return TEAM_OF[seat]
        return None

    # --- Trick play ---------------------------------------------------------

    def play_options_for(self, seat: Seat) -> dict:
        assert self.round_state is not None
        rs = self.round_state
        if seat != self.next_to_act:
            legal_cards: list[Card] = []
        else:
            led_suit = rs.current_trick[0][1].suit if rs.current_trick else None
            legal_cards = rules.legal_cards_to_play(
                rs.hands[seat],
                rs.current_trick,
                rs.trump,
                led_suit,
                player_seat=seat,
                partner_seat=PARTNER_OF[seat],
            )
        return {
            "legal_cards": legal_cards,
            "current_trick": list(rs.current_trick),
            "trump": rs.trump,
        }

    def submit_card(self, seat: Seat, card: Card) -> dict:
        assert self.round_state is not None
        rs = self.round_state

        if self.phase != "trick_play":
            raise IllegalCardError("Trick play is not currently open")
        if seat != self.next_to_act:
            raise NotYourTurnError(f"It is not {seat}'s turn to play")
        if card not in rs.hands[seat]:
            raise IllegalCardError(f"{seat} does not hold {card}")

        led_suit = rs.current_trick[0][1].suit if rs.current_trick else None
        legal_cards = rules.legal_cards_to_play(
            rs.hands[seat],
            rs.current_trick,
            rs.trump,
            led_suit,
            player_seat=seat,
            partner_seat=PARTNER_OF[seat],
        )
        if card not in legal_cards:
            raise IllegalCardError(f"{card} is not legal for {seat} to play right now")

        rs.hands[seat].remove(card)
        rs.current_trick.append((seat, card))

        # Belote/Rebelote (A11): the holder of both K+Q of trump announces
        # "Belote" when playing the first of the two and "Rebelote" when
        # playing the second; the +20 point bonus itself is credited
        # unconditionally at round scoring via `rs.belote_holder`.
        belote_announcement: str | None = None
        if (
            rs.belote_seat == seat
            and rs.trump is not None
            and card.suit == rs.trump
            and card.rank in ("R", "D")
        ):
            rs.belote_announced += 1
            belote_announcement = "belote" if rs.belote_announced == 1 else "rebelote"

        if len(rs.current_trick) < 4:
            self.next_to_act = seat.next()
            return {
                "trick_complete": False,
                "seat": seat,
                "card": card,
                "current_trick": list(rs.current_trick),
                "next_to_act": self.next_to_act,
                "belote_announcement": belote_announcement,
            }

        result = self._resolve_trick(seat, card)
        result["belote_announcement"] = belote_announcement
        return result

    def _resolve_trick(self, seat: Seat, card: Card) -> dict:
        assert self.round_state is not None
        rs = self.round_state

        led_suit = rs.current_trick[0][1].suit
        winner_seat = rules.trick_winner(rs.current_trick, rs.trump, led_suit)
        points_won = sum(
            rules.card_points(c, rs.trump) for _, c in rs.current_trick
        )
        rs.tricks_played += 1
        is_last_trick = rs.tricks_played == 8
        if is_last_trick:
            points_won += rules.DIX_DE_DER

        winner_team = TEAM_OF[winner_seat]
        rs.captured_points[winner_team] += points_won

        completed_trick = list(rs.current_trick)
        rs.trick_history.append({"winner_seat": winner_seat, "trick": completed_trick, "points_won": points_won})
        rs.current_trick = []
        rs.leader = winner_seat

        result: dict = {
            "seat": seat,
            "card": card,
            "current_trick": [],
            "trick_complete": True,
            "winner_seat": winner_seat,
            "completed_trick": completed_trick,
            "points_won": points_won,
            "tricks_played": rs.tricks_played,
            "tricks_remaining": 8 - rs.tricks_played,
        }

        if not is_last_trick:
            self.next_to_act = winner_seat
            result["round_complete"] = False
            result["next_to_act"] = winner_seat
            return result

        result["round_complete"] = True
        result.update(self._finish_round())
        return result

    def _finish_round(self) -> dict:
        assert self.bid_state is not None
        assert self.round_state is not None
        bid = self.bid_state
        rs = self.round_state
        contract = bid.current_highest_bid
        assert contract is not None

        capot_result: bool | None = None
        if contract["points"] == rules.CAPOT:
            attacking_team = contract["team"]
            capot_result = all(
                TEAM_OF[t["winner_seat"]] == attacking_team for t in rs.trick_history
            )

        round_score = rules.score_round(
            rs.captured_points,
            contract,
            bid.coinche_level,
            capot_result,
            rs.belote_holder,
        )

        for team in ("NS", "EW"):
            self.cumulative_scores[team] += round_score[team]["total"]

        ns, ew = self.cumulative_scores["NS"], self.cumulative_scores["EW"]
        game_over = False
        winning_team = None
        if ns >= self.target_score or ew >= self.target_score:
            if ns == ew:
                game_over = False
            else:
                game_over = True
                winning_team = "NS" if ns > ew else "EW"

        result: dict = {
            "round_score": round_score,
            "cumulative_scores": dict(self.cumulative_scores),
            "game_over": game_over,
            "winning_team": winning_team,
        }

        if game_over:
            self.phase = "game_over"
            self.game_over = True
            self.winning_team = winning_team
            result["next_dealer_seat"] = None
        else:
            self.dealer = self.dealer.next()
            start_event = self.start_round()
            result["next_dealer_seat"] = self.dealer
            result["new_round_number"] = start_event["round_number"]
            result["new_first_bidder_seat"] = start_event["first_bidder_seat"]

        return result

    # --- Reconnection snapshot (A16) -----------------------------------------

    def snapshot_for(self, seat: Seat) -> dict:
        """Pure, I/O-free view of the game from `seat`'s perspective (for resync)."""
        assert self.round_state is not None
        rs = self.round_state

        snapshot: dict = {
            "seat": seat,
            "hand": self.get_hand(seat),
            "phase": self.phase,
            "current_trick": list(rs.current_trick),
            "trump": rs.trump,
            "whose_turn": self.next_to_act,
            "cumulative_scores": dict(self.cumulative_scores),
            "round_number": self.round_number,
            "dealer_seat": self.dealer,
        }

        if self.phase == "bidding":
            assert self.bid_state is not None
            snapshot["current_highest_bid"] = self.bid_state.current_highest_bid
            snapshot["bid_history"] = list(self.bid_state.history)
        else:
            snapshot["current_highest_bid"] = None
            snapshot["bid_history"] = []

        return snapshot
