"""Tests for coinche.session_state: the I/O-free reducer + snapshot projection.

Covers the U1 extraction contract (BR-U1-*):
- `apply_message` reproduces every per-message `_apply_message` transition.
- `ApplyResult.action_requested` is True for exactly BID_REQUEST / PLAY_REQUEST /
  ROUND_SCORE / GAME_OVER, and False for every other message type.
- `snapshot_to_dict` is a pure, JSON-serializable projection that includes
  `last_round_contract` and only ever exposes the local seat's `hand`.
- Mirror parity (TR-2): a scripted server-message sequence yields a state whose
  `snapshot_to_dict` agrees with the fields the terminal renderer reads.

These are pure/sync — no socket, no asyncio, no rich — per the module's design.
"""

from __future__ import annotations

import json

from coinche import protocol
from coinche.cards import Seat
from coinche.session_state import (
    SYSTEM_CHAT_NAME,
    ApplyResult,
    ClientState,
    apply_message,
    snapshot_to_dict,
)


def _join(state: ClientState) -> ApplyResult:
    """Apply a canonical 4-player JOINED and return its ApplyResult."""
    return apply_message(
        state,
        protocol.JOINED,
        {
            "table_key": "table1",
            "seat": "S",
            "table_options": {
                "target_score": 1000,
                "coinche_blocks_bidding": True,
                "bot_type": "toto",
                "trick_pause_seconds": 2.5,
                "round_pause_seconds": 5.0,
                "bot_think_seconds": 1.0,
                "turn_timeout_seconds": 300.0,
            },
            "spectator_count": 2,
            "players": [
                {"seat": "N", "name": "Nord", "team_name": "Nous"},
                {"seat": "E", "name": "Est"},
                {"seat": "S", "name": "Moi"},
                {"seat": "W", "name": "Ouest"},
            ],
            "server_version": "9.9.9",
        },
    )


# --------------------------------------------------------------------------- #
# apply_message: per-message transitions                                      #
# --------------------------------------------------------------------------- #


def test_joined_populates_identity_and_players():
    state = ClientState()
    result = _join(state)
    assert result == ApplyResult(action_requested=False)
    assert state.joined_once is True
    assert state.table_key == "table1"
    assert state.table_options["target_score"] == 1000
    assert state.spectator_count == 2
    assert state.seat == Seat.S
    assert state.players == {Seat.N: "Nord", Seat.E: "Est", Seat.S: "Moi", Seat.W: "Ouest"}
    assert state.team_of == {Seat.N: "NS", Seat.E: "EW", Seat.S: "NS", Seat.W: "EW"}
    assert state.team_names == {"NS": "Nous"}
    assert state.status_message == "En attente de joueurs (4/4)..."
    assert state.can_fill_bots is False
    assert state.server_version == "9.9.9"


def test_left_resets_state_back_to_lobby():
    """LEFT clears every table/game-scoped field so both UIs return to the
    picker (seat = None, joined_once = False), keeping connection-scoped data
    (the live `tables` listing, server_version)."""
    state = ClientState()
    _join(state)
    # Muddy the state as if a round were under way.
    state.hand = ["A♠", "K♥"]
    state.whose_turn = Seat.S
    state.trump = "♠"
    state.contract_points = 90
    state.tables = [{"table_key": "table1"}]

    result = apply_message(state, protocol.LEFT, {})

    assert result == ApplyResult(action_requested=False)
    assert state.joined_once is False
    assert state.seat is None
    assert state.table_key is None
    assert state.table_options == {}
    assert state.spectator_count == 0
    assert state.players == {}
    assert state.hand == []
    assert state.whose_turn is None
    assert state.trump is None
    assert state.contract_points is None
    assert state.status_message == "Vous avez quitté la table."
    # Connection-scoped data is retained across a table switch.
    assert state.tables == [{"table_key": "table1"}]
    assert state.server_version == "9.9.9"


def test_lobby_update_refreshes_players_and_status():
    state = ClientState()
    _join(state)
    result = apply_message(
        state,
        protocol.LOBBY_UPDATE,
        {"players": [{"seat": "N", "name": "Nord"}], "seats_filled": 1},
    )
    assert result.action_requested is False
    assert state.players == {Seat.N: "Nord"}
    assert state.status_message == "En attente de joueurs (1/4)..."
    assert state.can_fill_bots is True


def test_bot_types_are_projected_and_updated_per_seat():
    state = ClientState()
    apply_message(
        state,
        protocol.LOBBY_UPDATE,
        {
            "players": [
                {"seat": "N", "name": "Alice", "is_bot": False},
                {"seat": "E", "name": "Bot", "is_bot": True, "bot_type": "noob"},
            ],
            "seats_filled": 2,
        },
    )

    apply_message(state, protocol.BOT_TYPE_CHANGED, {"seat": "E", "bot_type": "maestro"})

    assert state.bot_types == {Seat.E: "maestro"}
    assert snapshot_to_dict(state)["bot_types"] == {"E": "maestro"}


def test_spectator_count_is_projected_and_refreshed():
    state = ClientState()
    _join(state)

    apply_message(state, protocol.SPECTATOR_COUNT, {"count": 3})

    assert state.spectator_count == 3
    assert snapshot_to_dict(state)["spectator_count"] == 3


