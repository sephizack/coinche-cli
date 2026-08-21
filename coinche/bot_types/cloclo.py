"""An assertive, high-performance Coinche strategy with expert tactics and MCTS."""

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass, field
from functools import wraps

from coinche import rules
from coinche.bot_types.base import BotType
from coinche.bot_types.default import (
    _apply_determinization,
    _card_strength,
    _ceiling_value,
    _choose_counter_action,
    _choose_normal_bid,
    _defender_trump_lead_is_wasteful,
    _hand_strength,
    _has_been_played,
    _known_void_suits,
    _opening_ceiling,
    _opponent_may_ruff_suit,
    _opponents_may_hold_trump,
    _partner_allowance,
    _point_potential,
    _sample_hidden_hands,
    _team_auction_supports_trump,
)
from coinche.cards import Card, Seat
from coinche.game import PARTNER_OF, TEAM_OF, Game


@dataclass
class _SearchAction:
    visits: int = 0
    total_value: int = 0


@dataclass
class _SearchNode:
    visits: int = 0
    actions: dict[Card, _SearchAction] = field(default_factory=dict)


def _without_global_random_side_effects(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        random_state = random.getstate()
        try:
            return function(*args, **kwargs)
        finally:
            random.setstate(random_state)

    return wrapped


def _ten_bait_lead(game: Game, seat: Seat) -> Card | None:
    """Lead low from a protected Ace-King length to tempt out an unseen Ten."""
    assert game.round_state is not None
    round_state = game.round_state
    trump = round_state.trump
    contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
    if (
        trump is None
        or round_state.current_trick
        or contract is None
        or contract["team"] != TEAM_OF[seat]
        or _opponents_may_hold_trump(game, seat, trump)
    ):
        return None

    hand = game.get_hand(seat)
    candidates: list[tuple[list[Card], Card]] = []
    for suit in rules.ALLOWED_TRUMPS:
        if suit == trump:
            continue
        suit_cards = [card for card in hand if card.suit == suit]
        ranks = {card.rank for card in suit_cards}
        ten = Card("10", suit)
        if not {"A", "R"}.issubset(ranks) or ten in hand or _has_been_played(ten, round_state):
            continue
        low_cards = [card for card in suit_cards if card.rank not in {"A", "R"}]
        if low_cards:
            lowest_card = min(
                low_cards,
                key=lambda card: (rules.card_points(card, trump), rules.NONTRUMP_ORDER.index(card.rank)),
            )
            candidates.append((suit_cards, lowest_card))

    if not candidates:
        return None
    return max(candidates, key=lambda candidate: (len(candidate[0]), candidate[1].suit))[1]


class ClocloBot(BotType):
    """An offensive, champion-level Coinche strategy using expert heuristics and IS-MCTS."""

    def __init__(self, sample_count: int) -> None:
        self.sample_count = sample_count

    def choose_bid(self, game: Game, seat: Seat) -> dict:
        options = game.bid_options_for(seat)
        hand = game.get_hand(seat)

        # Cloclo signature offensive jump on opening
        if options["current_highest_bid"] is None:
            trump_ceilings = {trump: self._bid_ceiling(hand, trump) for trump in rules.ALLOWED_TRUMPS}
            strengths = {trump: _hand_strength(hand, trump) for trump in rules.ALLOWED_TRUMPS}
            best_trump = max(
                rules.ALLOWED_TRUMPS,
                key=lambda t: (self._ceiling_rank(trump_ceilings[t]), strengths[t]),
            )
            trump_ranks = {c.rank for c in hand if c.suit == best_trump}
            side_aces = sum(c.rank == "A" and c.suit != best_trump for c in hand)
            if {"V", "9"}.issubset(trump_ranks) and side_aces >= 2:
                ceiling = trump_ceilings[best_trump]
                if ceiling is not None:
                    own_bid = self._highest_affordable_bid(options, best_trump, ceiling)
                    if own_bid is not None:
                        return own_bid

        return self._choose_heuristic_bid(game, seat)

    def _choose_heuristic_bid(self, game: Game, seat: Seat) -> dict:
        options = game.bid_options_for(seat)
        hand = game.get_hand(seat)
        current = options["current_highest_bid"]
        strengths = {trump: _hand_strength(hand, trump) for trump in rules.ALLOWED_TRUMPS}

        # Counter actions (Coinche / Surcoinche)
        if current is not None:
            if current["points"] == rules.CAPOT and options["can_coinche"] and current["team"] != TEAM_OF[seat]:
                opp_ranks = {c.rank for c in hand if c.suit == current["trump"]}
                if "V" in opp_ranks or "9" in opp_ranks or sum(c.rank == "A" for c in hand) >= 2:
                    return {"action": "coinche"}

            counter_action = _choose_counter_action(hand, options, current, strengths)
            if counter_action is not None:
                return counter_action

        # Normal bid
        opening_ceilings = {trump: _opening_ceiling(hand, trump) for trump in rules.ALLOWED_TRUMPS}
        last_self_bid = next(
            (bid for bid in reversed(options["bid_history"]) if bid.get("action") == "bid" and bid["seat"] == seat),
            None,
        )
        if last_self_bid is not None:
            best_trump = last_self_bid["trump"]
        else:
            best_trump = max(
                rules.ALLOWED_TRUMPS,
                key=lambda trump: (_ceiling_value(opening_ceilings[trump]), strengths[trump]),
            )

        normal_action = _choose_normal_bid(game, seat, hand, best_trump, opening_ceilings, options)
        if (
            options["can_surcoinche"]
            and normal_action.get("action") == "bid"
            and current is not None
            and normal_action.get("trump") == current["trump"]
        ):
            return {"action": "surcoinche"}
        return normal_action

    def _choose_bid_by_rollout(
        self,
        game: Game,
        seat: Seat,
        actions: list[dict],
        heuristic_action: dict,
    ) -> dict:
        sample_budget = max(4, min(16, self.sample_count // 8))
        samples = _sample_hidden_hands(game, seat, sample_budget)
        if not samples:
            return heuristic_action
        values = {index: 0 for index in range(len(actions))}
        for hidden_hands in samples:
            for index, action in enumerate(actions):
                values[index] += self._bid_rollout_value(game, seat, action, hidden_hands)
        best_index = max(
            range(len(actions)),
            key=lambda index: (
                values[index],
                actions[index] == heuristic_action,
                actions[index]["action"] != "pass",
            ),
        )
        return actions[best_index]

    @_without_global_random_side_effects
    def _bid_rollout_value(
        self,
        game: Game,
        root_seat: Seat,
        action: dict,
        hidden_hands: dict[Seat, list[Card]],
    ) -> int:
        simulation = copy.deepcopy(game)
        self._apply_bidding_determinization(simulation, root_seat, hidden_hands)
        result = simulation.submit_bid(root_seat, **action)
        for _ in range(32):
            if simulation.phase != "bidding":
                break
            if result.get("outcome") == "redeal":
                return 0
            actor = simulation.next_to_act
            result = simulation.submit_bid(actor, **self._choose_heuristic_bid(simulation, actor))
        if simulation.phase == "bidding":
            raise RuntimeError("Bid rollout did not close the auction")

        for _ in range(32):
            actor = simulation.next_to_act
            options = simulation.play_options_for(actor)
            trump = options["trump"]
            assert trump is not None
            card = self._choose_tactical_card(simulation, actor, options["legal_cards"], trump)
            result = simulation.submit_card(actor, card)
            if result.get("round_complete"):
                root_team = TEAM_OF[root_seat]
                opposing_team = "EW" if root_team == "NS" else "NS"
                score = result["round_score"]
                return score[root_team]["total"] - score[opposing_team]["total"]
        raise RuntimeError("Bid rollout did not complete the round")

    @staticmethod
    def _apply_bidding_determinization(
        game: Game,
        root_seat: Seat,
        hidden_hands: dict[Seat, list[Card]],
    ) -> None:
        assert game.round_state is not None
        round_state = game.round_state
        for seat, hand in hidden_hands.items():
            if seat != root_seat:
                round_state.hands[seat] = list(hand)
        round_state.dealt_hands = {seat: list(hand) for seat, hand in round_state.hands.items()}

    def choose_card(self, game: Game, seat: Seat) -> Card:
        assert game.round_state is not None
        options = game.play_options_for(seat)
        legal_cards: list[Card] = options["legal_cards"]
        trump = options["trump"]
        assert trump is not None
        if len(legal_cards) == 1:
            return legal_cards[0]

        tactical_card = self._choose_tactical_card(game, seat, legal_cards, trump)
        samples = _sample_hidden_hands(game, seat, self.sample_count)
        if not samples:
            return tactical_card

        # Deterministic rules for opening lead and forced cash-outs
        if not game.round_state.current_trick:
            opening_card = self._choose_opening_card(game, seat, legal_cards, trump)
            if opening_card is not None and opening_card in legal_cards:
                return opening_card
            if _defender_trump_lead_is_wasteful(game, seat, trump):
                non_trumps = [c for c in legal_cards if c.suit != trump]
                if non_trumps:
                    legal_cards = non_trumps

        if game.round_state.current_trick:
            trick = game.round_state.current_trick
            if len(trick) == 3:
                return tactical_card
            led_suit = trick[0][1].suit
            if led_suit != trump and not any(c.suit == trump for _, c in trick):
                requested_ace = [c for c in legal_cards if c.rank == "A" and c.suit == led_suit]
                if requested_ace:
                    return requested_ace[0]

        nodes: dict[tuple, _SearchNode] = {}
        for hidden_hands in samples:
            self._search_determinization(game, seat, hidden_hands, nodes)

        root = nodes.get(self._information_key(game, seat))
        if root is None:
            return tactical_card
        return max(
            legal_cards,
            key=lambda card: (
                self._action_average(root.actions.get(card)),
                root.actions.get(card, _SearchAction()).visits,
                card == tactical_card,
                -rules.card_points(card, trump),
                -int(card.suit == trump),
                -_card_strength(card, trump),
                card.suit,
            ),
        )

    @_without_global_random_side_effects
    def _search_determinization(
        self,
        game: Game,
        root_seat: Seat,
        hidden_hands: dict[Seat, list[Card]],
        nodes: dict[tuple, _SearchNode],
    ) -> None:
        simulation = copy.deepcopy(game)
        _apply_determinization(simulation, root_seat, hidden_hands)
        path: list[tuple[_SearchNode, Card]] = []
        result: dict | None = None
        in_tree = True

        for _ in range(32):
            if simulation.round_state is None:
                break
            actor = simulation.next_to_act
            options = simulation.play_options_for(actor)
            legal_cards: list[Card] = options["legal_cards"]
            if not legal_cards:
                break

            if in_tree:
                key = self._information_key(simulation, actor)
                node = nodes.setdefault(key, _SearchNode())
                unvisited = sorted((card for card in legal_cards if card not in node.actions), key=str)
                if unvisited:
                    tactical = self._choose_tactical_card(simulation, actor, legal_cards, options["trump"])
                    card = tactical if tactical in unvisited else unvisited[0]
                    path.append((node, card))
                    in_tree = False
                else:
                    card = self._select_search_card(node, legal_cards, actor, TEAM_OF[root_seat])
                    path.append((node, card))
            else:
                card = self._choose_tactical_card(simulation, actor, legal_cards, options["trump"])

            result = simulation.submit_card(actor, card)
            if result.get("round_complete"):
                break

        if result is None or not result.get("round_complete"):
            raise RuntimeError("Information-set search did not complete the round")

        root_team = TEAM_OF[root_seat]
        opposing_team = "EW" if root_team == "NS" else "NS"
        score = result["round_score"]
        value = score[root_team]["total"] - score[opposing_team]["total"]
        for node, card in path:
            node.visits += 1
            action = node.actions.setdefault(card, _SearchAction())
            action.visits += 1
            action.total_value += value

    @staticmethod
    def _action_average(action: _SearchAction | None) -> float:
        if action is None or action.visits == 0:
            return float("-inf")
        return action.total_value / action.visits

    @classmethod
    def _select_search_card(
        cls,
        node: _SearchNode,
        legal_cards: list[Card],
        actor: Seat,
        root_team: str,
    ) -> Card:
        unvisited = sorted((card for card in legal_cards if card not in node.actions), key=str)
        if unvisited:
            return unvisited[0]
        actor_is_root_team = TEAM_OF[actor] == root_team
        exploration = math.sqrt(math.log(node.visits + 1))
        return max(
            legal_cards,
            key=lambda card: (
                (1 if actor_is_root_team else -1) * cls._action_average(node.actions[card])
                + 40.0 * exploration / math.sqrt(node.actions[card].visits),
                str(card),
            ),
        )

    @staticmethod
    def _information_key(game: Game, seat: Seat) -> tuple:
        assert game.round_state is not None
        round_state = game.round_state
        history = tuple(
            tuple((played_seat.value, str(card)) for played_seat, card in trick["trick"])
            for trick in round_state.trick_history
        )
        current_trick = tuple((played_seat.value, str(card)) for played_seat, card in round_state.current_trick)
        bid_history = (
            () if game.bid_state is None else tuple(tuple(sorted(entry.items())) for entry in game.bid_state.history)
        )
        return (
            seat.value,
            tuple(sorted(str(card) for card in game.get_hand(seat))),
            round_state.trump,
            history,
            current_trick,
            bid_history,
            tuple(sorted(round_state.captured_points.items())),
        )

    def _choose_opening_card(self, game: Game, seat: Seat, legal_cards: list[Card], trump: str) -> Card | None:
        assert game.round_state is not None
        contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
        if contract is None:
            return None

        own_hand = game.get_hand(seat)
        is_declarer = contract["team"] == TEAM_OF[seat]

        # Preneur: pull trumps while opponents might hold trump
        if is_declarer and _opponents_may_hold_trump(game, seat, trump):
            trumps = [card for card in legal_cards if card.suit == trump]
            if trumps:
                master_trumps = [card for card in trumps if self._is_master(card, own_hand, game, trump)]
                if master_trumps:
                    return max(master_trumps, key=lambda card: _card_strength(card, trump))
                if len(trumps) >= 2 and contract["seat"] == seat:
                    return max(trumps, key=lambda card: _card_strength(card, trump))
                if not _has_been_played(Card("V", trump), game.round_state):
                    nine_not_played = not _has_been_played(Card("9", trump), game.round_state)
                    partner_might_have_trumps = trump not in _known_void_suits(game.round_state)[PARTNER_OF[seat]]
                    if (
                        partner_might_have_trumps
                        and nine_not_played
                        and _team_auction_supports_trump(game, seat, trump)
                    ):
                        worst_trump = min(trumps, key=lambda card: _card_strength(card, trump))
                        return worst_trump

        # Cash side Aces (shortest suit first to create ruff opportunity)
        owned_non_trump_aces = [card for card in legal_cards if card.rank == "A" and card.suit != trump]
        if owned_non_trump_aces:
            if is_declarer and _opponents_may_hold_trump(game, seat, trump):
                safe_aces = [
                    ace for ace in owned_non_trump_aces if not _opponent_may_ruff_suit(game, seat, ace.suit, trump)
                ]
                if safe_aces:
                    suit_length = {
                        suit: sum(1 for card in own_hand if card.suit == suit)
                        for suit in {ace.suit for ace in safe_aces}
                    }
                    return min(safe_aces, key=lambda ace: (suit_length[ace.suit], ace.suit))
            else:
                suit_length = {
                    suit: sum(1 for card in own_hand if card.suit == suit)
                    for suit in {ace.suit for ace in owned_non_trump_aces}
                }
                return min(owned_non_trump_aces, key=lambda ace: (suit_length[ace.suit], ace.suit))

        # Cash non-trump masters when trumps are drawn
        if not _opponents_may_hold_trump(game, seat, trump):
            bait = _ten_bait_lead(game, seat)
            if bait is not None and bait in legal_cards:
                return bait
            non_trump_masters = [
                card for card in legal_cards if card.suit != trump and self._is_master(card, own_hand, game, trump)
            ]
            if non_trump_masters:
                return max(
                    non_trump_masters,
                    key=lambda card: (rules.card_points(card, trump), _card_strength(card, trump)),
                )

        # Preneur: if no side masters, squeeze out remaining trumps
        if is_declarer and _opponents_may_hold_trump(game, seat, trump) and contract["seat"] == seat:
            trumps = [card for card in legal_cards if card.suit == trump]
            if trumps:
                return max(trumps, key=lambda card: _card_strength(card, trump))

        # Defender: lead partner's bid suit if available
        if not is_declarer:
            partner_bids = [
                entry["trump"]
                for entry in game.bid_state.history
                if entry.get("action") == "bid"
                and entry.get("seat") == PARTNER_OF[seat]
                and entry.get("trump") != trump
            ]
            for p_suit in partner_bids:
                suit_cards = [c for c in legal_cards if c.suit == p_suit]
                if suit_cards:
                    return min(suit_cards, key=lambda c: _card_strength(c, trump))

            # Defender: never waste trump on opening lead unless holding master
            if _defender_trump_lead_is_wasteful(game, seat, trump):
                non_trumps = [card for card in legal_cards if card.suit != trump]
                if non_trumps:
                    suit_lengths = {s: sum(c.suit == s for c in own_hand) for s in {c.suit for c in non_trumps}}
                    shortest_suit = min(suit_lengths, key=suit_lengths.get)
                    shortest_cards = [c for c in non_trumps if c.suit == shortest_suit]
                    return min(shortest_cards, key=lambda c: _card_strength(c, trump))

        return None

    def _choose_tactical_card(self, game: Game, seat: Seat, legal_cards: list[Card], trump: str) -> Card:
        assert game.round_state is not None
        trick = game.round_state.current_trick
        if not trick:
            opening_card = self._choose_opening_card(game, seat, legal_cards, trump)
            if opening_card is not None and opening_card in legal_cards:
                return opening_card

            hand = game.get_hand(seat)
            masters = [card for card in legal_cards if self._is_master(card, hand, game, trump)]
            if masters:
                return max(masters, key=lambda card: (rules.card_points(card, trump), _card_strength(card, trump)))
            return self._best_discard(legal_cards, hand, trump)

        led_suit = trick[0][1].suit
        if led_suit != trump and not any(c.suit == trump for _, c in trick):
            requested_ace = [c for c in legal_cards if c.rank == "A" and c.suit == led_suit]
            if requested_ace:
                return requested_ace[0]

        winner = rules.trick_winner(trick, trump, led_suit)
        is_partner_winning = winner == PARTNER_OF[seat]

        if is_partner_winning:
            partner_play = next(card for played_seat, card in trick if played_seat == PARTNER_OF[seat])
            is_partner_master = self._is_master(partner_play, game.get_hand(seat), game, trump)
            if is_partner_master or len(trick) == 3:
                return self._partner_master_discard(game, seat, legal_cards, trump)

            # If partner is not guaranteed to win, check if we can secure the trick with a master
            winners = [
                card for card in legal_cards if rules.trick_winner([*trick, (seat, card)], trump, led_suit) == seat
            ]
            if winners:
                masters = [card for card in winners if self._is_master(card, game.get_hand(seat), game, trump)]
                if masters:
                    return min(masters, key=lambda card: self._winning_cost(card, trump))
            return self._partner_master_discard(game, seat, legal_cards, trump)

        winners = [card for card in legal_cards if rules.trick_winner([*trick, (seat, card)], trump, led_suit) == seat]
        if winners:
            if len(trick) == 3:
                return min(winners, key=lambda card: self._winning_cost(card, trump))
            hand = game.get_hand(seat)
            masters = [card for card in winners if self._is_master(card, hand, game, trump)]
            if masters:
                return min(masters, key=lambda card: self._winning_cost(card, trump))
            return min(winners, key=lambda card: self._winning_cost(card, trump))

        return self._best_discard(legal_cards, game.get_hand(seat), trump)

    def _partner_master_discard(self, game: Game, seat: Seat, legal_cards: list[Card], trump: str) -> Card:
        assert game.round_state is not None
        trick = game.round_state.current_trick
        hand = game.get_hand(seat)

        if len(trick) == 3:
            non_trumps = [card for card in legal_cards if card.suit != trump]
            if non_trumps:
                return max(
                    non_trumps,
                    key=lambda card: (
                        rules.card_points(card, trump),
                        -_card_strength(card, trump),
                    ),
                )
            return min(legal_cards, key=lambda card: self._discard_key(card, trump))

        direct_calls = [
            card
            for card in legal_cards
            if card.suit != trump and card.rank not in {"A", "10"} and Card("A", card.suit) in hand
        ]
        if direct_calls:
            return min(direct_calls, key=lambda card: self._discard_key(card, trump))
        return self._best_discard(legal_cards, hand, trump)

    @staticmethod
    def _best_discard(cards: list[Card], hand: list[Card], trump: str) -> Card:
        lengths = {suit: sum(other.suit == suit for other in hand) for suit in rules.ALLOWED_TRUMPS}

        def key(card: Card) -> tuple[int, int, int, int]:
            points = rules.card_points(card, trump)
            is_trump = int(card.suit == trump)
            side_length = 99 if card.suit == trump else lengths[card.suit]
            strength = _card_strength(card, trump)
            return (points, is_trump, side_length, strength)

        return min(cards, key=key)

    @classmethod
    def _support_bid(cls, options: dict, hand: list[Card], partner_bid: dict) -> dict | None:
        if partner_bid["points"] == rules.CAPOT:
            return None
        trump = partner_bid["trump"]
        trump_cards = [card for card in hand if card.suit == trump]
        trump_ranks = {card.rank for card in trump_cards}
        if not trump_cards:
            return None
        if len(trump_cards) == 1 and "V" not in trump_ranks and "9" not in trump_ranks:
            return None

        current = options["current_highest_bid"]
        if current is not None and (current["points"] == rules.CAPOT or current["points"] >= 120):
            return None

        partner_points = partner_bid["points"]
        side_aces = sum(card.rank == "A" and card.suit != trump for card in hand)
        has_34 = "V" in trump_ranks or "9" in trump_ranks

        if partner_points == rules.BID_MIN or (current is not None and current["team"] != TEAM_OF[partner_bid["seat"]]):
            if has_34:
                min_bid = partner_points + rules.BID_STEP
                if current is not None:
                    min_bid = max(min_bid, current["points"] + rules.BID_STEP)
                if min_bid <= 110:
                    return cls._highest_affordable_bid(options, trump, min_bid)
            elif len(trump_cards) >= 3 and side_aces >= 1:
                min_bid = partner_points + rules.BID_STEP
                if current is not None:
                    min_bid = max(min_bid, current["points"] + rules.BID_STEP)
                if min_bid <= 100:
                    return cls._highest_affordable_bid(options, trump, min_bid)
            return None

        steps = 0
        if "V" in trump_ranks or "9" in trump_ranks:
            steps += 1
        if len(trump_cards) >= 3:
            steps += 1
        steps += min(2, side_aces)
        if steps == 0:
            return None
        min_bid = partner_points + rules.BID_STEP
        if current is not None:
            min_bid = max(min_bid, current["points"] + rules.BID_STEP)
        if min_bid <= 120:
            return cls._highest_affordable_bid(options, trump, min_bid)
        return None

    @classmethod
    def _highest_affordable_bid(cls, options: dict, trump: str, ceiling: int | str) -> dict | None:
        affordable = [
            action
            for action in options["legal_actions"]
            if action.get("action") == "bid"
            and action.get("trump") == trump
            and cls._ceiling_rank(action["points"]) <= cls._ceiling_rank(ceiling)
        ]
        return max(affordable, key=lambda action: cls._ceiling_rank(action["points"]), default=None)

    @classmethod
    def _competitive_affordable_bid(cls, options: dict, trump: str, ceiling: int | str) -> dict | None:
        affordable = [
            action
            for action in options["legal_actions"]
            if action.get("action") == "bid"
            and action.get("trump") == trump
            and cls._ceiling_rank(action["points"]) <= cls._ceiling_rank(ceiling)
        ]
        return min(affordable, key=lambda action: cls._ceiling_rank(action["points"]), default=None)

    @staticmethod
    def _is_master(card: Card, hand: list[Card], game: Game, trump: str) -> bool:
        assert game.round_state is not None
        order = rules.TRUMP_ORDER if card.suit == trump else rules.NONTRUMP_ORDER
        stronger = order[order.index(card.rank) + 1 :]
        seen = set(hand)
        for completed_trick in game.round_state.trick_history:
            seen.update(played for _, played in completed_trick["trick"])
        seen.update(played for _, played in game.round_state.current_trick)
        return all(Card(rank, card.suit) in seen for rank in stronger)

    @staticmethod
    def _bid_ceiling(hand: list[Card], trump: str) -> int | str | None:
        if len(hand) == sum(card.suit == trump for card in hand):
            return rules.CAPOT
        trump_cards = [card for card in hand if card.suit == trump]
        if len(trump_cards) < 2:
            return None

        trump_ranks = {card.rank for card in trump_cards}
        if "V" not in trump_ranks and "9" not in trump_ranks:
            if len(trump_cards) < 5 or "A" not in trump_ranks:
                return None

        side_aces = sum(card.rank == "A" and card.suit != trump for card in hand)
        potential = _point_potential(hand, trump) + _partner_allowance(hand, trump)
        ceiling = rules.round_to_nearest_ten(int(17 + potential * 0.91))
        if ceiling < rules.BID_MIN:
            return None

        if {"V", "9"}.issubset(trump_ranks):
            ceiling = max(rules.BID_MIN + rules.BID_STEP, ceiling)
            if side_aces >= 2:
                ceiling += rules.BID_STEP * 2  # Cloclo offensive bonus
            return min(rules.BID_MAX, ceiling)

        return min(rules.BID_MAX, ceiling)

    @staticmethod
    def _ceiling_rank(points: int | str | None) -> int:
        if points == rules.CAPOT:
            return rules.BID_MAX + rules.BID_STEP
        return points if isinstance(points, int) else -1

    @staticmethod
    def _discard_key(card: Card, trump: str) -> tuple[bool, int, int, str]:
        order = rules.TRUMP_ORDER if card.suit == trump else rules.NONTRUMP_ORDER
        return (card.suit == trump, rules.card_points(card, trump), order.index(card.rank), card.suit)

    @staticmethod
    def _winning_cost(card: Card, trump: str) -> tuple[bool, int, int, str]:
        order = rules.TRUMP_ORDER if card.suit == trump else rules.NONTRUMP_ORDER
        return (card.suit == trump, order.index(card.rank), rules.card_points(card, trump), card.suit)
