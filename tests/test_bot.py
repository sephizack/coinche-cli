"""Tests for the pure server-controlled bot strategy."""

from __future__ import annotations

import copy
import random
from collections import Counter

import pytest

import coinche.bot as bot
import coinche.bot_types.noob as noob
from coinche import server
from coinche.bot import (
    _auction_card_weights,
    _known_void_suits,
    _sample_hidden_hands,
    _select_tactical_card_for_simulation,
    _support_ceiling,
    _team_auction_supports_trump,
    available_bot_types,
    choose_bid,
    choose_card,
    configure_samples,
)
from coinche.bot_types import BotType, ClocloBot
from coinche.cards import Card, Seat
from coinche.game import TEAM_OF, Game


def _cards(*values: str):
    return [Card(value[:-1], value[-1]) for value in values]


def test_bot_entry_point_dispatches_to_the_requested_type(monkeypatch) -> None:
    class TestBot:
        def __init__(self, sample_count: int) -> None:
            self.sample_count = sample_count

        def choose_bid(self, game: Game, seat: Seat) -> dict:
            return {"action": "test_bid", "samples": self.sample_count}

        def choose_card(self, game: Game, seat: Seat) -> Card:
            return Card("7", "♠")

    monkeypatch.setitem(bot._BOT_TYPES, "test", TestBot)
    monkeypatch.setattr(bot, "MONTE_CARLO_SAMPLES", 17)

    assert "test" in available_bot_types()
    assert choose_bid(Game(), Seat.W, "test") == {"action": "test_bid", "samples": 17}
    assert choose_card(Game(), Seat.W, "test") == Card("7", "♠")


def test_smart_is_the_public_name_of_the_default_strategy() -> None:
    assert "smart" in available_bot_types()
    assert "default" not in available_bot_types()


def test_cloclo_is_an_available_strategy() -> None:
    assert "cloclo" in available_bot_types()


def test_cloclo_implements_the_bot_type_directly() -> None:
    assert ClocloBot.__bases__ == (BotType,)


def test_cloclo_bids_its_own_strong_controlled_hand() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "7♠", "A♥", "A♦", "A♣", "8♥", "7♦")

    action = choose_bid(game, Seat.W, "cloclo")

    assert action == {"action": "bid", "trump": "♠", "points": 140}
    assert action in game.bid_options_for(Seat.W)["legal_actions"]


def test_cloclo_supports_partner_with_a_missing_trump_master() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣", "D♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E, "cloclo") == {"action": "bid", "trump": "♠", "points": 90}


def test_cloclo_discards_the_cheapest_legal_card_when_partner_is_winning() -> None:
    game = Game()
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")
    assert game.round_state is not None
    game.round_state.current_trick = [(Seat.S, Card("A", "♥"))]
    game.round_state.hands[Seat.N] = _cards("7♦", "A♣")
    game.next_to_act = Seat.N

    card = choose_card(game, Seat.N, "cloclo")

    assert card == Card("7", "♦")
    assert card in game.play_options_for(Seat.N)["legal_cards"]


def test_maestro_adds_one_trick_with_jack_nine_and_a_side_ace() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "7♠", "A♥", "A♦", "A♣", "8♥", "7♦")

    assert choose_bid(game, Seat.W, "smart") == {"action": "bid", "trump": "♠", "points": 120}
    assert choose_bid(game, Seat.W, "maestro") == {"action": "bid", "trump": "♠", "points": 130}


def test_maestro_keeps_a_bid_without_firm_trump_control() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "7♠", "8♠", "A♥", "8♥", "7♦", "8♦", "7♣")

    assert choose_bid(game, Seat.W, "maestro") == choose_bid(game, Seat.W, "smart")


def test_noob_does_not_use_the_default_bot_fallback_opener() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "7♠", "8♠", "A♥", "8♥", "7♦", "8♦", "7♣")

    assert choose_bid(game, Seat.W, "smart") == {"action": "bid", "trump": "♠", "points": 90}
    assert choose_bid(game, Seat.W, "noob") == {"action": "pass"}


def test_noob_randomly_raises_its_teams_latest_bid(monkeypatch) -> None:
    game = Game()
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "bid", trump="♥", points=90)
    game.submit_bid(Seat.E, "pass")
    monkeypatch.setattr(noob.random, "randrange", lambda stop: 0)

    assert choose_bid(game, Seat.N, "noob") == {"action": "bid", "trump": "♥", "points": 100}


def test_noob_keeps_its_regular_bid_logic_when_random_raise_does_not_trigger(monkeypatch) -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "7♠", "8♠", "A♥", "8♥", "7♦", "8♦", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♥", points=80)
    game.submit_bid(Seat.S, "pass")
    monkeypatch.setattr(noob.random, "randrange", lambda stop: 1)

    assert choose_bid(game, Seat.E, "noob") == {"action": "pass"}


def test_noob_plays_a_random_legal_card(monkeypatch) -> None:
    game = Game()
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("7♠", "A♥")
    monkeypatch.setattr(noob.random, "choice", lambda cards: cards[-1])

    assert choose_card(game, Seat.W, "noob") == Card("A", "♥")


def _maestro_trump_controlled_game() -> Game:
    game = Game()
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")
    assert game.round_state is not None
    game.round_state.trick_history = [
        {
            "trick": [
                (Seat.N, Card("V", "♠")),
                (Seat.E, Card("9", "♠")),
                (Seat.S, Card("A", "♠")),
                (Seat.W, Card("10", "♠")),
            ]
        },
        {
            "trick": [
                (Seat.N, Card("R", "♠")),
                (Seat.E, Card("D", "♠")),
                (Seat.S, Card("8", "♠")),
                (Seat.W, Card("7", "♠")),
            ]
        },
    ]
    return game


