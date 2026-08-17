"""Bot-strategy entry point selected from a table's configured bot type."""

from __future__ import annotations

from collections.abc import Callable

from coinche.bot_types import ClocloBot, DefaultBot, MaestroBot, NoobBot
from coinche.bot_types import default as _default
from coinche.bot_types.base import BotType
from coinche.cards import Card, Seat
from coinche.game import Game

# Number of imperfect-information determinizations used by the smart bot.
# The server configures this explicit runtime value through ``--bot-samples``.
MONTE_CARLO_SAMPLES = 100

_BOT_TYPES: dict[str, Callable[[int], BotType]] = {
    "smart": DefaultBot,
    "maestro": MaestroBot,
    "cloclo": ClocloBot,
    "noob": NoobBot,
}


def available_bot_types() -> tuple[str, ...]:
    """Return the strategy types that may be configured for a table."""
    return tuple(_BOT_TYPES)


def is_supported_bot_type(bot_type: str) -> bool:
    """Whether *bot_type* names a registered strategy."""
    return bot_type in _BOT_TYPES


def _get_bot(bot_type: str) -> BotType:
    """Build the requested strategy, falling back to SmartBot for unknown types."""
    strategy_type = _BOT_TYPES.get(bot_type, DefaultBot)
    return strategy_type(MONTE_CARLO_SAMPLES)


def choose_bid(game: Game, seat: Seat, bot_type: str = "smart") -> dict:
    """Choose an auction action using the table's configured bot strategy."""
    return _get_bot(bot_type).choose_bid(game, seat)


def choose_card(game: Game, seat: Seat, bot_type: str = "smart") -> Card:
    """Choose a card using the table's configured bot strategy."""
    return _get_bot(bot_type).choose_card(game, seat)


def configure_samples(samples: int) -> int:
    """Install an explicit positive Monte-Carlo sample count for card choices."""
    global MONTE_CARLO_SAMPLES
    if samples < 1:
        raise ValueError("Bot sample count must be at least 1")
    MONTE_CARLO_SAMPLES = samples
    return MONTE_CARLO_SAMPLES


def __getattr__(name: str):
    """Keep historical strategy helpers importable from ``coinche.bot``."""
    return getattr(_default, name)