def test_table_listing_stored_and_projected():
    """TABLE_LISTING feeds the web lobby: stored verbatim on the state and
    surfaced in the snapshot so the browser can render mini-tables."""
    state = ClientState()
    tables = [
        {
            "table_key": "table1",
            "in_progress": False,
            "seats_filled": 2,
            "players": [
                {"seat": "N", "name": "Alice", "team_name": "Equipe 1", "connected": True, "is_bot": False},
                {"seat": "E", "name": "Bob", "team_name": "Equipe 2", "connected": True, "is_bot": False},
            ],
        }
    ]
    result = apply_message(state, protocol.TABLE_LISTING, {"tables": tables})
    assert result.action_requested is False
    assert state.tables == tables
    snap = snapshot_to_dict(state)
    assert snap["tables"] == tables
    # Projection copies each entry (mutating the snapshot must not touch state).
    snap["tables"][0]["table_key"] = "mutated"
    assert state.tables[0]["table_key"] == "table1"


def test_deal_resets_round_state_and_sorts_hand():
    state = ClientState()
    _join(state)
    state.round_over_screen = True  # DEAL must NOT clear this
    result = apply_message(
        state,
        protocol.DEAL,
        {
            "hand": ["7♠", "A♥", "10♠", "R♦"],
            "first_bidder_seat": "N",
            "dealer_seat": "W",
            "round_number": 3,
        },
    )
    assert result.action_requested is False
    assert set(state.hand) == {"7♠", "A♥", "10♠", "R♦"}
    assert state.legal_cards == []
    assert state.current_trick == {}
    assert state.trump is None
    assert state.coinche_level == 1
    assert state.bid_marks == {}
    assert state.whose_turn == Seat.N
    assert state.dealer_seat == Seat.W
    assert state.last_action == "Nouvelle donne #3 (donneur W)"
    assert [(name, text, team) for name, text, team, _ts, _system in state.system_messages] == [
        (SYSTEM_CHAT_NAME, "── Manche 3 ──", None),
    ]
    assert state.round_over_screen is True  # preserved deliberately


def test_bid_request_sets_pending_and_requests_action():
    state = ClientState()
    _join(state)
    result = apply_message(
        state,
        protocol.BID_REQUEST,
        {
            "legal_actions": [{"trump": "♥"}],
            "current_highest_bid": {"trump": "♥", "points": 80, "seat": "N"},
            "can_coinche": False,
            "can_surcoinche": False,
        },
    )
    assert result.action_requested is True
    assert state.pending_bid_request is not None
    assert state.current_bid_trump == "♥"
    assert state.current_bid_points == 80
    assert state.current_bid_seat == Seat.N
    assert state.whose_turn == Seat.S


def test_bid_request_with_no_highest_bid_clears_current_bid():
    state = ClientState()
    _join(state)
    result = apply_message(
        state,
        protocol.BID_REQUEST,
        {"legal_actions": [], "current_highest_bid": None, "can_coinche": False, "can_surcoinche": False},
    )
    assert result.action_requested is True
    assert state.current_bid_trump is None
    assert state.current_bid_points is None
    assert state.current_bid_seat is None


def test_bid_update_records_mark_and_current_bid():
    state = ClientState()
    _join(state)
    result = apply_message(
        state,
        protocol.BID_UPDATE,
        {"seat": "N", "action": "bid", "trump": "♥", "points": 90, "next_to_act": "E"},
    )
    assert result.action_requested is False
    assert state.last_action == "Nord a annoncé 90 ♥"
    assert state.bid_marks[Seat.N] == "90 ♥"
    assert state.current_bid_trump == "♥"
    assert state.current_bid_points == 90
    assert state.current_bid_seat == Seat.N
    assert state.whose_turn == Seat.E


def _open_bid_request(state: ClientState) -> None:
    apply_message(
        state,
        protocol.BID_REQUEST,
        {
            "legal_actions": [{"trump": "♥", "points": 80}],
            "current_highest_bid": None,
            "can_coinche": False,
            "can_surcoinche": False,
        },
    )
    assert state.pending_bid_request is not None


def test_bid_update_clears_pending_bid_request():
    # Regression: pending_bid_request must be cleared once the turn ends, so the
    # web bid panel / CLI bid menu never lingers past our turn.
    state = ClientState()
    _join(state)
    _open_bid_request(state)
    apply_message(state, protocol.BID_UPDATE, {"seat": "S", "action": "pass", "next_to_act": "E"})
    assert state.pending_bid_request is None


def test_bidding_result_clears_pending_bid_request():
    state = ClientState()
    _join(state)
    _open_bid_request(state)
    apply_message(
        state,
        protocol.BIDDING_RESULT,
        {"outcome": "contract", "trump": "♥", "points": 90, "seat": "N", "coinche_level": 1, "first_leader": "E"},
    )
    assert state.pending_bid_request is None


def test_deal_clears_pending_requests():
    state = ClientState()
    _join(state)
    _open_bid_request(state)
    state.pending_play_request = {"stale": True}
    apply_message(
        state,
        protocol.DEAL,
        {"hand": ["7♥"], "first_bidder_seat": "S", "dealer_seat": "N", "round_number": 2},
    )
    assert state.pending_bid_request is None
    assert state.pending_play_request is None


def test_bid_update_pass_does_not_set_current_bid():
    state = ClientState()
    _join(state)
    apply_message(state, protocol.BID_UPDATE, {"seat": "E", "action": "pass", "next_to_act": "S"})
    assert state.bid_marks[Seat.E] == "Passe"
    assert state.current_bid_trump is None
    assert state.whose_turn == Seat.S


