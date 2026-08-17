"""Reproducible self-play measurements for Coinche bot strategies."""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass

from coinche.bot_types.base import BotType
from coinche.bot_types.cloclo import ClocloBot
from coinche.bot_types.default import DefaultBot
from coinche.cards import Seat
from coinche.game import TEAM_OF, Game


@dataclass(frozen=True)
class BenchmarkResult:
    """Score differentials from Cloclo's perspective across completed deals."""

    score_deltas: tuple[int, ...]
    redeals: int

    @property
    def deals(self) -> int:
        return len(self.score_deltas)

    @property
    def mean_score_delta(self) -> float:
        return statistics.mean(self.score_deltas)

    @property
    def cloclo_win_rate(self) -> float:
        return sum(delta > 0 for delta in self.score_deltas) / self.deals

    @property
    def confidence_interval(self) -> tuple[float, float]:
        """Return a normal-approximation 95% interval for mean score delta."""
        mean = self.mean_score_delta
        if self.deals < 2:
            return mean, mean
        margin = 1.96 * statistics.stdev(self.score_deltas) / self.deals**0.5
        return mean - margin, mean + margin


def run_cloclo_benchmark(deals: int = 32, sample_count: int = 12, seed: int = 0) -> BenchmarkResult:
    """Compare Cloclo and DefaultBot over alternating teams and fixed deals."""
    if deals < 1:
        raise ValueError("Benchmark needs at least one completed deal")
    if sample_count < 1:
        raise ValueError("Bot sample count must be at least one")

    score_deltas: list[int] = []
    redeals = 0
    attempt = 0
    while len(score_deltas) < deals:
        cloclo_team = "NS" if len(score_deltas) % 2 == 0 else "EW"
        score = _play_seeded_deal(seed + attempt, cloclo_team, sample_count)
        attempt += 1
        if score is None:
            redeals += 1
            continue
        opposing_team = "EW" if cloclo_team == "NS" else "NS"
        score_deltas.append(score[cloclo_team]["total"] - score[opposing_team]["total"])
    return BenchmarkResult(tuple(score_deltas), redeals)


def _play_seeded_deal(seed: int, cloclo_team: str, sample_count: int) -> dict | None:
    random_state = random.getstate()
    try:
        random.seed(seed)
        game = Game(target_score=999_999, initial_dealer=tuple(Seat)[seed % len(Seat)])
        strategies: dict[str, BotType] = {
            cloclo_team: ClocloBot(sample_count),
            "EW" if cloclo_team == "NS" else "NS": DefaultBot(sample_count),
        }
        result: dict | None = None
        for _ in range(32):
            if game.phase != "bidding":
                break
            actor = game.next_to_act
            result = game.submit_bid(actor, **strategies[TEAM_OF[actor]].choose_bid(game, actor))
            if result.get("outcome") == "redeal":
                return None
        if game.phase == "bidding":
            raise RuntimeError("Benchmark auction did not close")

        for _ in range(32):
            actor = game.next_to_act
            card = strategies[TEAM_OF[actor]].choose_card(game, actor)
            result = game.submit_card(actor, card)
            if result.get("round_complete"):
                return result["round_score"]
        raise RuntimeError("Benchmark deal did not complete")
    finally:
        random.setstate(random_state)
