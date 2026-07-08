"""Tests for coinche.game: auction, trick play, scoring, redeal, snapshot_for."""

from coinche.cards import Card
from coinche.game import TEAM_OF, Game, Seat


def _finalize_simple_contract(game: Game, trump: str = "♠", points: int = 80) -> dict:
    """Bid `points`/`trump` then pass 3 times to close the auction."""
    bidder = game.next_to_act
    game.submit_bid(bidder, "bid", trump=trump, points=points)
    seat = bidder.next()
    result = None
    for _ in range(3):
        result = game.submit_bid(seat, "pass")
        seat = result.get("next_to_act", seat)
    return result


def _play_full_round(game: Game) -> dict:
    """Play all 8 tricks, always choosing the first currently-legal card."""
    result = None
    for _ in range(8):
        for _ in range(4):
            seat = game.next_to_act
            card = game.play_options_for(seat)["legal_cards"][0]
            result = game.submit_card(seat, card)
    return result


def test_full_round_e2e_deal_bid_tricks_score():
    game = Game(initial_dealer=Seat.N)
    assert game.phase == "bidding"

    contract_result = _finalize_simple_contract(game, trump="♠", points=80)
    assert contract_result["outcome"] == "contract"
    assert game.phase == "trick_play"

    final_result = _play_full_round(game)
    assert final_result["trick_complete"] is True
    assert final_result["round_complete"] is True
    assert "round_score" in final_result
    assert set(final_result["round_score"].keys()) == {"NS", "EW"}
    total_scored = (
        final_result["round_score"]["NS"]["total"] + final_result["round_score"]["EW"]["total"]
    )
    assert final_result["cumulative_scores"]["NS"] + final_result["cumulative_scores"]["EW"] == total_scored


def test_all_pass_redeal_rotates_dealer():
    game = Game(initial_dealer=Seat.N)
    seat = game.next_to_act
    result = None
    for _ in range(4):
        result = game.submit_bid(seat, "pass")
        seat = result.get("next_to_act", seat)
    assert result["outcome"] == "redeal"
    assert result["dealer_seat"] == Seat.W  # N -> W per A1 rotation
    assert game.dealer == Seat.W
    assert game.phase == "bidding"


def test_coinched_contract_doubles_final_multiplier():
    game = Game(initial_dealer=Seat.N)
    bidder = game.next_to_act
    game.submit_bid(bidder, "bid", trump="♠", points=80)
    opponent_seat = bidder.next()  # rotation alternates teams every seat (A2)
    coinche_result = game.submit_bid(opponent_seat, "coinche")
    assert game.bid_state.coinche_level == 2

    seat = coinche_result["next_to_act"]
    result = None
    for _ in range(3):
        result = game.submit_bid(seat, "pass")
        seat = result.get("next_to_act", seat)
    assert result["outcome"] == "contract"
    assert result["coinche_level"] == 2

    final_result = _play_full_round(game)
    assert final_result["round_score"]["NS"]["multiplier"] == 2
    assert final_result["round_score"]["EW"]["multiplier"] == 2


