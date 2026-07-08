# Discoveries - Coinche Network Game

This document records the analyzed deficiencies, benchmarks, and recommendations.

---

### [DISC-001]: No package/project scaffolding exists (pyproject.toml, src layout, tests)
- **Status:** Open
- **Evidence:** Workspace root only contains [demo_table.py](../../demo_table.py), [README.md](../../README.md), and [requirements.txt](../../requirements.txt); `list_dir` on the root shows no `pyproject.toml`, `setup.py`, `src/`, or `tests/` directories.
- **Reason for Deferral:** Out of scope for exploration; this is a scaffolding/tooling decision the Planner or implementer should make when structuring the server/client/game-engine modules, not something to decide during context gathering.
- **Smallest Sensible Next Step:** When implementation begins, add a minimal `pyproject.toml` (or a `src/coinche/` package layout) plus a `tests/` folder with `pytest` before writing significant game logic, so bidding/scoring rules can be unit-tested.

### [DISC-002]: No CI, linter, or formatter configuration present
- **Status:** Open
- **Evidence:** No `.github/workflows/`, `.ruff.toml`, `pyproject.toml` `[tool.*]` sections, or `.pre-commit-config.yaml` found anywhere in the workspace listing.
- **Reason for Deferral:** Tooling/process choice unrelated to the immediate feature's game/network logic; adding it now would expand scope beyond "deep exploration only."
- **Smallest Sensible Next Step:** Once a package layout exists, add a lightweight `ruff` + `pytest` GitHub Actions workflow.

### [DISC-003]: Reconnection / disconnect handling is unspecified
- **Status:** Open
- **Evidence:** Feature summary describes join-flow (IP + table key + name) and gameplay phases but says nothing about a player dropping mid-hand; [demo_table.py](../../demo_table.py) has no session/connection model at all to reference.
- **Reason for Deferral:** Requires a product decision (pause-and-wait vs timeout-forfeit vs AI-takeover) before it can be designed; flagged as an Open Question in context-map.md for the Planner rather than decided here.
- **Smallest Sensible Next Step:** Planner should decide a minimal policy (e.g. "pause game and wait indefinitely for reconnect using the same player name+table key") for the first implementation, with room to improve later.

### [DISC-004]: Potential future feature — AI/bot players to fill seats
- **Status:** Open
- **Evidence:** Feature summary requires exactly 4 human players per table with no mention of bots; the waiting-room-until-4-players flow implies games can't start with fewer.
- **Reason for Deferral:** Explicitly beyond the initial scope described by the user (networked 4-human-player game); would add significant AI/decision-engine complexity.
- **Smallest Sensible Next Step:** After the human-only flow is stable, consider a simple heuristic bot (e.g. random-legal-move) that can occupy an empty seat on request.

### [DISC-005]: Potential future feature — spectator/replay mode
- **Status:** Open
- **Evidence:** Current design only supports 4 active player connections per table (per feature summary); no notion of a read-only observer connection exists.
- **Reason for Deferral:** Not requested; adding spectator support would require a separate "public-state-only" client mode and broadcast channel, which is an enhancement beyond the initial 4-player game loop.
- **Smallest Sensible Next Step:** Once the public/private message-shape split (see context-map.md architecture section) exists, a spectator is "just" a client that only ever receives the public-state messages.

### [DISC-006]: House-rule variance in Coinche scoring/deal mechanics needs explicit configuration
- **Status:** Open
- **Evidence:** Documented in context-map.md's Coinche Rules Reference under multiple `[VARIANT]` tags: dealing packet split (3-2-3 vs alternatives), Sans-Atout/Tout-Atout point tables, Capot bonus amount, coinche/surcoinche multiplier values, and game-end target score all have known regional/club variance.
- **Reason for Deferral:** Choosing single canonical values is a rules/product decision for the Planner, not a fact to be discovered by exploration; documented as Open Questions instead of guessed at.
- **Smallest Sensible Next Step:** Planner should pick one canonical rule set for v1 (recommend documenting the chosen numbers directly in a `RULES.md` or config constants file) while leaving room to make them table-configurable later.