def test_maestro_leads_low_to_draw_an_unseen_ten_from_ace_king_length() -> None:
    game = _maestro_trump_controlled_game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("A♥", "R♥", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣")

    assert choose_card(game, Seat.W, "smart") == Card("A", "♥")
    assert choose_card(game, Seat.W, "maestro") == Card("7", "♥")


def test_maestro_does_not_bait_the_ten_while_an_opponent_may_still_ruff() -> None:
    game = Game()
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("A♥", "R♥", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣")

    assert choose_card(game, Seat.W, "maestro") == Card("A", "♥")


def test_maestro_cashes_ace_then_master_king_after_the_ten_falls() -> None:
    game = _maestro_trump_controlled_game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("A♥", "R♥", "7♦", "8♦", "7♣", "8♣")
    game.round_state.trick_history.append({"trick": [(Seat.N, Card("10", "♥"))]})

    assert choose_card(game, Seat.W, "maestro") == Card("A", "♥")

    game.round_state.hands[Seat.W].remove(Card("A", "♥"))
    game.round_state.trick_history.append({"trick": [(Seat.W, Card("A", "♥"))]})

    assert choose_card(game, Seat.W, "maestro") == Card("R", "♥")


def test_bot_bids_a_strong_trump_hand() -> None:
    # V-9-A-10 (the four boss trumps) plus two side aces: near-total trump
    # control makes both aces cash and promises heavy partner help, so the pair
    # contract is worth well above the bare minimum.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "A♠", "10♠", "A♥", "A♦", "7♣", "8♣")

    action = choose_bid(game, Seat.W)

    assert action == {"action": "bid", "trump": "♠", "points": 130}


def test_bid_choice_does_not_depend_on_real_hidden_hands() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "A♠", "10♠", "A♥", "A♦", "7♣", "8♣")

    altered = copy.deepcopy(game)
    assert altered.round_state is not None
    hidden_seats = [Seat.N, Seat.E, Seat.S]
    hidden_hands = [list(game.round_state.hands[seat]) for seat in hidden_seats]
    hidden_dealt_hands = [list(game.round_state.dealt_hands[seat]) for seat in hidden_seats]
    for target, hand, dealt_hand in zip(
        hidden_seats,
        hidden_hands[1:] + hidden_hands[:1],
        hidden_dealt_hands[1:] + hidden_dealt_hands[:1],
        strict=True,
    ):
        altered.round_state.hands[target] = hand
        altered.round_state.dealt_hands[target] = dealt_hand

    assert choose_bid(game, Seat.W) == choose_bid(altered, Seat.W)


def test_bot_values_side_aces_behind_a_jack_nine_even_with_only_three_trumps() -> None:
    # V♠+9♠ give firm trump control, so the three outside aces are cashable and
    # count toward the bid -- and, controlled like this, promise the partner will
    # cash behind them. Point potential plus that partner help, not raw trump
    # count, drives the opening well past a bare 90.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "7♠", "A♥", "A♦", "A♣", "8♥", "7♦")

    assert choose_bid(game, Seat.W) == {"action": "bid", "trump": "♠", "points": 120}


def test_side_aces_raise_the_opening_over_the_same_trumps_without_them() -> None:
    # Same three-card Valet-9 trump holding in both hands; the only difference
    # is outside aces. The hand with the aces must be valued strictly higher --
    # opening value tracks point potential, not the (identical) trump count.
    from coinche.bot import _point_potential

    with_aces = _cards("V♠", "9♠", "7♠", "A♥", "A♦", "A♣", "8♥", "7♦")
    without_aces = _cards("V♠", "9♠", "7♠", "8♥", "7♥", "8♦", "7♦", "8♣")

    assert _point_potential(with_aces, "♠") > _point_potential(without_aces, "♠")


def test_partner_allowance_lifts_a_controlled_ace_hand_toward_a_pair_contract() -> None:
    # A bid is a pair contract. With firm trump control (V+9) the partner will
    # cash behind the bidder's side aces, so the opening ceiling exceeds what the
    # hand takes on its own. No control, no meaningful allowance.
    from coinche.bot import _opening_ceiling, _partner_allowance, _point_potential

    controlled = _cards("V♠", "9♠", "7♠", "A♥", "A♦", "A♣", "8♥", "7♦")
    uncontrolled = _cards("7♠", "8♠", "A♥", "A♦", "A♣", "8♥", "7♦", "8♦")

    assert _partner_allowance(controlled, "♠") > _partner_allowance(uncontrolled, "♠")
    assert _opening_ceiling(controlled, "♠") > _point_potential(controlled, "♠")


def test_bot_supports_partner_eighty_with_the_missing_jack() -> None:
    # V completes the partner's 80 signal: partner_looking_for_34 jumps 1 step.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣", "D♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♠", "points": 90}


def test_bot_supports_partner_eighty_with_the_missing_nine() -> None:
    # 9 completes the partner's 80 signal: partner_looking_for_34 jumps 1 step.
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
    # V+9 give partner_looking_for_34: jump 1 step (80→90).
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "A♠", "7♥", "8♥", "7♦", "8♦", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♠", "points": 90}


def test_bot_supports_partner_high_bid_with_side_ace() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "A♠", "7♠", "A♥", "7♥", "8♦", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♥", points=110)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♥", "points": 120}


def test_bot_passes_with_a_weak_hand() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("7♠", "8♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣")

    assert choose_bid(game, Seat.W) == {"action": "pass"}


