"""Tests for the pure server-controlled bot strategy."""

import copy
import json
import random
from collections import Counter

import coinche.bot as bot
from coinche.bot import (
    MONTE_CARLO_CANDIDATES,
    _build_calibration_game,
    _choose_tactical_card,
    _known_void_suits,
    _sample_hidden_hands,
    _trump_honor_bias,
    calibrate_samples,
    choose_bid,
    choose_card,
)
from coinche.cards import Card, Seat
from coinche.game import TEAM_OF, Game


def _cards(*values: str) -> list[Card]:
    return [Card(value[:-1], value[-1]) for value in values]


def test_bot_bids_a_strong_trump_hand() -> None:
    # V-9-A-10 (the four boss trumps) plus two side aces: near-total trump
    # control makes both aces cash and promises heavy partner help, so the pair
    # contract is worth well above the bare minimum.
    game = Game()
    assert game.round_state is not None
    game.round_state.hands[Seat.W] = _cards("V♠", "9♠", "A♠", "10♠", "A♥", "A♦", "7♣", "8♣")

    action = choose_bid(game, Seat.W)

    assert action == {"action": "bid", "trump": "♠", "points": 130}


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

    assert _choose_tactical_card(game, Seat.S) == Card("7", "♦")


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


def test_partner_of_taker_does_not_lead_trump_under_the_taker() -> None:
    # N took the contract; S (the partner) is on lead holding the 9 of trump.
    # Leading it would force N's Valet to overtrump its own partner, burning
    # two masters in one trick. S must discard low instead and let N pull.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "NS", "seat": Seat.N, "trump": "♠", "points": 80}
    game.phase = "trick_play"
    game.next_to_act = Seat.S
    game.round_state.current_trick = []
    game.round_state.hands[Seat.S] = _cards("9♠", "7♣", "8♦")

    assert choose_card(game, Seat.S) == Card("7", "♣")


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

    assert _choose_tactical_card(game, Seat.S) == Card("V", "♠")


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

    assert _choose_tactical_card(game, Seat.S) == Card("10", "♠")


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

    assert _choose_tactical_card(game, Seat.S) == Card("9", "♠")


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
    assert _choose_tactical_card(game, Seat.S) == Card("A", "♥")


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


def test_determinization_favours_the_taker_for_trump_honours() -> None:
    # The auction says W holds the trump suit; sampled hidden hands should place
    # the trump Valet with W far more often than an even 1-in-3 split would.
    game = Game()
    assert game.round_state is not None and game.bid_state is not None
    game.round_state.trump = "♠"
    game.bid_state.current_highest_bid = {"team": "EW", "seat": Seat.W, "trump": "♠", "points": 80}
    game.bid_state.history = [{"seat": Seat.W, "action": "bid", "trump": "♠", "points": 80}]
    game.phase = "trick_play"
    game.next_to_act = Seat.N
    # N (the sampling seat) does not hold V♠, so it is an unseen card to place.
    game.round_state.hands[Seat.N] = _cards("A♥", "10♥", "R♥", "D♥", "A♦", "10♦", "R♦", "D♦")

    bias = _trump_honor_bias(game)
    assert bias[Seat.W] > bias[Seat.S] == bias[Seat.N]

    samples = _sample_hidden_hands(game, Seat.N, 200)
    holders = Counter(
        other for hands in samples for other, hand in hands.items() if Card("V", "♠") in hand
    )
    assert holders[Seat.W] > holders[Seat.S]
    assert holders[Seat.W] > holders[Seat.E]


def _play_round(source: Game, optimize_ew: bool) -> int:
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


def test_monte_carlo_team_outscores_greedy_play_across_deals() -> None:
    # Aggregate over several deals rather than a single fixed one: the Monte
    # Carlo advantage is a statistical property, and any one deal can swing the
    # other way. Summing EW's differential over a fixed seed range keeps the
    # test deterministic while asserting the property that actually matters.
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


