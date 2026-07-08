"""Coinche rules: point tables, bid legality, card legality, trick winner, scoring.

Implements assumptions A5-A13 from plan.md. Sans-Atout (no-trump) is explicitly
NOT a legal declaration in this game (A7) — the only trump declarations are one
of the 4 suits, "tout_atout", or "capot" (bid within a suit or tout_atout).
"""

from __future__ import annotations

from coinche.cards import SUITS, Card, Seat

# --- Point tables (A7) -------------------------------------------------------

TRUMP_POINTS: dict[str, int] = {
    "V": 20,
    "9": 14,
    "A": 11,
    "10": 10,
    "R": 4,
    "D": 3,
    "8": 0,
    "7": 0,
}

NONTRUMP_POINTS: dict[str, int] = {
    "A": 11,
    "10": 10,
    "R": 4,
    "D": 3,
    "V": 2,
    "9": 0,
    "8": 0,
    "7": 0,
}

# Rank order, lowest to highest, used for trick-winner/overtrump comparisons.
TRUMP_ORDER: tuple[str, ...] = ("7", "8", "D", "R", "10", "A", "9", "V")
NONTRUMP_ORDER: tuple[str, ...] = ("7", "8", "9", "V", "D", "R", "10", "A")

# --- Ruleset constants (A5-A13) ----------------------------------------------

BID_MIN = 80
BID_MAX = 180
BID_STEP = 10
CAPOT = "capot"
ALLOWED_TRUMPS: tuple[str, ...] = (*SUITS, "tout_atout")

CAPOT_BONUS = 250  # A8
COINCHE_MULTIPLIER = 2  # A9
SURCOINCHE_MULTIPLIER = 4  # A9
BELOTE_BONUS = 20  # A11
DIX_DE_DER = 10  # last-trick bonus, folded into captured points by callers

NORMAL_POOL = 162  # 152 card points + 10 dix-de-der (A10)
TOUT_ATOUT_POOL = 258  # 4 x 62 + 10 dix-de-der (A7/A10)

DEFAULT_TARGET_SCORE = 1000  # A12


def card_points(card: Card, trump_suit: str | None, declaration: str) -> int:
    """Return a card's point value for the given declaration.

    `declaration` is one of "normal" (one suit is trump, the other 3 use
    NONTRUMP_POINTS) or "tout_atout" (all 4 suits use TRUMP_POINTS).
    """
    if declaration == "tout_atout":
        return TRUMP_POINTS[card.rank]
    if declaration == "normal":
        if card.suit == trump_suit:
            return TRUMP_POINTS[card.rank]
        return NONTRUMP_POINTS[card.rank]
    raise ValueError(f"Unknown declaration: {declaration!r}")


# --- Bidding legality (A5/A6) -------------------------------------------------


def _bid_rank(bid: dict | None) -> int:
    """Numeric rank for comparing bids; capot always outranks any numeric bid."""
    if bid is None:
        return -1
    if bid["points"] == CAPOT:
        return BID_MAX + BID_STEP  # always higher than any numeric bid
    return bid["points"]


def legal_bid_actions(current_highest_bid: dict | None) -> list[dict]:
    """Return the list of legal {"trump", "points"} bid options.

    Per A6, a new bid is legal only if its rank strictly exceeds the current
    highest bid's rank; capot always outranks any numeric bid, and only one
    capot bid is allowed per auction (a second capot is illegal).
    """
    if current_highest_bid is not None and current_highest_bid["points"] == CAPOT:
        return []

    trumps = list(ALLOWED_TRUMPS)
    actions: list[dict] = []

    start = BID_MIN
    if current_highest_bid is not None:
        start = current_highest_bid["points"] + BID_STEP

    points = start
    while points <= BID_MAX:
        for trump in trumps:
            actions.append({"trump": trump, "points": points})
        points += BID_STEP

    for trump in trumps:
        actions.append({"trump": trump, "points": CAPOT})

    return actions


def is_valid_bid(new_bid: dict, current_highest_bid: dict | None) -> bool:
    """Validate a single proposed bid against the current highest bid (A5/A6)."""
    if new_bid.get("trump") not in ALLOWED_TRUMPS:
        return False

    points = new_bid.get("points")
    if points != CAPOT:
        if not isinstance(points, int):
            return False
        if points < BID_MIN or points > BID_MAX or points % BID_STEP != 0:
            return False

    return _bid_rank(new_bid) > _bid_rank(current_highest_bid)


# --- Card-play legality --------------------------------------------------------


def legal_cards_to_play(
    hand: list[Card],
    current_trick: list[tuple[Seat, Card]],
    trump_suit: str | None,
    led_suit: str | None,
    player_seat: Seat | None = None,
    partner_seat: Seat | None = None,
) -> list[Card]:
    """Return the subset of `hand` legal to play next.

    `trump_suit` is None for a "tout_atout" declaration: no suit cuts another,
    players simply follow suit if able or discard freely otherwise (A7 only
    changes point values, not trick-cutting mechanics for tout_atout).

    Enforces: follow-suit -> must-trump -> must-overtrump -> under-trump
    exception when the partner holds the trick's current highest trump ->
    free discard fallback.
    """
    if not current_trick:
        return list(hand)

    if led_suit is None:
        led_suit = current_trick[0][1].suit

    same_suit_cards = [c for c in hand if c.suit == led_suit]

    if trump_suit is None:
        # Tout-atout: no suit is designated as trump for cutting purposes.
        if same_suit_cards:
            return same_suit_cards
        return list(hand)

    if led_suit == trump_suit:
        if same_suit_cards:
            return _apply_overtrump_rule(
                same_suit_cards, current_trick, trump_suit, partner_seat
            )
        return list(hand)

    if same_suit_cards:
        return same_suit_cards

    trump_cards = [c for c in hand if c.suit == trump_suit]
    if not trump_cards:
        return list(hand)

    return _apply_overtrump_rule(trump_cards, current_trick, trump_suit, partner_seat)


