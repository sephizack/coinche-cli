"""Tests for coinche.rules: point tables, bid legality, card legality, scoring."""

import pytest

from coinche.cards import Card, Seat
from coinche.rules import (
    ALLOWED_TRUMPS,
    CAPOT,
    NORMAL_POOL,
    TOUT_ATOUT_POOL,
    card_points,
    is_valid_bid,
    legal_bid_actions,
    legal_cards_to_play,
    score_round,
    trick_winner,
)


# --- card_points / point tables (A7) -----------------------------------------


def test_card_points_normal_trump_suit():
    assert card_points(Card("V", "♠"), "♠", "normal") == 20
    assert card_points(Card("9", "♠"), "♠", "normal") == 14
    assert card_points(Card("A", "♠"), "♠", "normal") == 11


def test_card_points_normal_non_trump_suit():
    assert card_points(Card("A", "♥"), "♠", "normal") == 11
    assert card_points(Card("V", "♥"), "♠", "normal") == 2  # jack is non-trump here


def test_card_points_tout_atout_uses_trump_ladder_for_all_suits():
    for suit in ("♠", "♥", "♦", "♣"):
        assert card_points(Card("V", suit), None, "tout_atout") == 20
        assert card_points(Card("9", suit), None, "tout_atout") == 14


def test_card_points_unknown_declaration_raises():
    with pytest.raises(ValueError):
        card_points(Card("A", "♠"), "♠", "sans_atout")


# --- Bidding legality (A5/A6) --------------------------------------------------


def test_legal_bid_actions_no_current_bid_starts_at_minimum():
    actions = legal_bid_actions(None)
    points_values = {a["points"] for a in actions if a["points"] != CAPOT}
    assert min(points_values) == 80
    assert max(points_values) == 180
    # No sans_atout trump ever appears.
    trumps = {a["trump"] for a in actions}
    assert "sans_atout" not in trumps
    assert trumps == set(ALLOWED_TRUMPS)


def test_legal_bid_actions_only_capot_after_capot_bid():
    current = {"trump": "♠", "points": CAPOT}
    assert legal_bid_actions(current) == []


def test_is_valid_bid_rejects_equal_or_lower_rank():
    current = {"trump": "♠", "points": 100}
    assert not is_valid_bid({"trump": "♥", "points": 100}, current)  # equal rank
    assert not is_valid_bid({"trump": "♥", "points": 90}, current)  # lower rank
    assert is_valid_bid({"trump": "♥", "points": 110}, current)  # higher rank ok


def test_is_valid_bid_rejects_sans_atout():
    new_bid = {"trump": "sans_atout", "points": 100}
    assert not is_valid_bid(new_bid, None)


def test_is_valid_bid_capot_outranks_any_numeric_bid():
    current = {"trump": "♠", "points": 180}
    assert is_valid_bid({"trump": "♠", "points": CAPOT}, current)


def test_is_valid_bid_rejects_second_capot():
    current = {"trump": "♠", "points": CAPOT}
    assert not is_valid_bid({"trump": "♥", "points": CAPOT}, current)


def test_all_pass_is_not_restricted_by_bid_legality():
    # rules.py does not model "pass" as a bid action; passing is always
    # available independent of legal_bid_actions, which only enumerates
    # the concrete bid options a player could make instead of passing.
    actions_before_any_bid = legal_bid_actions(None)
    assert len(actions_before_any_bid) > 0  # bids exist, but pass remains legal too


# --- Card-play legality --------------------------------------------------------


def test_legal_cards_leading_player_can_play_anything():
    hand = [Card("7", "♠"), Card("A", "♥")]
    assert legal_cards_to_play(hand, [], "♠", None) == hand


def test_legal_cards_must_follow_suit():
    hand = [Card("7", "♠"), Card("A", "♥")]
    trick = [(Seat.N, Card("8", "♠"))]
    result = legal_cards_to_play(hand, trick, "♦", "♠")
    assert result == [Card("7", "♠")]


def test_legal_cards_must_trump_if_void_of_led_suit():
    hand = [Card("V", "♦"), Card("A", "♥")]  # void of led suit (♠), has trump ♦
    trick = [(Seat.N, Card("8", "♠"))]
    result = legal_cards_to_play(hand, trick, "♦", "♠")
    assert result == [Card("V", "♦")]


def test_legal_cards_must_overtrump_when_opponent_holds_highest_trump():
    # Opponent (N) played 10♦ (trump). Player (S) is void of led suit and
    # holds both a low trump (8♦) and a higher trump (A♦) plus a non-trump.
    hand = [Card("8", "♦"), Card("A", "♦"), Card("R", "♥")]
    trick = [(Seat.N, Card("8", "♠")), (Seat.E, Card("10", "♦"))]
    result = legal_cards_to_play(
        hand, trick, "♦", "♠", player_seat=Seat.S, partner_seat=Seat.W
    )
    assert result == [Card("A", "♦")]  # only the overtrumping card is legal