def test_a13_tie_break_sudden_death_then_resolves():
    game = Game(target_score=100, initial_dealer=Seat.N)

    bidder = game.next_to_act
    attacking_team = TEAM_OF[bidder]
    defending_team = "EW" if attacking_team == "NS" else "NS"

    _finalize_simple_contract(game, trump="♠", points=80)
    assert game.phase == "trick_play"
    game.round_state.captured_points = {attacking_team: 100, defending_team: 52}
    game.round_state.trick_history = [
        {"winner_seat": bidder, "trick": [], "points_won": 0} for _ in range(8)
    ]
    game.round_state.tricks_played = 8
    game.round_state.belote_holder = None  # force determinism regardless of random deal
    game.cumulative_scores = {attacking_team: 0, defending_team: 48}

    result = game._finish_round()
    assert result["game_over"] is False  # tied at 100 == 100 -> sudden death continues
    assert game.cumulative_scores == {"NS": 100, "EW": 100}
    assert game.phase == "bidding"  # a new round was started automatically

    bidder2 = game.next_to_act
    attacking_team2 = TEAM_OF[bidder2]
    defending_team2 = "EW" if attacking_team2 == "NS" else "NS"

    _finalize_simple_contract(game, trump="♠", points=80)
    game.round_state.captured_points = {attacking_team2: 90, defending_team2: 10}
    game.round_state.trick_history = [
        {"winner_seat": bidder2, "trick": [], "points_won": 0} for _ in range(8)
    ]
    game.round_state.tricks_played = 8
    game.round_state.belote_holder = None  # force determinism regardless of random deal

    result = game._finish_round()
    assert result["game_over"] is True
    assert result["winning_team"] == attacking_team2
    assert game.game_over is True
    assert game.winning_team == attacking_team2


def test_belote_then_rebelote_announced_once_each_holding_king_and_queen():
    game = Game(initial_dealer=Seat.N)
    _finalize_simple_contract(game, trump="♠", points=80)
    rs = game.round_state
    holder = rs.leader  # the round's first leader (forced belote holder below)
    rs.belote_seat = holder
    rs.belote_holder = TEAM_OF[holder]

    # Force the holder's hand to contain both trump King and Queen so the
    # announcement can actually fire (keeps the original hand size of 8).
    other_cards = [c for c in rs.hands[holder] if not (c.suit == "♠" and c.rank in ("R", "D"))]
    rs.hands[holder] = [Card("R", "♠"), Card("D", "♠")] + other_cards[:6]

    assert game.next_to_act == holder
    king_result = game.submit_card(holder, Card("R", "♠"))
    assert king_result["belote_announcement"] == "belote"

    # Simulate the holder leading a later trick to play the Queen.
    rs.current_trick = []
    game.next_to_act = holder
    queen_result = game.submit_card(holder, Card("D", "♠"))
    assert queen_result["belote_announcement"] == "rebelote"

    # A further, unrelated play must not trigger any announcement.
    rs.current_trick = []
    game.next_to_act = holder
    other_card = rs.hands[holder][0]
    other_result = game.submit_card(holder, other_card)
    assert other_result["belote_announcement"] is None


def test_no_belote_announcement_without_both_king_and_queen():
    game = Game(initial_dealer=Seat.N)
    _finalize_simple_contract(game, trump="♠", points=80)
    rs = game.round_state
    holder = rs.leader
    rs.belote_seat = None  # nobody holds both trump King and Queen
    rs.belote_holder = None

    king_of_trump = next(
        (c for c in rs.hands[holder] if c.suit == "♠" and c.rank == "R"),
        None,
    )
    card_to_play = king_of_trump or rs.hands[holder][0]
    result = game.submit_card(holder, card_to_play)
    assert result["belote_announcement"] is None


def test_snapshot_for_mid_bidding():
    game = Game(initial_dealer=Seat.N)
    bidder = game.next_to_act
    game.submit_bid(bidder, "bid", trump="♥", points=90)

    snap = game.snapshot_for(bidder)
    assert snap["phase"] == "bidding"
    assert snap["current_highest_bid"]["points"] == 90
    assert snap["current_highest_bid"]["trump"] == "♥"
    assert len(snap["hand"]) == 8
    assert snap["whose_turn"] == bidder.next()


def test_snapshot_for_mid_trick():
    game = Game(initial_dealer=Seat.N)
    _finalize_simple_contract(game, trump="♥", points=80)
    assert game.phase == "trick_play"

    leader = game.next_to_act
    card = game.play_options_for(leader)["legal_cards"][0]
    game.submit_card(leader, card)

    snap = game.snapshot_for(leader)
    assert snap["phase"] == "trick_play"
    assert len(snap["current_trick"]) == 1
    assert len(snap["hand"]) == 7
    assert snap["trump"] == "♥"
