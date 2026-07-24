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

import time
from collections import deque

from rich.align import Align
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from coinche.cards import Seat

RED_SUITS = {"♥", "♦"}
TEAM_COLORS = {"nous": "cyan", "eux": "magenta"}

MAX_CHAT_LEN = 256

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


def player_panel(
    name: str, team: str, played: str | None, is_turn: bool, connected: bool = True, is_dealer: bool = False
) -> Panel:
    """A single seat's panel. `name` is untrusted and wrapped via Text(), never markup."""
    style = f"bold {TEAM_COLORS[team]}" + (" reverse" if is_turn else "")
    title = Text(name, style=style)
    if is_dealer:
        title.append(" (D)", style="bold yellow")
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
    dealer_seat: Seat | None = None,
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
            is_dealer=(seat == dealer_seat),
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
        # Tokens are assigned by each card's position within `legal_cards`
        # (matching `render_play_menu`'s numbering), then looked up by card
        # identity rather than by matching position in `cards` — `cards` is
        # typically sorted for display and does not share `legal_cards`'
        # (server-side, unsorted-hand) ordering.
        token_by_card = {card: i for i, card in enumerate(legal_cards, start=1)}
        number_cells: list[Text] = [
            Text(str(token_by_card[card]), style="bold yellow", justify="center") if card in token_by_card else Text("")
            for card in cards
        ]
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


def contract_text(
    trump: str | None,
    points: str | int | None,
    bidder_name: str | None,
    coinche_level: int = 1,
) -> Text:
    """'Annonce en cours' line (e.g. "Annonce : 90 Cœur (Paul)"). `bidder_name`
    is untrusted and always appended via Text.append(), never markup-formatted.
    Returns empty text while no contract has been settled yet.

    `coinche_level` (2 = coinché, 4 = surcoinché) appends a coloured badge
    after the annonce so players can see the stakes were doubled/quadrupled."""
    if not trump or points is None:
        return Text("")
    trump_label = trump
    points_label = "Capot" if points == "capot" else str(points)
    text = Text("Annonce : ", style="grey70")
    text.append(f"{points_label} {trump_label}", style="bold gold3")
    if bidder_name:
        text.append(" (", style="grey70")
        text.append(bidder_name, style="bold white")
        text.append(")", style="grey70")
    if coinche_level == 2:
        text.append("  Coinché ×2 ", style="bold white on red3")
    elif coinche_level >= 4:
        text.append("  Surcoinché ×4 ", style="bold white on dark_red")
    return text


def last_trick_grid(local_seat: Seat, last_trick: dict[Seat, str]) -> Panel | None:
    """Mini table-shaped rendering of the most recently completed trick: each
    card is positioned at its own player's seat, rotated so `local_seat`
    always renders at the bottom ("south") — mirroring `build_table_layout`'s
    N/E/S/W cross shape instead of a flat card list, so the trick's shape
    matches the table and stays intuitive for every player. Returns None
    before any trick has completed."""
    if not last_trick:
        return None
    grid = Table.grid(padding=0)
    grid.add_column(justify="center", width=3)
    grid.add_column(justify="center", width=3)
    grid.add_column(justify="center", width=3)
    empty = Text("")
    cells: dict[str, Text] = {}
    for seat, card in last_trick.items():
        cells[_visual_position(seat, local_seat)] = card_text(card)
    grid.add_row(empty, cells.get("north", empty), empty)
    grid.add_row(cells.get("west", empty), empty, cells.get("east", empty))
    grid.add_row(empty, cells.get("south", empty), empty)
    return Panel(grid, title="Dernier pli", border_style="grey50", padding=(0, 1), expand=False)


