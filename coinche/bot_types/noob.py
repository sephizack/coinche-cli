"""A deliberately simple bot built on the default strategy's opening logic."""

from __future__ import annotations

import random

from coinche import rules
from coinche.bot_types.default import DefaultBot, _ceiling_value, _opening_ceiling, _try_open_suit
from coinche.cards import Card, Seat
from coinche.game import Game


class NoobBot(DefaultBot):
    """Open with the default heuristic, then play a random legal card."""

    def choose_bid(self, game: Game, seat: Seat) -> dict:
        options = game.bid_options_for(seat)
        hand = game.get_hand(seat)
        opening_ceilings = {trump: _opening_ceiling(hand, trump) for trump in rules.ALLOWED_TRUMPS}
        best_trump = max(
            rules.ALLOWED_TRUMPS,
            key=lambda trump: _ceiling_value(opening_ceilings[trump]),
        )
        return _try_open_suit(hand, best_trump, opening_ceilings, options, seat) or {"action": "pass"}

    def choose_card(self, game: Game, seat: Seat) -> Card:
        return random.choice(game.play_options_for(seat)["legal_cards"])
