"""An assertive, information-safe Coinche strategy for AI challenges."""

from __future__ import annotations

from coinche import rules
from coinche.bot_types.base import BotType
from coinche.bot_types.default import DefaultBot
from coinche.cards import Card, Seat
from coinche.game import PARTNER_OF, TEAM_OF, Game


class ClocloBot(BotType):
    """An independent aggressive strategy that uses only its hand and public play."""

    def __init__(self, sample_count: int) -> None:
        self.sample_count = sample_count
        self._smart = DefaultBot(sample_count)

    def choose_bid(self, game: Game, seat: Seat) -> dict:
        options = game.bid_options_for(seat)
        hand = game.get_hand(seat)
        ceilings = {trump: self._bid_ceiling(hand, trump) for trump in rules.ALLOWED_TRUMPS}
        best_trump = max(rules.ALLOWED_TRUMPS, key=lambda trump: self._ceiling_rank(ceilings[trump]))
        best_ceiling = ceilings[best_trump]
        smart_action = self._smart.choose_bid(game, seat)
        current_bid = options["current_highest_bid"]

        # The smart strategy carries the public auction context: partner support,
        # Coinche thresholds, and Surcoinche decisions. Cloclo only raises that
        # decision when its unusually strong private holding justifies more.
        if smart_action["action"] in {"coinche", "surcoinche"}:
            return smart_action

        if current_bid is not None and current_bid["trump"] == best_trump:
            current_points = current_bid["points"]
            if options["can_surcoinche"] and current_bid["team"] == TEAM_OF[seat] and best_ceiling == rules.CAPOT:
                return {"action": "surcoinche"}
            if (
                options["can_coinche"]
                and current_bid["team"] != TEAM_OF[seat]
                and isinstance(current_points, int)
                and self._ceiling_rank(best_ceiling) >= current_points + 20
            ):
                return {"action": "coinche"}

        if best_ceiling is None:
            return smart_action

        legal_bids = [action for action in options["legal_actions"] if action["trump"] == best_trump]
        affordable_bids = [
            action for action in legal_bids if self._ceiling_rank(action["points"]) <= self._ceiling_rank(best_ceiling)
        ]
        if not affordable_bids:
            return smart_action
        aggressive_action = max(affordable_bids, key=lambda action: self._ceiling_rank(action["points"]))
        if aggressive_action["points"] > self._ceiling_rank(smart_action.get("points")):
            return aggressive_action
        return smart_action

    def choose_card(self, game: Game, seat: Seat) -> Card:
        assert game.round_state is not None
        options = game.play_options_for(seat)
        legal_cards: list[Card] = options["legal_cards"]
        trump = options["trump"]
        trick = game.round_state.current_trick
        assert trump is not None
        if trick and rules.trick_winner(trick, trump) == PARTNER_OF[seat]:
            return min(legal_cards, key=lambda card: self._discard_key(card, trump))
        return self._smart.choose_card(game, seat)

    @staticmethod
    def _bid_ceiling(hand: list[Card], trump: str) -> int | str | None:
        trump_cards = [card for card in hand if card.suit == trump]
        if len(trump_cards) == len(hand):
            return rules.CAPOT
        if len(trump_cards) < 3:
            return None

        trump_ranks = {card.rank for card in trump_cards}
        side_aces = sum(card.rank == "A" and card.suit != trump for card in hand)
        side_tens = sum(card.rank == "10" and card.suit != trump for card in hand)
        strength = sum(rules.card_points(card, trump) for card in trump_cards)
        strength += side_aces * 8 + side_tens * 2 + (len(trump_cards) - 2) * 3
        strength += 8 if "V" in trump_ranks else 0
        strength += 6 if "9" in trump_ranks else 0

        if strength < 39:
            return None
        return min(rules.BID_MAX, rules.BID_MIN + ((strength - 39) // 6) * rules.BID_STEP)

    @staticmethod
    def _ceiling_rank(points: int | str | None) -> int:
        if points == rules.CAPOT:
            return rules.BID_MAX + rules.BID_STEP
        return points if isinstance(points, int) else -1

    @staticmethod
    def _discard_key(card: Card, trump: str) -> tuple[bool, int, int, str]:
        order = rules.TRUMP_ORDER if card.suit == trump else rules.NONTRUMP_ORDER
        return (card.suit == trump, rules.card_points(card, trump), order.index(card.rank), card.suit)