def test_bidding_result_contract_settles_and_clears_marks():
    state = ClientState()
    _join(state)
    state.hand = ["7♥", "A♠"]
    state.bid_marks = {Seat.N: "90 ♥"}
    result = apply_message(
        state,
        protocol.BIDDING_RESULT,
        {"outcome": "contract", "trump": "♥", "points": 90, "seat": "N", "coinche_level": 2, "first_leader": "E"},
    )
    assert result.action_requested is False
    assert state.bid_marks == {}
    assert state.trump == "♥"
    assert state.contract_points == 90
    assert state.contract_bidder == Seat.N
    assert state.coinche_level == 2
    assert state.bid_effect_level == 2
    assert state.last_action == "Contrat retenu : 90 ♥ par Nord"
    assert state.whose_turn == Seat.E


def test_bidding_result_redeal():
    state = ClientState()
    _join(state)
    state.bid_marks = {Seat.N: "Passe"}
    apply_message(state, protocol.BIDDING_RESULT, {"outcome": "redeal"})
    assert state.bid_marks == {}
    assert state.last_action == "Tout le monde a passé — nouvelle donne"
    assert state.whose_turn is None
    assert state.trump is None


def test_play_request_sets_legal_cards_and_requests_action():
    state = ClientState()
    _join(state)
    state.pending_last_trick = {Seat.N: "A♠"}
    result = apply_message(
        state,
        protocol.PLAY_REQUEST,
        {
            "legal_cards": ["7♥", "R♥"],
            "trump": "♥",
            "current_trick": [{"seat": "E", "card": "9♥"}],
        },
    )
    assert result.action_requested is True
    assert set(state.legal_cards) == {"7♥", "R♥"}
    assert state.trump == "♥"
    assert state.current_trick == {Seat.E: "9♥"}
    # pending_last_trick promoted to last_trick as a new trick starts.
    assert state.last_trick == {Seat.N: "A♠"}
    assert state.pending_last_trick is None
    assert state.whose_turn == Seat.S


def test_turn_deadline_is_local_and_timeout_blocks_actions(monkeypatch):
    state = ClientState()
    _join(state)
    monkeypatch.setattr("coinche.session_state.time.time", lambda: 1_000.0)
    apply_message(
        state,
        protocol.BID_REQUEST,
        {
            "legal_actions": [],
            "current_highest_bid": None,
            "can_coinche": False,
            "can_surcoinche": False,
            "turn_timeout_seconds": 42.5,
        },
    )
    assert state.turn_timeout_seconds == 42.5
    assert state.turn_deadline == 1042.5

    apply_message(state, protocol.TURN_TIMEOUT, {"message": "Remplacé par un bot."})
    assert state.turn_timed_out is True
    assert state.turn_deadline is None
    assert state.pending_bid_request is None
    assert state.turn_timeout_message == "Remplacé par un bot."
    assert state.joined_once is False
    assert state.seat is None
    assert state.table_key is None
    assert state.status_message == "Remplacé par un bot."
    assert snapshot_to_dict(state)["turn_timeout_message"] == "Remplacé par un bot."

    _join(state)
    assert state.turn_timed_out is False
    assert state.turn_timeout_message is None
    assert state.last_action == ""


def test_card_played_removes_own_card_and_updates_trick():
    state = ClientState()
    _join(state)
    state.hand = ["7♥", "R♥"]
    result = apply_message(
        state,
        protocol.CARD_PLAYED,
        {
            "seat": "S",
            "card": "7♥",
            "current_trick": [{"seat": "S", "card": "7♥"}],
            "next_to_act": "W",
            "belote_announcement": "belote",
        },
    )
    assert result.action_requested is False
    assert state.current_trick == {Seat.S: "7♥"}
    assert "7♥" not in state.hand
    assert state.last_action == "Moi a joué 7♥ — Belote !"
    assert state.whose_turn == Seat.W


def test_card_played_belote_and_rebelote_announced_in_system_messages():
    state = ClientState()
    _join(state)
    state.hand = ["R♥", "D♥"]
    apply_message(
        state,
        protocol.CARD_PLAYED,
        {
            "seat": "S",
            "card": "R♥",
            "current_trick": [{"seat": "S", "card": "R♥"}],
            "next_to_act": "W",
            "belote_announcement": "belote",
        },
    )
    apply_message(
        state,
        protocol.CARD_PLAYED,
        {
            "seat": "S",
            "card": "D♥",
            "current_trick": [{"seat": "S", "card": "D♥"}],
            "next_to_act": "W",
            "belote_announcement": "rebelote",
        },
    )
    assert [(name, text, team) for name, text, team, _ts, _system in state.system_messages] == [
        ("Moi", "Belote !", "NS"),
        ("Moi", "Rebelote !", "NS"),
    ]


def test_card_played_without_belote_adds_no_chat_message():
    state = ClientState()
    _join(state)
    state.hand = ["7♥"]
    apply_message(
        state,
        protocol.CARD_PLAYED,
        {
            "seat": "S",
            "card": "7♥",
            "current_trick": [{"seat": "S", "card": "7♥"}],
            "next_to_act": "W",
            "belote_announcement": None,
        },
    )
    assert len(state.chat_messages) == 0