def build_footer(
    cumulative_scores: dict[str, int],
    local_team: str,
    last_action: str,
    waiting: Text | None = None,
    contract: Text | None = None,
    last_trick: Panel | None = None,
    team_names: dict[str, str] | None = None,
    web_url: str | None = None,
) -> Table:
    """`team_names` (team id -> untrusted free-text label) is only ever
    embedded via an f-string into a `Text`/`Text.assemble` tuple, never parsed
    as markup, so it's safe the same way other untrusted strings are handled
    elsewhere in this module."""
    other_team = "EW" if local_team == "NS" else "NS"
    local_label = (team_names or {}).get(local_team) or "Nous"
    other_label = (team_names or {}).get(other_team) or "Eux"
    footer = Table.grid(expand=True, padding=(0, 2))
    footer.add_column(justify="left")
    footer.add_column(justify="right")
    scores = Text.assemble(
        (f"{local_label} ", f"bold {TEAM_COLORS['nous']}"),
        (f"{cumulative_scores.get(local_team, 0)}", "bold white"),
        (f"   {other_label} ", f"bold {TEAM_COLORS['eux']}"),
        (f"{cumulative_scores.get(other_team, 0)}", "bold white"),
    )
    if last_trick is not None:
        footer.add_row(Text(""), Align.right(last_trick))
    if contract is not None and contract.plain:
        footer.add_row(Text(""), contract)
    footer.add_row(Text(last_action, style="italic grey50"), scores)
    if waiting is not None and waiting.plain:
        footer.add_row(waiting, Text(""))
    if web_url:
        # In-game reminder of the web UI address, rendered as an OSC-8 hyperlink
        # so terminals that support it make the URL clickable. `web_url` is a
        # locally-computed http URL (never untrusted input), so embedding it in
        # a Text is safe.
        web_line = Text.assemble(
            ("\U0001f310 Interface web : ", "grey50"),
            (web_url, f"bold {TEAM_COLORS['nous']} link {web_url}"),
        )
        footer.add_row(web_line, Text(""))
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
    trump: str | None = None,
    contract_points: str | int | None = None,
    contract_bidder_name: str | None = None,
    coinche_level: int = 1,
    last_trick: dict[Seat, str] | None = None,
    dealer_seat: Seat | None = None,
    bid_menu: Group | Text | None = None,
    team_names: dict[str, str] | None = None,
    web_url: str | None = None,
) -> Group:
    """Compose the whole table view into one root renderable for rich.live.Live.

    `bid_menu`, when given, is the current bidding-turn prompt (either the
    stage-1 choice grid from `render_bid_menu` or the stage-2 point-value
    prompt from `render_bid_value_prompt`) rendered inline as part of the
    persistent live view, right below the hand -- instead of being printed
    as a separate block above the live region.

    `team_names`, when given, maps team id ("NS"/"EW") to a free-text label a
    player chose (via `--team`); shown in the footer instead of "Nous"/"Eux"
    for that team.
    """
    table_layout = build_table_layout(
        local_seat,
        players,
        team_of,
        current_trick,
        whose_turn,
        center=center,
        connection_status=connection_status,
        dealer_seat=dealer_seat,
    )
    waiting = waiting_for_text(whose_turn, players, team_of, local_seat)
    contract = contract_text(trump, contract_points, contract_bidder_name, coinche_level)
    last_trick_panel = last_trick_grid(local_seat, last_trick or {})
    blocks: list[RenderableType] = [
        Align.center(table_layout),
        Text(""),
        Align.center(build_hand(hand, legal_cards)),
    ]
    if bid_menu is not None:
        blocks.append(Text(""))
        blocks.append(Align.center(bid_menu))
    blocks.append(Text(""))
    blocks.append(
        build_footer(
            cumulative_scores,
            local_team,
            last_action,
            waiting,
            contract,
            last_trick_panel,
            team_names,
            web_url=web_url,
        )
    )
    return Group(*blocks)


_BID_CARD_WIDTH = 18


