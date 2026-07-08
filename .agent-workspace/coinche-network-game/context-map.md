# Context Map - Coinche Network Game

## Workspace Overview
- The workspace is a nearly-empty greenfield repo (`coinche-cli`) containing only a UI proof-of-concept ([demo_table.py](../../demo_table.py)), a one-line [README.md](../../README.md) (`# coinche-cli`), and a single-dependency [requirements.txt](../../requirements.txt) (`rich>=13.7`).
- No package structure exists yet (no `src/`, no `pyproject.toml`/`setup.py`, no `tests/`, no `LICENSE`). There is no networking code, no client/server split, no game-logic module, and no test suite — everything for the feature (server, protocol, game engine, bidding, scoring, CLI client) will be new.
- A local virtual environment (`.venv/`) is already provisioned and only contains `rich` and its transitive deps (`markdown_it`, `mdurl`, `pygments`, `pip`). Nothing networking- or CLI-framework-related is installed.
- Git repo is initialized (`.git/`); a `.vscode/process/` folder exists but is empty (no task/launch configs to leverage).

## Core Directory Map
- `/` (workspace root)
  - [demo_table.py](../../demo_table.py) — sole existing source file; a self-contained `rich`-based terminal rendering demo of a 4-seat Coinche table. No classes/modules, no game state, no networking — purely presentational.
  - [README.md](../../README.md) — placeholder title only, no docs/architecture notes.
  - [requirements.txt](../../requirements.txt) — pins `rich>=13.7` only.
  - `.venv/` — Python **3.14.2** virtualenv (`pyvenv.cfg`: `home = /usr/local/opt/python@3.14/bin`), `site-packages` = `rich-15.0.0`, `markdown_it_py-4.2.0`, `mdurl-0.1.2`, `pygments-2.20.0`, `pip-25.3`. No `click`, `typer`, `textual`, `websockets`, `pytest`, or `asyncio`-adjacent third-party packages (asyncio itself is stdlib, fully available on 3.14).
  - `.vscode/process/` — empty directory, no tooling hints.
  - `.agent-workspace/coinche-network-game/` — this task's workflow folder (newly created), containing `status.md`, `context-map.md` (this file), `discoveries.md`.
- No existing `server/`, `client/`, `game/`, or `network/` directories — all will need to be created by the Planner/implementer.

## Module and Dependency Flow
- **Current state**: [demo_table.py](../../demo_table.py) is a flat script (`main()` entry point guarded by `if __name__ == "__main__":`), with no imports beyond `rich` submodules (`rich.align`, `rich.console`, `rich.padding`, `rich.panel`, `rich.table`, `rich.text`). There is no dependency graph to preserve beyond "use `rich` for terminal rendering."
- **Conventions observed in demo_table.py** (to inform later style, not to be violated without reason):
  - French-language domain vocabulary is used throughout: `atout` (trump), `contrat` (contract), `camp`/`nous`/`eux` (team/us/them), `coinché` (doubled). Card strings use French rank letters, e.g. `"V♠"` for Valet (Jack) and `"D♦"` for Dame (Queen) — see the `"Q♠".replace("Q", "D")` line, and suits as literal Unicode glyphs `♠ ♥ ♦ ♣`.
  - Type hints are used on all function signatures (PEP 604 `str | None` unions, `list[str]`), suggesting Python ≥3.10 style consistent with the 3.14 venv.
  - Docstrings are French, module docstring documents purpose + run instructions (`pip install -r requirements.txt` / `python demo_table.py`).
  - Small, single-purpose builder functions returning `rich` renderables (`card_text`, `player_panel`, `center_panel`, `build_table_layout`, `build_hand`, `build_footer`) composed in `main()` — a functional, non-OO style for rendering.
  - Color/style conventions: red suits (`♥ ♦`) rendered `bold red3`, black suits `bold white`; team colors fixed via `TEAM_COLORS = {"nous": "cyan", "eux": "magenta"}`; active turn highlighted via `border_style="yellow"` and reversed/bold title text; face-down/unknown card rendered as `🂠` glyph with `grey42` style.
  - Layout: 3x3 grid via `Table.grid` — N at top, W/center/E in middle row, S (the local player) at bottom, with a separate "hand" panel below the table and a footer with scores + status message.
- **No import of `socket`, `asyncio`, `argparse`, `click`, `typer`, or `textual` anywhere in the codebase** — this confirms the networking/CLI-framework layer is a fresh area with zero existing conventions to inherit from.

## Key Execution Entry Points
- [demo_table.py](../../demo_table.py) `main()` (module-level, run via `python demo_table.py`) is the only current runnable entry point; it is a static, single-render demo (no loop, no input, no network) — useful as a **visual reference** for the eventual client's rendering layer, not as an architectural template for game/server logic.
- No server entry point, no client entry point, no CLI argument parsing exists yet. The Planner will need to define new entry points, e.g. a `coinche-server` script (host, port, table-key management) and a `coinche-client` script (server IP, table key, player name).