def test_legal_cards_under_trump_exception_when_partner_holds_highest_trump():
    # Partner (N) holds the current highest trump; player (S) need not
    # overtrump and may play any trump card they hold.
    hand = [Card("8", "♦"), Card("7", "♦")]
    trick = [(Seat.N, Card("10", "♦")), (Seat.E, Card("8", "♠"))]
    result = legal_cards_to_play(
        hand, trick, "♦", "♠", player_seat=Seat.S, partner_seat=Seat.N
    )
    assert result == hand  # free choice among trumps, no overtrump required


def test_legal_cards_tout_atout_no_cutting_concept():
    hand = [Card("A", "♥"), Card("A", "♦")]  # void of led suit
    trick = [(Seat.N, Card("8", "♠"))]
    result = legal_cards_to_play(hand, trick, None, "♠")
    assert result == hand  # free discard, no suit designated as trump


# --- trick_winner --------------------------------------------------------------


def test_trick_winner_highest_trump_wins():
    trick = [
        (Seat.N, Card("8", "♠")),
        (Seat.E, Card("10", "♦")),
        (Seat.S, Card("A", "♦")),
        (Seat.W, Card("7", "♦")),
    ]
    assert trick_winner(trick, "♦", "♠") == Seat.S


def test_trick_winner_highest_led_suit_wins_when_no_trump_played():
    trick = [
        (Seat.N, Card("8", "♠")),
        (Seat.E, Card("A", "♠")),
        (Seat.S, Card("7", "♥")),
        (Seat.W, Card("10", "♠")),
    ]
    assert trick_winner(trick, "♦", "♠") == Seat.E


# --- Scoring (A8-A11) ----------------------------------------------------------


def test_score_round_contract_made_normal():
    captured = {"NS": 100, "EW": 52}
    bid = {"team": "NS", "trump": "♠", "points": 90}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder=None)
    assert result["NS"]["contract_result"] == "made"
    assert result["NS"]["total"] == 100
    assert result["EW"]["total"] == 52


def test_score_round_contract_failed_defenders_get_full_pool():
    captured = {"NS": 70, "EW": 82}
    bid = {"team": "NS", "trump": "♠", "points": 90}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder=None)
    assert result["NS"]["contract_result"] == "failed"
    assert result["NS"]["total"] == 0
    assert result["EW"]["total"] == NORMAL_POOL


def test_score_round_tout_atout_pool():
    captured = {"NS": 50, "EW": 208}
    bid = {"team": "NS", "trump": "tout_atout", "points": 100}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder=None)
    assert result["NS"]["contract_result"] == "failed"
    assert result["EW"]["total"] == TOUT_ATOUT_POOL


def test_score_round_capot_achieved():
    captured = {"NS": 152, "EW": 0}
    bid = {"team": "NS", "trump": "♠", "points": CAPOT}
    result = score_round(captured, bid, coinche_level=1, capot_result=True, belote_holder=None)
    assert result["NS"]["contract_result"] == "capot_achieved"
    assert result["NS"]["total"] == 250
    assert result["EW"]["total"] == 0


def test_score_round_capot_failed():
    captured = {"NS": 100, "EW": 52}
    bid = {"team": "NS", "trump": "♠", "points": CAPOT}
    result = score_round(captured, bid, coinche_level=1, capot_result=False, belote_holder=None)
    assert result["NS"]["contract_result"] == "capot_failed"
    assert result["NS"]["total"] == 0
    assert result["EW"]["total"] == NORMAL_POOL


def test_score_round_coinche_doubles_winning_side():
    captured = {"NS": 100, "EW": 52}
    bid = {"team": "NS", "trump": "♠", "points": 90}
    result = score_round(captured, bid, coinche_level=2, capot_result=None, belote_holder=None)
    assert result["NS"]["total"] == 200  # attacking side doubled on success
    assert result["EW"]["total"] == 52  # defending side unmultiplied


def test_score_round_surcoinche_quadruples_winning_side():
    captured = {"NS": 70, "EW": 82}
    bid = {"team": "NS", "trump": "♠", "points": 90}
    result = score_round(captured, bid, coinche_level=4, capot_result=None, belote_holder=None)
    assert result["NS"]["total"] == 0
    assert result["EW"]["total"] == NORMAL_POOL * 4  # defending side (winner) quadrupled


def test_score_round_belote_bonus_credited_regardless_of_outcome():
    captured = {"NS": 70, "EW": 82}
    bid = {"team": "NS", "trump": "♠", "points": 90}
    result = score_round(
        captured, bid, coinche_level=1, capot_result=None, belote_holder="NS"
    )
    # NS's contract failed (0 base), but belote bonus still credited.
    assert result["NS"]["total"] == 20
    assert result["NS"]["belote_bonus"] == 20
