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
python -m pytest                          # 93 tests, ~1.5s
```

`ruff check . --fix` and `ruff format coinche demo_table.py` apply autofixes.
CI (`.github/workflows/ci.yml`) runs the same three checks on push/PR for Python 3.10 and 3.13.

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