def test_calibration_game_is_a_worst_case_lead() -> None:
    # The benchmark scenario must be the heaviest decision: acting seat leads the
    # opening trick (empty history) with all eight cards legal, as declarer.
    game = _build_calibration_game()
    assert game.round_state is not None
    seat = game.next_to_act
    assert game.phase == "trick_play"
    assert game.round_state.trick_history == []
    assert game.round_state.current_trick == []
    options = game.play_options_for(seat)
    assert len(options["legal_cards"]) == 8
    contract = game.bid_state.current_highest_bid
    assert contract is not None and contract["seat"] == seat


def test_calibrate_samples_picks_a_candidate_within_budget() -> None:
    original = bot.MONTE_CARLO_SAMPLES
    try:
        # A tiny budget must still land on the smallest candidate rather than
        # failing -- a bot always needs *some* samples.
        chosen = calibrate_samples(target_seconds=0.0001, use_cache=False)
        assert chosen == MONTE_CARLO_CANDIDATES[0]
        assert bot.MONTE_CARLO_SAMPLES == chosen

        # A generous budget must never exceed the largest offered candidate.
        chosen_big = calibrate_samples(target_seconds=3600.0, use_cache=False)
        assert chosen_big == MONTE_CARLO_CANDIDATES[-1]
        assert bot.MONTE_CARLO_SAMPLES == chosen_big
    finally:
        bot.MONTE_CARLO_SAMPLES = original


def test_calibrate_samples_caches_and_reuses_result(tmp_path, monkeypatch) -> None:
    original = bot.MONTE_CARLO_SAMPLES
    cache_file = tmp_path / ".bot-bench"
    monkeypatch.setattr(bot, "CALIBRATION_CACHE_PATH", str(cache_file))
    try:
        # First call benchmarks and writes the cache file.
        chosen = calibrate_samples(target_seconds=3600.0)
        assert chosen == MONTE_CARLO_CANDIDATES[-1]
        assert cache_file.exists()

        # A second call with a *tiny* budget would normally land on the smallest
        # candidate -- but the cache is keyed on the budget, so this benchmarks
        # afresh rather than reusing the generous-budget result.
        chosen_tiny = calibrate_samples(target_seconds=0.0001)
        assert chosen_tiny == MONTE_CARLO_CANDIDATES[0]

        # Re-running the tiny budget now hits the cache: even a value the live
        # benchmark could never produce is returned verbatim, proving no
        # re-measurement happened.
        cache_file.write_text(
            json.dumps(
                {
                    "fingerprint": bot._calibration_fingerprint(0.0001),
                    "samples": MONTE_CARLO_CANDIDATES[-1],
                }
            ),
            encoding="utf-8",
        )
        reused = calibrate_samples(target_seconds=0.0001)
        assert reused == MONTE_CARLO_CANDIDATES[-1]
        assert bot.MONTE_CARLO_SAMPLES == reused
    finally:
        bot.MONTE_CARLO_SAMPLES = original


def test_calibrate_samples_use_cache_false_ignores_cache(tmp_path, monkeypatch) -> None:
    original = bot.MONTE_CARLO_SAMPLES
    cache_file = tmp_path / ".bot-bench"
    monkeypatch.setattr(bot, "CALIBRATION_CACHE_PATH", str(cache_file))
    try:
        # Seed a bogus cache that a tiny-budget benchmark could never produce.
        cache_file.write_text(
            json.dumps(
                {
                    "fingerprint": bot._calibration_fingerprint(0.0001),
                    "samples": MONTE_CARLO_CANDIDATES[-1],
                }
            ),
            encoding="utf-8",
        )
        chosen = calibrate_samples(target_seconds=0.0001, use_cache=False)
        assert chosen == MONTE_CARLO_CANDIDATES[0]
    finally:
        bot.MONTE_CARLO_SAMPLES = original