def _bid_choice_card(token: str, label: str) -> Panel:
    """One small rectangular "card" for a stage-1 bid choice: just the number and
    the action, no free-typing needed to pick it."""
    label_style = "bold red3" if label in RED_SUITS else "bold white"
    content = Text.assemble((f"{token}) ", "bold yellow"), (label, label_style))
    return Panel(Align.center(content), width=_BID_CARD_WIDTH, padding=(0, 1), border_style="grey50")


def _cards_grid(entries: list[tuple[str, str]], cards_per_row: int) -> Table:
    """Lay `entries` out as a grid of `_bid_choice_card`s, `cards_per_row` per line."""
    grid = Table.grid(padding=(0, 1))
    for _ in range(cards_per_row):
        grid.add_column()
    row: list[Panel] = []
    for tok, label in entries:
        row.append(_bid_choice_card(tok, label))
        if len(row) == cards_per_row:
            grid.add_row(*row)
            row = []
    if row:
        grid.add_row(*row)
    return grid


def render_bid_menu(
    legal_actions: list[dict],
    current_highest_bid: dict | None,
    can_coinche: bool = False,
    can_surcoinche: bool = False,
    cards_per_row: int = 3,
) -> tuple[Group, dict[str, dict]]:
    """Stage-1 bid menu: Passer / Annoncer <trump> / Coinche / Surcoinche, laid out
    as a grid of small numbered cards (`cards_per_row` per line) instead of a
    plain vertical list.

    Token numbers are stable regardless of which optional actions are
    available: 1 is always "Passer" and the four suits always occupy 2-5 (in
    fixed suit order), since they're always legal bid choices. Coinche/
    Surcoinche are only sometimes on offer, so they're numbered last instead
    of being inserted before the suits (which would otherwise shift the
    suits' numbers around depending on the auction state).

    One card per distinct trump present in `legal_actions` (no free-typing here
    either). The point value itself is a separate stage 2 the player types by
    hand — see `render_bid_value_prompt` — instead of enumerating every single
    point level as its own card.

    Returns a plain Rich renderable (a `Group`, not a pre-rendered string), so
    the caller can embed it directly into the persistent `rich.live.Live` view
    (e.g. via `build_table_view`'s `bid_menu` param) instead of baking it into
    an ANSI-escaped string via a throwaway `Console` and printing it above the
    live region -- that round trip previously caused the raw ANSI codes to be
    corrupted when the string was fed back through another `Console.print`'s
    markup parser (the "[38;5;244m"-as-literal-text display bug).
    """
    entries: list[tuple[str, str]] = []
    tokens: dict[str, dict] = {}
    token = 1

    entries.append((str(token), "Passer"))
    tokens[str(token)] = {"action": "pass"}
    token += 1

    seen_trumps: list[str] = []
    for bid in legal_actions:
        if bid["trump"] not in seen_trumps:
            seen_trumps.append(bid["trump"])

    for trump in seen_trumps:
        entries.append((str(token), trump))
        tokens[str(token)] = {"action": "select_trump", "trump": trump}
        token += 1

    if can_coinche:
        entries.append((str(token), "Coinche"))
        tokens[str(token)] = {"action": "coinche"}
        token += 1

    if can_surcoinche:
        entries.append((str(token), "Surcoinche"))
        tokens[str(token)] = {"action": "surcoinche"}
        token += 1

    if current_highest_bid is None:
        header = "Enchère actuelle : aucune"
    else:
        cur_trump = current_highest_bid["trump"]
        cur_points = "Capot" if current_highest_bid["points"] == "capot" else current_highest_bid["points"]
        header = f"Enchère actuelle : {cur_points} {cur_trump}"

    grid = _cards_grid(entries, cards_per_row)
    menu = Group(Text(header, style="grey70"), grid)
    return menu, tokens


