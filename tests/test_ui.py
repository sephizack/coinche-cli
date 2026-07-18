"""Tests for coinche.ui: rich-markup-injection mitigation, menu builders, layout rotation.

Covers the security-relevant assertion in ui.py's module docstring: untrusted
player names/chat text must never be interpolated into a rich markup-format
string, only ever wrapped via the plain rich.text.Text(value) constructor.
"""

from __future__ import annotations

import io

from rich.console import Console
from rich.text import Text

from coinche.cards import Seat
from coinche.ui import (
    build_chat_panel,
    build_hand,
    build_split_view,
    build_table_layout,
    card_text,
    center_panel,
    contract_text,
    player_panel,
    render_bid_menu,
    render_bid_value_prompt,
    render_connection_banner,
    render_game_over,
    render_lobby,
    render_play_menu,
    render_round_score,
    render_team_picker,
)

MALICIOUS_NAME = "[bold red]INJECTED[/bold red]"


def _plain(renderable) -> str:
    """Render any Rich renderable to plain text for substring assertions."""
    buffer = io.StringIO()
    Console(file=buffer, width=120, no_color=True, force_terminal=False).print(renderable)
    return buffer.getvalue()


# --- Rich-markup-injection mitigation -----------------------------------------


def test_player_panel_name_is_not_parsed_as_markup():
    panel = player_panel(MALICIOUS_NAME, "nous", played=None, is_turn=False)
    assert isinstance(panel.title, Text)
    # The raw bracket syntax must survive untouched in the plain text content --
    # if it had gone through markup parsing, the "[bold red]"/"[/bold red]"
    # tags would have been consumed/stripped rather than appearing literally.
    assert panel.title.plain == MALICIOUS_NAME


def test_player_panel_disconnected_suffix_appended_via_text_append_not_fstring():
    panel = player_panel(MALICIOUS_NAME, "nous", played=None, is_turn=False, connected=False)
    assert MALICIOUS_NAME in panel.title.plain
    assert "(déconnecté)" in panel.title.plain


def test_render_connection_banner_name_is_not_parsed_as_markup():
    banner_disconnected = render_connection_banner(MALICIOUS_NAME, "disconnected")
    assert isinstance(banner_disconnected, Text)
    assert MALICIOUS_NAME in banner_disconnected.plain

    banner_reconnected = render_connection_banner(MALICIOUS_NAME, "reconnected")
    assert MALICIOUS_NAME in banner_reconnected.plain


def test_center_panel_contract_label_is_not_parsed_as_markup():
    malicious_label = f"Contrat de {MALICIOUS_NAME}"
    panel = center_panel("♠", malicious_label, "nous")
    rendered = panel.renderable
    # Group of Text renderables; find the one containing our malicious label.
    texts = [r.renderable if hasattr(r, "renderable") else r for r in rendered.renderables]
    plains = []
    for t in texts:
        inner = t.renderable if hasattr(t, "renderable") else t
        plains.append(inner.plain if isinstance(inner, Text) else str(inner))
    assert any(malicious_label in p for p in plains)


# --- Card rendering ------------------------------------------------------------


def test_card_text_red_suits_styled_differently_from_black():
    red = card_text("10♥")
    black = card_text("10♠")
    assert red.style != black.style


def test_card_text_none_renders_card_back_glyph():
    back = card_text(None)
    assert back.plain == "🂠"


def test_contract_text_shows_coinche_badge_when_doubled():
    plain = contract_text("♥", 90, "Paul", coinche_level=1).plain
    assert "Coinché" not in plain and "Surcoinché" not in plain

    coinched = contract_text("♥", 90, "Paul", coinche_level=2).plain
    assert "Coinché" in coinched and "×2" in coinched

    surcoinched = contract_text("♥", 90, "Paul", coinche_level=4).plain
    assert "Surcoinché" in surcoinched and "×4" in surcoinched


# --- Numbered menus (no raw card-string typing) -------------------------------


def test_render_bid_menu_always_offers_pass_first():
    legal_actions = [{"trump": "♠", "points": 80}, {"trump": "♥", "points": 80}]
    menu, tokens = render_bid_menu(legal_actions, current_highest_bid=None)
    assert tokens["1"] == {"action": "pass"}
    assert "Passer" in _plain(menu)


def test_render_bid_menu_includes_coinche_and_surcoinche_tokens_when_allowed():
    legal_actions: list[dict] = []
    current = {"trump": "♠", "points": 90}
    menu, tokens = render_bid_menu(
        legal_actions, current_highest_bid=current, can_coinche=True, can_surcoinche=False
    )
    assert any(choice.get("action") == "coinche" for choice in tokens.values())
    assert not any(choice.get("action") == "surcoinche" for choice in tokens.values())