@pytest.mark.parametrize(
    ("points", "hand", "expected"),
    [
        (100, _cards("7♠", "8♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣"), {"action": "pass"}),
        (100, _cards("V♠", "9♠", "7♠", "7♥", "8♥", "7♦", "8♦", "7♣"), {"action": "pass"}),
        (100, _cards("V♠", "9♠", "7♠", "A♥", "7♥", "8♦", "7♣", "D♣"), {"action": "coinche"}),
        (120, _cards("V♠", "9♠", "7♠", "7♥", "8♥", "7♦", "8♦", "7♣"), {"action": "pass"}),
        (120, _cards("V♠", "9♠", "7♠", "A♥", "7♥", "8♦", "7♣", "D♣"), {"action": "coinche"}),
        (140, _cards("V♠", "9♠", "7♠", "7♥", "8♥", "7♦", "8♦", "7♣"), {"action": "coinche"}),
        (140, _cards("V♠", "9♦", "7♠", "A♥", "7♥", "8♦", "7♣", "D♣"), {"action": "coinche"}),
        (160, _cards("V♠", "9♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣"), {"action": "coinche"}),
        ("capot", _cards("V♠", "9♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣"), {"action": "coinche"}),
        ("capot", _cards("V♠", "9♥", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣"), {"action": "coinche"}),
    ],
)
def test_bot_coinche_threshold_drops_as_opponent_contract_rises(
    points: int | str, hand: list[Card], expected: dict
) -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.S] = hand
    game.submit_bid(Seat.W, "bid", trump="♠", points=points)

    assert choose_bid(game, Seat.S) == expected


@pytest.mark.parametrize(
    ("points", "hand", "expected"),
    [
        (100, _cards("V♠", "9♠", "7♠", "A♥", "7♥", "8♦", "7♣", "D♣"), {"action": "pass"}),
        (100, _cards("V♠", "9♠", "A♠", "10♠", "R♠", "A♥", "7♥", "8♦"), {"action": "surcoinche"}),
        (120, _cards("V♠", "9♠", "7♠", "A♥", "7♥", "8♦", "7♣", "D♣"), {"action": "pass"}),
        (120, _cards("V♠", "9♠", "A♠", "10♠", "A♥", "7♥", "8♦", "7♣"), {"action": "pass"}),
        (120, _cards("V♠", "9♠", "A♠", "10♠", "A♥", "7♥", "A♦", "7♣"), {"action": "surcoinche"}),
        (140, _cards("V♠", "9♠", "A♠", "10♠", "A♥", "7♥", "8♦", "7♣"), {"action": "pass"}),
        (140, _cards("V♠", "9♠", "A♠", "10♠", "R♠", "A♥", "7♥", "8♦"), {"action": "pass"}),
        (140, _cards("V♠", "9♠", "A♠", "10♠", "R♠", "A♥", "7♥", "A♦"), {"action": "surcoinche"}),
        (160, _cards("V♠", "9♠", "7♠", "7♥", "8♥", "7♦", "8♦", "7♣"), {"action": "pass"}),
        (160, _cards("V♠", "9♠", "A♠", "10♠", "R♠", "A♥", "7♥", "8♦"), {"action": "pass"}),
        (160, _cards("V♠", "9♠", "A♠", "10♠", "R♠", "A♥", "10♥", "A♦"), {"action": "surcoinche"}),
        ("capot", _cards("V♠", "9♠", "A♠", "10♠", "R♠", "A♥", "10♥", "A♦"), {"action": "surcoinche"}),
    ],
)
def test_bot_surcoinche_threshold_rises_with_contract_value(
    points: int | str, hand: list[Card], expected: dict
) -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = hand
    game.submit_bid(Seat.W, "bid", trump="♠", points=points)
    game.submit_bid(Seat.S, "coinche")
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")

    assert choose_bid(game, Seat.W) == expected


def test_bot_opens_eighty_with_valet_two_trumps_and_side_ace() -> None:
    # V + 2 other low trumps + 1 side Ace: normal algo passes (opening_ceiling
    # is None) but the fallback recognises the hand as worth an 80/90 attempt.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "7♠", "8♠", "A♥", "8♥", "7♦", "8♦", "7♣")

    action = choose_bid(game, Seat.W)

    assert action == {"action": "bid", "trump": "♠", "points": 90}


def test_bot_passes_with_valet_two_trumps_but_no_side_ace() -> None:
    # V + 2 other low trumps but no side Ace: too weak to open.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "7♠", "8♠", "10♥", "8♥", "7♦", "8♦", "7♣")

    assert choose_bid(game, Seat.W) == {"action": "pass"}


def test_bot_passes_with_valet_one_trump_and_side_ace() -> None:
    # V + only 1 other trump + side Ace: not enough trumps to open.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "7♠", "A♥", "A♦", "8♥", "7♦", "8♦", "7♣")

    assert choose_bid(game, Seat.W) == {"action": "pass"}


def test_bot_does_not_fallback_when_someone_already_bid() -> None:
    # Someone already bid — the fallback only triggers when current is None.
    # E holds a fallback-eligible hand (V+2 trumps+side Ace) but the fallback
    # must not fire because the normal support path takes precedence.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "7♠", "8♠", "A♥", "8♥", "7♦", "8♦", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♥", points=80)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "pass"}


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


def test_discard_shortens_the_shortest_side_suit() -> None:
    # Opponent (N) is winning with a card S cannot beat, so S discards. Both 7♦
    # and 7♣ are worth zero points, but ♦ is a singleton while ♣ is longer:
    # throwing the singleton ♦ makes S void there and able to ruff ♦ next time.
    game = Game()
    assert game.round_state is not None
    game.round_state.trump = "♠"
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.current_trick = [(Seat.N, Card("A", "♥"))]
    game.round_state.hands[Seat.S] = _cards("7♦", "7♣", "8♣", "9♣")

    assert _select_tactical_card_for_simulation(game, Seat.S) == Card("7", "♦")


def test_bot_cashes_the_requested_suit_ace_when_no_trump_is_in_the_trick() -> None:
    # N leads a side suit (♥) while trumps are ♠. W holds the Ace and King of
    # the led suit and nobody has ruffed yet, so the bot cashes the master Ace
    # to bank the trick outright instead of exposing it to a later ruff.
    game = Game()
    assert game.round_state is not None
    game.phase = "trick_play"
    game.next_to_act = Seat.W
    game.round_state.trump = "♠"
    game.round_state.current_trick = [(Seat.N, Card("D", "♥"))]
    game.round_state.hands[Seat.W] = _cards("A♥", "R♥")

    assert choose_card(game, Seat.W) == Card("A", "♥")


def test_card_choice_does_not_depend_on_any_real_hidden_hand() -> None:
    game = Game()
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")
    assert game.round_state is not None

    altered = copy.deepcopy(game)
    assert altered.round_state is not None
    hidden_seats = [Seat.N, Seat.E, Seat.S]
    hidden_hands = [list(game.round_state.hands[seat]) for seat in hidden_seats]
    hidden_dealt_hands = [list(game.round_state.dealt_hands[seat]) for seat in hidden_seats]
    for target, hand, dealt_hand in zip(
        hidden_seats,
        hidden_hands[1:] + hidden_hands[:1],
        hidden_dealt_hands[1:] + hidden_dealt_hands[:1],
        strict=True,
    ):
        altered.round_state.hands[target] = hand
        altered.round_state.dealt_hands[target] = dealt_hand

    assert choose_card(game, Seat.W) == choose_card(altered, Seat.W)


def test_partner_of_taker_leads_a_non_master_trump_to_help_pull() -> None:
    # N took the contract; S (the partner) is on lead holding the 9 of trump.
    # While opponents may still hold trumps and the Valet has not fallen, the
    # declaring side leads trump to strip the opponents' ruffers, so S opens
    # with its lone 9 of trump rather than idly discarding a side card.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("9♠", "7♣", "8♦")

    assert choose_card(game, Seat.S) == Card("9", "♠")


def test_partner_pulls_with_a_non_master_trump_when_taker_is_known_void(monkeypatch) -> None:
    # N took the contract but publicly discarded on a side lead while S was not
    # master, proving N has no trump. S may now lead the non-master 9♠ safely:
    # N cannot overtrump a partner and the lead strips an opponent ruffer.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.W,
            "trick": [
                (Seat.W, Card("A", "♥")),
                (Seat.S, Card("7", "♥")),
                (Seat.E, Card("8", "♥")),
                (Seat.N, Card("7", "♦")),
            ],
            "points_won": 11,
        }
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("9♠", "7♣", "8♦")

    assert _select_tactical_card_for_simulation(game, Seat.S) == Card("9", "♠")
    monkeypatch.setattr(bot, "MONTE_CARLO_SAMPLES", 20)
    assert choose_card(game, Seat.S) == Card("9", "♠")


def test_partner_of_taker_leads_a_master_trump_to_help_pull() -> None:
    # N took the contract; S (the partner) is on lead. Unlike the 9 above, S now
    # holds the trump Valet -- the outright master -- so leading it wins the trick
    # outright and never forces N to overtrump their own partner. The whole
    # declaring team pulls the opponents' trumps, not just the taker.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("V♠", "7♣", "8♦")

    assert _select_tactical_card_for_simulation(game, Seat.S) == Card("V", "♠")


def test_taker_leads_its_top_trump() -> None:
    # When the bot IS the taker, pulling trump is correct.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.S, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("9♠", "7♣", "8♦")

    assert choose_card(game, Seat.S) == Card("9", "♠")


# A prior trick that burned the trump V, 9 and A (♠), so the 10 of trump left
# in S's hand is now the outright master trump -- yet it is worth only 10 points
# while a side Ace is worth 11. This is what separates "cash the higher-scoring
# side Ace" (old behaviour) from "pull with the master trump first" (new).
_TRUMP_HONORS_BURNED = {
    "winner_seat": Seat.N,
    "trick": [
        (Seat.N, Card("V", "♠")),
        (Seat.E, Card("9", "♠")),
        (Seat.S, Card("8", "♠")),
        (Seat.W, Card("A", "♠")),
    ],
    "points_won": 33,
}


def test_taker_pulls_master_trump_before_cashing_a_side_ace() -> None:
    # S is the taker holding the master trump (10♠, since V/9/A♠ are gone) plus a
    # side Ace worth MORE points. While opponents may still hold trump, S must
    # LEAD the master trump to pull theirs out rather than cash the ace and risk
    # being ruffed later -- even though the ace scores more on this one trick.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.S, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [_TRUMP_HONORS_BURNED]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("10♠", "A♥", "7♦")

    assert _select_tactical_card_for_simulation(game, Seat.S) == Card("10", "♠")


