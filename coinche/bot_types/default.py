"""I/O-free imperfect-information player for server-controlled Coinche bots."""

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass, field

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


def _support_ceiling(
    hand: list[Card],
    trump: str,
    current_partner_bid_point: int,
    has_opponents_bid_before: bool,
    current_highest_bid: dict | None = None,
) -> int | None:
    """Return the highest safe support bid from the partner's announced strength.

    An opening of 80 promises a playable trump suit. A bot holding the Valet
    or 9 in that suit therefore completes the partner's signal with a trump
    master and can promise one additional trick, even with fewer than three
    trumps itself.
    """

    if current_highest_bid is not None and current_highest_bid["points"] == rules.CAPOT:
        return None
    trump_cards = [card for card in hand if card.suit == trump]
    trump_ranks = {card.rank for card in trump_cards}
    trump_count = len(trump_cards)

    partner_looking_for_34 = current_partner_bid_point == rules.BID_MIN or (
        current_partner_bid_point == rules.BID_MIN + rules.BID_STEP and has_opponents_bid_before
    )
    if partner_looking_for_34:
        if "V" in trump_ranks or "9" in trump_ranks:
            minimum_bid = current_partner_bid_point + rules.BID_STEP
            if current_highest_bid is not None:
                if current_highest_bid["points"] == rules.CAPOT or current_highest_bid["points"] >= 120:
                    return None
                minimum_bid = max(minimum_bid, current_highest_bid["points"] + rules.BID_STEP)
            return minimum_bid
    else:
        additional_steps = 0
        if "V" in trump_ranks or "9" in trump_ranks:
            additional_steps += 2
        elif trump_count >= 3:
            additional_steps += 1
        additional_steps += _side_aces(hand, trump)
        if additional_steps > 0:
            new_bid = current_partner_bid_point + additional_steps * rules.BID_STEP
            # Check if new bid is legal
            if current_highest_bid is not None:
                if new_bid == current_highest_bid["points"]:
                    # When this condition is true, it means the current highest bid is necessarily from the opponents.
                    # If we miss only 10 point to support, allow to increase by 10 more, if more we will pass
                    new_bid += rules.BID_STEP
            return new_bid
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


def _is_partner_bid(bid: dict, seat: Seat) -> bool:
    """Whether a bid was made by the acting seat's partner."""
    return TEAM_OF[bid["seat"]] == TEAM_OF[seat] and bid["seat"] != seat


def _try_partner_support(
    hand: list[Card],
    last_partner_bid: dict,
    options: dict,
    seat: Seat,
    best_trump: str,
) -> dict | None:
    """Attempt to support or respect the partner's last bid.

    Returns a bid/pass action dict, or None when the partner's bid does not
    apply (no partner bid, already supported, or partner bid is too high to
    interfere with).
    """
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
            options["current_highest_bid"],
        )
        if new_bid is not None:
            if new_bid >= rules.BID_MAX:
                new_bid = rules.CAPOT
            legal_for_suit = _legal_bids_up_to(options, last_partner_bid["trump"], new_bid)
            if not legal_for_suit:
                return {"action": "pass"}
            return legal_for_suit[-1] if legal_for_suit else {"action": "pass"}

    if last_partner_bid["points"] >= 100 and last_partner_bid["trump"] != best_trump:
        return {"action": "pass"}

    return None