def test_render_bid_menu_offers_one_select_trump_entry_per_distinct_trump():
    legal_actions = [
        {"trump": "♠", "points": 80},
        {"trump": "♠", "points": 90},
        {"trump": "♥", "points": 80},
    ]
    menu, tokens = render_bid_menu(legal_actions, current_highest_bid=None)
    select_trump_tokens = [c for c in tokens.values() if c.get("action") == "select_trump"]
    # One entry per distinct trump, not one per point level: 2 entries, not 3.
    assert select_trump_tokens == [
        {"action": "select_trump", "trump": "♠"},
        {"action": "select_trump", "trump": "♥"},
    ]
    # Point values are never enumerated in this stage-1 menu.
    plain = _plain(menu)
    assert "80" not in plain
    assert "Capot" not in plain


def test_render_bid_menu_suit_tokens_are_stable_regardless_of_coinche_surcoinche():
    # The four suits must always land on the same token numbers (2-5), whether
    # or not Coinche/Surcoinche are on offer -- those are appended last instead
    # of being inserted before the suits.
    legal_actions = [
        {"trump": "♠", "points": 80},
        {"trump": "♥", "points": 80},
        {"trump": "♦", "points": 80},
        {"trump": "♣", "points": 80},
    ]
    _, tokens_no_extras = render_bid_menu(legal_actions, current_highest_bid=None)
    _, tokens_with_extras = render_bid_menu(
        legal_actions, current_highest_bid=None, can_coinche=True, can_surcoinche=True
    )
    for tok in ("2", "3", "4", "5"):
        assert tokens_no_extras[tok] == tokens_with_extras[tok]
    assert tokens_no_extras["2"] == {"action": "select_trump", "trump": "♠"}
    assert tokens_no_extras["5"] == {"action": "select_trump", "trump": "♣"}
    assert tokens_with_extras["6"] == {"action": "coinche"}
    assert tokens_with_extras["7"] == {"action": "surcoinche"}


def test_render_bid_value_prompt_lists_range_and_capot_for_chosen_trump():
    legal_actions = [
        {"trump": "♠", "points": 80},
        {"trump": "♠", "points": 90},
        {"trump": "♠", "points": "capot"},
        {"trump": "♥", "points": 80},
    ]
    prompt, valid_points = render_bid_value_prompt("♠", legal_actions)
    assert valid_points == [80, 90, "capot"]
    prompt_text = _plain(prompt)
    assert "80" in prompt_text and "90" in prompt_text and "capot" in prompt_text
    # Only the chosen trump's values are considered.
    assert render_bid_value_prompt("♥", legal_actions)[1] == [80]


def test_render_play_menu_maps_numeric_tokens_to_cards_never_raw_input():
    legal_cards = ["7♠", "V♠", "10♥"]
    menu_text, tokens = render_play_menu(legal_cards)
    assert tokens == {"1": "7♠", "2": "V♠", "3": "10♥"}
    assert "1) 7♠" in menu_text


# --- Layout rotation (local seat always renders at "south") -------------------


def test_build_table_layout_rotates_local_seat_to_south_regardless_of_actual_seat():
    players = {Seat.N: "Alice", Seat.E: "Bob", Seat.S: "Carol", Seat.W: "Dave"}
    team_of = {Seat.N: "NS", Seat.S: "NS", Seat.E: "EW", Seat.W: "EW"}

    # When the local player is physically seated at E, the grid must still
    # render E's own panel at the bottom ("south") slot.
    grid = build_table_layout(Seat.E, players, team_of, current_trick={}, whose_turn=None)
    # The grid is a 3x3 Table.grid; row 2 (index 2) holds the "south" panel.
    bottom_row_renderables = grid.columns[1]._cells
    bottom_panel_title = bottom_row_renderables[2].title
    assert bottom_panel_title.plain == "Bob"


def test_build_hand_renders_one_column_per_card():
    hand = ["7♠", "V♠", "10♥"]
    panel = build_hand(hand)
    assert panel.title == "Ta main"


def test_build_hand_numbers_only_legal_cards_matching_render_play_menu_tokens():
    hand = ["7♠", "V♠", "10♥"]
    legal_cards = ["7♠", "10♥"]
    panel = build_hand(hand, legal_cards)
    grid = panel.renderable
    numbers = [grid.columns[i]._cells[1].plain for i in range(len(hand))]
    assert numbers == ["1", "", "2"]
    _, tokens = render_play_menu(legal_cards)
    assert tokens == {"1": "7♠", "2": "10♥"}


# --- Score / game-over panels ---------------------------------------------------


def test_render_round_score_contains_totals_for_both_teams():
    round_score = {
        "NS": {"total": 162, "card_points": 152},
        "EW": {"total": 0, "card_points": 10},
    }
    cumulative = {"NS": 162, "EW": 0}
    panel = render_round_score(round_score, cumulative, local_team="NS")
    assert panel.title == "Score de la manche"


