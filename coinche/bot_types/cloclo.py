"""An assertive, information-safe Coinche strategy for AI challenges."""

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass, field
from functools import wraps

from coinche import rules
from coinche.bot_types.base import BotType
from coinche.bot_types.default import _apply_determinization, _sample_hidden_hands
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


class ClocloBot(BotType):
    """An independent aggressive strategy that uses only its hand and public play."""

    def __init__(self, sample_count: int) -> None:
        self.sample_count = sample_count

    def choose_bid(self, game: Game, seat: Seat) -> dict:
        heuristic_action = self._choose_heuristic_bid(game, seat)
        options = game.bid_options_for(seat)
        if options["current_highest_bid"] is None and heuristic_action["action"] == "pass":
            return heuristic_action
        partner_bid = next(
            (
                bid
                for bid in reversed(options["bid_history"])
                if bid.get("action") == "bid" and bid["seat"] == PARTNER_OF[seat]
            ),
            None,
        )
        if (
            heuristic_action["action"] == "bid"
            and partner_bid is not None
            and heuristic_action["trump"] == partner_bid["trump"]
        ):
            return heuristic_action

        actions = [heuristic_action]
        if heuristic_action["action"] != "pass":
            actions.append({"action": "pass"})
        if options["can_coinche"] and heuristic_action["action"] == "coinche":
            actions.append({"action": "coinche"})
        if options["can_surcoinche"] and heuristic_action["action"] == "surcoinche":
            actions.append({"action": "surcoinche"})
        unique_actions = [action for index, action in enumerate(actions) if action not in actions[:index]]
        return self._choose_bid_by_rollout(game, seat, unique_actions, heuristic_action)

    def _choose_heuristic_bid(self, game: Game, seat: Seat) -> dict:
        options = game.bid_options_for(seat)
        hand = game.get_hand(seat)
        ceilings = {trump: self._bid_ceiling(hand, trump) for trump in rules.ALLOWED_TRUMPS}
        best_trump = max(rules.ALLOWED_TRUMPS, key=lambda trump: self._ceiling_rank(ceilings[trump]))
        best_ceiling = ceilings[best_trump]
        current_bid = options["current_highest_bid"]

        if current_bid is not None and isinstance(current_bid["points"], int):
            current_points = current_bid["points"]
            if current_bid["trump"] == best_trump:
                if options["can_surcoinche"] and current_bid["team"] == TEAM_OF[seat] and best_ceiling == rules.CAPOT:
                    return {"action": "surcoinche"}
                if (
                    options["can_coinche"]
                    and current_bid["team"] != TEAM_OF[seat]
                    and self._ceiling_rank(best_ceiling) >= current_points + 20
                ):
                    return {"action": "coinche"}

            # Defensive coinche on opponent contract
            if options["can_coinche"] and current_bid["team"] != TEAM_OF[seat]:
                opp_trump = current_bid["trump"]
                opp_trump_ranks = {card.rank for card in hand if card.suit == opp_trump}
                defensive_trumps = ("V" in opp_trump_ranks) + ("9" in opp_trump_ranks) + ("A" in opp_trump_ranks)
                side_aces = sum(card.rank == "A" and card.suit != opp_trump for card in hand)
                if defensive_trumps >= 2 and side_aces >= 2 and current_points >= 100:
                    return {"action": "coinche"}
                if defensive_trumps >= 1 and side_aces >= 3 and current_points >= 110:
                    return {"action": "coinche"}

        partner_bid = next(
            (
                bid
                for bid in reversed(options["bid_history"])
                if bid.get("action") == "bid" and bid["seat"] == PARTNER_OF[seat]
            ),
            None,
        )
        if partner_bid is not None:
            # Prevent infinite self-partner escalation spirals on the same trump
            self_bids_on_suit = [
                bid
                for bid in options["bid_history"]
                if bid.get("action") == "bid" and bid["seat"] == seat and bid["trump"] == partner_bid["trump"]
            ]
            opponent_intervened = False
            if self_bids_on_suit:
                last_self_bid_idx = options["bid_history"].index(self_bids_on_suit[-1])
                opponent_intervened = any(
                    bid.get("action") == "bid" and TEAM_OF[bid["seat"]] != TEAM_OF[seat]
                    for bid in options["bid_history"][last_self_bid_idx:]
                )
            if not self_bids_on_suit or opponent_intervened:
                # If our own independent suit is much stronger than supporting partner's weak suit, propose it
                if (
                    best_ceiling is not None
                    and self._ceiling_rank(best_ceiling) >= (partner_bid["points"] + 30)
                    and best_trump != partner_bid["trump"]
                ):
                    own_bid = self._highest_affordable_bid(options, best_trump, best_ceiling)
                    if own_bid is not None:
                        return own_bid
                support = self._support_bid(options, hand, partner_bid)
                if support is not None:
                    return support

        if best_ceiling is not None:
            own_bid = self._highest_affordable_bid(options, best_trump, best_ceiling)
            if own_bid is not None:
                return own_bid
        return {"action": "pass"}

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
            trick = simulation.round_state.current_trick
            if trick and rules.trick_winner(trick, trump) == PARTNER_OF[actor]:
                card = self._partner_master_discard(simulation, actor, options["legal_cards"], trump)
            else:
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
        trick = game.round_state.current_trick
        assert trump is not None
        if len(legal_cards) == 1:
            return legal_cards[0]
        if trick and rules.trick_winner(trick, trump) == PARTNER_OF[seat]:
            return self._partner_master_discard(game, seat, legal_cards, trump)

        tactical_card = self._choose_tactical_card(game, seat, legal_cards, trump)
        samples = _sample_hidden_hands(game, seat, self.sample_count)
        if not samples:
            return tactical_card

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
                -self._discard_key(card, trump)[2],
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

        for _ in range(32):
            actor = simulation.next_to_act
            options = simulation.play_options_for(actor)
            legal_cards: list[Card] = options["legal_cards"]
            if not legal_cards:
                raise RuntimeError("Information-set search reached a seat without a legal card")
            key = self._information_key(simulation, actor)
            node = nodes.setdefault(key, _SearchNode())
            card = self._select_search_card(node, legal_cards, actor, TEAM_OF[root_seat])
            path.append((node, card))
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
                + 40 * exploration / math.sqrt(node.actions[card].visits),
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

    def _choose_tactical_card(self, game: Game, seat: Seat, legal_cards: list[Card], trump: str) -> Card:
        assert game.round_state is not None
        trick = game.round_state.current_trick
        if trick:
            led_suit = trick[0][1].suit
            if rules.trick_winner(trick, trump, led_suit) == PARTNER_OF[seat]:
                return self._partner_master_discard(game, seat, legal_cards, trump)
            winners = [
                card for card in legal_cards if rules.trick_winner([*trick, (seat, card)], trump, led_suit) == seat
            ]
            if winners:
                if len(trick) == 3:
                    return min(winners, key=lambda card: self._winning_cost(card, trump))
                hand = game.get_hand(seat)
                masters = [card for card in winners if self._is_master(card, hand, game, trump)]
                if masters:
                    return min(masters, key=lambda card: self._winning_cost(card, trump))
                return min(winners, key=lambda card: self._winning_cost(card, trump))
            return min(legal_cards, key=lambda card: self._discard_key(card, trump))

        hand = game.get_hand(seat)
        masters = [card for card in legal_cards if self._is_master(card, hand, game, trump)]
        contract = game.bid_state.current_highest_bid if game.bid_state is not None else None
        is_declaring_team = contract is not None and contract["team"] == TEAM_OF[seat]
        master_trumps = [card for card in masters if card.suit == trump]
        if is_declaring_team and master_trumps:
            return max(master_trumps, key=lambda card: rules.TRUMP_ORDER.index(card.rank))
        if is_declaring_team:
            trumps = [card for card in legal_cards if card.suit == trump]
            if len(trumps) >= 2:
                return max(trumps, key=lambda card: rules.TRUMP_ORDER.index(card.rank))
        side_masters = [card for card in masters if card.suit != trump]
        if side_masters:
            return max(side_masters, key=lambda card: rules.NONTRUMP_ORDER.index(card.rank))

        # Defending team on lead: avoid giving a cheap trump lead
        if not is_declaring_team:
            non_trumps = [card for card in legal_cards if card.suit != trump]
            if non_trumps:
                return min(non_trumps, key=lambda card: self._discard_key(card, trump))

        return min(legal_cards, key=lambda card: self._discard_key(card, trump))

    def _partner_master_discard(self, game: Game, seat: Seat, legal_cards: list[Card], trump: str) -> Card:
        assert game.round_state is not None
        trick = game.round_state.current_trick
        hand = game.get_hand(seat)
        direct_calls = [
            card
            for card in legal_cards
            if card.suit != trump and card.rank not in {"A", "10"} and Card("A", card.suit) in hand
        ]
        if direct_calls:
            return min(direct_calls, key=lambda card: self._discard_key(card, trump))
        if len(trick) == 3:
            return max(
                legal_cards,
                key=lambda card: (
                    rules.card_points(card, trump),
                    -int(card.suit == trump),
                    -self._discard_key(card, trump)[2],
                ),
            )
        return min(legal_cards, key=lambda card: self._discard_key(card, trump))

    @classmethod
    def _support_bid(cls, options: dict, hand: list[Card], partner_bid: dict) -> dict | None:
        if partner_bid["points"] == rules.CAPOT:
            return None
        trump = partner_bid["trump"]
        trump_cards = [card for card in hand if card.suit == trump]
        trump_ranks = {card.rank for card in trump_cards}
        steps = 0
        if "V" in trump_ranks or "9" in trump_ranks:
            steps += 1
        if len(trump_cards) >= 3:
            steps += 1
        steps += min(2, sum(card.rank == "A" and card.suit != trump for card in hand))
        if steps == 0:
            return None
        target = min(rules.BID_MAX, partner_bid["points"] + steps * rules.BID_STEP)
        return cls._highest_affordable_bid(options, trump, target)

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
        trump_cards = [card for card in hand if card.suit == trump]
        if len(trump_cards) == len(hand):
            return rules.CAPOT
        if len(trump_cards) < 2:
            return None

        trump_ranks = {card.rank for card in trump_cards}
        side_aces = sum(card.rank == "A" and card.suit != trump for card in hand)
        side_tens = sum(card.rank == "10" and card.suit != trump for card in hand)
        if len(trump_cards) == 2 and not ({"V", "9"}.issubset(trump_ranks) or ("V" in trump_ranks and side_aces >= 2)):
            return None

        strength = sum(rules.card_points(card, trump) for card in trump_cards)
        strength += side_aces * 8 + side_tens * 2 + (len(trump_cards) - 2) * 3
        strength += 8 if "V" in trump_ranks else 0
        strength += 6 if "9" in trump_ranks else 0
        if {"R", "D"}.issubset(trump_ranks):
            strength += 4

        if strength < 39:
            return None
        return min(rules.BID_MAX, rules.BID_MIN + ((strength - 39) // 6) * rules.BID_STEP)

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