def test_taker_leads_a_non_master_trump_before_cashing_a_side_ace() -> None:
    # S is the taker holding a NON-master trump (9♠ -- V♠ is still out) alongside
    # a side Ace. With opponents still able to hold trump, S must lead the trump
    # to force theirs out before cashing the ace, rather than cashing the ace and
    # leaving the side suit open to a later ruff. This is the "dumb bot cashes the
    # ace first" case for the taker himself, not just the partner.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.S, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("9♠", "A♥", "7♦")

    assert _select_tactical_card_for_simulation(game, Seat.S) == Card("9", "♠")


def test_declaring_partner_hook_pulls_before_monte_carlo_when_protecting_ace_or_ten(monkeypatch) -> None:
    # This tests the actual `choose_card` hook for the taker's partner. The
    # Monte-Carlo sampler must not even run: a lead with an unprotected A/10
    # and possible defensive trumps is forced to pull with the best available
    # trump.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("9♠", "A♥", "10♦", "7♣")

    def unexpected_sampling(*args: object) -> list[dict[Seat, list[Card]]]:
        raise AssertionError("the hard trump-pull rule must run before Monte-Carlo sampling")

    monkeypatch.setattr(bot, "_sample_hidden_hands", unexpected_sampling)

    assert choose_card(game, Seat.S) == Card("9", "♠")


def test_hard_trump_hook_pulls_a_master_after_jack_has_fallen(monkeypatch) -> None:
    # V/9/A♠ have already fallen, so 10♠ is a certain winner even though the
    # Valet is no longer available for the partner to cover the lead.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [_TRUMP_HONORS_BURNED]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("10♠", "A♥", "7♦")

    def unexpected_sampling(*args: object) -> list[dict[Seat, list[Card]]]:
        raise AssertionError("a master trump must be played before Monte-Carlo sampling")

    monkeypatch.setattr(bot, "_sample_hidden_hands", unexpected_sampling)

    assert choose_card(game, Seat.S) == Card("10", "♠")


def test_opening_cashes_a_side_ace_after_the_jack_has_fallen_but_not_nine_but_annonce_pourrie(monkeypatch) -> None:
    # Once V♠ has been played, 10♠ is not a guaranteed winner. The opening
    # policy instead cashes the available side Ace before Monte-Carlo runs.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("V", "♠")),
                (Seat.W, Card("7", "♠")),
                (Seat.S, Card("8", "♠")),
                (Seat.E, Card("R", "♠")),
            ],
            "points_won": 24,
        }
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("10♠", "A♥", "7♣")

    assert not _team_auction_supports_trump(game, Seat.S, "♠"), (
        "Only announces 80 points, so the team is not expected to have V and 9 of trump"
    )

    samples_called = False

    def no_samples(*args: object) -> list[dict[Seat, list[Card]]]:
        nonlocal samples_called
        samples_called = True
        return []

    monkeypatch.setattr(bot, "_sample_hidden_hands", no_samples)
    assert choose_card(game, Seat.S) == Card("A", "♥")
    assert not samples_called


def test_opening_cashes_a_side_ace_after_the_jack_has_fallen_but_not_nine_with_big_annonce_self(monkeypatch) -> None:
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.history.append({"team": "NS", "seat": Seat.S, "action": "bid", "trump": "♠", "points": 130})
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.S, "trump": "♠", "points": 130}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("V", "♠")),
                (Seat.W, Card("7", "♠")),
                (Seat.S, Card("8", "♠")),
                (Seat.E, Card("R", "♠")),
            ],
            "points_won": 24,
        }
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("10♠", "A♥", "7♣")

    samples_called = False

    assert not _team_auction_supports_trump(game, Seat.S, "♠"), (
        "Announces 130 points himself, without partner support, so we dont assume anything on the team"
    )

    def no_samples(*args: object) -> list[dict[Seat, list[Card]]]:
        nonlocal samples_called
        samples_called = True
        return []

    monkeypatch.setattr(bot, "_sample_hidden_hands", no_samples)
    assert choose_card(game, Seat.S) == Card("A", "♥")
    assert not samples_called


def test_opening_cashes_a_side_ace_after_the_jack_has_fallen_but_not_nine_with_big_annonce(monkeypatch) -> None:
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.history.append({"team": "NS", "seat": Seat.N, "action": "bid", "trump": "♠", "points": 130})
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 130}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("V", "♠")),
                (Seat.W, Card("7", "♠")),
                (Seat.S, Card("8", "♠")),
                (Seat.E, Card("R", "♠")),
            ],
            "points_won": 24,
        }
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("10♠", "A♥", "7♣")

    samples_called = False

    assert _team_auction_supports_trump(game, Seat.S, "♠"), (
        "Announces 130 points, so the team is expected to have V and 9 of trump"
    )

    def no_samples(*args: object) -> list[dict[Seat, list[Card]]]:
        nonlocal samples_called
        samples_called = True
        return []

    monkeypatch.setattr(bot, "_sample_hidden_hands", no_samples)
    assert choose_card(game, Seat.S) == Card("10", "♠")
    assert not samples_called


def test_opening_cashes_a_side_ace_after_the_jack_has_fallen_but_not_nine_with_montage_annonce(monkeypatch) -> None:
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.history.append({"team": "NS", "seat": Seat.S, "action": "bid", "trump": "♠", "points": 80})
    game.bid_state.history.append({"team": "NS", "seat": Seat.N, "action": "bid", "trump": "♠", "points": 90})
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 90}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("V", "♠")),
                (Seat.W, Card("7", "♠")),
                (Seat.S, Card("8", "♠")),
                (Seat.E, Card("R", "♠")),
            ],
            "points_won": 24,
        }
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("10♠", "A♥", "7♣")

    assert _team_auction_supports_trump(game, Seat.S, "♠"), (
        "Announced 80 then partner increased, so the team is expected to have V and 9 of trump"
    )

    samples_called = False

    def no_samples(*args: object) -> list[dict[Seat, list[Card]]]:
        nonlocal samples_called
        samples_called = True
        return []

    monkeypatch.setattr(bot, "_sample_hidden_hands", no_samples)
    assert choose_card(game, Seat.S) == Card("10", "♠")
    assert not samples_called


def test_opening_cashes_a_side_ace_after_the_jack_and_nine_has_fallen(monkeypatch) -> None:
    # Once V♠ has been played, 10♠ is not a guaranteed winner. The opening
    # policy instead cashes the available side Ace before Monte-Carlo runs.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("V", "♠")),
                (Seat.W, Card("9", "♠")),
                (Seat.S, Card("8", "♠")),
                (Seat.E, Card("R", "♠")),
            ],
            "points_won": 24,
        }
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("10♠", "A♥", "7♣")

    samples_called = False

    def no_samples(*args: object) -> list[dict[Seat, list[Card]]]:
        nonlocal samples_called
        samples_called = True
        return []

    monkeypatch.setattr(bot, "_sample_hidden_hands", no_samples)
    assert choose_card(game, Seat.S) == Card("A", "♥")
    assert not samples_called


