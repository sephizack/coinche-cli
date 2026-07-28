"""I/O-free imperfect-information player for server-controlled Coinche bots."""

from __future__ import annotations

import copy
import random

from coinche import rules
from coinche.cards import Card, Seat, build_deck
from coinche.game import PARTNER_OF, TEAM_OF, Game, RoundState

MONTE_CARLO_SAMPLES = 100

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

# How much more likely a seat that bid the winning trump is to hold each trump
# honour, relative to a neutral seat. Used only to bias imperfect-information
# determinizations from the *public* auction — never to read a real hand. The
# Valet dominates because a trump opening is almost always built around it.
_TRUMP_HONOR_BIAS = {
    "V": 6.0,
    "9": 3.0,
    "A": 2.0,
    "10": 1.4,
}
# A seat that merely raised (rather than opened) the trump gets a milder share
# of the same signal.
_SUPPORTER_BIAS_FACTOR = 0.5


def _side_aces(hand: list[Card], trump: str) -> int:
    """Count the most reliable non-trump winners available to the bot."""
    return sum(card.rank == "A" and card.suit != trump for card in hand)


def _trump_control(hand: list[Card], trump: str) -> float:
    """Fraction (0..1) of how much this hand dominates the trump suit.

    Control is what lets side aces actually cash and lets ruffs land: it rises
    with trump length and, above all, with the two boss trumps -- the Valet
    (worth twice) and the 9. A lone Valet already controls more than three low
    trumps, which is why raw trump *count* is a poor guide on its own.
    """
    trump_cards = [card for card in hand if card.suit == trump]
    trump_ranks = {card.rank for card in trump_cards}
    top = (2 if "V" in trump_ranks else 0) + (1 if "9" in trump_ranks else 0)
    return min(1.0, 0.16 * len(trump_cards) + 0.22 * top)


def _point_potential(hand: list[Card], trump: str) -> float:
    """Estimate the point-taking power of a hand for a given trump suit.

    Not a raw trump count: it sums what the hand can actually *bring in* --
    trump honours (the Valet and 9 dominate), side aces, ruffs from short
    suits, and the belote -- each discounted by how firmly the hand controls
    the trump suit, because an uncontrolled ace or ruff is easily denied.
    """
    trump_cards = [card for card in hand if card.suit == trump]
    trump_ranks = {card.rank for card in trump_cards}
    control = _trump_control(hand, trump)

    trump_points = sum(rules.TRUMP_POINTS[card.rank] for card in trump_cards)
    trump_take = trump_points * (0.55 + 0.45 * control) + control * len(trump_cards) * 6

    side_take = _side_aces(hand, trump) * 11 * (0.35 + 0.65 * control)

    spare_trumps = max(0, len(trump_cards) - 2)
    short_side_suits = sum(
        1 for suit in rules.ALLOWED_TRUMPS if suit != trump and 0 < sum(card.suit == suit for card in hand) <= 1
    )
    ruff_take = min(spare_trumps, short_side_suits) * 6

    belote = rules.BELOTE_BONUS if {"R", "D"}.issubset(trump_ranks) else 0
    return trump_take + side_take + ruff_take + belote


def _partner_allowance(hand: list[Card], trump: str) -> float:
    """Estimate the extra points the *partner's* hidden hand will contribute.

    A bid is a pair contract, not a solo one: the partner holds eight cards too
    and will pitch into tricks the bidder controls. That help is worth the most
    exactly when the bidder controls trump and holds firm side winners -- then
    the partner's own cards cash behind them -- so the allowance scales with
    controlled side aces rather than being a flat bonus.
    """
    return min(_side_aces(hand, trump), 3) * _trump_control(hand, trump) * 8


def _opening_ceiling(hand: list[Card], trump: str) -> int | str | None:
    """Return a conservative opening ceiling for a proposed trump suit.

    Driven by estimated point potential rather than a fixed trump-count
    pattern: the Valet, the 9 and side aces are what a bid promises, so a hand
    with three trumps headed by the Valet-9 plus outside aces outbids a hand
    with four low trumps and nothing else. The estimate is a *pair* contract --
    the partner's likely contribution is added -- not just what this hand takes.
    """
    if len(hand) == sum(card.suit == trump for card in hand):
        return rules.CAPOT
    potential = _point_potential(hand, trump) + _partner_allowance(hand, trump)
    ceiling = rules.round_to_nearest_ten(int(17 + potential * 0.91))
    if ceiling < rules.BID_MIN:
        return None
    return min(rules.BID_MAX, ceiling)