def test_card_played_other_seat_keeps_local_hand():
    state = ClientState()
    _join(state)
    state.hand = ["7♥", "R♥"]
    apply_message(
        state,
        protocol.CARD_PLAYED,
        {"seat": "N", "card": "A♠", "current_trick": [{"seat": "N", "card": "A♠"}], "next_to_act": "E"},
    )
    assert state.hand == ["7♥", "R♥"]
    assert state.last_action == "Nord a joué A♠"


def test_trick_result_stashes_pending_last_trick():
    state = ClientState()
    _join(state)
    state.current_trick = {Seat.N: "A♠", Seat.E: "R♠"}
    result = apply_message(
        state,
        protocol.TRICK_RESULT,
        {"trick": [{"seat": "N", "card": "A♠"}, {"seat": "E", "card": "R♠"}], "winner_seat": "N", "points_won": 15},
    )
    assert result.action_requested is False
    # current_trick left intact (still shown big during the pause).
    assert state.current_trick == {Seat.N: "A♠", Seat.E: "R♠"}
    assert state.pending_last_trick == {Seat.N: "A♠", Seat.E: "R♠"}
    assert state.last_action == "Pli remporté par Nord (+15 pts)"
    assert state.whose_turn == Seat.N


def test_trick_cleared_promotes_pending_and_clears_current():
    state = ClientState()
    _join(state)
    state.current_trick = {Seat.N: "A♠"}
    state.pending_last_trick = {Seat.N: "A♠", Seat.E: "R♠"}
    result = apply_message(state, protocol.TRICK_CLEARED, {})
    assert result.action_requested is False
    assert state.current_trick == {}
    assert state.last_trick == {Seat.N: "A♠", Seat.E: "R♠"}
    assert state.pending_last_trick is None


def test_round_score_sets_recap_and_requests_action():
    state = ClientState()
    _join(state)
    # Contract fields still describe the round that just ended.
    state.contract_bidder = Seat.N
    state.trump = "♥"
    state.contract_points = 90
    result = apply_message(
        state,
        protocol.ROUND_SCORE,
        {
            "cumulative": {"NS": 90, "EW": 0},
            "team_NS": {"contract_result": "made"},
            "team_EW": {"contract_result": "failed"},
        },
    )
    assert result.action_requested is True
    assert state.cumulative_scores == {"NS": 90, "EW": 0}
    assert state.last_round_score == {"NS": {"contract_result": "made"}, "EW": {"contract_result": "failed"}}
    assert state.last_round_contract == {
        "trump": "♥",
        "points": 90,
        "bidder_name": "Nord",
        "attacking_team": "NS",
        "result": "made",
        "coinche_level": 1,
    }
    assert state.round_over_screen is True
    assert state.whose_turn is None


def test_game_over_retains_final_round_recap_and_requests_action():
    state = ClientState()
    _join(state)
    state.contract_bidder = Seat.N
    state.trump = "♥"
    state.contract_points = 90
    apply_message(
        state,
        protocol.ROUND_SCORE,
        {
            "cumulative": {"NS": 1500, "EW": 800},
            "team_NS": {"contract_result": "made"},
            "team_EW": {"contract_result": "failed"},
        },
    )
    result = apply_message(
        state,
        protocol.GAME_OVER,
        {"final_scores": {"NS": 1500, "EW": 800}, "winning_team": "NS"},
    )
    assert result.action_requested is True
    assert state.game_over is True
    assert state.round_over_screen is True
    assert state.last_round_score == {
        "NS": {"contract_result": "made"},
        "EW": {"contract_result": "failed"},
    }
    assert state.last_round_contract is not None
    assert state.final_scores == {"NS": 1500, "EW": 800}
    assert state.winning_team == "NS"
    assert state.last_action == "Partie terminée — vainqueur : NS"


def test_new_game_resets_scores_and_flags():
    state = ClientState()
    _join(state)
    state.game_over = True
    state.bid_effect_level = 4
    state.cumulative_scores = {"NS": 1500, "EW": 800}
    state.last_round_contract = {"trump": "♥"}
    result = apply_message(state, protocol.NEW_GAME, {})
    assert result.action_requested is False
    assert state.game_over is False
    assert state.round_over_screen is False
    assert state.final_scores == {"NS": 0, "EW": 0}
    assert state.winning_team is None
    assert state.bid_effect_level == 1
    assert state.last_round_score is None
    assert state.last_round_contract is None
    assert state.cumulative_scores == {"NS": 0, "EW": 0}
    assert state.last_action == "Nouvelle partie !"


def test_resync_rebuilds_state():
    state = ClientState()
    result = apply_message(
        state,
        protocol.RESYNC,
        {
            "table_key": "table1",
            "seat": "S",
            "trump": "♥",
            "hand": ["7♥", "A♠"],
            "current_trick": [{"seat": "E", "card": "9♥"}],
            "cumulative_scores": {"NS": 10, "EW": 20},
            "server_version": "9.9.9",
            "whose_turn": "S",
            "phase": "playing",
        },
    )
    assert result.action_requested is False
    assert state.joined_once is True
    assert state.seat == Seat.S
    assert state.trump == "♥"
    assert set(state.hand) == {"7♥", "A♠"}
    assert state.current_trick == {Seat.E: "9♥"}
    assert state.cumulative_scores == {"NS": 10, "EW": 20}
    assert state.round_over_screen is False
    assert state.whose_turn == Seat.S
    # Not in players yet -> a placeholder self entry is created.
    assert state.players.get(Seat.S) == "Moi"