## Dependencies & Environment Detail
- Python **3.14.2** (very recent CPython, installed via Homebrew `python@3.14`), venv at `.venv/`, activate via standard `source .venv/bin/activate`.
- Installed packages: `rich==15.0.0` (+ transitive `markdown_it_py`, `mdurl`, `pygments`), `pip==25.3`. Nothing else.
- `requirements.txt` currently lists only `rich>=13.7` — will need additions once the server/client/protocol layer and any testing tooling are chosen (e.g. `pytest` for tests; no networking library is strictly required since `socket`/`asyncio` are stdlib).
- No `pyproject.toml`, no linter/formatter config (no `ruff`, `black`, `mypy` config files found), no CI config (no `.github/workflows`). This is a blank slate for tooling decisions — flagged as an open question below rather than assumed.

## Coinche Rules Reference (authoritative summary for the Planner)

> Compiled from standard French "belote coinchée" club rules. Some numeric
> details (exact game-end target, Sans-Atout/Tout-Atout point tables, capot
> bonus amount, dealing packet split, deal rotation direction) have known
> regional/club variants; these are flagged explicitly as **[VARIANT]** so
> the Planner can pick one and make it configurable rather than hard-coded.

### Deck & Setup
- 32-card deck: ranks 7, 8, 9, 10, Jack (Valet/J), Queen (Dame/Q), King (Roi/K), Ace (As/A), in all 4 suits (♠ ♣ ♥ ♦).
- 4 players in 2 fixed partnerships; partners sit **across** from each other (e.g. North-South vs East-West), so turn order alternates teams every player.
- Turn order and card play proceed in a single fixed rotation for the whole game (commonly counter-clockwise in French clubs) **[VARIANT: some tables play clockwise — must be a fixed, agreed convention]**.

### Deal
- Dealer deals all 8 cards to each player before bidding starts, typically in two packets **[VARIANT: 3-2-3, 3-3-2, or 2-3-3 split]** rather than one card at a time.
- The turn to bid first goes to the player next after the dealer in rotation order.

