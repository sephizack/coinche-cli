"""
Démo visuelle de la table de jeu (coinche/belote) dans le terminal.

Objectif : valider la lisibilité d'une table avec 4 joueurs (Nord/Sud/Est/Ouest),
les cartes posées au centre, la main du joueur, le contrat et les scores.

Lancement :
    pip install -r requirements.txt
    python demo_table.py
"""

from rich.align import Align
from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

RED_SUITS = {"♥", "♦"}
TEAM_COLORS = {"nous": "cyan", "eux": "magenta"}


def card_text(card: str | None) -> Text:
    """Rend une carte (ex: '10♥', 'V♠') avec la bonne couleur, ou un dos de carte si None."""
    if card is None:
        return Text("🂠", style="grey42")
    suit = card[-1]
    color = "bold red3" if suit in RED_SUITS else "bold white"
    return Text(card, style=color, justify="center")


def player_panel(name: str, team: str, played: str | None, is_turn: bool) -> Panel:
    title = Text(name, style=f"bold {TEAM_COLORS[team]}" + (" reverse" if is_turn else ""))
    body = Align.center(card_text(played), vertical="middle")
    return Panel(
        body,
        title=title,
        title_align="center",
        border_style="yellow" if is_turn else "grey50",
        width=20,
        height=3,
    )


def center_panel(atout: str, contrat: str, camp: str) -> Panel:
    lines = Group(
        Align.center(Text(f"Atout : {atout}", style="bold gold3")),
        Align.center(Text(contrat, style=f"bold {TEAM_COLORS[camp]}")),
    )
    return Panel(lines, border_style="grey50", width=34, height=4)


def build_table_layout() -> Table:
    grid = Table.grid(expand=False, padding=(0, 2))
    grid.add_column(justify="center")
    grid.add_column(justify="center")
    grid.add_column(justify="center")

    empty = Text("")

    # Rangée du haut : Nord
    grid.add_row(empty, player_panel("Nord (Marie)", "eux", "R♥", is_turn=False), empty)

    # Rangée du milieu : Ouest - centre - Est
    grid.add_row(
        player_panel("Ouest (Paul)", "nous", "Q♠".replace("Q", "D"), is_turn=False),
        center_panel(atout="♦", contrat="Paul (Nous) : 90 Coinché", camp="nous"),
        player_panel("Est (Julie)", "eux", "9♦", is_turn=True),
    )

    # Rangée du bas : Sud (toi)
    grid.add_row(empty, player_panel("Sud (Toi)", "nous", None, is_turn=False), empty)

    return grid


def build_hand(cards: list[str]) -> Panel:
    row = Table.grid(padding=(0, 1))
    for _ in cards:
        row.add_column(justify="center")
    row.add_row(*[card_text(c) for c in cards])
    return Panel(row, title="Ta main", border_style="green", padding=(0, 1))


def build_footer(nous: int, eux: int, message: str) -> Table:
    footer = Table.grid(expand=True, padding=(0, 2))
    footer.add_column(justify="left")
    footer.add_column(justify="right")
    scores = Text.assemble(
        ("Nous ", f"bold {TEAM_COLORS['nous']}"),
        (f"{nous}", "bold white"),
        ("   Eux ", f"bold {TEAM_COLORS['eux']}"),
        (f"{eux}", "bold white"),
    )
    footer.add_row(Text(message, style="italic grey70"), scores)
    return footer


def main() -> None:
    console.rule("[bold]Coinche CLI — démo de table[/bold]")
    console.print()

    console.print(Align.center(build_table_layout()))
    console.print()

    hand = [
        "7♠", "8♠", "A♠",
        "9♥", "V♥",
        "D♦", "R♦",
        "10♣",
    ]
    console.print(Align.center(build_hand(hand)))
    console.print()

    console.print(build_footer(nous=82, eux=64, message="Au tour de Julie (Est) de jouer…"))
    console.rule()


if __name__ == "__main__":
    main()
