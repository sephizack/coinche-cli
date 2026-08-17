"""An assertive, information-safe Coinche strategy for AI challenges."""

from __future__ import annotations

from coinche import rules
from coinche.bot_types.base import BotType
from coinche.cards import Card, Seat
from coinche.game import PARTNER_OF, TEAM_OF, Game


class ClocloBot(BotType):
    """An independent aggressive strategy that uses only its hand and public play."""

    def __init__(self, sample_count: int) -> None:
        self.sample_count = sample_count

    def choose_bid(self, game: Game, seat: Seat) -> dict:
        options = game.bid_options_for(seat)
        hand = game.get_hand(seat)
        ceilings = {trump: self._bid_ceiling(hand, trump) for trump in rules.ALLOWED_TRUMPS}
        best_trump = max(rules.ALLOWED_TRUMPS, key=lambda trump: self._ceiling_rank(ceilings[trump]))
        best_ceiling = ceilings[best_trump]
        current_bid = options["current_highest_bid"]

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

        partner_bid = next(
            (
                bid
                for bid in reversed(options["bid_history"])
                if bid.get("action") == "bid" and bid["seat"] == PARTNER_OF[seat]
            ),
            None,
        )
        if partner_bid is not None:
            support = self._support_bid(options, hand, partner_bid)
            if support is not None:
                return support

        if best_ceiling is not None:
            own_bid = self._highest_affordable_bid(options, best_trump, best_ceiling)
            if own_bid is not None:
                return own_bid
        return {"action": "pass"}

    def choose_card(self, game: Game, seat: Seat) -> Card:
        assert game.round_state is not None
        options = game.play_options_for(seat)
        legal_cards: list[Card] = options["legal_cards"]
        trump = options["trump"]
        trick = game.round_state.current_trick
        assert trump is not None
        if trick and rules.trick_winner(trick, trump) == PARTNER_OF[seat]:
            return min(legal_cards, key=lambda card: self._discard_key(card, trump))

        if trick:
            led_suit = trick[0][1].suit
            winners = [
                card for card in legal_cards if rules.trick_winner([*trick, (seat, card)], trump, led_suit) == seat
            ]
            if winners:
                return min(winners, key=lambda card: self._winning_cost(card, trump))
            return min(legal_cards, key=lambda card: self._discard_key(card, trump))

        hand = game.get_hand(seat)
        masters = [card for card in legal_cards if self._is_master(card, hand, game, trump)]
        contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
        is_declaring_team = contract is not None and contract["team"] == TEAM_OF[seat]
        master_trumps = [card for card in masters if card.suit == trump]
        if is_declaring_team and master_trumps:
            return max(master_trumps, key=lambda card: rules.TRUMP_ORDER.index(card.rank))
        side_masters = [card for card in masters if card.suit != trump]
        if side_masters:
            return max(side_masters, key=lambda card: rules.NONTRUMP_ORDER.index(card.rank))
        if is_declaring_team:
            trumps = [card for card in legal_cards if card.suit == trump]
            if len(trumps) >= 3:
                return max(trumps, key=lambda card: rules.TRUMP_ORDER.index(card.rank))
        return min(legal_cards, key=lambda card: self._discard_key(card, trump))

    @classmethod
    def _support_bid(cls, options: dict, hand: list[Card], partner_bid: dict) -> dict | None:
        if partner_bid["points"] == rules.CAPOT:
            return None
        trump = partner_bid["trump"]
        trump_cards = [card for card in hand if card.suit == trump]
        trump_ranks = {card.rank for card in trump_cards}
        steps = 0
        if "V" in trump_ranks or "9" in trump_ranks:
            steps += 1
        if len(trump_cards) >= 3:
            steps += 1
        steps += min(2, sum(card.rank == "A" and card.suit != trump for card in hand))
        if steps == 0:
            return None
        target = min(rules.BID_MAX, partner_bid["points"] + steps * rules.BID_STEP)
        return cls._highest_affordable_bid(options, trump, target)

    @classmethod
    def _highest_affordable_bid(cls, options: dict, trump: str, ceiling: int | str) -> dict | None:
        affordable = [
            action
            for action in options["legal_actions"]
            if action.get("action") == "bid"
            and action.get("trump") == trump
            and cls._ceiling_rank(action["points"]) <= cls._ceiling_rank(ceiling)
        ]
        return max(affordable, key=lambda action: cls._ceiling_rank(action["points"]), default=None)

    @staticmethod
    def _is_master(card: Card, hand: list[Card], game: Game, trump: str) -> bool:
        assert game.round_state is not None
        order = rules.TRUMP_ORDER if card.suit == trump else rules.NONTRUMP_ORDER
        stronger = order[order.index(card.rank) + 1 :]
        seen = set(hand)
        for completed_trick in game.round_state.trick_history:
            seen.update(played for _, played in completed_trick["trick"])
        seen.update(played for _, played in game.round_state.current_trick)
        return all(Card(rank, card.suit) in seen for rank in stronger)

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

    @staticmethod
    def _winning_cost(card: Card, trump: str) -> tuple[bool, int, int, str]:
        order = rules.TRUMP_ORDER if card.suit == trump else rules.NONTRUMP_ORDER
        return (card.suit == trump, order.index(card.rank), rules.card_points(card, trump), card.suit)
