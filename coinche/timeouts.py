"""Shared timeout defaults and validation for the game and meta-client services."""

from __future__ import annotations

DEFAULT_TURN_TIMEOUT_SECONDS = 300.0
DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS = 15 * 60.0


def validate_timeout_order(turn_timeout: float, global_kick_timeout: float) -> None:
    """Require the per-turn timeout to expire before the global idle kick."""
    if turn_timeout <= 0:
        raise ValueError("turn timeout must be strictly positive")
    if global_kick_timeout <= 0:
        raise ValueError("global kick timeout must be strictly positive")
    if turn_timeout >= global_kick_timeout:
        raise ValueError("turn timeout must be strictly below the global kick timeout")
