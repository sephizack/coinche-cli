"""Tests for the pure server-controlled bot strategy."""

import copy
import random

from coinche.bot import _choose_tactical_card, choose_bid, choose_card
from coinche.cards import Card, Seat
from coinche.game import TEAM_OF, Game


def _cards(*values: str) -> list[Card]:
    return [Card(value[:-1], value[-1]) for value in values]


def test_bot_bids_a_strong_trump_hand() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "A♠", "10♠", "A♥", "A♦", "7♣", "8♣")

    action = choose_bid(game, Seat.W)

    assert action == {"action": "bid", "trump": "♠", "points": 120}


def test_bot_does_not_add_side_aces_without_four_jack_nine_trumps() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "7♠", "A♥", "A♦", "A♣", "8♥", "7♦")

    assert choose_bid(game, Seat.W) == {"action": "bid", "trump": "♠", "points": 90}


def test_bot_supports_partner_eighty_with_the_missing_jack() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣", "D♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♠", "points": 90}


def test_bot_supports_partner_eighty_with_the_missing_nine() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("9♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣", "D♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♠", "points": 90}


def test_bot_does_not_open_with_a_single_trump_master() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣", "D♣")

    assert choose_bid(game, Seat.W) == {"action": "pass"}


def test_bot_supports_partner_by_one_trick_not_two() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "A♠", "7♥", "8♥", "7♦", "8♦", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♠", "points": 90}


def test_bot_passes_when_an_opponent_has_already_reached_its_safe_ceiling() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "A♠", "7♠", "A♥", "7♥", "8♦", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♥", points=110)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "pass"}


def test_bot_passes_with_a_weak_hand() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("7♠", "8♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣")

    assert choose_bid(game, Seat.W) == {"action": "pass"}


def test_fourth_bot_opens_at_eighty_instead_of_forcing_endless_redeals() -> None:
    game = Game()
    assert game.round_state is not None
    assert game.bid_state is not None
    game.round_state.hands[Seat.W] = _cards("7♠", "8♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣")
    game.bid_state.pass_streak = 3

    action = choose_bid(game, Seat.W)

    assert action["action"] == "bid"
    assert action["points"] == 80


def test_bot_discards_low_when_partner_is_winning() -> None:
    game = Game()
    assert game.round_state is not None
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trump = "♠"
    game.round_state.current_trick = [(Seat.N, Card("A", "♥")), (Seat.W, Card("7", "♥"))]
    game.round_state.hands[Seat.S] = _cards("A♦", "7♣")

    assert choose_card(game, Seat.S) == Card("7", "♣")


def test_bot_uses_the_cheapest_card_that_wins() -> None:
    game = Game()
    assert game.round_state is not None
    game.phase = "trick_play"
    game.next_to_act = Seat.W
    game.round_state.trump = "♠"
    game.round_state.current_trick = [(Seat.N, Card("D", "♥"))]
    game.round_state.hands[Seat.W] = _cards("A♥", "R♥")

    assert choose_card(game, Seat.W) == Card("R", "♥")


def test_card_choice_does_not_depend_on_real_hidden_hands() -> None:
    game = Game()
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")
    assert game.round_state is not None

    altered = copy.deepcopy(game)
    assert altered.round_state is not None
    altered.round_state.hands[Seat.N], altered.round_state.hands[Seat.E] = (
        altered.round_state.hands[Seat.E],
        altered.round_state.hands[Seat.N],
    )

    assert choose_card(game, Seat.W) == choose_card(altered, Seat.W)


def test_monte_carlo_team_outscores_greedy_play_on_a_fixed_deal() -> None:
    random_state = random.getstate()
    try:
        random.seed(0)
        game = Game(target_score=99999)
    finally:
        random.setstate(random_state)
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")

    def play_round(source: Game, optimize_ew: bool) -> int:
        simulation = copy.deepcopy(source)
        while simulation.phase == "trick_play":
            acting_seat = simulation.next_to_act
            if optimize_ew and TEAM_OF[acting_seat] == "EW":
                card = choose_card(simulation, acting_seat)
            else:
                card = _choose_tactical_card(simulation, acting_seat)
            result = simulation.submit_card(acting_seat, card)
            if result.get("round_complete"):
                score = result["round_score"]
                return score["EW"]["total"] - score["NS"]["total"]
        raise AssertionError("round did not complete")

    assert play_round(game, optimize_ew=True) > play_round(game, optimize_ew=False)