def test_resync_bidding_phase_rebuilds_marks():
    state = ClientState()
    apply_message(
        state,
        protocol.RESYNC,
        {
            "table_key": "table1",
            "seat": "S",
            "trump": None,
            "hand": ["7♥"],
            "current_trick": [],
            "cumulative_scores": {"NS": 0, "EW": 0},
            "whose_turn": "S",
            "phase": "bidding",
            "current_highest_bid": {"trump": "♥", "points": 80, "seat": "N"},
            "bid_history": [{"seat": "N", "action": "bid", "trump": "♥", "points": 80}],
        },
    )
    assert state.current_bid_trump == "♥"
    assert state.current_bid_seat == Seat.N
    assert state.bid_marks[Seat.N] == "80 ♥"


def test_connection_status_updates_map_without_rich():
    state = ClientState()
    _join(state)
    result = apply_message(
        state,
        protocol.CONNECTION_STATUS,
        {"seat": "N", "name": "Nord", "status": "disconnected"},
    )
    assert result.action_requested is False
    assert state.connection_status[Seat.N] is False
    assert state.last_action == "⚠ En attente de Nord (reconnexion...)"

    apply_message(state, protocol.CONNECTION_STATUS, {"seat": "N", "name": "Nord", "status": "connected"})
    assert state.connection_status[Seat.N] is True
    assert state.last_action == "✓ Nord reconnecté"


def test_connection_banner_matches_ui_renderer():
    """BR-U1-7: the plain notice must stay byte-identical to the rich banner's
    `.plain` text (the terminal side may still re-render it styled)."""
    from coinche import ui

    state = ClientState()
    _join(state)
    for status in ("disconnected", "connected", "bot_replaced", "joined"):
        apply_message(state, protocol.CONNECTION_STATUS, {"seat": "N", "name": "Nord", "status": status})
        assert state.last_action == ui.render_connection_banner("Nord", status).plain


def test_bot_replaced_status_swaps_seat_name_and_shows_banner():
    """A human replacing a bot: the seat's displayed name follows to the human's
    and the other players see a join banner (seat/team are unchanged)."""
    state = ClientState()
    _join(state)
    # A bot occupies seat E under some game-character name.
    state.players[Seat.E] = "Sephiroth"
    result = apply_message(
        state,
        protocol.CONNECTION_STATUS,
        {"seat": "E", "name": "Bob", "status": "bot_replaced"},
    )
    assert result.action_requested is False
    assert state.players[Seat.E] == "Bob"
    assert state.bots[Seat.E] is False  # the seat is no longer a bot
    assert state.connection_status[Seat.E] is True
    assert state.last_action == "🙋 Bob a rejoint la table à la place d'un bot"


def test_replaced_by_bot_status_renames_seat_and_flags_bot():
    """A human leaving mid-game: the seat is relabeled to the bot's fresh name
    (`bot_name`) and flagged as a bot, while the banner still names the departed
    human. The bot flag also surfaces in the snapshot for the felt badge."""
    state = ClientState()
    _join(state)
    assert state.bots.get(Seat.N) in (False, None)
    result = apply_message(
        state,
        protocol.CONNECTION_STATUS,
        {"seat": "N", "name": "Nord", "bot_name": "Sephiroth", "status": "replaced_by_bot"},
    )
    assert result.action_requested is False
    assert state.players[Seat.N] == "Sephiroth"  # seat relabeled to the bot
    assert state.bots[Seat.N] is True
    assert state.last_action == ""
    assert snapshot_to_dict(state)["bots"]["N"] is True


def test_join_effect_set_on_bot_replaced():
    """A human replacing a bot triggers the join effect with the newcomer's name."""
    state = ClientState()
    _join(state)
    state.players[Seat.E] = "Sephiroth"
    apply_message(
        state,
        protocol.CONNECTION_STATUS,
        {"seat": "E", "name": "Bob", "status": "bot_replaced"},
    )
    assert state.join_effect_name == "Bob"
    assert state.join_effect_seq == 1
    snap = snapshot_to_dict(state)
    assert snap["join_effect_name"] == "Bob"
    assert snap["join_effect_seq"] == 1


def test_join_effect_set_on_reconnected():
    """A reconnecting player triggers the join effect."""
    state = ClientState()
    _join(state)
    apply_message(
        state,
        protocol.CONNECTION_STATUS,
        {"seat": "N", "name": "Nord", "status": "reconnected"},
    )
    assert state.join_effect_name == "Nord"
    assert state.join_effect_seq == 1


def test_join_effect_set_on_joined():
    """A pre-game empty seat fill triggers the join effect."""
    state = ClientState()
    _join(state)
    apply_message(
        state,
        protocol.CONNECTION_STATUS,
        {"seat": "E", "name": "Est", "status": "joined"},
    )
    assert state.join_effect_name == "Est"
    assert state.join_effect_seq == 1
    assert state.last_action == "👋 Est a rejoint la table"


def test_join_effect_not_set_on_disconnected():
    """A disconnection does NOT trigger the join effect."""
    state = ClientState()
    _join(state)
    apply_message(
        state,
        protocol.CONNECTION_STATUS,
        {"seat": "N", "name": "Nord", "status": "disconnected"},
    )
    assert state.join_effect_name is None
    assert state.join_effect_seq == 0


