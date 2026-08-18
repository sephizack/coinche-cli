"""A deliberately simple bot built on the default strategy's opening logic."""

from __future__ import annotations

import random

from coinche import rules
from coinche.bot_types.default import DefaultBot, _ceiling_value, _legal_bids_up_to, _opening_ceiling
from coinche.cards import Card, Seat
from coinche.game import TEAM_OF, Game


class NoobBot(DefaultBot):
    """Open with the default heuristic, then play a random legal card."""

    def choose_bid(self, game: Game, seat: Seat) -> dict:
        options = game.bid_options_for(seat)
        team_bids = [
            bid
            for bid in options["bid_history"]
            if bid.get("action") == "bid" and TEAM_OF[bid["seat"]] == TEAM_OF[seat]
        ]
        if team_bids and random.randrange(4) == 0:
            last_team_bid = team_bids[-1]
            same_trump_bids = [bid for bid in options["legal_actions"] if bid["trump"] == last_team_bid["trump"]]
            if same_trump_bids:
                return same_trump_bids[0]

        hand = game.get_hand(seat)
        opening_ceilings = {trump: _opening_ceiling(hand, trump) for trump in rules.ALLOWED_TRUMPS}
        best_trump = max(
            rules.ALLOWED_TRUMPS,
            key=lambda trump: _ceiling_value(opening_ceilings[trump]),
        )
        if options["can_coinche"] and random.randrange(12) == 0:
            return {"action": "coinche"}
        if options["can_surcoinche"] and random.randrange(6) == 0:
            return {"action": "surcoinche"}

        maximum_for_hand = opening_ceilings[best_trump]
        if maximum_for_hand:
            if isinstance(maximum_for_hand, int):
                troll_level = 0
                for _i in range(3):
                    if random.randrange(5) == 0:
                        troll_level += 1
                maximum_for_hand = int(maximum_for_hand) + rules.BID_STEP
            legal_for_suit = (
                [] if maximum_for_hand is None else _legal_bids_up_to(options, best_trump, maximum_for_hand)
            )
            if legal_for_suit:
                choice = legal_for_suit[-1]
                return {"action": "bid", "trump": choice["trump"], "points": choice["points"]}

        # if no bid, randomly bid a random trump
        if random.randrange(4) == 0 and options["current_highest_bid"] is None:
            random_trump = random.choice(rules.ALLOWED_TRUMPS)
            return {"action": "bid", "trump": random_trump, "points": 80}

        return {"action": "pass"}

    def choose_card(self, game: Game, seat: Seat) -> Card:
        return random.choice(game.play_options_for(seat)["legal_cards"])
