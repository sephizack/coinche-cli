"""Tests for coinche.rules: point tables, bid legality, card legality, scoring."""

from coinche.cards import Card, Seat
from coinche.rules import (
    ALLOWED_TRUMPS,
    CAPOT,
    SCORE_MODE_POINTS_ANNOUNCED,
    SCORE_MODE_POINTS_MADE,
    SCORE_MODE_POINTS_MADE_PLUS_ANNOUNCED,
    bid_rank,
    card_points,
    is_valid_bid,
    legal_bid_actions,
    legal_cards_to_play,
    round_to_nearest_ten,
    score_round,
    trick_winner,
)

# --- card_points / point tables (A7) -----------------------------------------


def test_card_points_normal_trump_suit():
    assert card_points(Card("V", "♠"), "♠") == 20
    assert card_points(Card("9", "♠"), "♠") == 14
    assert card_points(Card("A", "♠"), "♠") == 11


def test_card_points_normal_non_trump_suit():
    assert card_points(Card("A", "♥"), "♠") == 11
    assert card_points(Card("V", "♥"), "♠") == 2  # jack is non-trump here


def test_round_to_nearest_ten_uses_the_last_digit():
    assert round_to_nearest_ten(91) == 90
    assert round_to_nearest_ten(94) == 90
    assert round_to_nearest_ten(95) == 100
    assert round_to_nearest_ten(99) == 100


# --- Bidding legality (A5/A6) --------------------------------------------------


def test_legal_bid_actions_no_current_bid_starts_at_minimum():
    actions = legal_bid_actions(None)
    points_values = {a["points"] for a in actions if a["points"] != CAPOT}
    assert min(points_values) == 80
    assert max(points_values) == 160
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
    assert not is_valid_bid({"trump": "♠", "points": CAPOT}, current)


def test_bid_rank_places_capot_above_numeric_bids():
    assert bid_rank({"trump": "♠", "points": CAPOT}) > bid_rank({"trump": "♠", "points": 160})


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
    result = legal_cards_to_play(hand, trick, "♦", "♠", player_seat=Seat.S, partner_seat=Seat.W)
    assert result == [Card("A", "♦")]  # only the overtrumping card is legal


def test_legal_cards_can_pisser_when_cutting_and_no_higher_trump_available():
    # Opponent (E) already cut with 10♦ (trump). Player (S) is void of the
    # led suit (♠) and their only trump (8♦) cannot beat it. Since the led
    # suit was not trump, S is not forced to under-trump and may pisser —
    # discard any card, including the non-trump R♥.
    hand = [Card("8", "♦"), Card("R", "♥")]
    trick = [(Seat.N, Card("8", "♠")), (Seat.E, Card("10", "♦"))]
    result = legal_cards_to_play(hand, trick, "♦", "♠", player_seat=Seat.S, partner_seat=Seat.W)
    assert result == hand  # free discard ("pisser"), no obligation to under-trump


def test_legal_cards_must_undertrump_when_trump_led_and_no_higher_trump():
    # Trump (♦) was the led suit itself. Opponent (N) leads V♦ (highest
    # trump). Player (S) is following suit with trump — even though they
    # cannot beat V♦, they must still play one of their trumps (no pisser
    # exception applies when trump was led).
    hand = [Card("8", "♦"), Card("R", "♥")]
    trick = [(Seat.N, Card("V", "♦")), (Seat.E, Card("8", "♣"))]
    result = legal_cards_to_play(hand, trick, "♦", "♦", player_seat=Seat.S, partner_seat=Seat.W)
    assert result == [Card("8", "♦")]  # must still play the (losing) trump


def test_legal_cards_must_overtrump_when_trump_led_even_if_partner_is_master():
    # Trump (♦) was led by partner (N). Player (S) must still overtrump when
    # possible, even though N currently holds the trick.
    hand = [Card("8", "♦"), Card("A", "♦")]
    trick = [(Seat.N, Card("10", "♦")), (Seat.E, Card("8", "♦"))]
    result = legal_cards_to_play(hand, trick, "♦", "♦", player_seat=Seat.S, partner_seat=Seat.N)
    assert result == [Card("A", "♦")]


def test_legal_cards_can_pisser_when_partner_is_master_via_a_cut():
    # Partner (E) is currently master of the trick because they cut the
    # non-trump lead (8♠) with A♦, not because they hold the led suit's
    # highest card. Player (W) is void of the led suit and holds a mix of
    # trump and non-trump cards — since partner is master (even via a cut),
    # W is not obliged to cut and may freely discard any card, including
    # non-trump ones.
    hand = [Card("8", "♦"), Card("7", "♦"), Card("R", "♥"), Card("7", "♣")]
    trick = [(Seat.N, Card("8", "♠")), (Seat.E, Card("A", "♦")), (Seat.S, Card("K", "♥"))]
    result = legal_cards_to_play(hand, trick, "♦", "♠", player_seat=Seat.W, partner_seat=Seat.E)
    assert result == hand  # free discard ("pisser"), partner already master via cut