def test_join_effect_cleared_on_new_deal():
    """The join effect is cleared when a new deal starts."""
    state = ClientState()
    _join(state)
    state.join_effect_name = "Bob"
    state.join_effect_seq = 1
    apply_message(
        state,
        protocol.DEAL,
        {
            "hand": ["7♠", "8♠", "9♠", "10♠", "V♠", "D♠", "R♠", "A♠", "7♥"],
            "round_number": 1,
            "first_bidder_seat": "E",
            "dealer_seat": "N",
        },
    )
    assert state.join_effect_name is None


def test_chat_appends_message_with_team():
    state = ClientState()
    _join(state)
    result = apply_message(state, protocol.CHAT, {"seat": "N", "text": "Salut"})
    assert result.action_requested is False
    assert len(state.chat_messages) == 1
    name, text, team, ts, system = state.chat_messages[0]
    assert (name, text, team) == ("Nord", "Salut", "NS")
    assert isinstance(ts, float)
    assert system is False


def test_system_chat_is_kept_out_of_human_messages():
    state = ClientState()
    _join(state)
    apply_message(state, protocol.CHAT, {"seat": None, "name": "Système", "text": "Fin de manche", "system": True})

    assert len(state.chat_messages) == 0
    name, text, team, ts, system = state.system_messages[0]
    assert (name, text, team, system) == (SYSTEM_CHAT_NAME, "Fin de manche", None, True)
    assert isinstance(ts, float)
    assert snapshot_to_dict(state)["system_messages"][0]["text"] == "Fin de manche"


def test_chat_history_is_not_pruned():
    state = ClientState()
    _join(state)
    for index in range(25):
        apply_message(state, protocol.CHAT, {"seat": "N", "text": f"message {index}"})

    assert len(state.chat_messages) == 25
    assert state.chat_messages[0][1] == "message 0"


def test_bid_announcements_are_added_to_system_messages_and_trigger_coinche_effect():
    state = ClientState()
    _join(state)
    apply_message(
        state,
        protocol.BID_UPDATE,
        {"seat": "N", "action": "bid", "trump": "♥", "points": 90, "next_to_act": "E"},
    )
    apply_message(state, protocol.BID_UPDATE, {"seat": "E", "action": "coinche", "next_to_act": "S"})

    assert [(name, text, team) for name, text, team, _ts, _system in state.system_messages] == [
        ("Nord", "Annonce : 90 ♥", "NS"),
        ("Est", "Coinche ! ×2", "EW"),
    ]
    assert state.bid_effect_level == 2


def test_deal_adds_a_system_separator_after_prior_announcements():
    state = ClientState()
    _join(state)
    apply_message(
        state,
        protocol.BID_UPDATE,
        {"seat": "N", "action": "bid", "trump": "♥", "points": 90, "next_to_act": "E"},
    )

    apply_message(
        state,
        protocol.DEAL,
        {"hand": ["7♥"], "first_bidder_seat": "S", "dealer_seat": "N", "round_number": 2},
    )

    assert [(name, text, team) for name, text, team, _ts, _system in state.system_messages] == [
        ("Nord", "Annonce : 90 ♥", "NS"),
        (SYSTEM_CHAT_NAME, "── Manche 2 ──", None),
    ]
    assert snapshot_to_dict(state)["system_messages"][-1]["system"] is True


def test_surcoinche_result_is_added_to_system_messages_and_triggers_effect():
    state = ClientState()
    _join(state)
    apply_message(
        state,
        protocol.BIDDING_RESULT,
        {
            "outcome": "contract",
            "trump": "♥",
            "points": 90,
            "seat": "N",
            "coinche_level": 4,
            "first_leader": "E",
            "final_bid_action": "surcoinche",
            "final_bid_seat": "S",
        },
    )

    assert [(name, text, team) for name, text, team, _ts, _system in state.system_messages] == [
        ("Moi", "Surcoinche ! ×4", "NS"),
    ]
    assert state.bid_effect_level == 4


def test_error_appends_and_sets_last_action():
    state = ClientState()
    result = apply_message(state, protocol.ERROR, {"code": "NOT_YOUR_TURN"})
    assert result.action_requested is False
    assert len(state.errors) == 1
    assert state.errors[0][1] == "NOT_YOUR_TURN"
    assert state.last_action == "NOT_YOUR_TURN"


# --------------------------------------------------------------------------- #
# ApplyResult flag: exactly the four wake messages                            #
# --------------------------------------------------------------------------- #


def test_action_requested_true_only_for_wake_messages():
    """The four messages that formerly called action_event.set() inside the
    reducer request action; nothing else does."""
    wake = {protocol.BID_REQUEST, protocol.PLAY_REQUEST, protocol.ROUND_SCORE, protocol.GAME_OVER}

    def fresh() -> ClientState:
        s = ClientState()
        _join(s)
        s.contract_bidder = Seat.N  # so ROUND_SCORE can build a contract
        s.trump = "♥"
        s.contract_points = 90
        return s

    samples: dict[str, dict] = {
        protocol.LOBBY_UPDATE: {"players": [], "seats_filled": 0},
        protocol.DEAL: {"hand": ["7♥"], "first_bidder_seat": "N", "dealer_seat": "W", "round_number": 1},
        protocol.BID_REQUEST: {
            "legal_actions": [],
            "current_highest_bid": None,
            "can_coinche": False,
            "can_surcoinche": False,
        },
        protocol.BID_UPDATE: {"seat": "N", "action": "pass", "next_to_act": "E"},
        protocol.BIDDING_RESULT: {"outcome": "redeal"},
        protocol.PLAY_REQUEST: {"legal_cards": ["7♥"], "trump": "♥", "current_trick": []},
        protocol.CARD_PLAYED: {"seat": "N", "card": "A♠", "current_trick": [], "next_to_act": "E"},
        protocol.TRICK_RESULT: {"trick": [], "winner_seat": "N", "points_won": 10},
        protocol.TRICK_CLEARED: {},
        protocol.ROUND_SCORE: {
            "cumulative": {"NS": 90, "EW": 0},
            "team_NS": {"contract_result": "made"},
            "team_EW": {"contract_result": "failed"},
        },
        protocol.GAME_OVER: {"final_scores": {"NS": 1000, "EW": 0}, "winning_team": "NS"},
        protocol.NEW_GAME: {},
        protocol.CONNECTION_STATUS: {"seat": "N", "name": "Nord", "status": "connected"},
        protocol.CHAT: {"seat": "N", "text": "hi"},
        protocol.ERROR: {"code": "X"},
    }
    for msg_type, payload in samples.items():
        result = apply_message(fresh(), msg_type, payload)
        assert result.action_requested is (msg_type in wake), msg_type