def render_bid_value_prompt(trump: str, legal_actions: list[dict]) -> tuple[Text, list[int | str]]:
    """Stage-2 prompt: player types the point value by hand for the chosen `trump`.

    Returns the prompt text (a `Text` renderable, embeddable directly in the
    live view) plus the sorted list of legal values (ints, and "capot" if it's
    still an option) so the caller can validate the typed input.
    """
    trump_label = trump
    points_for_trump = [bid["points"] for bid in legal_actions if bid["trump"] == trump]
    numeric_points = sorted(p for p in points_for_trump if p != "capot")
    has_capot = "capot" in points_for_trump

    range_parts = []
    if numeric_points:
        range_parts.append(f"{numeric_points[0]} à {numeric_points[-1]} (pas de 10)")
    if has_capot:
        range_parts.append("'capot'")

    prompt = Text(f"Valeur de l'annonce pour {trump_label} ({' ou '.join(range_parts)}) : ", style="bold gold3")
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


def render_update_notice(current_version: str, server_version: str) -> Panel:
    """One-time banner shown when the client's version doesn't match the server's,
    prompting the player to update (e.g. via `git pull`) before playing."""
    text = Text()
    text.append("⚠ Nouvelle version disponible ", style="bold yellow")
    text.append(f"(vous : {current_version}, serveur : {server_version}).\n", style="grey70")
    text.append("Mettez à jour le client (", style="grey70")
    text.append("git pull", style="bold white")
    text.append(") puis relancez-le pour éviter d'éventuels problèmes de compatibilité.", style="grey70")
    return Panel(text, border_style="yellow", title="Mise à jour recommandée", title_align="left")


def render_round_score(
    round_score: dict,
    cumulative: dict,
    local_team: str,
    team_names: dict[str, str] | None = None,
    contract: dict | None = None,
) -> Panel:
    """End-of-round recap: each team's points for the manche just played, the
    updated cumulative score, and (if `contract` is given) whether the
    announced contract was honored.

    `contract`, when given, has the same shape as `render_game_over`'s:
    {"trump": str, "points": int|"capot", "bidder_name": str (untrusted,
    always wrapped via Text), "attacking_team": "NS"|"EW", "result":
    "made"|"failed"|"capot_achieved"|"capot_failed"}.
    """
    other_team = "EW" if local_team == "NS" else "NS"
    local_label = (team_names or {}).get(local_team) or "Nous"
    other_label = (team_names or {}).get(other_team) or "Eux"
    blocks = [
        Align.center(
            Text(
                f"{local_label} : {round_score[local_team]['total']} pts "
                f"(cartes : {round_score[local_team]['card_points']})",
                style=f"bold {TEAM_COLORS['nous']}",
            )
        ),
        Align.center(
            Text(
                f"{other_label} : {round_score[other_team]['total']} pts "
                f"(cartes : {round_score[other_team]['card_points']})",
                style=f"bold {TEAM_COLORS['eux']}",
            )
        ),
        Align.center(
            Text(
                f"Cumulé — {local_label} : {cumulative[local_team]}   {other_label} : {cumulative[other_team]}",
                style="bold white",
            )
        ),
    ]

    if contract is not None:
        points_label = "Capot" if contract["points"] == "capot" else str(contract["points"])
        camp = "nous" if contract["attacking_team"] == local_team else "eux"
        contract_line = Text("Annonce : ", style="grey70")
        contract_line.append(f"{points_label} {contract['trump']}", style=f"bold {TEAM_COLORS[camp]}")
        contract_line.append(" par ", style="grey70")
        contract_line.append(contract["bidder_name"], style="bold white")
        result_text, result_style = _CONTRACT_RESULT_LABELS.get(contract["result"], (contract["result"], "bold white"))
        blocks.append(Text(""))
        blocks.append(Align.center(contract_line))
        blocks.append(Align.center(Text(result_text, style=result_style)))

    blocks.append(Text(""))
    blocks.append(Align.center(Text("Appuyez sur une touche pour continuer...", style="bold yellow")))

    return Panel(Group(*blocks), title="Score de la manche", border_style="green")


