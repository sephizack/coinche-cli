"""I/O-free imperfect-information player for server-controlled Coinche bots."""

from __future__ import annotations

import copy
import random

from coinche import rules
from coinche.cards import Card, Seat, build_deck
from coinche.game import PARTNER_OF, TEAM_OF, Game, RoundState

# Number of imperfect-information determinizations `choose_card` averages over.
# The server configures this explicit runtime value through `--bot-samples`.
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
_AUCTION_TRUMP_SIGNALS = {
    "V": 1.60,
    "9": 1.25,
    "A": 0.85,
    "10": 0.70,
    "R": 0.45,
    "D": 0.35,
    "8": 0.15,
    "7": 0.10,
}


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


def _support_ceiling(hand: list[Card], trump: str, current_points: int, has_opponents_bid_before: bool) -> int | None:
    """Return the highest safe support bid from the partner's announced strength.

    An opening of 80 promises a playable trump suit. A bot holding the Valet
    or 9 in that suit therefore completes the partner's signal with a trump
    master and can promise one additional trick, even with fewer than three
    trumps itself.
    """
    trump_cards = [card for card in hand if card.suit == trump]
    trump_ranks = {card.rank for card in trump_cards}
    trump_count = len(trump_cards)

    partner_looking_for_34 = current_points == rules.BID_MIN or (
        current_points == rules.BID_MIN + rules.BID_STEP and has_opponents_bid_before
    )
    if partner_looking_for_34:
        if "V" in trump_ranks or "9" in trump_ranks:
            return current_points + rules.BID_STEP * 2
    else:
        additional_steps = _side_aces(hand, trump)
        if trump_count >= 3:
            additional_steps += 1
        if "V" in trump_ranks or "9" in trump_ranks:
            additional_steps += 2
        if {"R", "D"}.issubset(trump_ranks):
            additional_steps += 1
        if additional_steps > 0:
            return current_points + additional_steps * rules.BID_STEP
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
    return ceiling if isinstance(ceiling, int) else 0


def _forced_opener_trump(hand: list[Card]) -> str | None:
    """Find a trump suit where the hand has V + at least 2 other trumps + a side Ace.

    This is a last-resort opener: when nobody has bid and the normal
    evaluation decides to pass, the bot checks whether its hand is strong
    enough to try an 80 anyway.  The Valet plus two more trumps give
    reasonable trump control, and a side Ace promises at least one
    additional trick.
    """
    best_trump: str | None = None
    best_count = 0
    for trump in rules.ALLOWED_TRUMPS:
        trump_cards = [card for card in hand if card.suit == trump]
        trump_ranks = {card.rank for card in trump_cards}
        if "V" not in trump_ranks:
            continue
        if len(trump_cards) < 3:
            continue
        side_aces = sum(1 for card in hand if card.rank == "A" and card.suit != trump)
        if side_aces < 1:
            continue
        score = len(trump_cards) + side_aces
        if score > best_count:
            best_count = score
            best_trump = trump
    return best_trump


