"""Shared timeout defaults and validation for the game and meta-client services."""

from __future__ import annotations

import os


def _get_env_float(*env_vars: str, default: float) -> float:
    for var in env_vars:
        val = os.environ.get(var)
        if val is not None:
            try:
                return float(val)
            except ValueError:
                pass
    return default


DEFAULT_TURN_TIMEOUT_SECONDS = _get_env_float("COINCHE_TURN_TIMEOUT", "COINCHE_TURN_TIMEOUT_SECONDS", default=300.0)
DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS = _get_env_float(
    "COINCHE_GLOBAL_KICK_TIMEOUT", "COINCHE_GLOBAL_KICK_TIMEOUT_SECONDS", default=15 * 60.0
)
DEFAULT_TRICK_PAUSE_SECONDS = _get_env_float("COINCHE_TRICK_PAUSE", "COINCHE_TRICK_PAUSE_SECONDS", default=2.5)
DEFAULT_ROUND_PAUSE_SECONDS = _get_env_float("COINCHE_ROUND_PAUSE", "COINCHE_ROUND_PAUSE_SECONDS", default=4.0)
DEFAULT_BOT_THINK_SECONDS = _get_env_float("COINCHE_BOT_THINK", "COINCHE_BOT_THINK_SECONDS", default=1.0)


def validate_timeout_order(turn_timeout: float, global_kick_timeout: float) -> None:
    """Require the per-turn timeout to expire before the global idle kick."""
    if turn_timeout <= 0:
        raise ValueError("turn timeout must be strictly positive")
    if global_kick_timeout <= 0:
        raise ValueError("global kick timeout must be strictly positive")
    if turn_timeout >= global_kick_timeout:
        raise ValueError("turn timeout must be strictly below the global kick timeout")