_CONTRACT_RESULT_LABELS: dict[str, tuple[str, str]] = {
    "made": ("Annonce réussie", "bold green"),
    "failed": ("Annonce chutée", "bold red3"),
    "capot_achieved": ("Capot réussi", "bold green"),
    "capot_failed": ("Capot chuté", "bold red3"),
}


def render_game_over(
    final_scores: dict,
    winning_team: str,
    local_team: str,
    contract: dict | None = None,
    team_names: dict[str, str] | None = None,
) -> Panel:
    """End-of-game screen: final cumulative score per team, the overall winner,
    and (if `contract` is given) whether the very last round's announced
    contract was honored, plus a prompt to start a new game or quit.

    `contract`, when given, is {"trump": str, "points": int|"capot",
    "bidder_name": str (untrusted, always wrapped via Text), "attacking_team":
    "NS"|"EW", "result": "made"|"failed"|"capot_achieved"|"capot_failed"}
    describing the last round played.
    """
    other_team = "EW" if local_team == "NS" else "NS"
    local_label = (team_names or {}).get(local_team) or "Nous"
    other_label = (team_names or {}).get(other_team) or "Eux"
    won = winning_team == local_team
    result_label = "Victoire !" if won else "Défaite"
    style = "bold green" if won else "bold red3"

    blocks = [
        Align.center(Text(result_label, style=style)),
        Align.center(
            Text(
                f"Score final — {local_label} : {final_scores[local_team]}"
                f"   {other_label} : {final_scores[other_team]}",
                style="bold white",
            )
        ),
    ]

    if contract is not None:
        points_label = "Capot" if contract["points"] == "capot" else str(contract["points"])
        camp = "nous" if contract["attacking_team"] == local_team else "eux"
        contract_line = Text("Dernière annonce : ", style="grey70")
        contract_line.append(f"{points_label} {contract['trump']}", style=f"bold {TEAM_COLORS[camp]}")
        contract_line.append(" par ", style="grey70")
        contract_line.append(contract["bidder_name"], style="bold white")
        result_text, result_style = _CONTRACT_RESULT_LABELS.get(contract["result"], (contract["result"], "bold white"))
        blocks.append(Text(""))
        blocks.append(Align.center(contract_line))
        blocks.append(Align.center(Text(result_text, style=result_style)))

    blocks.append(Text(""))
    blocks.append(Align.center(Text("[1] Nouvelle partie     [2] Quitter", style="bold yellow")))

    return Panel(Group(*blocks), title="Partie terminée", border_style="gold3")


def build_chat_panel(
    messages: deque[tuple[str, str, str | None, float]],
    buffer: str,
    active: bool,
    error: bool = False,
    local_team: str | None = None,
    cursor: int | None = None,
) -> Panel:
    """Right-side chat panel: message list + inline input buffer.

    Each message is ``(name, text, team_id, ts)`` where *team_id* is
    ``"NS"``/``"EW"`` or ``None`` and *ts* is a client-side receive timestamp
    (``time.time()``).  When *local_team* is given, the sender's name is
    coloured with the matching ``TEAM_COLORS`` (``"nous"`` for same-team,
    ``"eux"`` for opposite-team).

    When *active*, the input buffer is rendered with a reverse-video block
    cursor at the position indicated by *cursor* (default: end of buffer).

    All player-supplied strings are wrapped via plain ``Text(value)`` to
    prevent rich-markup injection (see module docstring).
    """
    if cursor is None:
        cursor = len(buffer)
    _NAME_WIDTH = 10
    lines: list[RenderableType] = []
    for name, text, team, ts in messages:
        if team is not None and local_team is not None:
            camp = "nous" if team == local_team else "eux"
            name_style = f"bold {TEAM_COLORS[camp]}"
        else:
            name_style = "bold"
        line = Text(style="dim" if not active else "")
        line.append(time.strftime("%H:%M", time.localtime(ts)), style="dim")
        line.append(" ")
        line.append(name.ljust(_NAME_WIDTH), style=name_style)
        line.append(" ")
        line.append(text)
        lines.append(line)
    if not lines:
        lines.append(Text("  (aucun message)", style="italic grey50"))
    # Echo the typed buffer at the bottom
    prompt = Text("> ", style="bold green" if active else "grey50")
    if active:
        before = buffer[:cursor]
        after = buffer[cursor:]
        if before:
            prompt.append(before, style="bold white")
        if after:
            prompt.append(Text(after[0], style="reverse bold white"))
            if len(after) > 1:
                prompt.append(after[1:], style="bold white")
        else:
            prompt.append(Text(" ", style="reverse"))
    else:
        prompt.append(buffer, style="bold white")
    if error:
        prompt.append("  \u26a0 trop long", style="bold red")
    if active and len(buffer) >= int(0.8 * MAX_CHAT_LEN):
        count_style = "bold red" if error else "yellow"
        prompt.append(f"  {len(buffer)}/{MAX_CHAT_LEN}", style=count_style)
    lines.append(prompt)
    border = "bold cyan" if active else "grey50"
    body = Group(*lines)
    return Panel(body, title="Chat", title_align="center", border_style=border, expand=True)


