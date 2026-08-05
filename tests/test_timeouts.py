from __future__ import annotations

import pytest

from coinche.timeouts import (
    DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS,
    DEFAULT_TURN_TIMEOUT_SECONDS,
    validate_timeout_order,
)


def test_default_turn_timeout_precedes_global_kick() -> None:
    assert DEFAULT_TURN_TIMEOUT_SECONDS < DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS
    validate_timeout_order(DEFAULT_TURN_TIMEOUT_SECONDS, DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS)


@pytest.mark.parametrize("turn_timeout", [0, -1, DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS, 1_000])
def test_timeout_order_rejects_invalid_turn_timeout(turn_timeout: float) -> None:
    with pytest.raises(ValueError):
        validate_timeout_order(turn_timeout, DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS)
