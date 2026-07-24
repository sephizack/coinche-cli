# AGENTS.md

Guidance for AI coding agents working in `coinche-cli`. Keep changes small,
tested, and consistent with the existing layered design.

## What this is

A networked, terminal Coinche (belote coinchée) card game:

- an asyncio **TCP server** (`coinche/server.py`) hosting multiple 4-player tables, and
- a `rich`-based **CLI client** (`coinche/client.py`) that joins a table and plays a full game.

Deal → bid → trick play → score, repeated until a target score is reached.
Python 3.10+ (uses `from __future__ import annotations` and modern type-hint syntax).

## Architecture (respect these boundaries)

The core rule is a **strict separation between game logic and I/O**:

| Layer | Files | Responsibility |
|---|---|---|
| Pure rules/state | `cards.py`, `rules.py`, `game.py` | I/O-free, no `await`/sockets. Methods return plain event/result dicts or raise a `GameError` subclass. |
| Wire protocol | `protocol.py` | Newline-delimited JSON encode/decode; centralizes enum↔string conversion. |
| Transport / sessions | `server.py`, `table.py` | asyncio connection handling, seat assignment, disconnect/reconnect. Server-authoritative validation. |
| Client / rendering | `client.py`, `ui.py` | Connects, renders live table view with `rich`. |
| Session state (I/O-free) | `session_state.py` | The one `ClientState` + `apply_message` reducer + `snapshot_to_dict` projection shared by the terminal and web views. No `await`/sockets/`rich`. |
| Web overlay (client-local) | `web/` (`server.py`, `messages.py`, `static/`) | In-process HTTP+WebSocket bridge that mirrors `ClientState` to browsers and relays browser actions through the client's `ClientLink`. |

Rules to preserve:

- **Never add `await`, sockets, or network I/O to `game.py`/`rules.py`/`cards.py`.** They must stay unit-testable in isolation. Put transport concerns in `server.py`/`table.py`.
- **The server is authoritative.** All move validation lives in `game.py`/`rules.py` and is enforced server-side; the client only renders and prompts.
- **Assemble a player's view in exactly one place:** `Game.snapshot_for(seat)`. The per-turn request path and the reconnect path both reuse it — don't build a second view assembler.
- **All enum→JSON conversion goes through the shared helpers** in `protocol.py`. Duplicated inline conversions are the most common serialization-bug source here.

## Setup, checks, and the verification loop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Before submitting any change, run all three and make sure they pass:

```bash
ruff check .                              # lint (E, F, I, B, UP)
ruff format --check coinche demo_table.py # formatting
python -m pytest                          # ~110 tests, ~2s
```

`ruff check . --fix` and `ruff format coinche demo_table.py` apply autofixes.
CI (`.github/workflows/ci.yml`) runs the same three checks on push/PR for Python 3.10 and 3.13.

## Wire protocol additions

- **`LIST_TABLES`** (client→server, no payload): replies with **`TABLE_LISTING`**
  containing `{"tables": [{"table_key", "in_progress", "seats_filled", "players": [{"seat","name","team_name"}]}]}`.
  Sent by the client before `JOIN` to power the interactive table picker;
  `_resolve_join` loops over `LIST_TABLES` and only proceeds on `JOIN`.
- **`SUBSCRIBE_LOBBY`** (client→server, no payload): registers the connection
  for live push `TABLE_LISTING` updates.  The server immediately replies with
  the current listing, then pushes a fresh listing whenever a table is created,
  a seat is filled/removed, or a game starts/stops.  The writer is removed
  from the subscriber set on `JOIN` or disconnect (try/finally in
  `_resolve_join`).  Pushes are delivered via `notify_lobby_subscribers()` in
  `table.py`; `LOBBY_SUBSCRIBERS` (a `set[asyncio.StreamWriter]`) lives there
  next to `TABLES`.
  The lobby picker (`_lobby_picker` in `client.py`) is a two-step flow rendered
  in the alternate buffer (`Live(screen=True)`): step 1 — browse tables and
  pick one (Enter) or create a new one; step 2 — pick Equipe 1 or Equipe 2,
  Enter to JOIN, Esc to return to step 1.  Live `TABLE_LISTING` pushes refresh
  whichever step is active.