### [DISC-007]: No dependency exists yet for async-friendly terminal input on the client
- **Status:** Open
- **Evidence:** `.venv/lib/python3.14/site-packages/` contains only `rich`, `markdown_it_py`, `mdurl`, `pygments`, `pip` — no `textual`, `prompt_toolkit`, or similar async-input-capable library.
- **Reason for Deferral:** This is an architecture/library-choice decision (see Proposed Architecture Considerations in context-map.md), not something to resolve during exploration.
- **Smallest Sensible Next Step:** Prototype both a bare `asyncio.to_thread(input, ...)` approach and a `textual` app during implementation spike to compare ergonomics before committing.

### [DISC-008]: `_send_bid_request` sent an unserializable `Seat` enum in `current_highest_bid`, crashing the connection on the 2nd+ bidder's turn
- **Status:** Resolved
- **Evidence:** `coinche/server.py`'s `_send_bid_request` forwarded `Game.bid_options_for(seat)["current_highest_bid"]` (a dict containing a raw `Seat` enum under `"seat"`) directly into `protocol.encode(...)`/`json.dumps(...)`, raising `TypeError: Object of type Seat is not JSON serializable` and crashing the connection handler task any time a `bid_request` was sent to a seat while a bid was already on the table (i.e. the 2nd, 3rd, or 4th bidder in any auction with at least one non-pass bid). `_snapshot_to_wire` (used for `resync`) already converted this seat to a string, but the live `bid_request` path did not — a code-path inconsistency. Caught by `tests/test_integration.py::test_full_round_join_deal_bid_trick_score_flow`, which exercises a real multi-bidder auction end-to-end.
- **Fix Applied:** Added a shared `_bid_to_wire(bid)` helper in `coinche/server.py` that converts `bid["seat"]` to its wire string (or returns `None` unchanged), and reused it in both `_snapshot_to_wire` and `_send_bid_request`. Verified via `vscode_listCodeUsages` that these are the only two call sites constructing a wire-bound `current_highest_bid` payload.
- **Follow-up:** None — both known call sites are fixed and covered by passing tests. Flagging here per Discovery Discipline since this was found and fixed while completing `tests/test_integration.py`, not while executing a plan.md step that named this bug explicitly.

### [DISC-009]: Two unhandled-exception crash bugs found by Tony Tester (QA pass) in `coinche/server.py`'s wire-level input handling — both fixed
- **Status:** Resolved
- **Severity:** Medium (both are client-triggerable connection-handler crashes rather than clean protocol-level rejections; neither crashes the whole server process, but both violate the plan's explicit "Security & Anti-Cheat Model" promise that illegal/malformed input is always rejected cleanly with an `error` message rather than propagating as an unhandled exception).
- **Evidence / Reproduction (bug 1 — empty `card` string crashes the connection):**
  `coinche/server.py`'s `_dispatch` called `_wire_to_card(payload["card"])` *before* entering the `try/except` that catches `IllegalCardError`. `_wire_to_card` did `Card(rank=str(card_str)[:-1], suit=str(card_str)[-1])`; for `card_str = ""`, `""[-1]` raises an unhandled `IndexError` (confirmed interactively: `''[-1]` → `IndexError: string index out of range`). A scripted client sending `{"type": "play_card", "payload": {"card": ""}}` at any point during trick play crashed the connection-handler task with an unhandled `IndexError` instead of receiving a clean `ILLEGAL_CARD` error, and the `finally` cleanup in `handle_connection` would then mark that seat `disconnected` without any explanation ever reaching the client.
- **Evidence / Reproduction (bug 2 — oversized line crashes the connection):**
  `handle_connection`'s read loop (and `_resolve_join`'s initial `readline()`) only caught `(ConnectionError, asyncio.IncompleteReadError)` around `await reader.readline()`. Confirmed interactively with a standalone asyncio server/client pair that a line exceeding `StreamReader`'s default 64 KiB limit raises `ValueError: Separator is found, but chunk is longer than limit`, not `LimitOverrunError` or a caught type — this propagated as an unhandled exception out of the connection task instead of the clean `MALFORMED_MESSAGE` rejection plan.md's Security & Anti-Cheat Model describes ("relying on asyncio's default 64 KiB StreamReader line-length limit... as a built-in guard"). The guard existed structurally but its failure mode was never handled, so it wasn't actually functioning as a guard.