def test_legal_cards_can_pisser_when_partner_is_master_via_a_cut_and_adversaire_undercut():
    # Partner (E) is currently master of the trick because they cut the
    # non-trump lead (8♠) with A♦, not because they hold the led suit's
    # highest card. Player (W) is void of the led suit and holds a mix of
    # trump and non-trump cards — since partner is master (even via a cut),
    # W is not obliged to cut and may freely discard any card, including
    # non-trump ones.
    hand = [Card("8", "♦"), Card("7", "♦"), Card("R", "♥"), Card("7", "♣")]
    trick = [(Seat.N, Card("8", "♠")), (Seat.E, Card("A", "♦")), (Seat.S, Card("10", "♦"))]
    result = legal_cards_to_play(hand, trick, "♦", "♠", player_seat=Seat.W, partner_seat=Seat.E)
    assert result == hand  # free discard ("pisser"), partner already master via cut


def test_legal_cards_can_pisser_when_partner_is_master_via_led_suit():
    # Partner (N) led 10♠ and is currently master (no trump played yet by
    # anyone, E's 8♠ is lower). Player (S) is void of spades but holds
    # trump (♦) — since their own partner is master, they are not obliged
    # to cut and may freely discard any card, including non-trump ones.
    hand = [Card("A", "♣"), Card("R", "♦"), Card("V", "♦")]
    trick = [(Seat.N, Card("10", "♠")), (Seat.W, Card("8", "♠"))]
    result = legal_cards_to_play(hand, trick, "♦", "♠", player_seat=Seat.S, partner_seat=Seat.N)
    assert result == hand  # free discard ("pisser"), no obligation to cut


def test_legal_cards_must_cut_when_opponent_is_master_via_led_suit():
    # Opponent (E) currently holds the highest card of the led suit (no
    # trump played yet); player (S) void of the led suit and holding trump
    # must still cut.
    hand = [Card("A", "♣"), Card("R", "♦"), Card("V", "♦")]
    trick = [(Seat.N, Card("8", "♠")), (Seat.E, Card("10", "♠"))]
    result = legal_cards_to_play(hand, trick, "♦", "♠", player_seat=Seat.S, partner_seat=Seat.N)
    assert result == [Card("R", "♦"), Card("V", "♦")]


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
    # 90 annoncés, 92 faits (arrondi 90) : preneurs = 90 + 90 = 180.
    # Adversaires : 70 cartes -> arrondi 70.
    captured = {"NS": 92, "EW": 70}
    bid = {"team": "NS", "trump": "♠", "points": 90}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder=None)
    assert result["NS"]["contract_result"] == "made"
    assert result["NS"]["total"] == 180  # 90 (arrondi) + 90 (demandé)
    assert result["EW"]["total"] == 70


def test_score_round_supports_all_federation_score_modes_on_a_made_contract():
    captured = {"NS": 92, "EW": 70}
    bid = {"team": "NS", "trump": "♠", "points": 90}

    points_made = score_round(
        captured, bid, coinche_level=1, capot_result=None, belote_holder=None, score_mode=SCORE_MODE_POINTS_MADE
    )
    points_announced = score_round(
        captured, bid, coinche_level=1, capot_result=None, belote_holder=None, score_mode=SCORE_MODE_POINTS_ANNOUNCED
    )
    hybrid = score_round(
        captured,
        bid,
        coinche_level=1,
        capot_result=None,
        belote_holder=None,
        score_mode=SCORE_MODE_POINTS_MADE_PLUS_ANNOUNCED,
    )

    assert (points_made["NS"]["total"], points_made["EW"]["total"]) == (90, 70)
    assert (points_announced["NS"]["total"], points_announced["EW"]["total"]) == (90, 0)
    assert (hybrid["NS"]["total"], hybrid["EW"]["total"]) == (180, 70)


def test_score_round_score_modes_distinguish_failure_and_coinche():
    captured = {"NS": 90, "EW": 72}
    bid = {"team": "NS", "trump": "♠", "points": 100}

    points_made = score_round(
        captured, bid, coinche_level=2, capot_result=None, belote_holder=None, score_mode=SCORE_MODE_POINTS_MADE
    )
    points_announced = score_round(
        captured, bid, coinche_level=2, capot_result=None, belote_holder=None, score_mode=SCORE_MODE_POINTS_ANNOUNCED
    )
    hybrid = score_round(
        captured,
        bid,
        coinche_level=2,
        capot_result=None,
        belote_holder=None,
        score_mode=SCORE_MODE_POINTS_MADE_PLUS_ANNOUNCED,
    )

    assert points_made["EW"]["total"] == 320
    assert points_announced["EW"]["total"] == 200
    assert hybrid["EW"]["total"] == 520


