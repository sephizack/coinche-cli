"""An assertive auction style built on the default card-play strategy."""

from __future__ import annotations

from coinche import rules
from coinche.bot_types.default import DefaultBot, _has_been_played, _opponents_may_hold_trump
from coinche.cards import Card, Seat
from coinche.game import TEAM_OF, Game


def _ten_bait_lead(game: Game, seat: Seat) -> Card | None:
    """Lead low from a protected Ace-King length to tempt out an unseen Ten."""
    assert game.round_state is not None
    round_state = game.round_state
    trump = round_state.trump
    contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
    if (
        trump is None
        or round_state.current_trick
        or contract is None
        or contract["team"] != TEAM_OF[seat]
        or _opponents_may_hold_trump(game, seat, trump)
    ):
        return None

    hand = game.get_hand(seat)
    candidates: list[tuple[list[Card], Card]] = []
    for suit in rules.ALLOWED_TRUMPS:
        if suit == trump:
            continue
        suit_cards = [card for card in hand if card.suit == suit]
        ranks = {card.rank for card in suit_cards}
        ten = Card("10", suit)
        if not {"A", "R"}.issubset(ranks) or ten in hand or _has_been_played(ten, round_state):
            continue
        low_cards = [card for card in suit_cards if card.rank not in {"A", "R"}]
        if low_cards:
            lowest_card = min(
                low_cards,
                key=lambda card: (rules.card_points(card, trump), rules.NONTRUMP_ORDER.index(card.rank)),
            )
            candidates.append(
                (
                    suit_cards,
                    lowest_card,
                )
            )

    if not candidates:
        return None
    return max(candidates, key=lambda candidate: (len(candidate[0]), candidate[1].suit))[1]


class MaestroBot(DefaultBot):
    """Bid boldly and use protected side-suit lengths to draw out a Ten."""

    def choose_bid(self, game: Game, seat: Seat) -> dict:
        action = super().choose_bid(game, seat)
        if action.get("action") != "bid" or not isinstance(action.get("points"), int):
            return action

        trump = action["trump"]
        hand = game.get_hand(seat)
        trump_ranks = {card.rank for card in hand if card.suit == trump}
        trump_count = sum(card.suit == trump for card in hand)
        side_aces = sum(card.rank == "A" and card.suit != trump for card in hand)
        has_34 = {"V", "9"}.issubset(trump_ranks)
        has_controlled_ace = has_34 and side_aces > 0

        if not has_34:
            return action
        if not has_controlled_ace and trump_count < 4:
            return action

        next_points = action["points"] + rules.BID_STEP
        encore = next(
            (
                legal_action
                for legal_action in game.bid_options_for(seat)["legal_actions"]
                if legal_action["trump"] == trump and legal_action["points"] == next_points
            ),
            None,
        )
        if encore is None:
            return action
        return {
            "action": "bid",
            "trump": encore["trump"],
            "points": encore["points"],
        }

    def choose_card(self, game: Game, seat: Seat) -> Card:
        bait_lead = _ten_bait_lead(game, seat)
        if bait_lead is not None:
            return bait_lead
        return super().choose_card(game, seat)