- **Fix Applied:**
  1. In `_dispatch`'s `PLAY_CARD` branch, added an explicit type/length guard (`not isinstance(card_str, str) or len(card_str) < 2`) before calling `_wire_to_card`, sending a clean `ILLEGAL_CARD` error and returning early instead of crashing (every real card string is at least 2 characters, e.g. `"7♠"`).
  2. Added `except ValueError:` alongside the existing `readline()` calls in both `_resolve_join` (initial join line) and `handle_connection`'s main read loop, sending a `MALFORMED_MESSAGE` error (where a writer is available) and cleanly returning/breaking instead of letting the exception propagate.
- **Regression tests added** (`tests/test_integration.py`): `test_empty_card_string_rejected_with_error_not_a_crash`, `test_oversized_line_is_rejected_gracefully_not_a_server_crash` — both assert a clean error response (or graceful close) and that the connection/server remains usable afterward.
- **Follow-up:** None required — both are now handled at the exact point they were previously unhandled, confirmed by regression tests and a clean `get_errors` pass. Found and fixed during the Tony Tester QA pass per the task's explicit ask to spot-check server-authoritative validation and malformed/oversized input handling; logged per Discovery Discipline as this was not an explicitly-named plan.md step.

### [DISC-010]: `coinche/client.py` and (before this QA pass) `coinche/ui.py` had zero automated test coverage
- **Status:** Partially Resolved (ui.py) / Open (client.py)
- **Evidence:** `pytest --cov=coinche` before this QA pass showed `coinche/ui.py` and `coinche/client.py` at 0% statement coverage — no test file imported either module. This is consistent with plan.md's step 10 (`tests/test_integration.py` explicitly drives scripted raw-socket clients "no `coinche.ui`/terminal involved") and step's Multi-Step Verification Plan item 6, which defers the client/UI layer entirely to a **manual** smoke test rather than automated coverage. However, the task's explicit ask #4 ("spot-check `coinche/ui.py` for the rich-markup-injection mitigation... and for live-redraw... and keyboard-shortcut/numbered-menu prompts") calls for verifiable, not just reviewed, behavior.
- **Action Taken:** Added `tests/test_ui.py` (new, 14 tests) unit-testing `ui.py`'s pure rendering functions in isolation: confirms player names/chat-adjacent strings containing rich markup syntax (e.g. `"[bold red]INJECTED[/bold red]"`) survive untouched in the rendered `Text.plain` output (proving they are never parsed as markup, per the module's stated security mitigation), the numbered bid/play menus map tokens to structured choices only (never raw card strings), and the table layout correctly rotates so the local seat always renders at "south" regardless of actual seat. `coinche/ui.py` coverage rose from 0% to 90%.
- **Remaining Gap:** `coinche/client.py` (the `asyncio` connection/reconnect-supervisor/live-redraw orchestration loop) remains at 0% automated coverage. Unit-testing it meaningfully would require mocking `asyncio.open_connection`, `rich.live.Live`, and `asyncio.to_thread(input, ...)` simultaneously — a materially larger effort than the rest of this QA pass's scope, and the plan explicitly designates this layer for manual smoke-testing only (Multi-Step Verification Plan, item 6). Recorded here as a candidate follow-up rather than attempted now, to avoid a rushed/low-value mock-heavy test that wouldn't meaningfully catch real client bugs.
- **Smallest Sensible Next Step:** If automated client-layer coverage is wanted later, refactor `_apply_message`'s pure state-transition logic (already a standalone function taking `ClientState`) into more granular testable units — it's already decoupled from I/O, so a `tests/test_client_state.py` covering `_apply_message` for every message type would be the highest-value, lowest-effort next increment (no live sockets/console needed).