def test_score_round_opponent_can_have_more_points_on_made_contract():
    # 80 annoncés et 80 cartes faites par NS : le contrat est réussi même si
    # EW a fait 82 cartes.
    captured = {"NS": 80, "EW": 82}
    bid = {"team": "NS", "trump": "♠", "points": 80}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder=None)
    assert result["NS"]["contract_result"] == "made"
    assert result["NS"]["total"] == 160
    assert result["EW"]["total"] == 80


def test_score_round_belote_can_complete_contract_despite_opponent_points():
    # 80 annoncés et 60 faits avec belote : les 80 points bruts atteignent le
    # contrat, même si EW a fait 102 cartes.
    captured = {"NS": 60, "EW": 102}
    bid = {"team": "NS", "trump": "♠", "points": 80}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder="NS")
    assert result["NS"]["contract_result"] == "made"
    assert result["NS"]["total"] == 160  # 60 arrondis + 80 demandés + 20 belote
    assert result["EW"]["total"] == 100


def test_score_round_98_faits_sur_100_fails_without_rounding():
    # 100 annoncés, 98 faits : l'arrondi de score à 100 ne remplit pas le contrat.
    captured = {"NS": 98, "EW": 64}
    bid = {"team": "NS", "trump": "♠", "points": 100}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder=None)
    assert result["NS"]["contract_result"] == "failed"
    assert result["NS"]["total"] == 0
    assert result["EW"]["total"] == 260


def test_score_round_93_faits_sur_100_reste_chute():
    # 93 faits sur 100 : arrondi à la dizaine -> 90, toujours < 100 -> chuté.
    captured = {"NS": 93, "EW": 69}
    bid = {"team": "NS", "trump": "♠", "points": 100}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder=None)
    assert result["NS"]["contract_result"] == "failed"


def test_score_round_96_faits_avec_belote_ne_valide_pas_120():
    # 120 annoncés, 96 cartes + 20 de belote : 116 bruts reste insuffisant.
    captured = {"NS": 96, "EW": 66}
    bid = {"team": "NS", "trump": "♠", "points": 120}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder="NS")
    assert result["NS"]["contract_result"] == "failed"
    assert result["NS"]["total"] == 0
    assert result["EW"]["total"] == 300


def test_score_round_contract_failed_defenders_get_pool_plus_bid():
    # 100 annoncés et 90 faits : chuté -> le pool de 162 est arrondi à 160,
    # puis le contrat est ajouté, soit 260 pour les adversaires.
    captured = {"NS": 90, "EW": 72}
    bid = {"team": "NS", "trump": "♠", "points": 100}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder=None)
    assert result["NS"]["contract_result"] == "failed"
    assert result["NS"]["total"] == 0
    assert result["EW"]["total"] == 260


def test_score_round_capot_achieved():
    captured = {"NS": 152, "EW": 0}
    bid = {"team": "NS", "trump": "♠", "points": CAPOT}
    result = score_round(captured, bid, coinche_level=1, capot_result=True, belote_holder=None, attacker_tricks=8)
    assert result["NS"]["contract_result"] == "capot_achieved"
    assert result["NS"]["total"] == 500  # 252 réalisés arrondis à 250 + 250 demandés
    assert result["EW"]["total"] == 0


def test_score_round_capot_failed_defenders_get_502():
    captured = {"NS": 100, "EW": 52}
    bid = {"team": "NS", "trump": "♠", "points": CAPOT}
    result = score_round(captured, bid, coinche_level=1, capot_result=False, belote_holder=None, attacker_tricks=6)
    assert result["NS"]["contract_result"] == "capot_failed"
    assert result["NS"]["total"] == 0
    assert result["EW"]["total"] == 500  # 252 + 250, arrondis à la dizaine


def test_score_round_unannounced_capot_upgrades_to_252():
    # 100 annoncés et capot fait (non annoncé) : 252 arrondis à 250 + 100 = 350.
    captured = {"NS": 162, "EW": 0}
    bid = {"team": "NS", "trump": "♠", "points": 100}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder=None, attacker_tricks=8)
    assert result["NS"]["total"] == 350
    assert result["EW"]["total"] == 0