def test_opening_cashes_a_side_ace_when_partner_cannot_hold_unseen_nine(monkeypatch) -> None:
    # The 9♠ remains unseen, but N discarded while E was winning a side-suit
    # trick, which proves N has no trump. S must not lead 10♠ merely to let a
    # partner that cannot hold the 9♠ cover it.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("V", "♠")),
                (Seat.E, Card("R", "♠")),
                (Seat.S, Card("8", "♠")),
                (Seat.W, Card("7", "♠")),
            ],
            "points_won": 24,
        },
        {
            "winner_seat": Seat.E,
            "trick": [
                (Seat.E, Card("A", "♦")),
                (Seat.S, Card("7", "♦")),
                (Seat.N, Card("7", "♣")),
                (Seat.W, Card("8", "♦")),
            ],
            "points_won": 11,
        },
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("10♠", "A♥", "7♣")

    def no_samples(*args: object) -> list[dict[Seat, list[Card]]]:
        return []

    monkeypatch.setattr(bot, "_sample_hidden_hands", no_samples)
    assert choose_card(game, Seat.S) == Card("A", "♥")


def test_taker_stops_pulling_once_all_opponent_trumps_are_gone() -> None:
    # Same master-trump-vs-side-ace hand, but now both of S's opponents (E and W)
    # are provably void of trump: each discarded a side card when partner N led
    # trump. With no opponent trump left to pull, the taker cashes the
    # higher-scoring side Ace instead of leading the master trump into thin air.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.S, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("V", "♠")),
                (Seat.E, Card("7", "♥")),
                (Seat.S, Card("8", "♠")),
                (Seat.W, Card("7", "♦")),
            ],
            "points_won": 22,
        },
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("9", "♠")),
                (Seat.E, Card("8", "♥")),
                (Seat.S, Card("D", "♠")),
                (Seat.W, Card("8", "♦")),
            ],
            "points_won": 17,
        },
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("A", "♠")),
                (Seat.E, Card("9", "♥")),
                (Seat.S, Card("7", "♠")),
                (Seat.W, Card("9", "♦")),
            ],
            "points_won": 11,
        },
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("10♠", "A♥", "7♦")

    # Assert against the rollout policy directly: it holds the pull-trumps rule
    # and is deterministic, unlike the Monte-Carlo `choose_card` wrapper.
    assert _select_tactical_card_for_simulation(game, Seat.S) == Card("A", "♥")


def test_declarer_keeps_a_master_that_a_known_defender_can_ruff() -> None:
    # W cut a heart in the previous trick, so S must not lead the otherwise
    # master A♥ while W may still hold trump. S is the taker's partner and has
    # no master trump with which to pull first; developing with 7♣ keeps the
    # Ace for a safer opportunity.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.W,
            "trick": [
                (Seat.N, Card("7", "♥")),
                (Seat.W, Card("7", "♠")),
                (Seat.S, Card("8", "♠")),
                (Seat.E, Card("7", "♦")),
            ],
            "points_won": 0,
        }
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("A♥", "9♠", "7♣", "8♦")

    assert _select_tactical_card_for_simulation(game, Seat.S) == Card("7", "♣")


def test_defender_on_lead_does_not_open_a_trump_without_the_master() -> None:
    # EW took the contract; S is defending and on lead holding a lone 9♠ trump
    # (not the master, since the ♠ Valet is unseen). Opening trump only helps the
    # takers draw a round, so S must lead a side card instead of the trump.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "EW", "seat": Seat.W, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("9♠", "7♣", "8♦")

    assert choose_card(game, Seat.S).suit != "♠"


def test_defender_on_lead_tries_an_unplayed_side_suit_before_master_trump() -> None:
    # EW took the contract; S holds the master ♠ Valet, but both side suits are
    # still unplayed. The opening policy explores a new side suit first, using
    # the lowest card among those candidates.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "EW", "seat": Seat.W, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("V♠", "7♣", "8♦")

    assert choose_card(game, Seat.S) == Card("7", "♣")


def test_defender_holding_the_master_does_not_lead_trump_when_opponents_are_void() -> None:
    # EW took the contract; S is defending and on lead holding the ♠ Valet, the
    # outright master. But both opponents (E and W) discarded a side card when
    # partner N led trump, proving each is void: the only outstanding trumps sit
    # with the partner. Leading the master now strips no ruffer and just wastes
    # the lead, so S must open a side suit instead.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "EW", "seat": Seat.W, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.trick_history = [
        {
            "winner_seat": Seat.N,
            "trick": [
                (Seat.N, Card("A", "♠")),
                (Seat.E, Card("7", "♥")),
                (Seat.S, Card("8", "♠")),
                (Seat.W, Card("7", "♦")),
            ],
            "points_won": 11,
        }
    ]
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("V♠", "7♣", "8♦")

    assert choose_card(game, Seat.S).suit != "♠"


def test_discarding_on_a_side_lead_reveals_a_trump_void() -> None:
    # ♥ is led, W neither follows ♥ nor trumps (♠) while the winner so far (N)
    # is not W's partner: the rules would have forced a cut, so W holds no trump.
    game = Game()
    assert game.round_state is not None
    game.round_state.trump = "♠"
    game.round_state.current_trick = [(Seat.N, Card("A", "♥")), (Seat.W, Card("7", "♦"))]

    voids = _known_void_suits(game.round_state)

    assert "♥" in voids[Seat.W]
    assert "♠" in voids[Seat.W]


def test_discard_behind_a_played_trump_does_not_imply_a_trump_void() -> None:
    # A trump is already down, so W may legally "pisser" a low side card rather
    # than under-trump. The discard says nothing about W's trumps.
    game = Game()
    assert game.round_state is not None
    game.round_state.trump = "♠"
    game.round_state.current_trick = [(Seat.N, Card("A", "♥")), (Seat.E, Card("7", "♠")), (Seat.W, Card("7", "♦"))]

    voids = _known_void_suits(game.round_state)

    assert "♥" in voids[Seat.W]
    assert "♠" not in voids[Seat.W]


def test_determinization_uses_an_earlier_bid_not_just_the_final_contract() -> None:
    # W first announced hearts, then E won the auction in spades. The V♥ still
    # belongs more often to W in samples: every public bid remains evidence,
    # not only the final contract's trump honours.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "EW", "seat": Seat.E, "trump": "♠", "points": 90}
    game.bid_state.history = [
        {"seat": Seat.W, "action": "bid", "trump": "♥", "points": 80},
        {"seat": Seat.S, "action": "pass"},
        {"seat": Seat.E, "action": "bid", "trump": "♠", "points": 90},
    ]
    game.phase = "trick_play"
    game.next_to_act = Seat.N
    # N (the sampling seat) does not hold V♥, so it is an unseen card to place.
    game.round_state.hands[Seat.N] = _cards("A♥", "10♥", "R♥", "D♥", "A♦", "10♦", "R♦", "D♦")

    weights = _auction_card_weights(game)
    assert weights[Seat.W][Card("V", "♥")] > weights[Seat.E][Card("V", "♥")]
    assert weights[Seat.W][Card("V", "♥")] > weights[Seat.S][Card("V", "♥")]

    samples = _sample_hidden_hands(game, Seat.N, 400)
    holders = Counter(other for hands in samples for other, hand in hands.items() if Card("V", "♥") in hand)
    assert holders[Seat.W] > holders[Seat.S]
    assert holders[Seat.W] > holders[Seat.E]