### Bidding Phase ("Annonces")
- Players bid in turn; each bid names a trump declaration and a point contract, or the player passes.
- Trump declaration options: one of the 4 suits, **"Tout Atout"** (all-trump: every suit's cards use trump point values), or **"Sans Atout"** (no-trump: every suit uses non-trump point values).
- Point contract: minimum **80**, in increments of **10**, up to **180**; **"Capot"** (declaring team commits to winning all 8 tricks) is a special top-end declaration.
- A new bid must strictly outbid the previous one (higher point value, or a switch to a different trump declaration at an equal-or-higher level per table convention).
- **Coinche** ("double"): an opponent of the current highest bidder may coinche that contract instead of bidding/passing, doubling the stakes. The bidding team may respond with **Surcoinche** ("redouble"), further multiplying the stakes.
- Bidding ends when 3 consecutive players pass after the last valid bid. If all 4 players pass with no bid at all, the hand is typically thrown in and re-dealt (rotating the dealer).
- The team of the final (highest, possibly coinched) bidder becomes the **attacking team** (declarer's side); the other team is the **defending team**.

### Card Ranking & Point Values
- **Trump suit** ranking (high→low) and points: Jack = 20, 9 = 14, Ace = 11, 10 = 10, King = 4, Queen = 3, 8 = 0, 7 = 0 → **62 points** total in the trump suit.
- **Non-trump suits** ranking (high→low) and points: Ace = 11, 10 = 10, King = 4, Queen = 3, Jack = 2, 9 = 0, 8 = 0, 7 = 0 → **30 points** per non-trump suit.
- Total card points in a normal (one-suit-trump) hand: 62 + 3×30 = **152**, plus a **10-point "dix de der"** bonus for winning the last trick of the hand → **162 points** available.
- **"Tout Atout"**: all 4 suits use the trump point scale (4×62 = 248 base card points) **[VARIANT: exact total/bonus table varies by club — confirm before implementing]**.
- **"Sans Atout"**: all 4 suits use the non-trump point scale (4×30 = 120 base card points) **[VARIANT: some rule sets rescale so the deck still totals a fixed reference like 162 — confirm before implementing]**.
- **Belote/Rebelote**: if a player holds both the King and Queen of the trump suit, they announce "Belote" when playing the first of the two and "Rebelote" when playing the second; this awards a flat **+20 points** to their team, counted independently of whether the contract is fulfilled.

### Trick-Taking Rules
- The player to the left of the dealer (i.e., next in rotation order) leads the first trick; thereafter the winner of each trick leads the next.
- Players must **follow suit** if able.
- If unable to follow suit, a player must **trump** ("couper") if they hold a trump card.
- If an opponent has already trumped the current trick, a player who must trump is further required to **overtrump** ("monter", play a higher trump) if able.
- Exception: if the highest trump currently in the trick was played by the player's **own partner**, they are not required to overtrump (may play a lower trump or, if void of trump entirely, discard any card) — the "under-trump" exception.
- If void in the led suit and holding no trump, the player may discard any card freely.
- The highest trump played wins the trick if any trump was played; otherwise the highest card of the suit led wins.

### Scoring at End of a Hand
- Tally each team's captured card points (including the 10-point dix-de-der and any belote/rebelote bonus).
- **Contract fulfilled**: the attacking team scores their full captured points (rounded per table convention) plus the contract's bid value is what determines success — i.e., success requires captured points ≥ the bid amount; defending team scores their own captured points. Belote bonus is credited to whichever team holds it regardless of contract outcome.
- **Contract failed ("chute"/"dans les choux")**: the attacking team scores **0** for card points (their captured points are forfeited) and the defending team is credited the **entire 162 points** of the hand, instead of just what they actually captured; belote bonus still goes to whoever holds it.
- **Capot**: if the attacking team bid Capot and wins all 8 tricks, they score a fixed bonus **[VARIANT: commonly 250, sometimes 400 depending on club]** instead of the normal tally; if they fail to take all tricks after bidding Capot, they score 0 and the defenders get the full point pool + capot-failure bonus.
- **Coinche / Surcoinche multiplier**: applies to the final scored amount for the hand — coinche typically **×2**, surcoinche typically **×4** **[VARIANT: some clubs use ×2/×3 or other multiplier tables — confirm]**. The multiplier benefits whichever side "wins" the coinche wager (attacking side if contract met, defending side if contract failed).
- Running cumulative team scores across hands; the dealer position rotates after each hand.

### Game End Condition
- Play continues, hand after hand, until one team's cumulative score reaches or exceeds an agreed target. **[VARIANT: commonly 1000 or 1500/2000 points depending on club/tournament convention]** — recommend the Planner make this a configurable game setting rather than hard-coded.

## Proposed Architecture Considerations (options for the Planner — not a decision)

- **Transport**: Python stdlib `asyncio` + `asyncio.start_server` (TCP sockets) is the natural fit given zero existing networking conventions and no extra dependency needed. Alternative: `websockets` package (adds a dependency) if browser/future non-CLI clients are ever desired — likely unnecessary for a CLI-only client per the feature summary.
- **Protocol**: A simple newline-delimited JSON ("JSON Lines") message protocol over TCP is a lightweight, human-debuggable option consistent with a from-scratch design; alternative is a length-prefixed binary/msgpack protocol for efficiency (unnecessary at this scale — card game traffic is tiny).
- **Message shape**: needs distinct message types for join/lobby (table key + player name), private state (a player's own hand — must never be broadcast to others), and public state (table layout, last-played cards, scores, whose turn) — mirrors the public/private split already implicit in [demo_table.py](../../demo_table.py)'s rendering (each seat only shows a face-down back or the played card, while "Ta main" shows only the local player's hand).
- **Concurrency/state model**: server holds an authoritative in-memory registry of `Table` objects keyed by table key, each owning up to 4 connected client sessions and the current `Round`/game state machine (waiting-room → dealing → bidding → trick-play → scoring → next hand). A single asyncio event loop can multiplex all tables and connections without needing threads/processes.
- **Client input loop**: the client must read from the socket (async) while also accepting terminal input (bidding choices, card to play). Since `input()` is blocking, options are `asyncio.to_thread(input, ...)`, a dedicated input thread, or adopting `prompt_toolkit`/`textual` for async-native terminal interaction. `textual` (same ecosystem as the already-used `rich`) is a strong candidate if a richer, redrawing TUI is desired beyond the demo's static per-turn re-print style.
- **Rendering reuse**: the render helper functions in [demo_table.py](../../demo_table.py) (`player_panel`, `center_panel`, `build_hand`, `build_footer`, color/style conventions) are directly reusable/extendable as the basis of the real client's table view — they already encode the exact visual conventions (team colors, turn highlighting, card back glyph) the feature should keep.
- **CLI argument parsing**: no framework currently chosen; stdlib `argparse` requires no new dependency (server host/port/table-keys; client server-IP/table-key/player-name), vs. adding `click`/`typer` for nicer ergonomics.
- **Table key handling**: needs a decision on whether keys are server-generated (returned to the host player) or arbitrarily chosen by whoever starts a table, and whether collisions/validation are enforced.

## Open Questions
- Dealing rotation direction (clockwise vs counter-clockwise) and the exact dealing packet split (3-2-3 vs alternatives) — needs a single fixed convention.
- Exact point tables and bonus values for "Sans Atout" and "Tout Atout" contracts, and the Capot bonus amount — regional variance noted above; Planner should pick concrete numbers (ideally configurable/documented) before implementation.
- Coinche/Surcoinche multiplier values (×2/×4 assumed) — confirm final values.
- Target cumulative score for game end (1000 vs 1500/2000) — should this be a configurable server/table setting?
- Reconnection handling: what happens if a client disconnects mid-hand? Not specified in the feature summary; needs a policy (pause/timeout/bot takeover/forfeit).
- Table key semantics: server-generated vs host-chosen, uniqueness enforcement, and whether any authentication beyond knowing the key is required (currently no security model exists in the codebase).
- Should the client UI be a static "re-print each turn" model (matching the current demo) or a live-redrawing TUI (e.g. via `textual`)? This affects the client architecture significantly.
- Testing strategy: no test framework or CI is present yet — should `pytest` and a CI workflow be introduced as part of this feature?
