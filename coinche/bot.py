"""I/O-free heuristic player for server-controlled Coinche bots."""

from __future__ import annotations

from coinche import rules
from coinche.cards import Card, Seat
from coinche.game import PARTNER_OF, TEAM_OF, Game

_TRUMP_HAND_WEIGHTS = {
    "V": 30,
    "9": 22,
    "A": 14,
    "10": 10,
    "R": 6,
    "D": 5,
    "8": 1,
    "7": 1,
}
_NONTRUMP_HAND_WEIGHTS = {
    "A": 11,
    "10": 6,
    "R": 3,
    "D": 2,
    "V": 1,
    "9": 0,
    "8": 0,
    "7": 0,
}


def _hand_strength(hand: list[Card], trump: str) -> int:
    trump_cards = [card for card in hand if card.suit == trump]
    score = sum((_TRUMP_HAND_WEIGHTS if card.suit == trump else _NONTRUMP_HAND_WEIGHTS)[card.rank] for card in hand)
    score += max(0, len(trump_cards) - 2) * 7
    trump_ranks = {card.rank for card in trump_cards}
    if {"R", "D"}.issubset(trump_ranks):
        score += 8

    for suit in rules.ALLOWED_TRUMPS:
        if suit == trump:
            continue
        suit_length = sum(card.suit == suit for card in hand)
        if suit_length == 0:
            score += 4
        elif suit_length == 1:
            score += 2
    return score


def choose_bid(game: Game, seat: Seat) -> dict:
    """Choose a legal auction action from hand strength and the standing bid."""
    options = game.bid_options_for(seat)
    hand = game.get_hand(seat)
    strengths = {trump: _hand_strength(hand, trump) for trump in rules.ALLOWED_TRUMPS}
    best_trump = max(rules.ALLOWED_TRUMPS, key=lambda trump: strengths[trump])
    best_strength = strengths[best_trump]
    current = options["current_highest_bid"]

    if options["can_surcoinche"] and current is not None and strengths[current["trump"]] >= 78:
        return {"action": "surcoinche"}
    if (
        options["can_coinche"]
        and current is not None
        and current["points"] != rules.CAPOT
        and current["points"] <= 100
        and strengths[current["trump"]] >= 72
    ):
        return {"action": "coinche"}

    maximum = 70 + max(0, (best_strength - 37) // 7) * 10
    maximum = min(maximum, 130)
    if best_strength >= 105:
        maximum = rules.CAPOT_ANNOUNCE

    if current is not None and current["team"] == TEAM_OF[seat]:
        current_points = rules.CAPOT_ANNOUNCE if current["points"] == rules.CAPOT else current["points"]
        if maximum < current_points + 20:
            return {"action": "pass"}

    legal_for_suit = [
        action
        for action in options["legal_actions"]
        if action["trump"] == best_trump
        and (rules.CAPOT_ANNOUNCE if action["points"] == rules.CAPOT else action["points"]) <= maximum
    ]
    if legal_for_suit:
        choice = legal_for_suit[-1]
        return {"action": "bid", "trump": choice["trump"], "points": choice["points"]}

    bid_state = game.bid_state
    if current is None and bid_state is not None and bid_state.pass_streak == 3:
        forced = next(action for action in options["legal_actions"] if action["trump"] == best_trump)
        return {"action": "bid", "trump": forced["trump"], "points": forced["points"]}
    return {"action": "pass"}


def _card_strength(card: Card, trump: str) -> int:
    order = rules.TRUMP_ORDER if card.suit == trump else rules.NONTRUMP_ORDER
    return order.index(card.rank)


def _discard_key(card: Card, trump: str) -> tuple[int, int, int]:
    return (rules.card_points(card, trump), card.suit == trump, _card_strength(card, trump))


def _is_master(card: Card, hand: list[Card], game: Game, trump: str) -> bool:
    assert game.round_state is not None
    order = rules.TRUMP_ORDER if card.suit == trump else rules.NONTRUMP_ORDER
    stronger_ranks = set(order[order.index(card.rank) + 1 :])
    known_cards = set(hand)
    for trick in game.round_state.trick_history:
        known_cards.update(played for _, played in trick["trick"])
    known_cards.update(played for _, played in game.round_state.current_trick)
    return all(Card(rank, card.suit) in known_cards for rank in stronger_ranks)


def choose_card(game: Game, seat: Seat) -> Card:
    """Choose a legal card, preserving winners and spending the cheapest card that wins."""
    assert game.round_state is not None
    options = game.play_options_for(seat)
    legal_cards: list[Card] = options["legal_cards"]
    trick = game.round_state.current_trick
    trump = game.round_state.trump
    assert legal_cards and trump is not None

    if not trick:
        hand = game.get_hand(seat)
        masters = [card for card in legal_cards if _is_master(card, hand, game, trump)]
        if masters:
            return max(masters, key=lambda card: (rules.card_points(card, trump), _card_strength(card, trump)))

        contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
        if contract is not None and contract["team"] == TEAM_OF[seat]:
            trumps = [card for card in legal_cards if card.suit == trump]
            if trumps:
                return max(trumps, key=lambda card: _card_strength(card, trump))
        return min(legal_cards, key=lambda card: _discard_key(card, trump))

    led_suit = trick[0][1].suit
    current_winner = rules.trick_winner(trick, trump, led_suit)
    if current_winner == PARTNER_OF[seat]:
        return min(legal_cards, key=lambda card: _discard_key(card, trump))

    winners = [card for card in legal_cards if rules.trick_winner([*trick, (seat, card)], trump, led_suit) == seat]
    if winners:
        return min(winners, key=lambda card: (_card_strength(card, trump), rules.card_points(card, trump)))
    return min(legal_cards, key=lambda card: _discard_key(card, trump))