def _hand_strength(hand: list[Card], trump: str) -> int:
    trump_cards = [card for card in hand if card.suit == trump]
    score = sum((_TRUMP_HAND_WEIGHTS if card.suit == trump else _NONTRUMP_HAND_WEIGHTS)[card.rank] for card in hand)
    score += max(0, len(trump_cards) - 2) * 7
    score += _side_aces(hand, trump) * 6
    trump_ranks = {card.rank for card in trump_cards}
    if {"R", "D"}.issubset(trump_ranks):
        score += 6

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

    if options["can_surcoinche"] and current is not None and strengths[current["trump"]] >= 98:
        return {"action": "surcoinche"}
    if (
        options["can_coinche"]
        and current is not None
        and current["points"] != rules.CAPOT
        and current["points"] <= 100
        and strengths[current["trump"]] >= 72
    ):
        return {"action": "coinche"}

    last_partner_bid = next(
        (
            bid
            for bid in reversed(options["bid_history"])
            if bid.get("action") == "bid" and TEAM_OF[bid["seat"]] == TEAM_OF[seat] and bid["seat"] != seat
        ),
        None,
    )
    if last_partner_bid:
        if last_partner_bid["points"] == rules.CAPOT:
            return {"action": "pass"}
        self_already_supported_partner = any(
            bid
            for bid in options["bid_history"]
            if bid.get("action") == "bid"
            and TEAM_OF[bid["seat"]] == TEAM_OF[seat]
            and bid["seat"] == seat
            and bid["trump"] == last_partner_bid["trump"]
        )
        if not self_already_supported_partner:
            has_opponents_bid_before = any(
                bid
                for bid in options["bid_history"]
                if bid.get("action") == "bid"
                and TEAM_OF[bid["seat"]] != TEAM_OF[seat]
                and bid["points"] < last_partner_bid["points"]
            )
            new_bid = _support_ceiling(
                hand,
                last_partner_bid["trump"],
                last_partner_bid["points"],
                has_opponents_bid_before,
            )
            if new_bid is not None:
                if new_bid >= rules.BID_MAX:
                    new_bid = rules.CAPOT
                return {"action": "bid", "trump": last_partner_bid["trump"], "points": new_bid}
    if last_partner_bid and last_partner_bid["points"] >= 100:
        return {"action": "pass"}
    # we cant support the partner, so we can try to open a new suit if we have a good hand
    maximum_for_hand = opening_ceilings[best_trump]
    if maximum_for_hand is not None:
        trump_ranks = {card.rank for card in hand if card.suit == best_trump}
        if "V" not in trump_ranks and "9" not in trump_ranks:
            return {"action": "pass"}
        if "V" not in trump_ranks or "9" not in trump_ranks:
            has_partner_bid_on_trump = any(
                bid
                for bid in options["bid_history"]
                if bid.get("action") == "bid"
                and TEAM_OF[bid["seat"]] == TEAM_OF[seat]
                and bid["trump"] == best_trump
                and bid["seat"] != seat
            )
            if has_partner_bid_on_trump:
                # consider we have the missing card, so we can bid higher
                if maximum_for_hand != rules.CAPOT:
                    maximum_for_hand = int(maximum_for_hand) + rules.BID_STEP*2
                    if maximum_for_hand >= rules.BID_MAX:
                        maximum_for_hand = rules.CAPOT
            else:
                if options["current_highest_bid"] is None:
                    maximum_for_hand = rules.BID_MIN
                elif (
                    options["current_highest_bid"]["points"] == rules.BID_MIN
                    and options["current_highest_bid"]["trump"] != best_trump
                    and options["current_highest_bid"]["team"] != TEAM_OF[seat]
                ):
                    maximum_for_hand = rules.BID_MIN + rules.BID_STEP
                else:
                    return {"action": "pass"}
        legal_for_suit = [] if maximum_for_hand is None else _legal_bids_up_to(options, best_trump, maximum_for_hand)
        if legal_for_suit:
            choice = legal_for_suit[-1]
            return {"action": "bid", "trump": choice["trump"], "points": choice["points"]}
    if current is None:
        fallback_trump = _forced_opener_trump(hand)
        if fallback_trump is not None:
            legal_for_suit = _legal_bids_up_to(options, fallback_trump, rules.BID_MIN + rules.BID_STEP)
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


def _has_been_played(card: Card, round_state: RoundState) -> bool:
    """Whether a card is visible in completed or current tricks."""
    return any(
        played == card
        for trick in [*round_state.trick_history, {"trick": round_state.current_trick}]
        for _, played in trick["trick"]
    )


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


def _opponent_may_ruff_suit(game: Game, seat: Seat, suit: str, trump: str) -> bool:
    """Whether public information says a defender could cut a lead in `suit`."""
    assert game.round_state is not None
    if suit == trump:
        return False
    voids = _known_void_suits(game.round_state)
    return any(
        other != seat and TEAM_OF[other] != TEAM_OF[seat] and suit in voids[other] and trump not in voids[other]
        for other in Seat
    )


def _partner_is_known_void_of_trump(game: Game, seat: Seat, trump: str) -> bool:
    """Whether public play proves the acting seat's partner has no trump left."""
    assert game.round_state is not None
    return trump in _known_void_suits(game.round_state)[PARTNER_OF[seat]]


def _defender_trump_lead_is_wasteful(game: Game, seat: Seat, trump: str) -> bool:
    """Whether a defender on lead should keep trump out of its opening options.

    Leading a trump when the opponents took the contract usually just helps them
    draw a round and burns one of our own guards. The one time it pays is holding
    the master of the outstanding trumps *while the takers may still hold trump*:
    then the master wins outright and strips a ruffer. Once the opponents are out
    of trump even the master lead is pointless, so trump is dropped in that case
    too.
    """
    assert game.round_state is not None
    contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
    if contract is None or contract["team"] == TEAM_OF[seat]:
        return False
    hand = game.get_hand(seat)
    holds_master = any(card.suit == trump and _is_master(card, hand, game, trump) for card in hand)
    return not (holds_master and _opponents_may_hold_trump(game, seat, trump))