def _support_ceiling(hand: list[Card], trump: str, current_points: int) -> int | None:
    """Return the highest safe support bid from the partner's announced strength.

    An opening of 80 promises a playable trump suit. A bot holding the Valet
    or 9 in that suit therefore completes the partner's signal with a trump
    master and can promise one additional trick, even with fewer than three
    trumps itself.
    """
    trump_cards = [card for card in hand if card.suit == trump]
    trump_ranks = {card.rank for card in trump_cards}
    trump_count = len(trump_cards)

    if current_points == rules.BID_MIN:
        has_complementary_master = bool({"V", "9"} & trump_ranks)
        if trump_count >= 3 or has_complementary_master:
            return current_points + rules.BID_STEP
        return None
    if current_points == 90:
        if (
            "V" in trump_ranks
            or trump_count >= 4
            or ("9" in trump_ranks and trump_count >= 2)
            or ("A" in trump_ranks and trump_count >= 3)
        ):
            return current_points + rules.BID_STEP
        return None
    if trump_count >= 3 and {"V", "9"}.issubset(trump_ranks):
        extra_tricks = 1 + min(_side_aces(hand, trump), 1)
        return current_points + extra_tricks * rules.BID_STEP
    return None


def _legal_bids_up_to(options: dict, trump: str, ceiling: int | str) -> list[dict]:
    """Return legal bids for one trump suit that do not exceed a safe ceiling."""
    if ceiling == rules.CAPOT:
        return [action for action in options["legal_actions"] if action["trump"] == trump]
    return [
        action
        for action in options["legal_actions"]
        if action["trump"] == trump and action["points"] != rules.CAPOT and action["points"] <= ceiling
    ]


def _ceiling_value(ceiling: int | str | None) -> int:
    """Normalize a bid ceiling for comparing candidate trump suits."""
    if ceiling == rules.CAPOT:
        return rules.CAPOT_ANNOUNCE
    return ceiling or 0


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
    """Choose a legal, conservative auction action from the bot's own hand."""
    options = game.bid_options_for(seat)
    hand = game.get_hand(seat)
    strengths = {trump: _hand_strength(hand, trump) for trump in rules.ALLOWED_TRUMPS}
    opening_ceilings = {trump: _opening_ceiling(hand, trump) for trump in rules.ALLOWED_TRUMPS}
    best_trump = max(
        rules.ALLOWED_TRUMPS,
        key=lambda trump: (_ceiling_value(opening_ceilings[trump]), strengths[trump]),
    )
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

    if current is not None and current["team"] == TEAM_OF[seat]:
        if current["points"] == rules.CAPOT:
            return {"action": "pass"}
        maximum = _support_ceiling(hand, current["trump"], current["points"])
        if maximum is None:
            return {"action": "pass"}
        legal_for_suit = _legal_bids_up_to(options, current["trump"], maximum)
    else:
        maximum = opening_ceilings[best_trump]
        legal_for_suit = [] if maximum is None else _legal_bids_up_to(options, best_trump, maximum)
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


def _best_discard(cards: list[Card], hand: list[Card], trump: str) -> Card:
    """Pick the least useful card to throw away.

    Value comes first (never sacrifice a point card to shape the hand), then
    keeping trump, then — as the tie-break that used to just take the lowest
    rank — shortening the shortest non-trump side suit so the bot can create a
    ruff sooner. A singleton side card goes before one of a longer suit.
    """
    lengths = {suit: sum(other.suit == suit for other in hand) for suit in rules.ALLOWED_TRUMPS}

    def key(card: Card) -> tuple[int, int, int, int]:
        points, is_trump, strength = _discard_key(card, trump)
        side_length = 99 if card.suit == trump else lengths[card.suit]
        return (points, is_trump, side_length, strength)

    return min(cards, key=key)


def _is_master(card: Card, hand: list[Card], game: Game, trump: str) -> bool:
    assert game.round_state is not None
    order = rules.TRUMP_ORDER if card.suit == trump else rules.NONTRUMP_ORDER
    stronger_ranks = set(order[order.index(card.rank) + 1 :])
    known_cards = set(hand)
    for trick in game.round_state.trick_history:
        known_cards.update(played for _, played in trick["trick"])
    known_cards.update(played for _, played in game.round_state.current_trick)
    return all(Card(rank, card.suit) in known_cards for rank in stronger_ranks)


def _outstanding_trumps(game: Game, seat: Seat, trump: str) -> int:
    """Count trumps still unaccounted for from this seat's viewpoint.

    A trump is "seen" if it's in the acting seat's own hand or has already been
    played to any trick. The rest are outstanding in the three hidden hands
    (partner or opponents).
    """
    assert game.round_state is not None
    rs = game.round_state
    seen = sum(1 for card in game.get_hand(seat) if card.suit == trump)
    for trick in rs.trick_history:
        seen += sum(1 for _, card in trick["trick"] if card.suit == trump)
    seen += sum(1 for _, card in rs.current_trick if card.suit == trump)
    return len(rules.TRUMP_ORDER) - seen