# --------------------------------------------------------------------------- #
# snapshot_to_dict: shape, purity, own-seat-only                              #
# --------------------------------------------------------------------------- #


def test_snapshot_is_json_serializable_and_decoded():
    state = ClientState()
    _join(state)
    apply_message(
        state,
        protocol.DEAL,
        {"hand": ["7♠", "A♥"], "first_bidder_seat": "N", "dealer_seat": "W", "round_number": 1},
    )
    snap = snapshot_to_dict(state)
    # Round-trips through JSON (all values are str/int/bool/list/dict).
    encoded = json.dumps(snap)
    assert json.loads(encoded) == snap
    # Seat keys/values are decoded strings, not enums.
    assert snap["seat"] == "S"
    assert snap["players"] == {"N": "Nord", "E": "Est", "S": "Moi", "W": "Ouest"}
    assert snap["dealer_seat"] == "W"
    assert set(snap["hand"]) == {"7♠", "A♥"}
    assert snap["can_fill_bots"] is False
    assert snap["table_options"]["bot_type"] == "toto"


def test_snapshot_includes_last_round_contract():
    state = ClientState()
    _join(state)
    state.contract_bidder = Seat.N
    state.trump = "♥"
    state.contract_points = 90
    apply_message(
        state,
        protocol.ROUND_SCORE,
        {
            "cumulative": {"NS": 90, "EW": 0},
            "team_NS": {"contract_result": "made"},
            "team_EW": {"contract_result": "failed"},
        },
    )
    snap = snapshot_to_dict(state)
    assert snap["last_round_contract"] == {
        "trump": "♥",
        "points": 90,
        "bidder_name": "Nord",
        "attacking_team": "NS",
        "result": "made",
        "coinche_level": 1,
    }


def test_snapshot_exposes_only_local_hand():
    """BR-U1-6 / NFR4: the projection contains only the local seat's hand and no
    key that could carry another seat's cards."""
    state = ClientState()
    _join(state)
    state.hand = ["7♥", "R♥"]
    snap = snapshot_to_dict(state)
    assert snap["hand"] == ["7♥", "R♥"]
    # No other-seat hand leaks: only trick maps are keyed by seat, and they hold
    # single played cards, not hands.
    assert "hands" not in snap
    for key in ("current_trick", "last_trick"):
        for value in snap[key].values():
            assert isinstance(value, str)


def test_snapshot_is_pure_and_non_mutating():
    """INV-2: two calls on unchanged state yield equal dicts and never mutate."""
    state = ClientState()
    _join(state)
    apply_message(
        state,
        protocol.DEAL,
        {"hand": ["7♠", "A♥"], "first_bidder_seat": "N", "dealer_seat": "W", "round_number": 1},
    )
    before_hand = list(state.hand)
    snap1 = snapshot_to_dict(state)
    snap2 = snapshot_to_dict(state)
    assert snap1 == snap2
    assert state.hand == before_hand
    # Mutating the projection must not touch state.
    snap1["hand"].append("X")
    assert state.hand == before_hand


# --------------------------------------------------------------------------- #
# Mirror parity (TR-2 / NFR2b)                                                #
# --------------------------------------------------------------------------- #