def _try_open_suit(
    hand: list[Card],
    best_trump: str,
    opening_ceilings: dict[str, int | str | None],
    options: dict,
    seat: Seat,
) -> dict | None:
    """Try to open a new suit when we have a good hand.

    Returns a bid/pass action dict, or None when the hand is not strong
    enough to open.
    """
    maximum_for_hand = opening_ceilings[best_trump]
    if maximum_for_hand is None:
        return None

    trump_ranks = {card.rank for card in hand if card.suit == best_trump}
    if "V" not in trump_ranks and "9" not in trump_ranks:
        return {"action": "pass"}
    if "V" not in trump_ranks or "9" not in trump_ranks:
        has_partner_bid_on_trump = any(
            bid
            for bid in options["bid_history"]
            if bid.get("action") == "bid" and _is_partner_bid(bid, seat) and bid["trump"] == best_trump
        )
        if has_partner_bid_on_trump:
            if maximum_for_hand != rules.CAPOT:
                maximum_for_hand = int(maximum_for_hand) + rules.BID_STEP * 2
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

    minimum_for_hand = rules.BID_MIN
    if "V" in trump_ranks and "9" in trump_ranks:
        minimum_for_hand = rules.BID_MIN + rules.BID_STEP
        if options["current_highest_bid"] and options["current_highest_bid"]["points"] == rules.BID_MIN:
            minimum_for_hand = rules.BID_MIN + rules.BID_STEP * 2
        if maximum_for_hand != rules.CAPOT:
            if minimum_for_hand == int(maximum_for_hand) + rules.BID_STEP:
                maximum_for_hand = int(maximum_for_hand) + rules.BID_STEP
            if minimum_for_hand > int(maximum_for_hand):
                return {"action": "pass"}

    legal_for_suit = [] if maximum_for_hand is None else _legal_bids_up_to(options, best_trump, maximum_for_hand)
    if minimum_for_hand > rules.BID_MIN:
        legal_for_suit = [bid for bid in legal_for_suit if bid["points"] >= minimum_for_hand]
    if legal_for_suit:
        choice = legal_for_suit[-1]
        return {"action": "bid", "trump": choice["trump"], "points": choice["points"]}

    return None


_COINCHE_THRESHOLDS: list[tuple[int, int]] = [(100, 80), (120, 65), (140, 50), (rules.BID_MAX, 40)]
_COINCHE_CAPOT_STRENGTH = 30
_SURCOINCHE_THRESHOLDS: list[tuple[int, int]] = [(100, 105), (120, 120), (140, 130), (rules.BID_MAX, 145)]
_SURCOINCHE_CAPOT_STRENGTH = 145


def _should_counter(
    action: str,
    current: dict,
    strengths: dict[str, int],
) -> bool:
    """Return True if the bot should coinche or surcoinche the standing bid."""
    if current["points"] == rules.CAPOT:
        capot_strength = _COINCHE_CAPOT_STRENGTH if action == "coinche" else _SURCOINCHE_CAPOT_STRENGTH
        return strengths[current["trump"]] >= capot_strength
    thresholds = _COINCHE_THRESHOLDS if action == "coinche" else _SURCOINCHE_THRESHOLDS
    required = next(r for max_pts, r in thresholds if current["points"] <= max_pts)
    return strengths[current["trump"]] >= required


def _choose_normal_bid(
    game: Game,
    seat: Seat,
    hand: list[Card],
    best_trump: str,
    opening_ceilings: dict[str, int | str | None],
    options: dict,
) -> dict:
    """Choose a normal bid or pass, excluding Coinche and Surcoinche."""
    last_partner_bid = next(
        (bid for bid in reversed(options["bid_history"]) if bid.get("action") == "bid" and _is_partner_bid(bid, seat)),
        None,
    )
    if last_partner_bid:
        support_partner_action = _try_partner_support(hand, last_partner_bid, options, seat, best_trump)
        if support_partner_action is not None:
            return support_partner_action

    own_action = _try_open_suit(hand, best_trump, opening_ceilings, options, seat)
    if own_action is not None:
        return own_action

    if options["current_highest_bid"] is None:
        fallback_trump = _forced_opener_trump(hand)
        if fallback_trump is not None:
            legal_for_suit = _legal_bids_up_to(options, fallback_trump, rules.BID_MIN + rules.BID_STEP)
            if legal_for_suit:
                choice = legal_for_suit[-1]
                return {"action": "bid", "trump": choice["trump"], "points": choice["points"]}

    bid_state = game.bid_state
    if options["current_highest_bid"] is None and bid_state is not None and bid_state.pass_streak == 3:
        forced = next(action for action in options["legal_actions"] if action["trump"] == best_trump)
        return {"action": "bid", "trump": forced["trump"], "points": forced["points"]}

    return {"action": "pass"}