def _opponents_may_hold_trump(game: Game, seat: Seat, trump: str) -> bool:
    """True if an opponent could still hold a trump (used to decide whether to pull).

    Requires at least one outstanding trump AND at least one opponent not yet
    provably void of trump. Once every opponent is known void (or no trump is
    left out), pulling is pointless and we stop.
    """
    assert game.round_state is not None
    if _outstanding_trumps(game, seat, trump) <= 0:
        return False
    voids = _known_void_suits(game.round_state)
    return any(other != seat and TEAM_OF[other] != TEAM_OF[seat] and trump not in voids[other] for other in Seat)


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
        contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
        is_taker = contract is not None and contract["seat"] == seat
        is_declarer = contract is not None and contract["team"] == TEAM_OF[seat]

        # Draw trumps first, cash side winners second. While an opponent may
        # still hold a trump, the declaring team leads trump BEFORE cashing side
        # masters -- cashing an ace early only to be ruffed later is the classic
        # "dumb bot" move. Pulling early strips the opponents of ruffers so the
        # side aces run safely once trumps are gone.
        if is_declarer and _opponents_may_hold_trump(game, seat, trump):
            # A master trump wins outright, so either declarer may lead one --
            # a partner's master lead never forces the taker to overtrump their
            # own side, and the next-highest trump becomes master afterwards, so
            # successive leads keep pulling round by round.
            master_trumps = [card for card in masters if card.suit == trump]
            if master_trumps:
                return max(master_trumps, key=lambda card: _card_strength(card, trump))
            # No master trump left, but pulling is still the taker's job: lead
            # the top trump to force out the opponents' higher ones. The partner
            # stays out here -- a *non-master* lead would make the taker overtrump
            # their own partner, burning two masters in one trick.
            if is_taker:
                trumps = [card for card in legal_cards if card.suit == trump]
                if trumps:
                    return max(trumps, key=lambda card: _card_strength(card, trump))

        if masters:
            return max(masters, key=lambda card: (rules.card_points(card, trump), _card_strength(card, trump)))

        # Opponents are out of trump (or this seat can't usefully pull): the
        # taker may still lead a bare trump to squeeze the last ones out.
        if is_taker:
            trumps = [card for card in legal_cards if card.suit == trump]
            if trumps:
                return max(trumps, key=lambda card: _card_strength(card, trump))
        return _best_discard(legal_cards, hand, trump)

    hand = game.get_hand(seat)
    led_suit = trick[0][1].suit
    current_winner = rules.trick_winner(trick, trump, led_suit)
    if current_winner == PARTNER_OF[seat]:
        if len(trick) == 3:
            return max(legal_cards, key=lambda card: (rules.card_points(card, trump), -_card_strength(card, trump)))
        return _best_discard(legal_cards, hand, trump)

    winners = [card for card in legal_cards if rules.trick_winner([*trick, (seat, card)], trump, led_suit) == seat]
    if winners:
        return min(winners, key=lambda card: (_card_strength(card, trump), rules.card_points(card, trump)))
    return _best_discard(legal_cards, hand, trump)


def _played_cards_by_seat(round_state: RoundState) -> dict[Seat, list[Card]]:
    played: dict[Seat, list[Card]] = {seat: [] for seat in Seat}
    for trick in round_state.trick_history:
        for seat, card in trick["trick"]:
            played[seat].append(card)
    for seat, card in round_state.current_trick:
        played[seat].append(card)
    return played


def _known_void_suits(round_state: RoundState) -> dict[Seat, set[str]]:
    """Infer only *certain* voids from public play.

    Two deductions, both airtight (a player can never hold a suit we mark):

    1. Failing to follow the led suit proves the player is void of it.
    2. Discarding (playing neither the led non-trump suit nor a trump) while
       the partner is *not* yet master and *no trump has been played in the
       trick yet* proves the player is void of trump: the rules would have
       forced a cut otherwise. When a trump is already down the player may
       legally "pisser" a low card instead of under-trumping, so a discard in
       that case says nothing about their trumps and is deliberately ignored.
    """
    trump = round_state.trump
    voids: dict[Seat, set[str]] = {seat: set() for seat in Seat}
    tricks = [trick["trick"] for trick in round_state.trick_history]
    if round_state.current_trick:
        tricks.append(round_state.current_trick)
    for trick in tricks:
        led_suit = trick[0][1].suit
        for index, (seat, card) in enumerate(trick[1:], start=1):
            if card.suit != led_suit:
                voids[seat].add(led_suit)
                if (
                    trump is not None
                    and led_suit != trump
                    and card.suit != trump
                    and not any(played.suit == trump for _, played in trick[:index])
                    and rules.trick_winner(trick[:index], trump, led_suit) != PARTNER_OF[seat]
                ):
                    voids[seat].add(trump)
    return voids


