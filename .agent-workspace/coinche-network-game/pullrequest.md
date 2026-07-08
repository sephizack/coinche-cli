# feat: networked terminal-based Coinche (belote coinchée) card game

## Context & Summary

Adds a complete networked implementation of Coinche: an asyncio TCP server hosting multiple 4-player tables, and a `rich`-based CLI client. Players join a table by host/port + table key + name, and the server drives the full game loop — deal → bid → trick play → score → repeat until a target cumulative score is reached — with server-authoritative rule enforcement and mid-game reconnection support. This is a from-scratch feature; no existing runtime code was modified.

## Key Changes

- [coinche/__init__.py](../../coinche/__init__.py) — new package marker.
- [coinche/cards.py](../../coinche/cards.py) — `Suit`/`Rank`/`Card`, 32-card deck build, and the 3-2-3 dealing split with N→W→S→E rotation.
- [coinche/rules.py](../../coinche/rules.py) — point tables, bid legality/raise rules, follow-suit/overtrump trick legality, and round scoring (contract, capot, coinche multipliers, belote).
- [coinche/game.py](../../coinche/game.py) — I/O-free `Game`/`RoundState` state machine: bidding, trick resolution, scoring, dealer rotation, game-over detection, and `snapshot_for(seat)` for reconnect resync.
- [coinche/protocol.py](../../coinche/protocol.py) — JSON-Lines message-type constants, `encode`/`decode`, and per-type field validation.
- [coinche/table.py](../../coinche/table.py) — `ClientSession`/`Table` seat management, join/name-collision rules, disconnect tracking, and reconnection.
- [coinche/server.py](../../coinche/server.py) — asyncio TCP server: connection handling, join/reconnect resolution, server-authoritative dispatch of `bid`/`play_card`, and broadcast/send-to translation of game events.
- [coinche/ui.py](../../coinche/ui.py) — `rich`-based live-redraw table rendering, rotated to the local seat, numbered bid/play menus, and connection-status banners.
- [coinche/client.py](../../coinche/client.py) — CLI client: connection/reconnect-supervisor loop, receiver + input-prompt coroutines driving a persistent `rich.live.Live` view.
- [pyproject.toml](../../pyproject.toml) — new minimal project/package definition with `[project.scripts]` entry points.
- [tests/test_cards.py](../../tests/test_cards.py), [tests/test_rules.py](../../tests/test_rules.py), [tests/test_game.py](../../tests/test_game.py), [tests/test_table.py](../../tests/test_table.py), [tests/test_ui.py](../../tests/test_ui.py) — unit suites for each module.
- [tests/test_integration.py](../../tests/test_integration.py) — real ephemeral-port TCP server + 4 scripted socket clients driving full rounds, illegal/out-of-turn actions, malformed input, and disconnect/reconnect scenarios end-to-end.
- [requirements.txt](../../requirements.txt) — confirmed `pytest>=8.0` already present (no change required).
- [README.md](../../README.md) — replaced placeholder with install/run instructions matching the actual CLI flags.

## Key Design Decisions (A1–A17)

House rules and protocol choices baked into the implementation (full rationale in [plan.md](plan.md)):

| # | Decision | Chosen value |
|---|---|---|
| A1 | Rotation | Counter-clockwise, N→W→S→E |
| A2 | Partnerships | N+S vs E+W (seat-based teams `NS`/`EW`, not player-relative) |
| A3 | Deal split | 3-2-3 |
| A4 | First bidder/leader | Player after dealer |
| A5 | Bid range | 80–180, step 10, + distinct top-level Capot |
| A6 | Bid raise rule | Strict rank raise only; Capot outranks all; one Capot per auction |
| A7 | Point table | Trump ladder on all 4 suits under Tout-Atout (258 pts); **Sans-Atout removed entirely** |
| A8 | Capot bonus | Flat 250 (0 if failed); no bonus for undeclared/incidental capot |
| A9 | Coinche/Surcoinche | ×2 / ×4, bidding team only, single surcoinche |
| A10 | Failed contract | Attackers score 0; defenders get full point pool (162/258) |
| A11 | Belote/Rebelote | Auto-detected and credited server-side, no announce message |
| A12 | Target score | 1000, configurable via `--target-score` |
| A13 | Tie at target | Sudden-death extra hand if tied; otherwise higher score wins outright |
| A14 | Table key | Host-chosen, lazy-created, case-insensitive alnum, 4–12 chars, no auth |
| A15 | Name collision | Rejected if connected; matched to reconnection if disconnected |
| A17 | Chat | Free-text passthrough, no persistence/rate limiting |

