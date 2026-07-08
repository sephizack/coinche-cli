---
status: "completed"
title: "Coinche Network Game"
created: 2026-07-08
updated: 2026-07-08
---

# Status

Exploration phase completed by Tony Explorer. See `context-map.md` for the
authoritative context (existing codebase conventions, environment, Coinche
rules reference, architecture considerations) and `discoveries.md` for
deferred/adjacent observations. No game or server code has been written yet;
this folder is exploration-only output for the Planner.

## Status Log

- **2026-07-08**: Plan approved by user; starting implementation.
- **2026-07-08**: Implementation completed across two Tony Builder runs.
  **Files created** (run 1, per plan.md steps 1-9): `coinche/__init__.py`,
  `coinche/cards.py`, `coinche/rules.py`, `coinche/game.py`,
  `coinche/protocol.py`, `coinche/table.py`, `coinche/server.py`,
  `coinche/ui.py`, `coinche/client.py`, `pyproject.toml`,
  `tests/test_cards.py`, `tests/test_rules.py`, `tests/test_game.py`,
  `tests/test_table.py`. **Files created/modified in this run** (plan.md
  steps 2-4): confirmed `requirements.txt` already had `pytest>=8.0` (no
  change needed); created `tests/test_integration.py` (real
  `asyncio.start_server` + 4 scripted socket clients driving one full round
  end-to-end, plus a disconnect/reconnect-mid-round scenario per A16);
  replaced the placeholder `README.md` with install/run instructions
  matching the actual `argparse` flags in `coinche/server.py`
  (`--host/--port/--target-score`) and `coinche/client.py`
  (`--host/--port/--table/--name`).
  **Bug found and fixed while writing the integration test**: `coinche/server.py`'s
  `_send_bid_request` forwarded a raw `Seat` enum inside `current_highest_bid`
  to `protocol.encode`/`json.dumps`, crashing the connection handler
  (`TypeError: Object of type Seat is not JSON serializable`) whenever a
  `bid_request` was sent after any bid had been made (i.e. for the 2nd+
  bidder in any auction). Fixed with a shared `_bid_to_wire` helper reused by
  both `_send_bid_request` and `_snapshot_to_wire`; logged as DISC-008
  (Resolved) in `discoveries.md`.
  **Final `pytest` results**: 49 passed, 0 failed (5 test files: `test_cards.py`,
  `test_rules.py`, `test_game.py`, `test_table.py`, `test_integration.py`).
  **`get_errors`** across `coinche/` and `tests/`: no errors found.
  Status remains `"ongoing"` — QA/review pending.