def _select_tactical_card_for_simulation(game: Game, seat: Seat) -> Card:
    """Choose a team-oriented card for one simulated world.

    The policy deliberately remains information-safe: it sees only the acting
    hand and public cards, even though the rollout contains a full sampled
    deal. It pulls trump for the declaring team, protects side masters a known
    defender can ruff, and avoids wasting points when the partner is master.
    """
    assert game.round_state is not None
    options = game.play_options_for(seat)
    legal_cards: list[Card] = options["legal_cards"]
    trick = game.round_state.current_trick
    trump = game.round_state.trump
    assert legal_cards and trump is not None

    if not trick:
        hand = game.get_hand(seat)
        # A defender on lead should not gift the takers a trump lead; keep trump
        # out of the options unless the master lead is worth it.
        if _defender_trump_lead_is_wasteful(game, seat, trump):
            non_trumps = [card for card in legal_cards if card.suit != trump]
            if non_trumps:
                legal_cards = non_trumps
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
            # A non-taking partner normally keeps a non-master trump: leading
            # it could force the taker to overtrump. If public play has already
            # proved the taker void of trump, that risk is gone, so pull the
            # opponents instead of preserving their future ruffing power.
            if _partner_is_known_void_of_trump(game, seat, trump):
                trumps = [card for card in legal_cards if card.suit == trump]
                if trumps:
                    return max(trumps, key=lambda card: _card_strength(card, trump))

        if masters:
            # If the declaring team cannot pull trump right now, do not cash a
            # side master into a suit an opponent is publicly known to be void
            # in. The defender can cut it. Prefer another safe master, then
            # develop the hand with a cheap discard rather than donate an Ace.
            if is_declarer and _opponents_may_hold_trump(game, seat, trump):
                safe_masters = [card for card in masters if not _opponent_may_ruff_suit(game, seat, card.suit, trump)]
                if safe_masters:
                    return max(
                        safe_masters,
                        key=lambda card: (rules.card_points(card, trump), _card_strength(card, trump)),
                    )
                non_masters = [card for card in legal_cards if card not in masters]
                if non_masters:
                    return _best_discard(non_masters, hand, trump)
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


def _bid_strength(points: int | str) -> float:
    """Convert a public bid level into a bounded distribution signal."""
    if points == rules.CAPOT:
        return 2.0
    assert isinstance(points, int)
    return 0.8 + (points - rules.BID_MIN) / 100


def _auction_card_weights(game: Game) -> dict[Seat, dict[Card, float]]:
    """Weight each possible card from the *entire* public auction history.

    Every bid is evidence for the bidder's announced colour, even if a later
    bid changes the final contract. A higher bid gives stronger evidence for
    trumps and outside winners. Coinche/surcoinche also indicate confidence in
    the standing trump. Passes modestly lower an opposing seat's probability of
    holding a trump honour strong enough to outbid the standing contract.
    """
    weights = {seat: {card: 1.0 for card in build_deck()} for seat in Seat}
    if game.bid_state is None:
        return weights

    standing: dict | None = None
    for entry in game.bid_state.history:
        action = entry["action"]
        bidder = entry["seat"]
        if action == "bid":
            trump = entry["trump"]
            strength = _bid_strength(entry["points"])
            is_support = (
                standing is not None and TEAM_OF[standing["seat"]] == TEAM_OF[bidder] and standing["trump"] == trump
            )
            if is_support:
                strength *= 0.75
            for card in build_deck():
                if card.suit == trump:
                    weights[bidder][card] *= 1.0 + strength * _AUCTION_TRUMP_SIGNALS[card.rank]
                elif card.rank == "A":
                    weights[bidder][card] *= 1.0 + strength * 0.28
                elif card.rank == "10":
                    weights[bidder][card] *= 1.0 + strength * 0.12
            standing = entry
        elif action in {"coinche", "surcoinche"} and standing is not None:
            for card in build_deck():
                if card.suit == standing["trump"]:
                    weights[bidder][card] *= 1.0 + 0.25 * _AUCTION_TRUMP_SIGNALS[card.rank]
        elif action == "pass" and standing is not None and TEAM_OF[bidder] != TEAM_OF[standing["seat"]]:
            for card in build_deck():
                if card.suit == standing["trump"] and card.rank in {"V", "9", "A", "10"}:
                    weights[bidder][card] *= 0.90
    return weights