def _trump_honor_bias(game: Game) -> dict[Seat, float]:
    """Per-seat multiplier for holding a trump honour, read from the auction.

    The seat that won the contract announced the trump suit, so it is far more
    likely to hold its top honours; any partner who *raised* the same suit gets
    a milder share of the same signal. Everyone else stays neutral (1.0). Built
    only from the public bid history — never from a real hand.
    """
    bias: dict[Seat, float] = {seat: 1.0 for seat in Seat}
    contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
    if contract is None:
        return bias
    trump = contract["trump"]
    taker = contract["seat"]
    bias[taker] = 2.0
    if game.bid_state is not None:
        for entry in game.bid_state.history:
            if entry.get("action") == "bid" and entry.get("trump") == trump and entry["seat"] != taker:
                bias[entry["seat"]] = max(bias[entry["seat"]], 1.0 + _SUPPORTER_BIAS_FACTOR)
    return bias


def _card_seat_weight(card: Card, seat: Seat, trump: str | None, bias: dict[Seat, float]) -> float:
    """Weight for dealing `card` to `seat`: >1 only for trump honours to biased seats."""
    if trump is None or card.suit != trump or card.rank not in _TRUMP_HONOR_BIAS:
        return 1.0
    honour_pull = _TRUMP_HONOR_BIAS[card.rank]
    return 1.0 + (honour_pull - 1.0) * (bias[seat] - 1.0)


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
    trump = round_state.trump
    bias = _trump_honor_bias(game)
    rng = random.Random(_public_seed(game, seat))
    samples: list[dict[Seat, list[Card]]] = []
    for _ in range(sample_count):
        assignment = _weighted_deal(unseen, opponents, counts, voids, trump, bias, rng)
        if assignment is None:
            assignment = _fallback_deal(unseen, opponents, counts, rng)
        samples.append(assignment)
    return samples


def _weighted_deal(
    unseen: list[Card],
    opponents: list[Seat],
    counts: dict[Seat, int],
    voids: dict[Seat, set[str]],
    trump: str | None,
    bias: dict[Seat, float],
    rng: random.Random,
) -> dict[Seat, list[Card]] | None:
    """Deal cards one at a time, each to a random eligible seat weighted by trump-honour bias.

    Constrained-scarce cards (a suit only a few seats can legally hold) are
    placed first so a greedy honour bias cannot paint a void seat into a corner
    and force a failed deal. Returns None if no legal placement exists for some
    card, letting the caller fall back to an unbiased split.
    """
    remaining = {other: counts[other] for other in opponents}

    def eligible(card: Card) -> list[Seat]:
        return [other for other in opponents if remaining[other] > 0 and card.suit not in voids[other]]

    order = list(unseen)
    rng.shuffle(order)
    # Fewest eligible seats first: hardest-to-place cards claim a seat before
    # the honour bias skews the easy ones.
    order.sort(key=lambda card: len(eligible(card)))

    hands: dict[Seat, list[Card]] = {other: [] for other in opponents}
    for card in order:
        takers = [other for other in eligible(card) if remaining[other] > 0]
        if not takers:
            return None
        weights = [_card_seat_weight(card, other, trump, bias) for other in takers]
        chosen = _weighted_choice(takers, weights, rng)
        hands[chosen].append(card)
        remaining[chosen] -= 1
    return hands


def _weighted_choice(seats: list[Seat], weights: list[float], rng: random.Random) -> Seat:
    total = sum(weights)
    threshold = rng.random() * total
    cumulative = 0.0
    for seat, weight in zip(seats, weights):
        cumulative += weight
        if threshold < cumulative:
            return seat
    return seats[-1]


def _fallback_deal(
    unseen: list[Card],
    opponents: list[Seat],
    counts: dict[Seat, int],
    rng: random.Random,
) -> dict[Seat, list[Card]]:
    """Unbiased split ignoring voids; used only when a constrained deal can't be built."""
    shuffled = list(unseen)
    rng.shuffle(shuffled)
    assignment: dict[Seat, list[Card]] = {}
    offset = 0
    for other in opponents:
        assignment[other] = list(shuffled[offset : offset + counts[other]])
        offset += counts[other]
    return assignment


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