def _play_round(source: Game, optimize_ew: bool) -> int:
    simulation = copy.deepcopy(source)
    while simulation.phase == "trick_play":
        acting_seat = simulation.next_to_act
        if optimize_ew and TEAM_OF[acting_seat] == "EW":
            card = choose_card(simulation, acting_seat)
        else:
            card = _select_tactical_card_for_simulation(simulation, acting_seat)
        result = simulation.submit_card(acting_seat, card)
        if result.get("round_complete"):
            score = result["round_score"]
            return score["EW"]["total"] - score["NS"]["total"]
    raise AssertionError("round did not complete")


def test_monte_carlo_team_outscores_greedy_play_across_deals(monkeypatch) -> None:
    # Aggregate over several deals rather than a single fixed one: the Monte
    # Carlo advantage is a statistical property, and any one deal can swing the
    # other way. Summing EW's differential over a fixed seed range keeps the
    # test deterministic while asserting the property that actually matters.
    # A small sample budget preserves that comparison without replaying a full
    # production-strength search for every EW turn in eight complete deals.
    monkeypatch.setattr(bot, "MONTE_CARLO_SAMPLES", 10)
    monte_carlo_total = 0
    greedy_total = 0
    for seed in range(8):
        random_state = random.getstate()
        try:
            random.seed(seed)
            game = Game(target_score=99999)
        finally:
            random.setstate(random_state)
        game.submit_bid(Seat.W, "bid", trump="♠", points=80)
        game.submit_bid(Seat.S, "pass")
        game.submit_bid(Seat.E, "pass")
        game.submit_bid(Seat.N, "pass")

        monte_carlo_total += _play_round(game, optimize_ew=True)
        greedy_total += _play_round(game, optimize_ew=False)

    assert monte_carlo_total > greedy_total


def test_configure_samples_sets_a_positive_explicit_value() -> None:
    original = bot.MONTE_CARLO_SAMPLES
    try:
        assert configure_samples(250) == 250
        assert bot.MONTE_CARLO_SAMPLES == 250
        with pytest.raises(ValueError, match="at least 1"):
            configure_samples(0)
    finally:
        bot.MONTE_CARLO_SAMPLES = original


def test_server_parser_accepts_only_positive_bot_sample_counts() -> None:
    parser = server.build_arg_parser()

    assert parser.parse_args(["--bot-samples", "250"]).bot_samples == 250
    with pytest.raises(SystemExit):
        parser.parse_args(["--bot-samples", "0"])


# ---------------------------------------------------------------------------
# _team_auction_supports_trump
# ---------------------------------------------------------------------------


def test_team_auction_true_when_both_allies_bid_trump() -> None:
    game = Game()
    assert game.bid_state is not None
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 90}
    game.bid_state.history = [
        {"seat": Seat.N, "action": "bid", "trump": "♠", "points": 80},
        {"seat": Seat.E, "action": "pass"},
        {"seat": Seat.S, "action": "bid", "trump": "♠", "points": 90},
        {"seat": Seat.W, "action": "pass"},
    ]
    assert _team_auction_supports_trump(game, Seat.N, "♠") is True
    assert _team_auction_supports_trump(game, Seat.S, "♠") is True


def test_team_auction_true_when_single_high_bid_by_partner() -> None:
    game = Game()
    assert game.bid_state is not None
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.S, "trump": "♠", "points": 110}
    game.bid_state.history = [
        {"seat": Seat.S, "action": "bid", "trump": "♠", "points": 110},
        {"seat": Seat.E, "action": "pass"},
        {"seat": Seat.S, "action": "pass"},
        {"seat": Seat.W, "action": "pass"},
    ]
    assert _team_auction_supports_trump(game, Seat.N, "♠") is True


def test_team_auction_false_when_single_high_bid_by_self() -> None:
    game = Game()
    assert game.bid_state is not None
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 110}
    game.bid_state.history = [
        {"seat": Seat.N, "action": "bid", "trump": "♠", "points": 110},
        {"seat": Seat.E, "action": "pass"},
        {"seat": Seat.S, "action": "pass"},
        {"seat": Seat.W, "action": "pass"},
    ]
    assert _team_auction_supports_trump(game, Seat.N, "♠") is False


def test_team_auction_true_when_single_capot_bid_by_self() -> None:
    game = Game()
    assert game.bid_state is not None
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.S, "trump": "♠", "points": "capot"}
    game.bid_state.history = [
        {"seat": Seat.S, "action": "pass"},
        {"seat": Seat.E, "action": "pass"},
        {"seat": Seat.S, "action": "bid", "trump": "♠", "points": "capot"},
        {"seat": Seat.W, "action": "pass"},
    ]
    assert _team_auction_supports_trump(game, Seat.S, "♠") is False


def test_team_auction_true_when_single_capot_bid_by_partner() -> None:
    game = Game()
    assert game.bid_state is not None
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": "capot"}
    game.bid_state.history = [
        {"seat": Seat.N, "action": "pass"},
        {"seat": Seat.E, "action": "pass"},
        {"seat": Seat.N, "action": "bid", "trump": "♠", "points": "capot"},
        {"seat": Seat.W, "action": "pass"},
    ]
    assert _team_auction_supports_trump(game, Seat.S, "♠") is True


def test_team_auction_false_when_single_low_bid() -> None:
    game = Game()
    assert game.bid_state is not None
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.bid_state.history = [
        {"seat": Seat.N, "action": "bid", "trump": "♠", "points": 80},
        {"seat": Seat.E, "action": "pass"},
        {"seat": Seat.S, "action": "pass"},
        {"seat": Seat.W, "action": "pass"},
    ]
    assert _team_auction_supports_trump(game, Seat.N, "♠") is False


def test_team_auction_false_when_no_bids_on_trump() -> None:
    game = Game()
    assert game.bid_state is not None
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.bid_state.history = []
    assert _team_auction_supports_trump(game, Seat.N, "♠") is False


def test_team_auction_false_when_bids_on_different_trump() -> None:
    game = Game()
    assert game.bid_state is not None
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.bid_state.history = [
        {"seat": Seat.N, "action": "bid", "trump": "♥", "points": 80},
        {"seat": Seat.E, "action": "pass"},
        {"seat": Seat.S, "action": "pass"},
        {"seat": Seat.W, "action": "pass"},
    ]
    assert _team_auction_supports_trump(game, Seat.N, "♠") is False


def test_team_auction_true_when_both_allies_bid_trump_regardless_of_points() -> None:
    game = Game()
    assert game.bid_state is not None
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.S, "trump": "♠", "points": 80}
    game.bid_state.history = [
        {"seat": Seat.N, "action": "bid", "trump": "♠", "points": 80},
        {"seat": Seat.E, "action": "pass"},
        {"seat": Seat.S, "action": "bid", "trump": "♠", "points": 90},
        {"seat": Seat.W, "action": "pass"},
    ]
    assert _team_auction_supports_trump(game, Seat.N, "♠") is True
    assert _team_auction_supports_trump(game, Seat.S, "♠") is True


# ---------------------------------------------------------------------------
# last_partner_bid support logic
# ---------------------------------------------------------------------------