def _card_seat_weight(card: Card, seat: Seat, auction_weights: dict[Seat, dict[Card, float]]) -> float:
    """Return the auction-derived likelihood of dealing a public card to a seat."""
    return auction_weights[seat][card]


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
    auction_weights = _auction_card_weights(game)
    rng = random.Random(_public_seed(game, seat))
    samples: list[dict[Seat, list[Card]]] = []
    for _ in range(sample_count):
        assignment = _weighted_deal(unseen, opponents, counts, voids, auction_weights, rng)
        if assignment is None:
            assignment = _fallback_deal(unseen, opponents, counts, rng)
        samples.append(assignment)
    return samples


def _weighted_deal(
    unseen: list[Card],
    opponents: list[Seat],
    counts: dict[Seat, int],
    voids: dict[Seat, set[str]],
    auction_weights: dict[Seat, dict[Card, float]],
    rng: random.Random,
) -> dict[Seat, list[Card]] | None:
    """Deal cards one at a time, weighted by the public auction and known voids.

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
        weights = [_card_seat_weight(card, other, auction_weights) for other in takers]
        chosen = _weighted_choice(takers, weights, rng)
        hands[chosen].append(card)
        remaining[chosen] -= 1
    return hands


def _weighted_choice(seats: list[Seat], weights: list[float], rng: random.Random) -> Seat:
    total = sum(weights)
    threshold = rng.random() * total
    cumulative = 0.0
    for i in range(len(seats)):
        cumulative += weights[i]
        if threshold < cumulative:
            return seats[i]
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
        result = simulation.submit_card(actor, _select_tactical_card_for_simulation(simulation, actor))
    if not result.get("round_complete"):
        raise RuntimeError("Bot rollout did not complete the round")

    team = TEAM_OF[seat]
    opponents = "EW" if team == "NS" else "NS"
    round_score = result["round_score"]
    return round_score[team]["total"] - round_score[opponents]["total"]


def _team_auction_supports_trump(game: Game, seat: Seat, trump: str) -> bool:
    """Return True when the declaring team's auction signals confidence in *trump*.

    Two conditions suffice:
    - Both members of the team each placed at least one bid on *trump*, OR
    - The team placed exactly one bid on *trump* and it was high (≥ 110).
    """
    assert game.bid_state is not None
    team = TEAM_OF[seat]
    team_bids_on_trump = [
        entry
        for entry in game.bid_state.history
        if entry["action"] == "bid" and entry["trump"] == trump and TEAM_OF[entry["seat"]] == team
    ]
    if len(team_bids_on_trump) >= 2:
        return True
    if len(team_bids_on_trump) == 1 and team_bids_on_trump[0]["seat"] != seat:
        points = team_bids_on_trump[0]["points"]
        return points == rules.CAPOT or points >= 100
    return False


def _choose_opening_card(game: Game, seat: Seat, legal_cards: list[Card], trump: str) -> tuple[Card | None, list[Card]]:
    """Apply the deterministic opening-lead rules before Monte-Carlo evaluation."""
    assert game.round_state is not None
    contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
    if contract is None:
        return None, legal_cards

    own_hand = game.get_hand(seat)

    # Hard safety rule for the actual choice, not only the rollout policy: a
    # declaring-team player who leads may remove possible defensive ruffers
    # before exposing an outside Ace or Ten. Monte-Carlo scores cannot override
    # a master trump lead, or a lead while the trump Valet can still be with the
    # partner. Once the Valet has fallen, a non-master trump is not forced.
    if contract["team"] == TEAM_OF[seat] and _opponents_may_hold_trump(game, seat, trump):
        trumps = [card for card in legal_cards if card.suit == trump]
        best_trump = max(trumps, key=lambda card: _card_strength(card, trump), default=None)
        worst_trump = min(trumps, key=lambda card: _card_strength(card, trump), default=None)
        # Si on a le meilleur atout, on le joue
        if best_trump is not None and _is_master(best_trump, own_hand, game, trump):
            return best_trump, legal_cards
        if worst_trump is not None:
            if not _has_been_played(Card("V", trump), game.round_state):
                return worst_trump, legal_cards
            nine_has_not_fallen = not _has_been_played(Card("9", trump), game.round_state)
            partner_might_have_trumps = trump not in _known_void_suits(game.round_state)[PARTNER_OF[seat]]
            if partner_might_have_trumps and nine_has_not_fallen and _team_auction_supports_trump(game, seat, trump):
                return worst_trump, legal_cards

    # Jouer les As hors atout. Le choix doit rester déterministe pour une même
    # information publique : on encaisse l'As de la couleur latérale la plus
    # courte en main (pour créer une chicane et pouvoir couper ensuite), en
    # départageant par la force de la couleur afin d'éviter tout aléa global.
    owned_non_trump_aces = [card for card in legal_cards if card.rank == "A" and card.suit != trump]
    if owned_non_trump_aces:
        suit_length = {
            suit: sum(1 for card in own_hand if card.suit == suit)
            for suit in {ace.suit for ace in owned_non_trump_aces}
        }
        chosen_ace = min(
            owned_non_trump_aces,
            key=lambda ace: (suit_length[ace.suit], ace.suit),
        )
        return chosen_ace, legal_cards

    # Defending team on lead: drop trump from the candidates (unless the
    # master lead is worth it) so neither Monte-Carlo nor the tactical
    # fallback can gift the takers a trump lead.
    if _defender_trump_lead_is_wasteful(game, seat, trump):
        non_trumps = [card for card in legal_cards if card.suit != trump]
        if non_trumps:
            legal_cards = non_trumps

    # Si ya plus d'atout, jouer les longeurs maitres
    if not _opponents_may_hold_trump(game, seat, trump):
        non_trump_masters = [
            card for card in legal_cards if card.suit != trump and _is_master(card, own_hand, game, trump)
        ]
        if non_trump_masters:
            return (
                max(
                    non_trump_masters,
                    key=lambda card: (rules.card_points(card, trump), _card_strength(card, trump)),
                ),
                legal_cards,
            )
    # tenter une couleur jamais jouée si possible
    never_played_suits = {
        card.suit for card in legal_cards if card.suit != trump and not _has_been_played(card, game.round_state)
    }
    if never_played_suits:
        return min(
            (card for card in legal_cards if card.suit in never_played_suits),
            key=lambda card: _card_strength(card, trump),
            default=None,
        ), legal_cards
    return None, legal_cards


def choose_card(game: Game, seat: Seat) -> Card:
    """Choose the legal card with the best average result across plausible hidden deals.

    Determinizations are built from this bot's hand and public play history only.
    The real hands stored by the authoritative server are deliberately ignored.
    """
    assert game.round_state is not None
    options = game.play_options_for(seat)
    legal_cards: list[Card] = options["legal_cards"]
    assert legal_cards
    if len(legal_cards) == 1:
        return legal_cards[0]

    trump = options["trump"]
    if not game.round_state.current_trick and trump is not None:
        opening_card, legal_cards = _choose_opening_card(game, seat, legal_cards, trump)
        if opening_card is not None:
            return opening_card

    if game.round_state.current_trick:
        # If the partner is winning the trick, discard a low card to develop the hand.
        trick = game.round_state.current_trick
        led_suit = trick[0][1].suit
        if led_suit != trump:
            requested_trick_ace = [card for card in legal_cards if card.rank == "A" and card.suit == led_suit]
            if any(requested_trick_ace) and not any(card.suit == trump for _, card in trick):
                return requested_trick_ace[0]

    # Default to Monte-Carlo simulation
    samples = _sample_hidden_hands(game, seat, MONTE_CARLO_SAMPLES)
    if not samples:
        return _select_tactical_card_for_simulation(game, seat)

    scores = {card: 0 for card in legal_cards}
    for hidden_hands in samples:
        for card in legal_cards:
            scores[card] += _rollout_score(game, seat, card, hidden_hands)

    tactical = _select_tactical_card_for_simulation(game, seat)
    return max(
        legal_cards,
        key=lambda card: (
            scores[card],
            card == tactical,
            tuple(-value for value in _discard_key(card, options["trump"])),
        ),
    )


def configure_samples(samples: int) -> int:
    """Install an explicit positive Monte-Carlo sample count for card choices."""
    global MONTE_CARLO_SAMPLES
    if samples < 1:
        raise ValueError("Bot sample count must be at least 1")
    MONTE_CARLO_SAMPLES = samples
    return MONTE_CARLO_SAMPLES