def _apply_overtrump_rule(
    candidate_cards: list[Card],
    current_trick: list[tuple[Seat, Card]],
    trump_suit: str,
    partner_seat: Seat | None,
) -> list[Card]:
    trumps_in_trick = [(s, c) for s, c in current_trick if c.suit == trump_suit]
    if not trumps_in_trick:
        return candidate_cards

    highest_seat, highest_card = max(
        trumps_in_trick, key=lambda sc: TRUMP_ORDER.index(sc[1].rank)
    )

    if partner_seat is not None and highest_seat == partner_seat:
        return candidate_cards  # under-trump exception: no need to overtrump

    higher_trumps = [
        c
        for c in candidate_cards
        if TRUMP_ORDER.index(c.rank) > TRUMP_ORDER.index(highest_card.rank)
    ]
    if higher_trumps:
        return higher_trumps

    return candidate_cards  # can't overtrump: any trump of that group is legal


def trick_winner(
    trick: list[tuple[Seat, Card]],
    trump_suit: str | None,
    led_suit: str | None = None,
) -> Seat:
    """Return the seat that wins the trick."""
    if led_suit is None:
        led_suit = trick[0][1].suit

    if trump_suit is not None:
        trump_plays = [(s, c) for s, c in trick if c.suit == trump_suit]
        if trump_plays:
            winner_seat, _ = max(
                trump_plays, key=lambda sc: TRUMP_ORDER.index(sc[1].rank)
            )
            return winner_seat

    led_plays = [(s, c) for s, c in trick if c.suit == led_suit]
    order = TRUMP_ORDER if trump_suit is None else NONTRUMP_ORDER
    winner_seat, _ = max(led_plays, key=lambda sc: order.index(sc[1].rank))
    return winner_seat


# --- Scoring (A8-A11) ----------------------------------------------------------


def score_round(
    captured_points_by_team: dict[str, int],
    bid: dict,
    coinche_level: int,
    capot_result: bool | None,
    belote_holder: str | None,
) -> dict[str, dict]:
    """Score a completed round.

    `captured_points_by_team` is each team's captured card points, including
    the dix-de-der bonus already folded in by the caller. `bid` is
    {"team": "NS"|"EW", "trump": ..., "points": int|"capot"}. `coinche_level`
    is 1 (no coinche), 2 (coinche), or 4 (surcoinche) (A9). `capot_result` is
    only meaningful when `bid["points"] == "capot"`: True if the attacking
    team won all 8 tricks, False otherwise. `belote_holder` is "NS"/"EW"/None.
    """
    attacking_team = bid["team"]
    defending_team = "EW" if attacking_team == "NS" else "NS"
    declaration = "tout_atout" if bid["trump"] == "tout_atout" else "normal"
    pool = TOUT_ATOUT_POOL if declaration == "tout_atout" else NORMAL_POOL

    belote_bonus = {"NS": 0, "EW": 0}
    if belote_holder is not None:
        belote_bonus[belote_holder] = BELOTE_BONUS

    capot_bonus = {"NS": 0, "EW": 0}

    if bid["points"] == CAPOT:
        if capot_result:
            attacking_before_mult = CAPOT_BONUS
            defending_before_mult = 0
            contract_result = "capot_achieved"
            capot_bonus[attacking_team] = CAPOT_BONUS
        else:
            attacking_before_mult = 0
            defending_before_mult = pool
            contract_result = "capot_failed"
    else:
        attacking_points = captured_points_by_team.get(attacking_team, 0)
        if attacking_points >= bid["points"]:
            attacking_before_mult = attacking_points
            defending_before_mult = captured_points_by_team.get(defending_team, 0)
            contract_result = "made"
        else:
            attacking_before_mult = 0
            defending_before_mult = pool
            contract_result = "failed"

    contract_succeeded = contract_result in ("made", "capot_achieved")
    if contract_succeeded:
        attacking_scored = attacking_before_mult * coinche_level
        defending_scored = defending_before_mult
    else:
        attacking_scored = 0
        defending_scored = defending_before_mult * coinche_level

    scored_by_team = {attacking_team: attacking_scored, defending_team: defending_scored}

    result: dict[str, dict] = {}
    for team in ("NS", "EW"):
        result[team] = {
            "card_points": captured_points_by_team.get(team, 0),
            "contract_result": contract_result,
            "belote_bonus": belote_bonus[team],
            "capot_bonus": capot_bonus[team],
            "multiplier": coinche_level,
            "total": scored_by_team[team] + belote_bonus[team],
        }

    return result
