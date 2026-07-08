"""Client-side rendering: live table view, keyboard-shortcut menus, status banners.

Ports demo_table.py's rich rendering conventions (team colors, turn highlighting,
card-back glyph, panel layout) to accept live game state instead of hardcoded
demo values, composed into one root renderable per redraw for rich.live.Live.

Security note: all player-supplied strings (names, chat text) are wrapped via
the plain rich.text.Text(value) constructor, never interpolated into a
markup-format string, to prevent rich-markup injection from a malicious player
name or chat message.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from coinche.cards import Seat

RED_SUITS = {"♥", "♦"}
TEAM_COLORS = {"nous": "cyan", "eux": "magenta"}

# Fixed counter-clockwise rotation order (A1), used to rotate the visual
# layout so the local seat always renders at "south".
_ROTATION: tuple[Seat, ...] = (Seat.N, Seat.W, Seat.S, Seat.E)
_VISUAL_SLOTS = ("south", "east", "north", "west")


def _visual_position(actual_seat: Seat, local_seat: Seat) -> str:
    offset = (_ROTATION.index(actual_seat) - _ROTATION.index(local_seat)) % 4
    return _VISUAL_SLOTS[offset]


def local_team_of(local_seat: Seat, team_of: dict[Seat, str]) -> str:
    """The local player's own team id ("NS"/"EW"), mapped to "nous" by the caller."""
    return team_of[local_seat]


def card_text(card: str | None) -> Text:
    """Render a card (e.g. '10♥', 'V♠') with suit coloring, or a card back if None."""
    if card is None:
        return Text("🂠", style="grey42")
    suit = card[-1]
    color = "bold red3" if suit in RED_SUITS else "bold white"
    return Text(card, style=color, justify="center")


def player_panel(name: str, team: str, played: str | None, is_turn: bool, connected: bool = True) -> Panel:
    """A single seat's panel. `name` is untrusted and wrapped via Text(), never markup."""
    style = f"bold {TEAM_COLORS[team]}" + (" reverse" if is_turn else "")
    title = Text(name, style=style)
    if not connected:
        title.append(" (déconnecté)", style="dim red")
    body = Align.center(card_text(played), vertical="middle")
    border_style = "yellow" if is_turn else ("red" if not connected else "grey50")
    return Panel(
        body,
        title=title,
        title_align="center",
        border_style=border_style,
        width=22,
        height=3,
    )


def center_panel(trump_display: str, contract_label: str, camp: str) -> Panel:
    """`contract_label` may embed an untrusted player name; always passed to Text()."""
    lines = Group(
        Align.center(Text(f"Atout : {trump_display}", style="bold gold3")),
        Align.center(Text(contract_label, style=f"bold {TEAM_COLORS[camp]}")),
    )
    return Panel(lines, border_style="grey50", width=34, height=4)


def build_table_layout(
    local_seat: Seat,
    players: dict[Seat, str],
    team_of: dict[Seat, str],
    current_trick: dict[Seat, str],
    whose_turn: Seat | None,
    center: Panel | None = None,
    connection_status: dict[Seat, bool] | None = None,
) -> Table:
    """3x3 grid, rotated so `local_seat` always renders at the bottom ("south")."""
    connection_status = connection_status or {}
    local_team = team_of[local_seat]
    grid = Table.grid(expand=False, padding=(0, 2))
    grid.add_column(justify="center")
    grid.add_column(justify="center")
    grid.add_column(justify="center")

    empty = Text("")
    panels: dict[str, Panel] = {}
    for seat, name in players.items():
        pos = _visual_position(seat, local_seat)
        camp = "nous" if team_of[seat] == local_team else "eux"
        panels[pos] = player_panel(
            name,
            camp,
            current_trick.get(seat),
            is_turn=(seat == whose_turn),
            connected=connection_status.get(seat, True),
        )

    grid.add_row(empty, panels.get("north", empty), empty)
    grid.add_row(panels.get("west", empty), center or empty, panels.get("east", empty))
    grid.add_row(empty, panels.get("south", empty), empty)
    return grid


