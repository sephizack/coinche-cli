"""Available server-controlled bot strategies."""

from coinche.bot_types.base import BotType
from coinche.bot_types.default import DefaultBot
from coinche.bot_types.maestro import MaestroBot
from coinche.bot_types.noob import NoobBot

__all__ = ["BotType", "DefaultBot", "MaestroBot", "NoobBot"]
