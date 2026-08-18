from __future__ import annotations

import importlib

import pytest

import coinche.timeouts
from coinche.timeouts import (
    DEFAULT_BOT_THINK_SECONDS,
    DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS,
    DEFAULT_ROUND_PAUSE_SECONDS,
    DEFAULT_TRICK_PAUSE_SECONDS,
    DEFAULT_TURN_TIMEOUT_SECONDS,
    validate_timeout_order,
)


def test_default_turn_timeout_precedes_global_kick() -> None:
    assert DEFAULT_TURN_TIMEOUT_SECONDS < DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS
    validate_timeout_order(DEFAULT_TURN_TIMEOUT_SECONDS, DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS)


def test_default_pause_and_think_values() -> None:
    assert DEFAULT_TRICK_PAUSE_SECONDS == 2.5
    assert DEFAULT_ROUND_PAUSE_SECONDS == 4.0
    assert DEFAULT_BOT_THINK_SECONDS == 1.0


def test_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COINCHE_TRICK_PAUSE", "1.5")
    monkeypatch.setenv("COINCHE_ROUND_PAUSE_SECONDS", "3.2")
    monkeypatch.setenv("COINCHE_BOT_THINK", "0.5")
    reloaded = importlib.reload(coinche.timeouts)
    try:
        assert reloaded.DEFAULT_TRICK_PAUSE_SECONDS == 1.5
        assert reloaded.DEFAULT_ROUND_PAUSE_SECONDS == 3.2
        assert reloaded.DEFAULT_BOT_THINK_SECONDS == 0.5
    finally:
        monkeypatch.delenv("COINCHE_TRICK_PAUSE", raising=False)
        monkeypatch.delenv("COINCHE_ROUND_PAUSE_SECONDS", raising=False)
        monkeypatch.delenv("COINCHE_BOT_THINK", raising=False)
        importlib.reload(coinche.timeouts)


def test_timeout_env_invalid_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COINCHE_TRICK_PAUSE", "not-a-number")
    reloaded = importlib.reload(coinche.timeouts)
    try:
        assert reloaded.DEFAULT_TRICK_PAUSE_SECONDS == 2.5
    finally:
        monkeypatch.delenv("COINCHE_TRICK_PAUSE", raising=False)
        importlib.reload(coinche.timeouts)


@pytest.mark.parametrize("turn_timeout", [0, -1, DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS, 1_000])
def test_timeout_order_rejects_invalid_turn_timeout(turn_timeout: float) -> None:
    with pytest.raises(ValueError):
        validate_timeout_order(turn_timeout, DEFAULT_GLOBAL_KICK_TIMEOUT_SECONDS)