def build_split_view(
    left: RenderableType,
    chat: RenderableType,
    focus: str = "game",
    height: int | None = None,
) -> Layout:
    """Side-by-side layout: left panel (table view) + right panel (chat).

    ``focus`` is ``"game"`` or ``"chat"``; the focused pane's border is
    rendered with a highlight colour by the callers of ``build_chat_panel``
    and the wrapping of ``left`` in ``Panel`` (done in ``client.py``).

    When *height* is given the layout fills exactly that many terminal rows
    so both panes stretch to the full screen height.
    """
    root = Layout()
    root.split_row(
        Layout(left, ratio=1),
        Layout(chat, ratio=1),
    )
    if height is not None:
        root.size = height
    return root


def render_lobby(
    tables: list[dict],
    cursor_index: int,
    status: str = "",
    error: str = "",
) -> RenderableType:
    """Interactive lobby table picker (step 1: table selection).

    Row 0 is always "✦ Nouvelle table"; rows 1..N are existing
    tables from *tables*.  The row at *cursor_index* is highlighted.

    All player-supplied strings (names, table keys) are rendered via
    ``Text()`` — never interpolated into markup — to prevent injection.
    """
    rows: list[RenderableType] = []

    # --- Row 0: new table -------------------------------------------------
    is_cursor = cursor_index == 0
    new_line = Text()
    new_line.append(" >> " if is_cursor else "    ", style="bold green")
    new_line.append("Nouvelle table", "white" + (" bold" if is_cursor else ""))
    rows.append(new_line)

    # --- Rows 1..N: existing tables ---------------------------------------
    for i, t in enumerate(tables, start=1):
        is_cursor = cursor_index == i
        locked = t["in_progress"] or t["seats_filled"] >= 4
        names = ", ".join(p["name"] for p in t["players"]) if t["players"] else "(vide)"
        status_tag = ""
        if t["in_progress"]:
            status_tag = " en cours"
        elif t["seats_filled"] >= 4:
            status_tag = " complète"
        seats_str = f"({t['seats_filled']}/4{status_tag})"

        style = (" dim" if locked else "") + (" bold" if is_cursor else "")

        line = Text()
        line.append(" >> " if is_cursor else "    ", style="bold green")
        line.append(t["table_key"].ljust(14), style=style)
        line.append(seats_str.ljust(18), style=style + " cyan")
        line.append(names, style=style)
        if locked:
            line.append(" 🔒", style="dim")
        rows.append(line)

    # --- Status / error / help --------------------------------------------
    if status:
        rows.append(Text(status, style="bold yellow"))
    if error:
        rows.append(Text(f"⚠ {error}", style="bold red"))
    rows.append(Text(""))
    rows.append(
        Text(
            "↑↓ sélectionner · Entrée choisir · Échap annuler",
            style="dim grey50",
        )
    )

    return Panel(
        Group(*rows),
        title="Lobby — Tables disponibles",
        title_align="left",
        border_style="bold cyan",
        expand=True,
    )


