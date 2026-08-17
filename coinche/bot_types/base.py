"""Common interface for server-controlled bot strategies."""

from __future__ import annotations

from typing import Protocol

from coinche.cards import Card, Seat
from coinche.game import Game


class BotType(Protocol):
    """Select legal auction and play actions for a bot seat."""

    def choose_bid(self, game: Game, seat: Seat) -> dict: ...

    def choose_card(self, game: Game, seat: Seat) -> Card: ...