def test_render_round_score_with_contract_shows_result_and_untrusted_name_unparsed():
    round_score = {
        "NS": {"total": 162, "card_points": 152},
        "EW": {"total": 0, "card_points": 10},
    }
    cumulative = {"NS": 162, "EW": 0}
    contract = {
        "trump": "♥",
        "points": 90,
        "bidder_name": MALICIOUS_NAME,
        "attacking_team": "NS",
        "result": "made",
    }
    panel = render_round_score(round_score, cumulative, local_team="NS", contract=contract)
    assert panel.title == "Score de la manche"
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert MALICIOUS_NAME in output


def test_render_game_over_declares_correct_winner_label():
    final_scores = {"NS": 1010, "EW": 640}
    panel_win = render_game_over(final_scores, winning_team="NS", local_team="NS")
    panel_lose = render_game_over(final_scores, winning_team="NS", local_team="EW")
    assert panel_win.title == "Partie terminée"
    assert panel_lose.title == "Partie terminée"


def test_render_game_over_with_contract_shows_result_and_untrusted_name_unparsed():
    final_scores = {"NS": 1010, "EW": 640}
    contract = {
        "trump": "♥",
        "points": 90,
        "bidder_name": MALICIOUS_NAME,
        "attacking_team": "NS",
        "result": "failed",
    }
    panel = render_game_over(final_scores, winning_team="NS", local_team="NS", contract=contract)
    assert panel.title == "Partie terminée"
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert MALICIOUS_NAME in output
    assert "Annonce chutée" in output


# --- Chat panel ----------------------------------------------------------------


def test_build_chat_panel_shows_placeholder_when_empty():
    from collections import deque

    panel = build_chat_panel(deque(maxlen=20), buffer="", active=False)
    assert panel.title == "Chat"
    assert "(aucun message)" in _plain(panel)


def test_build_chat_panel_renders_messages():
    from collections import deque

    msgs: deque[tuple[str, str, str | None, float]] = deque(maxlen=20)
    msgs.append(("Alice", "bonjour", "NS", 1700000000.0))
    msgs.append(("Bob", "salut", "EW", 1700000060.0))
    panel = build_chat_panel(msgs, buffer="", active=False, local_team="NS")
    plain = _plain(panel)
    assert "Alice" in plain
    assert "bonjour" in plain
    assert "Bob" in plain
    assert "salut" in plain


def test_build_chat_panel_shows_buffer_and_error():
    from collections import deque

    panel = build_chat_panel(deque(maxlen=20), buffer="hello", active=True, error=True)
    plain = _plain(panel)
    assert "hello" in plain
    assert "trop long" in plain


def test_build_chat_panel_active_border_differs():
    from collections import deque

    active = build_chat_panel(deque(maxlen=20), buffer="", active=True)
    inactive = build_chat_panel(deque(maxlen=20), buffer="", active=False)
    assert active.border_style != inactive.border_style


def test_build_chat_panel_name_not_parsed_as_markup():
    from collections import deque

    msgs: deque[tuple[str, str, str | None, float]] = deque(maxlen=20)
    msgs.append((MALICIOUS_NAME, "test", "NS", 1700000000.0))
    panel = build_chat_panel(msgs, buffer="", active=False, local_team="NS")
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    # The malicious markup must appear as literal text, not be parsed as rich markup.
    assert "[bold red]INJECTED[/bold red]" in output


def test_build_chat_panel_renders_timestamp():
    from collections import deque

    msgs: deque[tuple[str, str, str | None, float]] = deque(maxlen=20)
    msgs.append(("Alice", "bonjour", "NS", 1700000000.0))
    panel = build_chat_panel(msgs, buffer="", active=False, local_team="NS")
    plain = _plain(panel)
    # 1700000000.0 is 2023-11-14 22:13:20 UTC; local time varies but HH:MM is always 5 chars
    assert ":" in plain
    assert "Alice" in plain


def test_build_chat_panel_cursor_shown_when_active():
    from collections import deque

    panel = build_chat_panel(deque(maxlen=20), buffer="hi", active=True)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert "hi" in output
    assert ">" in output


def test_build_chat_panel_cursor_default_at_end():
    from collections import deque

    # Passing no cursor arg should render buffer with trailing cursor block
    panel = build_chat_panel(deque(maxlen=20), buffer="ab", active=True)
    plain = _plain(panel)
    assert "ab" in plain
    assert ">" in plain


def test_build_chat_panel_cursor_in_middle():
    from collections import deque

    panel = build_chat_panel(deque(maxlen=20), buffer="abc", active=True, cursor=1)
    plain = _plain(panel)
    assert "abc" in plain


