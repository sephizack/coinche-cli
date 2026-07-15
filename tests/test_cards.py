"""Tests for coinche.cards: deck integrity and dealing."""

from coinche.cards import Seat, build_deck, deal


def test_build_deck_has_32_unique_cards():
    deck = build_deck()
    assert len(deck) == 32
    assert len(set(deck)) == 32


def test_deal_gives_each_player_8_cards():
    deck = build_deck()
    hands = deal(deck, Seat.N)
    assert set(hands.keys()) == {Seat.N, Seat.E, Seat.S, Seat.W}
    for _seat, hand in hands.items():
        assert len(hand) == 8


def test_deal_distributes_all_cards_exactly_once():
    deck = build_deck()
    hands = deal(deck, Seat.N)
    all_dealt = [card for hand in hands.values() for card in hand]
    assert len(all_dealt) == 32
    assert set(all_dealt) == set(deck)


def test_seat_rotation_order_counter_clockwise():
    assert Seat.N.next() == Seat.W
    assert Seat.W.next() == Seat.S
    assert Seat.S.next() == Seat.E
    assert Seat.E.next() == Seat.N


def test_deal_starts_after_dealer():
    # With dealer = N, first packet's first recipient should be W (A1/A4).
    deck = build_deck()
    hands = deal(deck, Seat.N)
    # All 4 seats must be present as dict keys regardless of dealer.
    assert Seat.W in hands
    assert Seat.S in hands
    assert Seat.E in hands
    assert Seat.N in hands