def test_mirror_parity_scripted_sequence():
    """Apply a full scripted round and assert `snapshot_to_dict` reflects the
    same key fields the terminal renderer reads off `ClientState` directly."""
    state = ClientState()
    sequence = [
        (
            protocol.JOINED,
            {
                "table_key": "table1",
                "seat": "S",
                "players": [
                    {"seat": "N", "name": "Nord"},
                    {"seat": "E", "name": "Est"},
                    {"seat": "S", "name": "Moi"},
                    {"seat": "W", "name": "Ouest"},
                ],
            },
        ),
        (
            protocol.DEAL,
            {
                "hand": ["7♥", "R♥", "A♠"],
                "first_bidder_seat": "N",
                "dealer_seat": "W",
                "round_number": 1,
            },
        ),
        (
            protocol.BID_REQUEST,
            {
                "legal_actions": [{"trump": "♥"}],
                "current_highest_bid": None,
                "can_coinche": False,
                "can_surcoinche": False,
            },
        ),
        (
            protocol.BIDDING_RESULT,
            {
                "outcome": "contract",
                "trump": "♥",
                "points": 90,
                "seat": "S",
                "coinche_level": 1,
                "first_leader": "N",
            },
        ),
        (
            protocol.PLAY_REQUEST,
            {
                "legal_cards": ["7♥", "R♥"],
                "trump": "♥",
                "current_trick": [{"seat": "N", "card": "9♥"}],
            },
        ),
        (
            protocol.CARD_PLAYED,
            {
                "seat": "S",
                "card": "7♥",
                "current_trick": [{"seat": "N", "card": "9♥"}, {"seat": "S", "card": "7♥"}],
                "next_to_act": "E",
            },
        ),
        (
            protocol.TRICK_RESULT,
            {
                "trick": [{"seat": "N", "card": "9♥"}, {"seat": "S", "card": "7♥"}],
                "winner_seat": "N",
                "points_won": 12,
            },
        ),
        (protocol.TRICK_CLEARED, {}),
        (
            protocol.ROUND_SCORE,
            {
                "cumulative": {"NS": 90, "EW": 0},
                "team_NS": {"contract_result": "made"},
                "team_EW": {"contract_result": "failed"},
            },
        ),
    ]
    for msg_type, payload in sequence:
        apply_message(state, msg_type, payload)

    snap = snapshot_to_dict(state)

    # Both consumers must agree on the player-visible fields.
    assert snap["hand"] == state.hand  # local hand (7♥ removed after play)
    assert "7♥" not in snap["hand"]
    assert snap["legal_cards"] == state.legal_cards
    assert snap["current_trick"] == {s.value: c for s, c in state.current_trick.items()}
    assert snap["last_trick"] == {s.value: c for s, c in state.last_trick.items()}
    assert snap["trump"] == state.trump == "♥"
    assert snap["contract_points"] == state.contract_points == 90
    assert snap["contract_bidder"] == "S"
    assert snap["flags"]["round_over_screen"] is state.round_over_screen is True
    assert snap["last_round_contract"] == state.last_round_contract
    assert snap["last_round_contract"]["result"] == "made"
    # pending_last_trick was promoted into last_trick by TRICK_CLEARED.
    assert state.pending_last_trick is None
    assert snap["last_trick"] == {"N": "9♥", "S": "7♥"}


# --------------------------------------------------------------------------- #
# Spectator (SPECTATING) reducer + chat                                       #
# --------------------------------------------------------------------------- #


def _spectating(state: ClientState, phase: str = "trick_play") -> ApplyResult:
    return apply_message(
        state,
        protocol.SPECTATING,
        {
            "table_key": "table1",
            "spectator_name": "Eve",
            "players": [
                {"seat": "N", "name": "Nord", "team_name": "Nous"},
                {"seat": "E", "name": "Est"},
                {"seat": "S", "name": "Sud"},
                {"seat": "W", "name": "Ouest"},
            ],
            "phase": phase,
            "current_highest_bid": None,
            "bid_history": [],
            "current_trick": [{"seat": "W", "card": "A♥"}],
            "trump": "♥",
            "whose_turn": "N",
            "cumulative_scores": {"NS": 10, "EW": 20},
            "round_number": 3,
            "dealer_seat": "N",
            "contract": {"seat": "W", "trump": "♥", "points": 90, "coinche_level": 1},
            "target_score": 1000,
            "server_version": "9.9.9",
        },
    )


def test_spectating_marks_state_seatless_and_hand_free():
    state = ClientState()
    result = _spectating(state)
    assert result == ApplyResult(action_requested=False)
    assert state.is_spectator is True
    assert state.spectator_name == "Eve"
    assert state.joined_once is True
    assert state.seat is None
    assert state.hand == []
    assert state.can_fill_bots is False
    assert state.pending_bid_request is None
    assert state.trump == "♥"
    assert state.contract_points == 90
    assert state.contract_bidder == Seat.W
    assert state.cumulative_scores == {"NS": 10, "EW": 20}
    assert state.whose_turn == Seat.N

    snap = snapshot_to_dict(state)
    assert snap["is_spectator"] is True
    assert snap["spectator_name"] == "Eve"
    assert snap["seat"] is None
    assert snap["hand"] == []


def test_spectating_waiting_phase_shows_waiting_message():
    state = ClientState()
    apply_message(
        state,
        protocol.SPECTATING,
        {
            "table_key": "table1",
            "spectator_name": "Eve",
            "players": [{"seat": "N", "name": "Nord"}],
            "phase": "waiting",
            "current_highest_bid": None,
            "bid_history": [],
            "current_trick": [],
            "trump": None,
            "whose_turn": None,
            "cumulative_scores": {"NS": 0, "EW": 0},
            "round_number": 0,
            "dealer_seat": None,
            "contract": None,
            "target_score": 1000,
            "server_version": "9.9.9",
        },
    )
    assert state.is_spectator is True
    assert state.contract_points is None
    assert "observez" in state.status_message


def test_spectator_chat_uses_name_and_no_team():
    state = ClientState()
    _spectating(state)
    apply_message(state, protocol.CHAT, {"seat": None, "name": "Eve", "text": "Allez !"})
    who, text, team, _ts, system = state.chat_messages[-1]
    assert who == "Eve"
    assert text == "Allez !"
    assert team is None
    assert system is False


def test_left_clears_spectator_flags():
    state = ClientState()
    _spectating(state)
    apply_message(state, protocol.LEFT, {})
    assert state.is_spectator is False
    assert state.spectator_name is None
    assert state.seat is None
    assert state.joined_once is False