### Reconnection & Anti-Cheat Model (A16)

- **Server-authoritative validation:** the server independently re-validates every `bid`/`play_card` against live `Game` state (turn order, `rules.legal_bid_actions`/`legal_cards_to_play`) before applying or broadcasting it. Client-side numbered menus are a UX convenience only, never trusted.
- **Reconnection:** a disconnected seat's `ClientSession` is never cleared — only flagged `connected=False`. A subsequent `join` with a matching case-insensitive name at that seat is treated as a reconnect: the server re-attaches the new socket and sends a `resync` snapshot (`Game.snapshot_for(seat)`) instead of `joined`/`deal`, and broadcasts `connection_status` to the table. No bot takeover, no timeout/forfeit, no persistence across a server restart — a disconnected seat pauses the game indefinitely until it reconnects or the process ends.

## Impact & Limitations

- Fully self-contained new package; zero risk to existing code ([demo_table.py](../../demo_table.py) untouched).
- Server-side rules are exhaustively unit-tested; reconnection is verified end-to-end over real sockets in both the bidding and trick-play phases.
- **Out of scope / not implemented:** bot/AI seat-filling, spectator/replay mode, persistence or a database, CI/lint pipeline (no `.github/workflows/`).
- **Follow-up candidates (non-blocking, logged in [discoveries.md](discoveries.md)):**
  - DISC-011 — `player_name` has no explicit max length (only indirectly bounded by the 64 KiB line limit).
  - DISC-012 — no dedicated test for a coinched/surcoinched Capot contract (shared scoring code path, coverage gap only).
  - DISC-013 — belote/rebelote is not evaluated under `tout_atout` declarations (defensible interpretation, not explicitly specified).

## Verification

Full suite, run independently during final review:

```bash
python -m pytest -q
# 72 passed in ...

python -m pytest --cov=coinche --cov-report=term-missing -q
```

**Result:** 72/72 passing, 0 failures, stable across repeated runs (no flake observed). Coverage 72% overall:

| Module | Coverage |
|---|---|
| `cards.py` | 100% |
| `table.py` | 97% |
| `rules.py` | 96% |
| `game.py` | 92% |
| `ui.py` | 90% |
| `protocol.py` | 86% |
| `server.py` | 81% |
| `client.py` | 0% (manual-smoke-test-only layer, per plan; see DISC-010) |

**2 bugs found and fixed during QA**, both regression-tested:
1. Empty `card` string in `play_card` crashed the connection handler with an unhandled `IndexError` instead of a clean `ILLEGAL_CARD` error — fixed with an explicit length guard before parsing.
2. A line exceeding asyncio's 64 KiB `StreamReader` limit crashed the connection handler with an unhandled `ValueError` instead of `MALFORMED_MESSAGE` — fixed with an explicit `except ValueError` around both `readline()` call sites.

**Manual verification (per [README.md](../../README.md)):**

```bash
# Start the server
python -m coinche.server --port 8765 --target-score 1000

# In 4 separate terminals, start clients at the same table
python -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Alice
python -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Bob
python -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Carol
python -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Dave
```

Once all 4 seats fill, the server deals a hand and the game begins. Killing and relaunching a client with the same `--table`/`--name` reconnects to the same seat and resumes play.
