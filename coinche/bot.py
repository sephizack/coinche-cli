"""I/O-free imperfect-information player for server-controlled Coinche bots."""

from __future__ import annotations

import copy
import random

from coinche import rules
from coinche.cards import Card, Seat, build_deck
from coinche.game import PARTNER_OF, TEAM_OF, Game, RoundState

MONTE_CARLO_SAMPLES = 24

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


def _choose_tactical_card(game: Game, seat: Seat) -> Card:
    """Fast rollout policy using only the acting player's hand and public cards."""
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
        if len(trick) == 3:
            return max(legal_cards, key=lambda card: (rules.card_points(card, trump), -_card_strength(card, trump)))
        return min(legal_cards, key=lambda card: _discard_key(card, trump))

    winners = [card for card in legal_cards if rules.trick_winner([*trick, (seat, card)], trump, led_suit) == seat]
    if winners:
        return min(winners, key=lambda card: (_card_strength(card, trump), rules.card_points(card, trump)))
    return min(legal_cards, key=lambda card: _discard_key(card, trump))


def _played_cards_by_seat(round_state: RoundState) -> dict[Seat, list[Card]]:
    played: dict[Seat, list[Card]] = {seat: [] for seat in Seat}
    for trick in round_state.trick_history:
        for seat, card in trick["trick"]:
            played[seat].append(card)
    for seat, card in round_state.current_trick:
        played[seat].append(card)
    return played


def _known_void_suits(round_state: RoundState) -> dict[Seat, set[str]]:
    """Infer only certain voids: a player who failed to follow a led suit cannot hold it later."""
    voids: dict[Seat, set[str]] = {seat: set() for seat in Seat}
    tricks = [trick["trick"] for trick in round_state.trick_history]
    if round_state.current_trick:
        tricks.append(round_state.current_trick)
    for trick in tricks:
        led_suit = trick[0][1].suit
        for seat, card in trick[1:]:
            if card.suit != led_suit:
                voids[seat].add(led_suit)
    return voids


def _public_seed(game: Game, seat: Seat) -> str:
    """Stable seed built without any opponent hand, making equal information sets choose equally."""
    assert game.round_state is not None
    round_state = game.round_state
    history = [
        (trick["winner_seat"].value, tuple((played_seat.value, str(card)) for played_seat, card in trick["trick"]))
        for trick in round_state.trick_history
    ]
    current = tuple((played_seat.value, str(card)) for played_seat, card in round_state.current_trick)
    contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
    contract_key = (
        None if contract is None else (contract["team"], contract["seat"].value, contract["trump"], contract["points"])
    )
    return repr(
        (
            game.round_number,
            seat.value,
            tuple(sorted(str(card) for card in game.get_hand(seat))),
            tuple(history),
            current,
            contract_key,
            tuple(sorted(round_state.captured_points.items())),
        )
    )


def _sample_hidden_hands(game: Game, seat: Seat, sample_count: int) -> list[dict[Seat, list[Card]]]:
    """Deal unseen cards into plausible hands without reading the real hidden hands."""
    assert game.round_state is not None
    round_state = game.round_state
    played = _played_cards_by_seat(round_state)
    known = set(game.get_hand(seat))
    for cards in played.values():
        known.update(cards)
    unseen = [card for card in build_deck() if card not in known]

    opponents = [other for other in Seat if other != seat]
    counts = {other: 8 - len(played[other]) for other in opponents}
    if sum(counts.values()) != len(unseen):
        return []

    voids = _known_void_suits(round_state)
    rng = random.Random(_public_seed(game, seat))
    samples: list[dict[Seat, list[Card]]] = []
    for _ in range(sample_count):
        assignment: dict[Seat, list[Card]] | None = None
        shuffled = list(unseen)
        for _attempt in range(80):
            rng.shuffle(shuffled)
            offset = 0
            candidate: dict[Seat, list[Card]] = {}
            valid = True
            for other in opponents:
                hand = list(shuffled[offset : offset + counts[other]])
                offset += counts[other]
                if any(card.suit in voids[other] for card in hand):
                    valid = False
                    break
                candidate[other] = hand
            if valid:
                assignment = candidate
                break
        if assignment is None:
            offset = 0
            assignment = {}
            for other in opponents:
                assignment[other] = list(shuffled[offset : offset + counts[other]])
                offset += counts[other]
        samples.append(assignment)
    return samples


def _apply_determinization(game: Game, seat: Seat, hidden_hands: dict[Seat, list[Card]]) -> None:
    assert game.round_state is not None
    round_state = game.round_state
    played = _played_cards_by_seat(round_state)
    for other, hand in hidden_hands.items():
        round_state.hands[other] = list(hand)

    round_state.dealt_hands = {other: [*round_state.hands[other], *played[other]] for other in Seat}
    if round_state.belote_announced == 0:
        round_state.belote_holder = None
        round_state.belote_seat = None
        assert round_state.trump is not None
        for other, hand in round_state.dealt_hands.items():
            ranks = {card.rank for card in hand if card.suit == round_state.trump}
            if {"R", "D"}.issubset(ranks):
                round_state.belote_holder = TEAM_OF[other]
                round_state.belote_seat = other
                break


def _rollout_score(game: Game, seat: Seat, card: Card, hidden_hands: dict[Seat, list[Card]]) -> int:
    simulation = copy.deepcopy(game)
    _apply_determinization(simulation, seat, hidden_hands)
    result = simulation.submit_card(seat, card)

    for _ in range(31):
        if result.get("round_complete"):
            break
        actor = simulation.next_to_act
        result = simulation.submit_card(actor, _choose_tactical_card(simulation, actor))
    if not result.get("round_complete"):
        raise RuntimeError("Bot rollout did not complete the round")

    team = TEAM_OF[seat]
    opponents = "EW" if team == "NS" else "NS"
    round_score = result["round_score"]
    return round_score[team]["total"] - round_score[opponents]["total"]


def choose_card(game: Game, seat: Seat) -> Card:
    """Choose the legal card with the best average result across plausible hidden deals.

    Determinizations are built from this bot's hand and public play history only.
    The real hands stored by the authoritative server are deliberately ignored.
    """
    options = game.play_options_for(seat)
    legal_cards: list[Card] = options["legal_cards"]
    assert legal_cards
    if len(legal_cards) == 1:
        return legal_cards[0]

    samples = _sample_hidden_hands(game, seat, MONTE_CARLO_SAMPLES)
    if not samples:
        return _choose_tactical_card(game, seat)

    scores = {card: 0 for card in legal_cards}
    for hidden_hands in samples:
        for card in legal_cards:
            scores[card] += _rollout_score(game, seat, card, hidden_hands)

    tactical = _choose_tactical_card(game, seat)
    return max(
        legal_cards,
        key=lambda card: (
            scores[card],
            card == tactical,
            tuple(-value for value in _discard_key(card, options["trump"])),
        ),
    )