def render_team_picker(
    table_entry: dict,
    team_cursor: int,
    error: str = "",
) -> RenderableType:
    """Team selection panel (step 2) for a chosen table.

    Shows the table header (key, seats filled) with each player's name,
    then the two Equipe options (1 = ``team_cursor`` == 0, 2 = 1) with
    member lists and a 🔒 marker when a team is full.

    ``team_cursor`` is 0 (Equipe 1) or 1 (Equipe 2).

    All player-supplied strings are wrapped via ``Text()`` — never
    interpolated into markup.
    """
    rows: list[RenderableType] = []

    # Table header
    locked = table_entry["in_progress"] or table_entry["seats_filled"] >= 4
    names = ", ".join(p["name"] for p in table_entry["players"]) if table_entry["players"] else "(vide)"
    header = Text()
    header.append(table_entry["table_key"], style="bold white")
    header.append(f"  ({table_entry['seats_filled']}/4)", style="cyan")
    if locked:
        header.append(" 🔒", style="dim")
    rows.append(header)
    rows.append(Text(f"  {names}", style="dim" if locked else "white"))
    rows.append(Text(""))

    # Equipe options
    equipes: dict[str, list[str]] = {"Equipe 1": [], "Equipe 2": []}
    for p in table_entry["players"]:
        tn = p.get("team_name")
        if tn in equipes:
            equipes[tn].append(p["name"])

    for idx, label in enumerate(("Equipe 1", "Equipe 2")):
        is_cursor = team_cursor == idx
        members = equipes[label]
        full = len(members) >= 2
        member_str = ", ".join(members) if members else "(libre)"
        line = Text()
        style = (" dim" if full else "") + (" bold" if is_cursor else "")
        line.append(" >> " if is_cursor else "    ", style="bold green" if is_cursor else "")
        line.append(f"{idx + 1}) ", style="yellow" + style)
        line.append(f"{label} ", style="cyan" + style)
        line.append("— ", style="grey50" + style)
        line.append(member_str, style=("dim" if full else "white") + style)
        if full:
            line.append(" 🔒 complète", style="dim red")
        rows.append(line)

    if error:
        rows.append(Text(f"⚠ {error}", style="bold red"))
    rows.append(Text(""))
    rows.append(
        Text(
            "↑↓ ou 1/2 choisir l'équipe · Entrée rejoindre · Échap retour",
            style="dim grey50",
        )
    )

    return Panel(
        Group(*rows),
        title=f"Lobby — {table_entry['table_key']}",
        title_align="left",
        border_style="bold cyan",
        expand=True,
    )


def _render_team_options(table_entry: dict | None) -> RenderableType:
    """Render Equipe 1 / Equipe 2 options for the highlighted table entry."""
    equipes: dict[str, list[str]] = {"Equipe 1": [], "Equipe 2": []}
    if table_entry is not None:
        for p in table_entry["players"]:
            tn = p.get("team_name")
            if tn in equipes:
                equipes[tn].append(p["name"])

    lines: list[RenderableType] = []
    for idx, label in enumerate(("Equipe 1", "Equipe 2"), start=1):
        members = equipes[label]
        full = len(members) >= 2
        member_str = ", ".join(members) if members else "(libre)"
        line = Text()
        line.append(f"      {idx}) ", style="bold yellow")
        line.append(f"{label} ", style="bold cyan")
        line.append("— ", style="grey50")
        line.append(member_str, style="dim" if full else "white")
        if full:
            line.append(" 🔒 complète", style="dim red")
        lines.append(line)
    return Group(*lines)
