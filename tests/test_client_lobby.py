"""Tests for the client lobby picker's reconnect detection.

When a player quits and relaunches, the picker must let them re-select the
in-progress table they were on (matched by a disconnected seat carrying their
name) so the server's RESYNC path can put them back. `_reconnectable_seat` is
the pure predicate behind that behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coinche import protocol
from coinche.client import _auto_generate_table_key, _reconnectable_seat, _replaceable_bot_seats, build_arg_parser
from coinche.table import TABLE_NAMES, TABLE_NAMES_PATH


def _table(players):
    return {"table_key": "live1", "in_progress": True, "seats_filled": 4, "players": players}


def test_matches_disconnected_seat_case_insensitive():
    entry = _table(
        [
            {"seat": "N", "name": "Alice", "team_name": "Equipe 1", "connected": False},
            {"seat": "E", "name": "Bob", "team_name": "Equipe 2", "connected": True},
        ]
    )
    match = _reconnectable_seat(entry, "ALICE")
    assert match is not None
    assert match["seat"] == "N"
    assert match["team_name"] == "Equipe 1"


def test_no_match_for_connected_seat():
    entry = _table(
        [
            {"seat": "N", "name": "Alice", "team_name": None, "connected": True},
        ]
    )
    assert _reconnectable_seat(entry, "Alice") is None


def test_no_match_for_different_name():
    entry = _table(
        [
            {"seat": "N", "name": "Alice", "team_name": None, "connected": False},
        ]
    )
    assert _reconnectable_seat(entry, "Zoe") is None


def test_empty_player_name_never_matches():
    entry = _table(
        [
            {"seat": "N", "name": "", "team_name": None, "connected": False},
        ]
    )
    assert _reconnectable_seat(entry, "") is None
    assert _reconnectable_seat(entry, "   ") is None


def test_missing_connected_defaults_to_connected():
    # Older listings without the field must be treated as connected (not reconnectable).
    entry = _table(
        [
            {"seat": "N", "name": "Alice", "team_name": None},
        ]
    )
    assert _reconnectable_seat(entry, "Alice") is None


def test_replaceable_bot_seats_lists_bots_on_running_table():
    entry = _table(
        [
            {"seat": "N", "name": "Alice", "team_name": "Equipe 1", "is_bot": False},
            {"seat": "E", "name": "Sephiroth", "team_name": "Equipe 2", "is_bot": True},
            {"seat": "S", "name": "Cloud", "team_name": "Equipe 1", "is_bot": True},
            {"seat": "W", "name": "Tifa", "team_name": "Equipe 2", "is_bot": True},
        ]
    )
    bots = _replaceable_bot_seats(entry)
    assert [p["seat"] for p in bots] == ["E", "S", "W"]


def test_replaceable_bot_seats_empty_when_not_in_progress():
    # A table that hasn't started has no bots to replace even if seats are filled.
    entry = {
        "table_key": "wait1",
        "in_progress": False,
        "seats_filled": 1,
        "players": [{"seat": "N", "name": "Alice", "team_name": None, "is_bot": False}],
    }
    assert _replaceable_bot_seats(entry) == []


def test_replaceable_bot_seats_empty_for_all_human_running_table():
    entry = _table(
        [
            {"seat": "N", "name": "Alice", "team_name": "Equipe 1", "is_bot": False},
            {"seat": "E", "name": "Bob", "team_name": "Equipe 2", "is_bot": False},
        ]
    )
    assert _replaceable_bot_seats(entry) == []


def test_generated_table_key_uses_random_venue_name(monkeypatch):
    monkeypatch.setattr("coinche.client.random.choice", lambda names: "Midgar")

    key = _auto_generate_table_key([])

    assert key == "Midgar"
    assert key in TABLE_NAMES


def test_generated_table_key_adds_numeric_suffix_on_collision(monkeypatch):
    monkeypatch.setattr("coinche.client.random.choice", lambda names: "BalambGarden")
    existing_tables = [{"table_key": "BalambGarden"}, {"table_key": "BalambGarden2"}]

    key = _auto_generate_table_key(existing_tables)

    assert key == "BalambGarden3"
    assert key.isalnum()
    assert len(key) <= protocol.TABLE_KEY_MAX_LENGTH


def test_table_venue_names_match_server_key_constraints():
    assert all(
        name.isascii()
        and name.isalnum()
        and protocol.TABLE_KEY_MIN_LENGTH <= len(name) <= protocol.TABLE_KEY_MAX_LENGTH
        for name in TABLE_NAMES
    )


def test_table_venue_names_are_loaded_from_shared_static_json():
    assert TABLE_NAMES_PATH == Path(__file__).parent.parent / "coinche" / "web" / "static" / "table_names.json"
    assert tuple(json.loads(TABLE_NAMES_PATH.read_text(encoding="utf-8"))) == TABLE_NAMES


def test_target_score_cli_option_is_strictly_positive():
    parser = build_arg_parser()

    assert parser.parse_args([]).target_score is None
    assert parser.parse_args(["--target-score", "1500"]).target_score == 1500
    with pytest.raises(SystemExit):
        parser.parse_args(["--target-score", "0"])


def test_turn_timeout_cli_option_is_strictly_positive():
    parser = build_arg_parser()

    assert parser.parse_args([]).turn_timeout is None
    assert parser.parse_args(["--turn-timeout", "42.5"]).turn_timeout == 42.5
    with pytest.raises(SystemExit):
        parser.parse_args(["--turn-timeout", "0"])