def build_hand(cards: list[str], legal_cards: list[str] | None = None) -> Panel:
    """Render the hand; when `legal_cards` is given, a number appears under each
    legal card (matching the tokens from `render_play_menu`) so the player can
    pick a card by its number without a separate printed list."""
    row = Table.grid(padding=(0, 1))
    for _ in cards:
        row.add_column(justify="center")
    row.add_row(*[card_text(c) for c in cards])

    if legal_cards is not None:
        remaining = list(legal_cards)
        token = 1
        number_cells: list[Text] = []
        for card in cards:
            if remaining and card == remaining[0]:
                number_cells.append(Text(str(token), style="bold yellow", justify="center"))
                remaining.pop(0)
                token += 1
            else:
                number_cells.append(Text(""))
        row.add_row(*number_cells)

    return Panel(row, title="Ta main", border_style="green", padding=(0, 1))


def waiting_for_text(
    whose_turn: Seat | None,
    players: dict[Seat, str],
    team_of: dict[Seat, str],
    local_seat: Seat,
) -> Text:
    """Bold indicator of whose turn it is right now, distinct from the "last
    action" line: "À vous !" (reversed, unmissable) when it's the local
    player's turn, or "En attente de <Nom>" with the name in that player's
    team color when waiting on someone else. Returns empty text if unknown
    (e.g. between hands, before/after the round)."""
    if whose_turn is None:
        return Text("")
    if whose_turn == local_seat:
        return Text(" À vous ! ", style="bold black on yellow")
    name = players.get(whose_turn, whose_turn.value)
    camp = "nous" if team_of.get(whose_turn) == team_of.get(local_seat) else "eux"
    text = Text("En attente de ", style="grey70")
    text.append(f" {name} ", style=f"bold white on {'dark_cyan' if camp == 'nous' else 'magenta'}")
    return text


def build_footer(
    cumulative_scores: dict[str, int],
    local_team: str,
    last_action: str,
    waiting: Text | None = None,
) -> Table:
    other_team = "EW" if local_team == "NS" else "NS"
    footer = Table.grid(expand=True, padding=(0, 2))
    footer.add_column(justify="left")
    footer.add_column(justify="right")
    scores = Text.assemble(
        ("Nous ", f"bold {TEAM_COLORS['nous']}"),
        (f"{cumulative_scores.get(local_team, 0)}", "bold white"),
        ("   Eux ", f"bold {TEAM_COLORS['eux']}"),
        (f"{cumulative_scores.get(other_team, 0)}", "bold white"),
    )
    footer.add_row(Text(last_action, style="italic grey50"), scores)
    if waiting is not None and waiting.plain:
        footer.add_row(waiting, Text(""))
    return footer


def build_table_view(
    local_seat: Seat,
    players: dict[Seat, str],
    team_of: dict[Seat, str],
    current_trick: dict[Seat, str],
    whose_turn: Seat | None,
    hand: list[str],
    cumulative_scores: dict[str, int],
    local_team: str,
    last_action: str,
    center: Panel | None = None,
    connection_status: dict[Seat, bool] | None = None,
    legal_cards: list[str] | None = None,
) -> Group:
    """Compose the whole table view into one root renderable for rich.live.Live."""
    table_layout = build_table_layout(
        local_seat, players, team_of, current_trick, whose_turn, center=center, connection_status=connection_status
    )
    waiting = waiting_for_text(whose_turn, players, team_of, local_seat)
    return Group(
        Align.center(table_layout),
        Text(""),
        Align.center(build_hand(hand, legal_cards)),
        Text(""),
        build_footer(cumulative_scores, local_team, last_action, waiting),
    )


def render_bid_menu(
    legal_actions: list[dict],
    current_highest_bid: dict | None,
    can_coinche: bool = False,
    can_surcoinche: bool = False,
) -> tuple[str, dict[str, dict]]:
    """Stage-1 numbered bid menu: Passer / Coinche / Surcoinche / Annoncer <trump>.

    One line per distinct trump present in `legal_actions` (no free-typing here
    either). The point value itself is a separate stage 2 the player types by
    hand — see `render_bid_value_prompt` — instead of enumerating every single
    point level as its own menu entry.
    """
    lines: list[str] = []
    tokens: dict[str, dict] = {}
    token = 1

    lines.append(f"{token}) Passer")
    tokens[str(token)] = {"action": "pass"}
    token += 1

    if can_coinche:
        lines.append(f"{token}) Coinche")
        tokens[str(token)] = {"action": "coinche"}
        token += 1

    if can_surcoinche:
        lines.append(f"{token}) Surcoinche")
        tokens[str(token)] = {"action": "surcoinche"}
        token += 1

    seen_trumps: list[str] = []
    for bid in legal_actions:
        if bid["trump"] not in seen_trumps:
            seen_trumps.append(bid["trump"])

    for trump in seen_trumps:
        trump_label = "Tout Atout" if trump == "tout_atout" else trump
        lines.append(f"{token}) Annoncer {trump_label}")
        tokens[str(token)] = {"action": "select_trump", "trump": trump}
        token += 1

    if current_highest_bid is None:
        header = "Enchère actuelle : aucune"
    else:
        cur_trump = "Tout Atout" if current_highest_bid["trump"] == "tout_atout" else current_highest_bid["trump"]
        cur_points = "Capot" if current_highest_bid["points"] == "capot" else current_highest_bid["points"]
        header = f"Enchère actuelle : {cur_points} {cur_trump}"

    menu_text = header + "\n" + "\n".join(lines)
    return menu_text, tokens