def _choose_counter_action(options: dict, current: dict | None, strengths: dict[str, int]) -> dict | None:
    """Choose Coinche or Surcoinche without displacing a normal Capot rebid."""
    if current is None:
        return None
    if options["can_coinche"] and _should_counter("coinche", current, strengths):
        return {"action": "coinche"}
    if options["can_surcoinche"] and _should_counter("surcoinche", current, strengths):
        return {"action": "surcoinche"}
    return None


def choose_bid(game: Game, seat: Seat) -> dict:
    """Choose a legal, conservative auction action from the bot's own hand."""
    options = game.bid_options_for(seat)
    hand = game.get_hand(seat)
    strengths = {trump: _hand_strength(hand, trump) for trump in rules.ALLOWED_TRUMPS}
    opening_ceilings = {trump: _opening_ceiling(hand, trump) for trump in rules.ALLOWED_TRUMPS}
    current = options["current_highest_bid"]
    last_self_bid = next(
        (bid for bid in reversed(options["bid_history"]) if bid.get("action") == "bid" and bid["seat"] == seat),
        None,
    )
    best_trump = None
    if last_self_bid is not None:
        best_trump = last_self_bid["trump"]
    else:
        best_trump = max(
            rules.ALLOWED_TRUMPS,
            key=lambda trump: (_ceiling_value(opening_ceilings[trump]), strengths[trump]),
        )

    counter_action = _choose_counter_action(options, current, strengths)
    normal_action = _choose_normal_bid(game, seat, hand, best_trump, opening_ceilings, options)
    if counter_action is not None and normal_action.get("points") != rules.CAPOT:
        return counter_action
    if options["can_surcoinche"] and normal_action["action"] == "bid" and normal_action["trump"] == current["trump"]:
        return {"action": "surcoinche"}
    return normal_action


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
            non_trumps = [card for card in legal_cards if card.suit != trump]
            if non_trumps:
                return max(
                    non_trumps,
                    key=lambda card: (rules.card_points(card, trump), -_card_strength(card, trump)),
                )
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
    _apply_play_signal_weights(game, weights)
    return weights


def _apply_play_signal_weights(game: Game, weights: dict[Seat, dict[Card, float]]) -> None:
    """Apply a direct-call convention observed in public partner-winning discards.

    A low non-trump discard behind a partner's winning card can call the Ace
    of that discarded suit. This is soft evidence only: a player may discard
    for other reasons, so it never overrides void-suit constraints.
    """
    assert game.round_state is not None
    trump = game.round_state.trump
    if trump is None:
        return
    tricks = [entry["trick"] for entry in game.round_state.trick_history]
    if game.round_state.current_trick:
        tricks.append(game.round_state.current_trick)
    for trick in tricks:
        led_suit = trick[0][1].suit
        for index, (seat, card) in enumerate(trick[1:], start=1):
            if (
                card.suit != led_suit
                and card.suit != trump
                and card.rank not in {"A", "10"}
                and rules.trick_winner(trick[:index], trump, led_suit) == PARTNER_OF[seat]
            ):
                weights[seat][Card("A", card.suit)] *= 1.35
                weights[seat][Card("10", card.suit)] *= 1.10


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


@dataclass
class _SearchAction:
    visits: int = 0
    total_value: int = 0


@dataclass
class _SearchNode:
    visits: int = 0
    actions: dict[Card, _SearchAction] = field(default_factory=dict)


def _action_average(action: _SearchAction | None) -> float:
    if action is None or action.visits == 0:
        return float("-inf")
    return action.total_value / action.visits


def _select_search_card(
    node: _SearchNode,
    legal_cards: list[Card],
    actor: Seat,
    root_team: str,
) -> Card:
    unvisited = sorted((card for card in legal_cards if card not in node.actions), key=str)
    if unvisited:
        return unvisited[0]
    actor_is_root_team = TEAM_OF[actor] == root_team
    exploration = math.sqrt(math.log(node.visits + 1))
    return max(
        legal_cards,
        key=lambda card: (
            (1 if actor_is_root_team else -1) * _action_average(node.actions[card])
            + 40.0 * exploration / math.sqrt(node.actions[card].visits),
            str(card),
        ),
    )


