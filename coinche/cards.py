"""Cards, deck, and dealing for Coinche.

Wire format matches demo_table.py's convention, e.g. "10♥", "V♠", "D♦", "R♣", "A♠".
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum

SUITS: tuple[str, ...] = ("♠", "♥", "♦", "♣")
RANKS: tuple[str, ...] = ("7", "8", "9", "10", "V", "D", "R", "A")


class Seat(Enum):
    """A player's seat at the table.

    Rotation order is fixed counter-clockwise per A1: N -> W -> S -> E -> N,
    matching demo_table.py's spatial layout (N top, W left, E right, S bottom).
    """

    N = "N"
    E = "E"
    S = "S"
    W = "W"

    def next(self) -> Seat:
        order = (Seat.N, Seat.W, Seat.S, Seat.E)
        idx = order.index(self)
        return order[(idx + 1) % 4]


@dataclass(frozen=True)
class Card:
    """A single playing card."""

    rank: str
    suit: str

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"


def build_deck() -> list[Card]:
    """Build the standard 32-card Coinche deck."""
    return [Card(rank, suit) for suit in SUITS for rank in RANKS]


def deal(deck: list[Card], dealer_seat: Seat) -> dict[Seat, list[Card]]:
    """Shuffle and deal the deck in a 3-2-3 packet split (A3).

    Dealing starts with the player immediately after the dealer in rotation
    order (A1/A4) and proceeds packet-by-packet (three cards to each player,
    then two, then three) rather than one card at a time.
    """
    shuffled = list(deck)
    random.shuffle(shuffled)

    order: list[Seat] = []
    seat = dealer_seat.next()
    for _ in range(4):
        order.append(seat)
        seat = seat.next()

    hands: dict[Seat, list[Card]] = {s: [] for s in order}
    packet_sizes = (3, 2, 3)
    idx = 0
    for size in packet_sizes:
        for seat in order:
            hands[seat].extend(shuffled[idx : idx + size])
            idx += size

    return hands