def render_bid_value_prompt(trump: str, legal_actions: list[dict]) -> tuple[str, list[int | str]]:
    """Stage-2 prompt: player types the point value by hand for the chosen `trump`.

    Returns the prompt text plus the sorted list of legal values (ints, and
    "capot" if it's still an option) so the caller can validate the typed input.
    """
    trump_label = "Tout Atout" if trump == "tout_atout" else trump
    points_for_trump = [bid["points"] for bid in legal_actions if bid["trump"] == trump]
    numeric_points = sorted(p for p in points_for_trump if p != "capot")
    has_capot = "capot" in points_for_trump

    range_parts = []
    if numeric_points:
        range_parts.append(f"{numeric_points[0]} à {numeric_points[-1]} (pas de 10)")
    if has_capot:
        range_parts.append("'capot'")

    prompt = f"Valeur de l'annonce pour {trump_label} ({' ou '.join(range_parts)}) : "
    valid_points: list[int | str] = list(numeric_points)
    if has_capot:
        valid_points.append("capot")
    return prompt, valid_points


def render_play_menu(legal_cards: list[str]) -> tuple[str, dict[str, str]]:
    """Numbered card menu; the token->card map is what client.py uses, never raw input."""
    lines: list[str] = []
    tokens: dict[str, str] = {}
    for i, card in enumerate(legal_cards, start=1):
        lines.append(f"{i}) {card}")
        tokens[str(i)] = card
    return "\n".join(lines), tokens


def render_connection_banner(name: str, status: str) -> Text:
    """`name` is untrusted; always appended via Text.append(), never markup-formatted."""
    text = Text()
    if status == "disconnected":
        text.append("⚠ ", style="bold yellow")
        text.append("En attente de ", style="italic grey70")
        text.append(name, style="bold white")
        text.append(" (reconnexion...)", style="italic grey70")
    else:
        text.append("✓ ", style="bold green")
        text.append(name, style="bold white")
        text.append(" reconnecté", style="italic grey70")
    return text


def render_round_score(round_score: dict, cumulative: dict, local_team: str) -> Panel:
    other_team = "EW" if local_team == "NS" else "NS"
    lines = Group(
        Align.center(
            Text(
                f"Nous : {round_score[local_team]['total']} pts "
                f"(cartes : {round_score[local_team]['card_points']})",
                style=f"bold {TEAM_COLORS['nous']}",
            )
        ),
        Align.center(
            Text(
                f"Eux : {round_score[other_team]['total']} pts "
                f"(cartes : {round_score[other_team]['card_points']})",
                style=f"bold {TEAM_COLORS['eux']}",
            )
        ),
        Align.center(
            Text(
                f"Cumulé — Nous : {cumulative[local_team]}   Eux : {cumulative[other_team]}",
                style="bold white",
            )
        ),
    )
    return Panel(lines, title="Score de la manche", border_style="green")


def render_game_over(final_scores: dict, winning_team: str, local_team: str) -> Panel:
    other_team = "EW" if local_team == "NS" else "NS"
    won = winning_team == local_team
    result_label = "Victoire !" if won else "Défaite"
    style = "bold green" if won else "bold red3"
    lines = Group(
        Align.center(Text(result_label, style=style)),
        Align.center(
            Text(
                f"Score final — Nous : {final_scores[local_team]}   Eux : {final_scores[other_team]}",
                style="bold white",
            )
        ),
    )
    return Panel(lines, title="Partie terminée", border_style="gold3")