def _information_key(game: Game, seat: Seat) -> tuple:
    assert game.round_state is not None
    round_state = game.round_state
    history = tuple(
        tuple((played_seat.value, str(card)) for played_seat, card in trick["trick"])
        for trick in round_state.trick_history
    )
    current_trick = tuple((played_seat.value, str(card)) for played_seat, card in round_state.current_trick)
    bid_history = (
        () if game.bid_state is None else tuple(tuple(sorted(entry.items())) for entry in game.bid_state.history)
    )
    return (
        seat.value,
        tuple(sorted(str(card) for card in game.get_hand(seat))),
        round_state.trump,
        history,
        current_trick,
        bid_history,
        tuple(sorted(round_state.captured_points.items())),
    )


def _search_determinization(
    game: Game,
    root_seat: Seat,
    hidden_hands: dict[Seat, list[Card]],
    nodes: dict[tuple, _SearchNode],
) -> None:
    simulation = copy.deepcopy(game)
    _apply_determinization(simulation, root_seat, hidden_hands)
    path: list[tuple[_SearchNode, Card]] = []
    result: dict | None = None
    in_tree = True

    for _ in range(32):
        if simulation.round_state is None:
            break
        actor = simulation.next_to_act
        options = simulation.play_options_for(actor)
        legal_cards: list[Card] = options["legal_cards"]
        if not legal_cards:
            break

        if in_tree:
            key = _information_key(simulation, actor)
            node = nodes.setdefault(key, _SearchNode())
            unvisited = sorted((card for card in legal_cards if card not in node.actions), key=str)
            if unvisited:
                tactical = _select_tactical_card_for_simulation(simulation, actor)
                card = tactical if tactical in unvisited else unvisited[0]
                path.append((node, card))
                in_tree = False
            else:
                card = _select_search_card(node, legal_cards, actor, TEAM_OF[root_seat])
                path.append((node, card))
        else:
            card = _select_tactical_card_for_simulation(simulation, actor)

        result = simulation.submit_card(actor, card)
        if result.get("round_complete"):
            break

    if result is None or not result.get("round_complete"):
        raise RuntimeError("Information-set search did not complete the round")

    root_team = TEAM_OF[root_seat]
    opposing_team = "EW" if root_team == "NS" else "NS"
    score = result["round_score"]
    value = score[root_team]["total"] - score[opposing_team]["total"]
    for node, card in path:
        node.visits += 1
        action = node.actions.setdefault(card, _SearchAction())
        action.visits += 1
        action.total_value += value


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


def choose_card(game: Game, seat: Seat, sample_count: int | None = None) -> Card:
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
        trick = game.round_state.current_trick
        if len(trick) == 3 and trump is not None:
            return _select_tactical_card_for_simulation(game, seat)

        # If the partner is winning the trick, discard a low card to develop the hand.
        led_suit = trick[0][1].suit
        if led_suit != trump:
            requested_trick_ace = [card for card in legal_cards if card.rank == "A" and card.suit == led_suit]
            if any(requested_trick_ace) and not any(card.suit == trump for _, card in trick):
                return requested_trick_ace[0]

    # Information-set Monte-Carlo tree search
    samples = _sample_hidden_hands(game, seat, MONTE_CARLO_SAMPLES if sample_count is None else sample_count)
    tactical = _select_tactical_card_for_simulation(game, seat)
    if not samples:
        return tactical

    nodes: dict[tuple, _SearchNode] = {}
    for hidden_hands in samples:
        _search_determinization(game, seat, hidden_hands, nodes)

    root = nodes.get(_information_key(game, seat))
    if root is None:
        return tactical

    return max(
        legal_cards,
        key=lambda card: (
            _action_average(root.actions.get(card)),
            root.actions.get(card, _SearchAction()).visits,
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


class DefaultBot:
    """Current conservative, imperfect-information Coinche strategy."""

    def __init__(self, sample_count: int = MONTE_CARLO_SAMPLES) -> None:
        self.sample_count = sample_count

    def choose_bid(self, game: Game, seat: Seat) -> dict:
        return choose_bid(game, seat)

    def choose_card(self, game: Game, seat: Seat) -> Card:
        return choose_card(game, seat, self.sample_count)