- **2026-07-08 (Tony Tester QA pass)**: Independently re-ran the full suite
  (confirmed Builder's 49/49 claim) and reviewed every A1-A17 assumption in
  `plan.md` against test coverage, `rules.py`/`game.py`/`table.py`/`server.py`
  (server-authoritative validation), and `ui.py` (rich-markup-injection
  mitigation, live-redraw, numbered menus). Found and fixed **2 real bugs**
  in `coinche/server.py` (both crash bugs in wire-level input handling, not
  caught by the existing 49 tests because nothing exercised the server's
  error-translation paths at the wire level): (1) an empty `card` string in
  a `play_card` message crashed the connection handler with an unhandled
  `IndexError` instead of a clean `ILLEGAL_CARD` error; (2) a line exceeding
  asyncio's default 64 KiB `StreamReader` limit crashed the connection
  handler with an unhandled `ValueError` instead of the `MALFORMED_MESSAGE`
  rejection the plan's Security section describes. Both fixed with minimal,
  targeted guards; logged as **DISC-009 (Resolved)** with regression tests.
  Added **9 new integration tests** to `tests/test_integration.py`: illegal
  card play, out-of-turn bid, out-of-turn card play, the two crash-bug
  regressions above, malformed JSON line, 5th-player-join-rejected, duplicate
  name join (wire-level), and disconnect+reconnect during the **bidding**
  phase (previously only trick-play disconnect/reconnect was covered).
  Also found that `coinche/ui.py` (and `coinche/client.py`) had **0%**
  automated test coverage — added `tests/test_ui.py` (14 tests) covering the
  rich-markup-injection mitigation, numbered bid/play menus, and seat-rotation
  layout logic; `ui.py` coverage rose 0% → 90%. `client.py` remains at 0%
  (consistent with plan.md's manual-smoke-test-only design for that layer;
  logged as the open half of **DISC-010**).
  **Final independent `pytest` results**: **72 passed, 0 failed**, exit code
  `0`, confirmed stable across 4 consecutive runs (no flake observed).
  Statement coverage (`pytest-cov`, installed locally for this QA pass):
  **72% overall** (`cards.py` 100%, `rules.py` 96%, `table.py` 97%,
  `game.py` 92%, `ui.py` 90%, `protocol.py` 86%, `server.py` 81%,
  `client.py` 0%). `get_errors` across `coinche/` and `tests/`: no errors
  found. **Verdict: implementation is solid** — every A1-A17 assumption has
  direct test coverage or was confirmed correct by code reading; the two
  bugs found were edge-case crash bugs in error-handling paths, not core
  rules/scoring/reconnection logic defects, and both are now fixed and
  regression-tested. Status remains `"ongoing"` pending any user follow-up
  on the remaining open discoveries (DISC-001/002/004/005/007 unchanged,
  DISC-010's client.py coverage gap).

- **2026-07-08 (Tony Reviewer — final QA audit)**: Verdict **✅ PASS**.

  Independently re-ran the full suite (`python -m pytest -q`): **72 passed,
  0 failed**. `get_errors` across `coinche/` and `tests/`: no errors found.
  Re-ran with `--cov=coinche --cov-report=term-missing`: coverage numbers
  match the prior QA pass exactly (`cards.py` 100%, `rules.py` 96%,
  `table.py` 97%, `game.py` 92%, `ui.py` 90%, `protocol.py` 86%,
  `server.py` 81%, `client.py` 0%, **72% overall**) — no regression, no flake.

  **Plan vs. Implementation Matrix** (every row spot-checked directly against
  source, not just against prior agents' summaries):

  | Requirement | File(s) | Status |
  |---|---|---|
  | A1 Rotation N→W→S→E | [coinche/cards.py](../../coinche/cards.py) `Seat.next()` | ✅ |
  | A2 Partnerships N+S / E+W | [coinche/game.py](../../coinche/game.py) `TEAM_OF` | ✅ |
  | A3 Deal 3-2-3 packet split | [coinche/cards.py](../../coinche/cards.py) `deal()` | ✅ |
  | A4 First bidder = dealer.next() | [coinche/game.py](../../coinche/game.py) `start_round()` | ✅ |
  | A5 Bid range 80-180/step 10 + Capot | [coinche/rules.py](../../coinche/rules.py) `BID_MIN/MAX/STEP`, `legal_bid_actions` | ✅ |
  | A6 Strict-raise, Capot outranks, single Capot | [coinche/rules.py](../../coinche/rules.py) `is_valid_bid`, `_bid_rank` | ✅ (regression-tested: equal/lower rank rejected, second Capot rejected) |
  | A7 Trump ladder all suits under tout_atout; no sans_atout | [coinche/rules.py](../../coinche/rules.py) `TRUMP_POINTS`/`card_points`; `sans_atout` absent from `ALLOWED_TRUMPS` everywhere (rules/protocol/tests) | ✅ |
  | A8 Capot bonus 250 / 0 | [coinche/rules.py](../../coinche/rules.py) `score_round` capot branch | ✅ (success/failure both tested; coinched-capot combo untested — DISC-012, non-blocking) |
  | A9 Coinche ×2 / Surcoinche ×4, bidding-team-only surcoinche | [coinche/rules.py](../../coinche/rules.py) `score_round`; [coinche/game.py](../../coinche/game.py) `submit_bid` coinche/surcoinche eligibility checks | ✅ |
  | A10 Failed contract → defenders get full pool (162/258) | [coinche/rules.py](../../coinche/rules.py) `score_round` failed/capot_failed branches | ✅ |
  | A11 Belote/Rebelote auto-credit +20 | [coinche/game.py](../../coinche/game.py) `_detect_belote` (uses `dealt_hands`, correctly guarantees both cards are eventually played) | ✅ for `normal` declarations; not evaluated under `tout_atout` — DISC-013, non-blocking clarification candidate |
  | A12 Target score, configurable | [coinche/game.py](../../coinche/game.py) `Game(target_score=...)`; [coinche/server.py](../../coinche/server.py) `--target-score` | ✅ |
  | A13 Tie/simultaneous target-score resolution | [coinche/game.py](../../coinche/game.py) `_finish_round` | ✅ (sudden-death-then-resolve scenario passes) |
  | A14 Table key host-chosen, lazy, 4-12 alnum, full/in-progress rejection | [coinche/table.py](../../coinche/table.py) `add_player`, `get_or_create_table`; [coinche/server.py](../../coinche/server.py) `TABLE_KEY_PATTERN` | ✅ |
  | A15 Name-collision vs. reconnection disambiguation | [coinche/table.py](../../coinche/table.py) `add_player` (NameTakenError only checks `connected` sessions), `find_disconnected_seat` | ✅ |
  | A16 Full reconnection flow (disconnect→pause→resync→resume) | [coinche/table.py](../../coinche/table.py) `mark_disconnected`/`reconnect`; [coinche/server.py](../../coinche/server.py) `_resolve_join`/`handle_connection` finally-block; [coinche/game.py](../../coinche/game.py) `snapshot_for` | ✅ — verified end-to-end via real-socket integration tests for **both** bidding-phase and trick-play-phase disconnects (`test_disconnect_and_reconnect_mid_round`, `test_disconnect_and_reconnect_during_bidding_phase`): game pauses (no silent advance), `connection_status` broadcasts correctly, `resync` payload matches actual state, play resumes correctly post-reconnect |
  | A17 Chat passthrough | [coinche/protocol.py](../../coinche/protocol.py) `CHAT`; [coinche/table.py](../../coinche/table.py)/[coinche/server.py](../../coinche/server.py) dispatch; [coinche/client.py](../../coinche/client.py) rendering | ✅ |
  | Trick-taking legality (follow-suit→must-trump→must-overtrump→under-trump partner exception) | [coinche/rules.py](../../coinche/rules.py) `legal_cards_to_play`/`_apply_overtrump_rule` | ✅ — traced all 4 branches by hand against test cases; logic is correct and shared identically between the "led-suit-is-trump" and "forced-to-trump" code paths |
  | Protocol message list (client→server and server→client) | [coinche/protocol.py](../../coinche/protocol.py) | ✅ every message type/field in plan.md's list is present; no extra/undocumented types |
  | Concurrency model (per-table `asyncio.Lock`, one task per connection, I/O-free `Game`) | [coinche/table.py](../../coinche/table.py), [coinche/server.py](../../coinche/server.py) | ✅ lock held consistently across join/reconnect/dispatch/disconnect; no re-entrant/double-acquire issues found |
  | CLI entry points match README | [coinche/server.py](../../coinche/server.py) `build_arg_parser`, [coinche/client.py](../../coinche/client.py) `build_arg_parser`, [README.md](../../README.md) | ✅ flags, defaults, and behavior described in README match the actual `argparse` definitions exactly |
  | Security/anti-cheat (server-authoritative validation, rich-markup-injection mitigation) | [coinche/server.py](../../coinche/server.py) `_dispatch` (independent re-validation of every `bid`/`play_card`); [coinche/ui.py](../../coinche/ui.py) (`Text(value)` plain-constructor/`.append()` used exclusively for untrusted strings, confirmed by reading every render function and by `tests/test_ui.py`'s injection-payload assertions) | ✅ |
  | No scope creep (no bots/spectator/persistence/CI) | workspace-wide search: no `.github/`, no bot/spectator/db/sqlite files found | ✅ |
  | DISC-008/DISC-009 regressions still fixed | [coinche/server.py](../../coinche/server.py) `_bid_to_wire`, empty-card guard, `except ValueError` on both `readline()` sites | ✅ confirmed present in source, regression tests still pass |

  **New non-blocking findings** (logged to `discoveries.md`, none block PASS):
  DISC-011 (`player_name` has no explicit max length — not a plan.md
  requirement, indirectly bounded by the 64 KiB line limit), DISC-012 (no
  dedicated test for a coinched/surcoinched Capot contract — code path is
  shared/consistent with the well-tested normal-contract multiplication
  logic, so this is a coverage gap, not a suspected defect), DISC-013
  (belote/rebelote is not evaluated under `tout_atout` declarations — a
  defensible interpretation since plan.md never disambiguates this case,
  logged as a clarification candidate rather than a bug).

  **Conclusion:** No plan.md requirement (A1-A17, protocol, concurrency, CLI,
  security) is missing, incorrectly implemented, or contradicted by the
  actual source. All prior discoveries (DISC-008, DISC-009) remain resolved
  and regression-tested. No scope creep occurred. README accurately
  documents the real CLI. Status updated to `"completed"`.