- **`team_name` guard** (`Table.add_player`): when a `team_name` match is
  found but that label already has 2 seated players, the match branch is
  skipped and the player is seated by normal seat-filling order instead,
  preserving the opposite-seat pairing invariant.
- **`CHAT`** (client→server, `{"text": str}`): the server fans out
  `{"seat": str, "text": str}` to every client (including the sender).
  Messages are client-side ephemeral only — no server storage.  The client
  renders them in a split-pane chat panel (`ui.build_chat_panel`);
  `Tab` toggles focus between the game pane and the chat pane.

## Web overlay (`coinche/web/`)

Each client runs an optional in-process HTTP + WebSocket server (a **proxy**,
not a second game connection) that mirrors the local session to browsers.
Launch it with `python -m coinche.client ... [--web-port PORT]` (default `0` =
auto); the reachable URL(s) are printed on start (`Interface web disponible :
http://...`). It runs as a 3rd coroutine in `run_session`'s gather.

Boundaries to preserve (these are hard rules for this package):

- **Proxy only.** The bridge MUST NOT open a socket to the game server and MUST
  NOT encode a game-wire message. Every server-bound browser action goes through
  U1's `ClientLink.send_*` seams (the single writer); all wire encoding stays in
  `protocol.py`.
- **No authority.** The bridge never evaluates legality (legal cards, bids,
  scoring). It relays intent; the server decides and any `ERROR` flows back
  through the normal `apply_message` → `broadcast_state` state path.
- **Own-seat-only.** The bridge only ever pushes `snapshot_to_dict(state)`,
  which contains just the local seat's hand — never another seat's cards.
- **Error boundary.** No browser event (disconnect, malformed/oversized frame,
  slow socket) may propagate out to cancel `receiver_loop`/`input_loop`.
  `serve()` swallows non-`CancelledError` faults, `_handle_ws` isolates
  per-browser faults, and `broadcast_state` bounds each send with `wait_for`.
- **Unauthenticated `0.0.0.0` listener.** Intended for trusted LANs only —
  document this wherever you surface the overlay.
- **Transport is hand-rolled stdlib.** `web/server.py` implements the RFC 6455
  handshake + text frames directly (no `websockets` dependency). Keep it minimal
  (single unfragmented text/close/ping frames); binary/fragmented frames are
  rejected by design.
- **`messages.py` is pure** (no I/O): `parse_browser_message` enforces a 64 KiB
  size cap + shape validation, and the frame encoders are plain JSON.
- HTML-safe rendering of untrusted strings (names, chat) is the browser UI's job
  (`textContent`); the bridge passes them through as JSON data and MUST NOT emit
  HTML fragments.

## Testing conventions

- Tests live in `tests/`, mirroring modules (`test_rules.py`, `test_game.py`, …) plus `test_integration.py` for end-to-end server/client flows.
- Because the rules layer is I/O-free, **prefer testing game logic directly against `game.py`/`rules.py`** (fast, deterministic) rather than through the socket layer.
- Any behavior change to rules, scoring, or the protocol needs a matching test. Run the suite; passing tests are the primary safety net (there is no separate type-checker).

## Security notes

- **Untrusted strings** (player names, chat text) must be rendered with the plain `rich.text.Text(value)` constructor or `.append()`, **never** interpolated into a markup string like `console.print(f"[bold]{value}[/]")`. The bracket syntax would otherwise be parsed as `rich` markup (injection). See `ui.py`'s module docstring.
- The server relies on `asyncio.StreamReader.readline()`'s 64 KiB limit as a cheap DoS guard. Its failure mode is a bare `ValueError`, so **every `readline()` must be wrapped in `except ValueError`** (handshake and main loop) or the "guard" crashes the handler instead of rejecting cleanly.
- Validate all client input server-side; never trust the client to enforce legal moves.

## Files not to edit

- `.coverage`, `.pytest_cache/`, `__pycache__/`, `.venv/` — generated/local, git-ignored.
- `.agent-workspace/` — historical planning notes from a prior agent run; read for context, but it is not authoritative documentation.

## Conventions

- Line length 120 (see `pyproject.toml`).
- Existing code comments and docstrings are in a mix of French and English; match the surrounding file. User-facing strings are French.
- Keep guidance and behavior in sync: if you change a check, a command, or an architectural rule, update this file and `README.md`.
