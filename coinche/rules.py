"""Coinche rules: point tables, bid legality, card legality, trick winner, scoring.

Implements assumptions A5-A13 from plan.md. Sans-Atout (no-trump) and
Tout-Atout (all-trump) are explicitly NOT legal declarations in this game
(A7) — the only trump declarations are one of the 4 suits, or "capot" (bid
within a chosen suit).
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
BID_MAX = 160
BID_STEP = 10
CAPOT = "capot"
ALLOWED_TRUMPS: tuple[str, ...] = SUITS

CAPOT_ANNOUNCE = 250  # points "demandés" for an announced capot
CAPOT_POINTS = 252  # raw card points when a capot is actually made
COINCHE_MULTIPLIER = 2  # A9
SURCOINCHE_MULTIPLIER = 4  # A9
BELOTE_BONUS = 20  # A11
DIX_DE_DER = 10  # last-trick bonus, folded into captured points by callers

NORMAL_POOL = 162  # 152 card points + 10 dix-de-der (A10)
CAPOT_TOTAL = CAPOT_ANNOUNCE + CAPOT_POINTS  # raw value of an announced capot

TRICKS_PER_ROUND = 8

# Table-level scoring conventions recognised by the federation. Keep the
# current hybrid convention as the default for existing tables.
SCORE_MODE_POINTS_MADE = "points_made"
SCORE_MODE_POINTS_ANNOUNCED = "points_announced"
SCORE_MODE_POINTS_MADE_PLUS_ANNOUNCED = "points_made_plus_announced"
SCORE_MODES: tuple[str, ...] = (
    SCORE_MODE_POINTS_MADE,
    SCORE_MODE_POINTS_ANNOUNCED,
    SCORE_MODE_POINTS_MADE_PLUS_ANNOUNCED,
)
DEFAULT_SCORE_MODE = SCORE_MODE_POINTS_MADE_PLUS_ANNOUNCED


def is_supported_score_mode(score_mode: str) -> bool:
    """Return whether ``score_mode`` names one of the supported conventions."""
    return score_mode in SCORE_MODES


def round_to_nearest_ten(points: int) -> int:
    """Round card points to the nearest multiple of 10 (mathematical rounding,
    .5 rounds up): 94 -> 90, 68 -> 70, 46 -> 50, 45 -> 50, 44 -> 40."""
    return (points + 5) // 10 * 10


DEFAULT_TARGET_SCORE = 1000  # A12


def card_points(card: Card, trump_suit: str) -> int:
    """Return a card's point value: TRUMP_POINTS if `card` is in the trump
    suit, NONTRUMP_POINTS otherwise."""
    if card.suit == trump_suit:
        return TRUMP_POINTS[card.rank]
    return NONTRUMP_POINTS[card.rank]


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
            actions.append({"action": "bid", "trump": trump, "points": points})
        points += BID_STEP

    for trump in trumps:
        actions.append({"action": "bid", "trump": trump, "points": CAPOT})

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
    trump_suit: str,
    led_suit: str | None,
    player_seat: Seat | None = None,
    partner_seat: Seat | None = None,
) -> list[Card]:
    """Return the subset of `hand` legal to play next.

    Enforces: follow-suit -> must-overtrump when trump is led (including when
    the partner is master) -> master-partner "pisser" exception when void of
    a non-trump led suit -> must-trump -> must-overtrump -> free-discard
    exception when cutting and no overtrump is possible -> free discard
    fallback.

    Note the free-discard-when-can't-overtrump exception only applies when
    the player is cutting a non-trump lead while void of the led suit; if
    trump was the suit originally led, a player following suit with trump
    must still play a trump even when unable to beat the highest trump
    played so far (no "pisser" exception in that case).
    """
    if not current_trick:
        return list(hand)

    if led_suit is None:
        led_suit = current_trick[0][1].suit

    same_suit_cards = [c for c in hand if c.suit == led_suit]

    if led_suit == trump_suit:
        if same_suit_cards:
            return _apply_overtrump_rule(same_suit_cards, current_trick, trump_suit)
        return list(hand)

    if same_suit_cards:
        return same_suit_cards

    # Void of the led suit: no obligation to cut at all if the partner is
    # already master of the trick (whether by holding the highest card of
    # the led suit with no trump played yet, or the highest trump played so
    # far) — the player may "pisser" (discard any card freely).
    if partner_seat is not None and trick_winner(current_trick, trump_suit, led_suit) == partner_seat:
        return list(hand)

    trump_cards = [c for c in hand if c.suit == trump_suit]
    if not trump_cards:
        return list(hand)

    return _apply_overtrump_rule(trump_cards, current_trick, trump_suit, free_discard_fallback=list(hand))


def _apply_overtrump_rule(
    candidate_cards: list[Card],
    current_trick: list[tuple[Seat, Card]],
    trump_suit: str,
    free_discard_fallback: list[Card] | None = None,
) -> list[Card]:
    trumps_in_trick = [(s, c) for s, c in current_trick if c.suit == trump_suit]
    if not trumps_in_trick:
        return candidate_cards

    _, highest_card = max(trumps_in_trick, key=lambda sc: TRUMP_ORDER.index(sc[1].rank))

    higher_trumps = [c for c in candidate_cards if TRUMP_ORDER.index(c.rank) > TRUMP_ORDER.index(highest_card.rank)]
    if higher_trumps:
        return higher_trumps

    # Can't overtrump. When cutting a non-trump lead (free_discard_fallback
    # set), the player is not forced to under-trump and may "pisser" —
    # discard any card in hand freely. When following suit because trump
    # itself was led (free_discard_fallback is None), the player must still
    # play one of their trumps.
    if free_discard_fallback is not None:
        return free_discard_fallback

    return candidate_cards  # trump was led: must still follow suit with a trump


def trick_winner(
    trick: list[tuple[Seat, Card]],
    trump_suit: str,
    led_suit: str | None = None,
) -> Seat:
    """Return the seat that wins the trick."""
    if led_suit is None:
        led_suit = trick[0][1].suit

    trump_plays = [(s, c) for s, c in trick if c.suit == trump_suit]
    if trump_plays:
        winner_seat, _ = max(trump_plays, key=lambda sc: TRUMP_ORDER.index(sc[1].rank))
        return winner_seat

    led_plays = [(s, c) for s, c in trick if c.suit == led_suit]
    winner_seat, _ = max(led_plays, key=lambda sc: NONTRUMP_ORDER.index(sc[1].rank))
    return winner_seat


# --- Scoring (A8-A11) ----------------------------------------------------------


def score_round(
    captured_points_by_team: dict[str, int],
    bid: dict,
    coinche_level: int,
    capot_result: bool | None,
    belote_holder: str | None,
    attacker_tricks: int | None = None,
    score_mode: str = DEFAULT_SCORE_MODE,
) -> dict[str, dict]:
    """Score a completed round.

    `captured_points_by_team` is each team's captured card points, including
    the dix-de-der bonus already folded in by the caller. `bid` is
    {"team": "NS"|"EW", "trump": ..., "points": int|"capot"}. `coinche_level`
    is 1 (no coinche), 2 (coinche), or 4 (surcoinche) (A9). `capot_result` is
    only meaningful when `bid["points"] == "capot"`: True if the attacking
    team won all 8 tricks, False otherwise. `belote_holder` is "NS"/"EW"/None.
    `attacker_tricks` is the number of tricks the attacking team took — used to
    upgrade a numeric contract to a capot bonus when they take all 8 tricks
    without having announced it.

    ``score_mode`` selects one of the federation conventions: points made,
    points announced, or points made plus announced. Card points are rounded
    to the nearest 10 for modes that count them. The contract itself is always
    judged from the attacker's unrounded card points plus any belote.

    In the announced-points convention, belote can still complete a numeric
    contract but scores no separate points. In the other conventions it is
    added after the multiplier: on a normal failed contract the defence takes
    it; an attacking side that fails an announced capot loses it.
    """
    if not is_supported_score_mode(score_mode):
        raise ValueError(f"Unknown score mode: {score_mode!r}")

    attacking_team = bid["team"]
    defending_team = "EW" if attacking_team == "NS" else "NS"
    capot_bonus = {"NS": 0, "EW": 0}

    is_capot_bid = bid["points"] == CAPOT
    announced = CAPOT_ANNOUNCE if is_capot_bid else bid["points"]
    attacker_made_capot = attacker_tricks == TRICKS_PER_ROUND
    attacker_belote = BELOTE_BONUS if belote_holder == attacking_team else 0

    if is_capot_bid:
        contract_made = bool(capot_result)
    else:
        attacking_points = captured_points_by_team.get(attacking_team, 0)
        contract_made = attacking_points + attacker_belote >= bid["points"]

    winning_team = attacking_team if contract_made else defending_team
    made_capot = contract_made and (is_capot_bid or attacker_made_capot)

    if contract_made:
        if made_capot:
            capot_bonus[attacking_team] = round_to_nearest_ten(CAPOT_POINTS)

        if score_mode == SCORE_MODE_POINTS_MADE:
            attacking_base = capot_bonus[attacking_team] or round_to_nearest_ten(
                captured_points_by_team.get(attacking_team, 0)
            )
            defending_base = 0 if made_capot else round_to_nearest_ten(captured_points_by_team.get(defending_team, 0))
        elif score_mode == SCORE_MODE_POINTS_ANNOUNCED:
            attacking_base = CAPOT_ANNOUNCE if is_capot_bid else announced
            defending_base = 0
        else:
            attacker_realized = capot_bonus[attacking_team] or round_to_nearest_ten(
                captured_points_by_team.get(attacking_team, 0)
            )
            attacking_base = attacker_realized + announced
            defending_base = 0 if made_capot else round_to_nearest_ten(captured_points_by_team.get(defending_team, 0))
        contract_result = "capot_achieved" if is_capot_bid else "made"
    else:
        if is_capot_bid:
            if score_mode == SCORE_MODE_POINTS_ANNOUNCED:
                defending_base = CAPOT_ANNOUNCE if coinche_level > 1 else round_to_nearest_ten(NORMAL_POOL)
            else:
                defending_base = round_to_nearest_ten(CAPOT_TOTAL)
        else:
            defending_base = round_to_nearest_ten(NORMAL_POOL)
            if score_mode == SCORE_MODE_POINTS_MADE_PLUS_ANNOUNCED:
                defending_base += announced
        attacking_base = 0
        contract_result = "capot_failed" if is_capot_bid else "failed"

    base_by_team = {attacking_team: attacking_base, defending_team: defending_base}
    scored_by_team = dict(base_by_team)

    if coinche_level > 1:
        if made_capot or is_capot_bid:
            capot_value = CAPOT_ANNOUNCE if score_mode == SCORE_MODE_POINTS_ANNOUNCED else CAPOT_ANNOUNCE * 2
            scored_by_team = {"NS": 0, "EW": 0}
            scored_by_team[winning_team] = capot_value * coinche_level
        elif score_mode == SCORE_MODE_POINTS_MADE:
            scored_by_team = {"NS": 0, "EW": 0}
            scored_by_team[winning_team] = round_to_nearest_ten(NORMAL_POOL) * coinche_level
        elif score_mode == SCORE_MODE_POINTS_ANNOUNCED:
            scored_by_team = {"NS": 0, "EW": 0}
            scored_by_team[winning_team] = announced * coinche_level
        else:
            scored_by_team = {"NS": 0, "EW": 0}
            scored_by_team[winning_team] = (round_to_nearest_ten(NORMAL_POOL) + announced) * coinche_level

    belote_bonus = {"NS": 0, "EW": 0}
    if score_mode != SCORE_MODE_POINTS_ANNOUNCED and belote_holder is not None:
        if contract_made:
            belote_bonus[belote_holder] = BELOTE_BONUS
        elif not is_capot_bid:
            belote_bonus[defending_team] = BELOTE_BONUS
        elif belote_holder == defending_team:
            belote_bonus[defending_team] = BELOTE_BONUS

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