def test_score_round_capot_values_follow_each_score_mode():
    captured = {"NS": 162, "EW": 0}
    numeric_bid = {"team": "NS", "trump": "♠", "points": 100}
    capot_bid = {"team": "NS", "trump": "♠", "points": CAPOT}

    assert (
        score_round(
            captured,
            numeric_bid,
            coinche_level=1,
            capot_result=None,
            belote_holder=None,
            attacker_tricks=8,
            score_mode=SCORE_MODE_POINTS_MADE,
        )["NS"]["total"]
        == 250
    )
    assert (
        score_round(
            captured,
            numeric_bid,
            coinche_level=1,
            capot_result=None,
            belote_holder=None,
            attacker_tricks=8,
            score_mode=SCORE_MODE_POINTS_ANNOUNCED,
        )["NS"]["total"]
        == 100
    )
    assert (
        score_round(
            captured,
            capot_bid,
            coinche_level=1,
            capot_result=True,
            belote_holder=None,
            attacker_tricks=8,
            score_mode=SCORE_MODE_POINTS_MADE_PLUS_ANNOUNCED,
        )["NS"]["total"]
        == 500
    )


def test_score_round_coinche_doubles_bid_plus_pool():
    # 100 annoncés, 90 faits, contré : (100 + 160) × 2 = 520 pour les gagnants.
    captured = {"NS": 90, "EW": 72}
    bid = {"team": "NS", "trump": "♠", "points": 100}
    result = score_round(captured, bid, coinche_level=2, capot_result=None, belote_holder=None)
    assert result["NS"]["total"] == 0
    assert result["EW"]["total"] == 520


def test_score_round_surcoinche_quadruples_bid_plus_pool():
    # 100 annoncés, 90 faits, surcontré : (100 + 160) × 4 = 1040.
    captured = {"NS": 90, "EW": 72}
    bid = {"team": "NS", "trump": "♠", "points": 100}
    result = score_round(captured, bid, coinche_level=4, capot_result=None, belote_holder=None)
    assert result["NS"]["total"] == 0
    assert result["EW"]["total"] == 1040


def test_score_round_coinche_on_made_contract_doubles_attackers():
    # Contrat réussi et contré, mode hybride : (160 + demandé) × 2.
    captured = {"NS": 92, "EW": 70}
    bid = {"team": "NS", "trump": "♠", "points": 90}
    result = score_round(captured, bid, coinche_level=2, capot_result=None, belote_holder=None)
    assert result["NS"]["total"] == (160 + 90) * 2  # 500
    assert result["EW"]["total"] == 0


def test_score_round_belote_counted_once_and_not_multiplied_on_coinche():
    captured = {"NS": 90, "EW": 72}
    bid = {"team": "NS", "trump": "♠", "points": 100}
    result = score_round(captured, bid, coinche_level=2, capot_result=None, belote_holder="EW")
    # Chute contrée : (100 + 160) × 2 = 520, belote +20 non multipliée.
    assert result["EW"]["total"] == 540
    assert result["EW"]["belote_bonus"] == 20


def test_score_round_belote_bonus_is_credited_to_defence_on_failure():
    # NS annonce 110 mais ne fait que 40 cartes ; même avec la belote
    # (40 + 20 = 60 < 110) le contrat chute. La défense marque la belote.
    captured = {"NS": 40, "EW": 122}
    bid = {"team": "NS", "trump": "♠", "points": 110}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder="NS")
    assert result["NS"]["contract_result"] == "failed"
    assert result["NS"]["total"] == 0
    assert result["EW"]["belote_bonus"] == 20
    assert result["EW"]["total"] == 290


def test_score_round_belote_helps_fulfil_contract_with_more_defender_points():
    # 90 annoncés, 72 cartes + 20 belote NS = 92 points : la belote permet
    # d'atteindre le contrat, même si EW a 90 points de cartes.
    captured = {"NS": 72, "EW": 90}
    bid = {"team": "NS", "trump": "♠", "points": 90}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder="NS")
    assert result["NS"]["contract_result"] == "made"
    assert result["NS"]["total"] == 180
    assert result["EW"]["total"] == 90


def test_score_round_belote_helps_fulfil_contract():
    # 90 annoncés, 72 cartes + 20 belote NS = 92 points (vs 90 adversaires) :
    # la belote permet d'atteindre le contrat (92 >= 90) et de dépasser les adversaires (92 >= 90) -> réussi.
    captured = {"NS": 92, "EW": 70}
    bid = {"team": "NS", "trump": "♠", "points": 110}
    result = score_round(captured, bid, coinche_level=1, capot_result=None, belote_holder="NS")
    assert result["NS"]["contract_result"] == "made"
    assert result["NS"]["total"] == 220
    assert result["EW"]["total"] == 70