def test_bot_supports_partner_after_opponent_overbid() -> None:
    # Dealer=N → bidding order: W, S, E, N.
    # W (EW) bids 80♠, S (NS) bids 90♥ (opponent).
    # Now E's turn. last_partner_bid = W's 80♠.
    # has_opponents_bid_before: S bid 90♥, 90 < 80? No → False.
    # partner_looking_for_34: 80==80 → True. V → jump 1 step: 80+10=90.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "7♠", "A♥", "8♥", "7♦", "8♦", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "bid", trump="♥", points=90)

    action = choose_bid(game, Seat.E)
    assert action == {"action": "bid", "trump": "♠", "points": 100}


def test_bot_skips_support_when_already_supported_partner() -> None:
    # W bid 80♠, S passed, E bid 90♠ (support), N passed.
    # Now W's turn: self_already_supported_partner is True (W bid ♠ before).
    # W can't re-support; opening ceiling is 80 which is below current 90,
    # and fallback only triggers when current is None. So W passes.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "7♠", "A♥", "8♥", "7♦", "8♦", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "bid", trump="♠", points=90)
    game.submit_bid(Seat.N, "pass")

    action = choose_bid(game, Seat.W)
    # W already bid ♠ → self_already_supported_partner is True.
    # Opening ceiling is 80, below current 90 → can't open. Passes.
    assert action == {"action": "pass"}


def test_bot_passes_when_partner_bid_capot() -> None:
    # Partner bid capot → bot passes immediately.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "A♠", "A♥", "A♦", "7♣", "8♣", "D♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points="capot")
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "pass"}


def test_bot_supports_partner_100_bid_with_strong_hand() -> None:
    # Partner bid 100 → bot supports with strong trump + side aces.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "A♠", "A♥", "A♦", "7♣", "8♣", "D♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=100)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♠", "points": 140}


def test_bot_support_ceiling_scales_with_side_aces_and_trumps() -> None:
    # At current_points=90 (not partner_looking_for_34), the else branch
    # scales with side aces, trump count, V/9, belote.
    strong_hand = _cards("V♠", "9♠", "A♠", "A♥", "A♦", "8♥", "7♦", "7♣")
    weak_hand = _cards("V♠", "9♠", "7♠", "7♥", "8♥", "7♦", "8♦", "7♣")

    strong_ceiling = _support_ceiling(strong_hand, "♠", 90, False)
    weak_ceiling = _support_ceiling(weak_hand, "♠", 90, False)

    assert strong_ceiling is not None
    assert weak_ceiling is not None
    assert strong_ceiling > weak_ceiling


def test_bot_support_partner_looking_for_34_after_opponent_bid() -> None:
    # Dealer=N → bidding order: W, S, E, N.
    # W (EW) bid 80♠, S (NS) bid 90♥. Now E's turn.
    # last_partner_bid = W's 80♠. has_opponents_bid_before: S bid 90♥, 90 < 80? No.
    # partner_looking_for_34: 80 == 80 → True. V → jump 1 step: 80+10=90.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣", "D♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "bid", trump="♥", points=90)

    action = choose_bid(game, Seat.E)
    assert action == {"action": "bid", "trump": "♠", "points": 100}


def test_bot_has_34_in_trump_need_to_avoid_minimum() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "8♠", "A♥", "7♦", "10♣", "R♣", "D♣")
    game.submit_bid(Seat.W, "pass")
    game.submit_bid(Seat.S, "bid", trump="♥", points=80)

    action = choose_bid(game, Seat.E)
    assert action == {"action": "bid", "trump": "♠", "points": 100}


def test_bot_has_34_in_trump_need_to_avoid_minimum_and_all_passed() -> None:
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "8♠", "A♥", "7♦", "10♣", "R♣", "D♣")
    game.submit_bid(Seat.W, "pass")
    game.submit_bid(Seat.S, "pass")

    action = choose_bid(game, Seat.E)
    assert action == {"action": "bid", "trump": "♠", "points": 90}


# ---------------------------------------------------------------------------
# _support_ceiling unit tests
# ---------------------------------------------------------------------------


def test_support_ceiling_9_for_looking_for_34() -> None:
    hand = _cards("9♠", "A♥", "7♥", "7♦", "8♦", "7♣", "8♣", "D♣")
    assert _support_ceiling(hand, "♠", 80, False) == 90


def test_support_ceiling_none_without_v_or_9_for_34() -> None:
    hand = _cards("A♠", "7♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣")
    assert _support_ceiling(hand, "♠", 80, False) is None


def test_support_ceiling_with_belote() -> None:
    # R+D in trump -> belote bonus (+1 step).
    # 2 side aces + 3 trumps + no V/9 + belote = 4 steps.
    hand = _cards("R♠", "D♠", "7♠", "A♥", "A♦", "7♣", "8♣", "8♥")
    assert _support_ceiling(hand, "♠", 90, False) == 120


def test_support_ceiling_none_with_no_useful_cards() -> None:
    hand = _cards("7♥", "8♥", "7♦", "8♦", "7♣", "8♣", "10♥", "10♦")
    assert _support_ceiling(hand, "♠", 100, False) is None


def test_support_ceiling_with_only_side_aces() -> None:
    # 1 trump + 2 side aces, no V/9, no belote -> 2 steps.
    hand = _cards("7♠", "A♥", "A♦", "7♣", "8♣", "8♥", "7♥", "8♦")
    assert _support_ceiling(hand, "♠", 90, False) == 110


# ---------------------------------------------------------------------------
# Integration: choose_bid partner support
# ---------------------------------------------------------------------------


def test_bot_support_pushes_to_capot() -> None:
    # Partner bid 130♠. Hand has R♠, D♠, 9♠ + 3 side aces.
    # additional_steps = 3 (aces) + 1 (3 trumps) + 2 (9) + 1 (belote) = 7.
    # 130 + 70 = 200 >= BID_MAX -> CAPOT.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("R♠", "D♠", "9♠", "A♥", "A♦", "A♣", "7♣", "8♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=130)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♠", "points": "capot"}


def test_bot_passes_when_partner_high_but_hand_weak() -> None:
    # Partner bid 110♠. Hand has no ♠ and no aces -> _support_ceiling returns None.
    # Falls through to the >= 100 pass check.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("7♥", "8♥", "7♦", "8♦", "7♣", "8♣", "10♥", "10♦")
    game.submit_bid(Seat.W, "bid", trump="♠", points=110)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "pass"}


def test_bot_supports_partner_90_with_9_after_opponent_bid() -> None:
    # W (partner) bid 80♠, S (opponent) bid 90♥.
    # Now E's turn. last_partner_bid = W's 80♠.
    # has_opponents_bid_before: S bid 90♥, 90 < 80? No -> False.
    # partner_looking_for_34: 80 == 80 -> True. 9 in trump -> 80+10=90.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("9♠", "7♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "bid", trump="♥", points=90)

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♠", "points": 100}


def test_bot_passes_when_partner_80_but_no_v_nor_9() -> None:
    # Partner bid 80♠. Hand has ♠ but no V or 9 -> _support_ceiling returns None.
    # Opening ceiling is weak too -> passes.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("7♠", "8♠", "7♥", "8♥", "7♦", "8♦", "7♣", "8♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "pass"}


# ---------------------------------------------------------------------------
# Opening cap: V or 9 missing
# ---------------------------------------------------------------------------