### [DISC-011]: `player_name` has no explicit maximum length (unlike `table_key`'s 4-12 char regex)
- **Status:** Open (non-blocking)
- **Evidence:** `coinche/server.py`'s `_resolve_join` validates `table_key` against `TABLE_KEY_PATTERN` (4-12 alphanumeric) but only does `player_name = str(payload["player_name"]).strip()` followed by an empty-string check — no upper bound. Confirmed by reading `_resolve_join` end-to-end; plan.md's A14/A15 do not specify a name-length requirement, so this is not a missed plan requirement, just a robustness gap.
- **Reason for Deferral:** Not a plan.md requirement (A14 only specifies the length bound for `table_key`); indirectly bounded in practice by asyncio's default 64 KiB `StreamReader` line limit (the same mitigation plan.md cites for oversized input generally), so there is no unbounded-memory/DoS vector beyond what's already accepted for any single message.
- **Smallest Sensible Next Step:** If ever revisited, add a small explicit cap (e.g. 20-32 chars) on `player_name` in `_resolve_join` for cleaner UX (long names already break the fixed-width `ui.py` panels) rather than for security.

### [DISC-012]: No dedicated test for a coinched/surcoinched Capot contract (combination of A8 + A9)
- **Status:** Open (non-blocking)
- **Evidence:** `tests/test_rules.py` covers capot success/failure (`coinche_level=1` only) and coinche/surcoinche multipliers (on non-capot contracts only) as separate cases; no test exercises `score_round(..., bid={"points": CAPOT, ...}, coinche_level=2 or 4, ...)`. Read `coinche/rules.py::score_round`: the capot branch sets `attacking_before_mult = CAPOT_BONUS` (or `0`) and then falls through to the same `attacking_scored = attacking_before_mult * coinche_level` multiplication used by the normal-contract path — the logic is shared, not special-cased, so this is a test-coverage gap rather than a suspected defect.
- **Reason for Deferral:** plan.md's Multi-Step Verification Plan (step 2) lists "capot success/failure" and "coinche ×2/surcoinche ×4" as separate bullet items, not an explicit combined scenario; not a missed plan requirement.
- **Smallest Sensible Next Step:** Add `test_score_round_coinched_capot_achieved` / `..._failed` to `tests/test_rules.py` asserting `250 * 2 == 500` (and `* 4 == 1000`) to lock in the currently-correct-by-inspection behavior.

### [DISC-013]: Belote/Rebelote (A11) is only detected for `declaration == "normal"`; `tout_atout` rounds never credit a belote bonus
- **Status:** Open (non-blocking / clarification candidate)
- **Evidence:** `coinche/game.py::_finalize_contract` only calls `self._detect_belote(trump_suit)` inside `if declaration == "normal":`; for `tout_atout` contracts `belote_holder` stays at its `None` default. plan.md's A7/A11 do not explicitly state whether a belote (K+Q of a single suit) should count under `tout_atout` (where all 4 suits are simultaneously trump), so this is a reasonable, defensible interpretation rather than a bug, but it is an implicit product decision no one has explicitly signed off on.
- **Reason for Deferral:** Not contradicted by any stated assumption; resolving it either way is a rules clarification for the Planner/user, not something QA should decide unilaterally.
- **Smallest Sensible Next Step:** If the user wants belote recognized under `tout_atout` too (e.g. "any suit's K+Q held together" or restricted to one designated suit), add an explicit assumption (A7-bis) and extend `_detect_belote`/tests accordingly.

