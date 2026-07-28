"""Tests for the pure server-controlled bot strategy."""

from coinche.bot import choose_bid, choose_card
from coinche.cards import Card, Seat
from coinche.game import Game


def _cards(*values: str) -> list[Card]:
    return [Card(value[:-1], value[-1]) for value in values]


def test_bot_bids_a_strong_trump_hand() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "A♠", "10♠", "A♥", "A♦", "7♣", "8♣")

    action = choose_bid(game, Seat.W)

    assert action["action"] == "bid"
    assert action["trump"] == "♠"
    assert action["points"] in {*range(80, 181, 10), "capot"}


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