def test_bot_opens_exactly_80_when_missing_v_and_9() -> None:
    # First opener, strong side aces + 4 trumps but no V nor 9.
    # New rule: neither V nor 9 → forced pass.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("A♠", "10♠", "R♠", "D♠", "A♥", "A♦", "A♣", "7♣")

    assert choose_bid(game, Seat.W) == {"action": "pass"}


def test_bot_opens_90_to_outbid_opponent_when_missing_v_or_9() -> None:
    # Opponent bid 80 on a different suit. Bot has V but no 9 on ♠.
    # Can outbid to 90 because opponent opened on different trump.
    # Bidding order: W passes, S passes, E bids 80♥, N's turn.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.N] = _cards("V♠", "A♠", "10♠", "7♠", "7♥", "8♥", "7♦", "7♣")
    game.submit_bid(Seat.W, "pass")
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "bid", trump="♥", points=80)

    assert choose_bid(game, Seat.N) == {"action": "bid", "trump": "♠", "points": 90}


def test_bot_passes_when_missing_v_or_9_and_partner_bid_same_trump() -> None:
    # Partner bid 80♠. Bot has ♠ but no V/9. Else branch -> pass.
    # Bidding order: W (partner) bids 80♠, S passes, E's turn.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("A♠", "10♠", "R♠", "D♠", "A♥", "A♦", "A♣", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")

    assert choose_bid(game, Seat.E) == {"action": "pass"}


def test_bot_passes_when_missing_v_or_9_and_opponent_bid_same_trump() -> None:
    # Opponent bid 80 on same trump ♠. Bot has ♠ but no V/9. Else branch -> pass.
    # Bidding order: W passes, S passes, E bids 80♠, N's turn.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.N] = _cards("A♠", "10♠", "7♠", "7♥", "8♥", "7♦", "8♦", "7♣")
    game.submit_bid(Seat.W, "pass")
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "bid", trump="♠", points=80)

    assert choose_bid(game, Seat.N) == {"action": "pass"}


def test_bot_opens_above_80_when_has_both_v_and_9() -> None:
    # First opener, has both V and 9 -> no cap, normal ceiling applies.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "A♠", "A♥", "A♦", "8♥", "7♦", "7♣")

    action = choose_bid(game, Seat.W)
    assert action["action"] == "bid"
    assert action["trump"] == "♠"
    assert isinstance(action["points"], int)
    assert action["points"] > 80


# ---------------------------------------------------------------------------
# Partner bid bonus: opener re-enters opening branch after opponent overbid
# ---------------------------------------------------------------------------


def test_opener_bids_higher_after_partner_support_and_opponent_overbid() -> None:
    # Dealer=N → bidding order: W, S, E, N.
    # W (bot) opens 80♠ (has V♠ but not 9♠), S passes, E supports to 100♠,
    # N overbids 110♥ → back to W.
    # W: last_partner_bid = E's 100♠. self_already_supported_partner =
    # True (W bid ♠). Skip support. >=100 gate: 100>=100 but trump==best_trump
    # → condition false → falls through to opening branch. V♠ but not 9♠ +
    # partner bid on ♠ → bonus +20 → W outbids 110♥.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "A♠", "10♠", "7♠", "A♥", "A♦", "7♣", "8♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "bid", trump="♠", points=100)
    game.submit_bid(Seat.N, "bid", trump="♥", points=110)

    action = choose_bid(game, Seat.W)
    assert action["action"] == "bid"
    assert action["trump"] == "♠"
    assert isinstance(action["points"], int)
    assert action["points"] > 110


def test_opener_bids_higher_with_nine_instead_of_v() -> None:
    # Same as above but W holds 9♠ instead of V♠.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("9♠", "A♠", "10♠", "7♠", "A♥", "A♦", "7♣", "8♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "bid", trump="♠", points=100)
    game.submit_bid(Seat.N, "bid", trump="♥", points=110)

    action = choose_bid(game, Seat.W)
    assert action["action"] == "bid"
    assert action["trump"] == "♠"
    assert isinstance(action["points"], int)
    assert action["points"] > 110


def test_bot_rebids_capot_before_coinching() -> None:
    # Capot is the sole rebid that takes priority over a qualifying coinche.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "A♠", "10♠", "R♠", "A♥", "A♦", "A♣", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "bid", trump="♠", points=100)
    game.submit_bid(Seat.N, "bid", trump="♥", points=110)

    action = choose_bid(game, Seat.W)
    assert action == {"action": "bid", "trump": "♠", "points": "capot"}


def test_bot_surcoinches_own_rebid_when_coinche_does_not_block_bidding() -> None:
    game = Game(coinche_blocks_bidding=False)
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "A♠", "10♠", "R♠", "A♥", "A♦", "7♣")
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "coinche")
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")

    action = choose_bid(game, Seat.W)

    assert action == {"action": "surcoinche"}


def test_bot_announces_another_trump_after_non_blocking_opponent_coinche() -> None:
    # W opens ♥ and S coinches. E cannot support ♥ but has a strong ♠ hand,
    # so the still-open auction lets E replace the coinched contract with ♠.
    game = Game(coinche_blocks_bidding=False)
    assert game.round_state is not None
    game.round_state.hands[Seat.E] = _cards("V♠", "9♠", "A♠", "10♠", "A♦", "A♣", "7♥", "8♥")
    game.submit_bid(Seat.W, "bid", trump="♥", points=80)
    game.submit_bid(Seat.S, "coinche")

    assert choose_bid(game, Seat.E) == {"action": "bid", "trump": "♠", "points": 130}


def test_opener_passes_without_partner_bonus_when_no_partner_bid_on_trump() -> None:
    # Same hand as test_opener_bids_higher but partner never bid on ♠.
    # Without the partner bonus the else-branch kicks in and W passes.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "A♠", "10♠", "7♠", "A♥", "A♦", "7♣", "8♣")
    # W opens 80♠, opponent overbids 90♥, partner never bid on ♠.
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "bid", trump="♥", points=90)
    game.submit_bid(Seat.E, "pass")
    game.submit_bid(Seat.N, "pass")

    # last_partner_bid is None (E passed), so no partner bonus.
    # current_highest_bid = 90♥, different trump and higher → else → pass.
    assert choose_bid(game, Seat.W) == {"action": "pass"}


def test_opener_passes_when_partner_last_bid_is_different_trump_and_high() -> None:
    # Partner's last bid is on a different trump and >= 100.
    # But the support branch fires first (W has A♥ → supports ♥).
    # The >=100 guard only blocks the *opening* branch, not the support branch.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "A♠", "10♠", "7♠", "A♥", "A♦", "7♣", "8♣")
    # Dealer=N → bidding order: W, S, E, N.
    # W opens 80♠, S passes, E (partner) bids 110♥ (different trump, high).
    game.submit_bid(Seat.W, "bid", trump="♠", points=80)
    game.submit_bid(Seat.S, "pass")
    game.submit_bid(Seat.E, "bid", trump="♥", points=110)
    game.submit_bid(Seat.N, "pass")

    # W has A♥ → supports partner's ♥ bid (support branch fires before guard).
    action = choose_bid(game, Seat.W)
    assert action["action"] == "bid"
    assert action["trump"] == "♥"
    assert isinstance(action["points"], int)
    assert action["points"] > 110