def test_build_chat_panel_char_count_near_limit():
    from collections import deque

    long_buf = "a" * 210
    panel = build_chat_panel(deque(maxlen=20), buffer=long_buf, active=True)
    plain = _plain(panel)
    assert "210/256" in plain


def test_build_chat_panel_char_count_not_shown_when_inactive():
    from collections import deque

    long_buf = "a" * 210
    panel = build_chat_panel(deque(maxlen=20), buffer=long_buf, active=False)
    plain = _plain(panel)
    assert "210/256" not in plain


# --- Split view ----------------------------------------------------------------


def test_build_split_view_has_two_columns():
    from rich.layout import Layout
    from rich.text import Text

    left = Text("left")
    chat = Text("chat")
    layout = build_split_view(left, chat)
    assert isinstance(layout, Layout)
    assert len(layout.children) == 2


# --- render_lobby ---------------------------------------------------------------


def test_render_lobby_cursor_highlight():
    """The cursor row is highlighted and shows 'Nouvelle table' at index 0."""
    panel = render_lobby([], cursor_index=0)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert "Nouvelle table" in output


def test_render_lobby_shows_tables():
    """Existing tables are rendered with their key, seats, and member names."""
    tables = [
        {"table_key": "tbl1", "in_progress": False, "seats_filled": 2, "players": [
            {"seat": "N", "name": "Alice", "team_name": "Equipe 1"},
            {"seat": "S", "name": "Bob", "team_name": "Equipe 2"},
        ]},
    ]
    panel = render_lobby(tables, cursor_index=1)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert "tbl1" in output
    assert "Alice" in output
    assert "Bob" in output
    assert "(2/4)" in output


def test_render_lobby_in_progress_locked():
    """In-progress tables are shown with a lock icon."""
    tables = [
        {"table_key": "live1", "in_progress": True, "seats_filled": 4, "players": [
            {"seat": "N", "name": "A", "team_name": None},
            {"seat": "E", "name": "B", "team_name": None},
            {"seat": "S", "name": "C", "team_name": None},
            {"seat": "W", "name": "D", "team_name": None},
        ]},
    ]
    panel = render_lobby(tables, cursor_index=1)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert "🔒" in output
    assert "en cours" in output


def test_render_team_picker_shows_members():
    """Team picker shows member names under each Equipe option."""
    table = {"table_key": "t1", "in_progress": False, "seats_filled": 1, "players": [
        {"seat": "N", "name": "Alice", "team_name": "Equipe 1"},
    ]}
    panel = render_team_picker(table, team_cursor=0)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert "Equipe 1" in output
    assert "Equipe 2" in output
    assert "Alice" in output


def test_render_team_picker_full_team_locked():
    """A full team shows the lock marker in the team picker."""
    table = {"table_key": "t1", "in_progress": False, "seats_filled": 3, "players": [
        {"seat": "N", "name": "Alice", "team_name": "Equipe 1"},
        {"seat": "S", "name": "Bob", "team_name": "Equipe 1"},
        {"seat": "E", "name": "Carol", "team_name": "Equipe 2"},
    ]}
    panel = render_team_picker(table, team_cursor=1)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert "complète" in output


def test_render_team_picker_malicious_name_not_markup():
    """Player names in team picker are not parsed as rich markup."""
    table = {"table_key": "bad1", "in_progress": False, "seats_filled": 1, "players": [
        {"seat": "N", "name": MALICIOUS_NAME, "team_name": None},
    ]}
    panel = render_team_picker(table, team_cursor=0)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert "[bold red]INJECTED[/bold red]" in output


def test_render_lobby_status_and_error():
    """Status and error messages are rendered."""
    panel_ok = render_lobby([], cursor_index=0, status="Connecté")
    console = Console(record=True, width=100)
    console.print(panel_ok)
    assert "Connecté" in console.export_text()

    panel_err = render_lobby([], cursor_index=0, error="Table en cours")
    console2 = Console(record=True, width=100)
    console2.print(panel_err)
    assert "Table en cours" in console2.export_text()


def test_render_lobby_empty_table_vide():
    """An empty table shows '(vide)'."""
    tables = [
        {"table_key": "empty1", "in_progress": False, "seats_filled": 0, "players": []},
    ]
    panel = render_lobby(tables, cursor_index=1)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert "(vide)" in output


def test_render_lobby_malicious_name_not_markup():
    """Player names with rich-markup-like content are not parsed as markup."""
    tables = [
        {"table_key": "bad1", "in_progress": False, "seats_filled": 1, "players": [
            {"seat": "N", "name": MALICIOUS_NAME, "team_name": None},
        ]},
    ]
    panel = render_lobby(tables, cursor_index=1)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()
    assert "[bold red]INJECTED[/bold red]" in output
