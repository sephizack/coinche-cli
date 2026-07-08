# Knowledge Base

Cross-feature, generalizable architectural recommendations, gotchas, and
reusable solutions extracted by Tony Reviewer after QA passes. Keep entries
short, factual, and de-duplicated.

## Architectural Recommendations

- When building a wire protocol on top of Python `enum` types (e.g. a `Seat`
  enum), centralize every enum -> JSON-string conversion in a single shared
  helper (e.g. `_seat_to_str` / `_bid_to_wire`) and reuse it at every call
  site that builds an outbound payload. Duplicated inline conversions are the
  most common source of "works for the first code path, crashes on the
  second" JSON-serialization bugs (see Known Gotchas below).
- For a turn-based, request/response game protocol over asyncio TCP, a
  transport-agnostic, I/O-free state-machine module (pure functions/methods
  returning event dicts, no `await`/socket calls) kept fully separate from
  the connection-handling layer makes both layers independently unit- and
  integration-testable, and keeps server-authoritative validation trivial to
  reason about (coinche-cli's `game.py`/`rules.py` vs `server.py` split).

## Known Gotchas

- `asyncio.StreamReader.readline()`'s default line-length limit (64 KiB) is a
  reasonable zero-extra-code DoS guard for newline-delimited protocols, but
  its failure mode is a bare `ValueError` ("Separator is found, but chunk is
  longer than limit"), not a distinct/catchable exception type. Any read loop
  relying on this limit as a security boundary must explicitly
  `except ValueError` around every `readline()` call (both the initial
  handshake read and the main loop), or the "guard" will crash the connection
  handler instead of triggering a clean rejection.
- `rich.text.Text(value)`'s plain constructor (as opposed to
  `Text.from_markup(...)` or an f-string fed into a markup-aware
  `console.print(f"[bold]{value}[/]")`) is the correct, minimal mitigation
  for rendering untrusted strings (player names, chat text) with the `rich`
  library — the bracket syntax is never parsed as markup when passed through
  the plain constructor or `.append()`. Reusable pattern for any `rich`-based
  CLI that renders user-supplied text.

## Unique Solutions

- Reconnection-after-socket-drop for an in-memory, single-process game server
  can be modeled cleanly as: (1) never clear a seat's session on disconnect,
  only flip a `connected: bool` flag; (2) treat a subsequent `join` with a
  matching case-insensitive name at a disconnected seat as a reconnect rather
  than a new-seat request; (3) expose one pure `snapshot_for(seat)` method on
  the state machine that both the normal per-turn request builder and the
  reconnect path reuse, so there is exactly one code path that assembles
  "this player's current view of the game" (coinche-cli's `Game.snapshot_for`
  / `Table.reconnect`).
