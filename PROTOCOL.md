# Coinche server protocol — writing a new client

This document describes everything a new client (web app, mobile app,
another CLI, a bot, ...) needs to implement to talk to the `coinche.server`
process. It does not depend on `coinche/client.py`'s `rich` UI code at all —
it is purely about the wire protocol and the client-side state machine you
need to drive it.

## 1. Transport

- The server is a plain **WebSocket** server (RFC 6455), started with
  `python -m coinche.server [--host HOST] [--port PORT]` (default
  `0.0.0.0:8765`).
- Connect to `ws://HOST:PORT` (no path, no sub-protocol, no auth/handshake
  headers required). Use `wss://` instead if you put a TLS-terminating
  reverse proxy in front of it — the server itself only speaks plain `ws://`.
- Every logical message is exactly **one WebSocket text frame** (one
  `send()` == one `recv()`). There is no extra length-prefix or newline
  framing: WebSocket's own frame boundaries are the message boundaries.
- The server enforces a **64 KiB max message size**. Sending a bigger frame
  gets the connection closed by the library itself (close code `1009`,
  "message too big") before your message ever reaches the game logic — you
  won't get an `error` message back for that, just a closed connection.
- One WebSocket connection == one seat at one table. There is no
  multiplexing of several tables/seats over a single connection.

## 2. Message envelope

Every frame, in both directions, is UTF-8 JSON with exactly this shape:

```json
{"type": "<message_type>", "payload": { ... }}
```

- `type` is always a string from the tables below.
- `payload` is always a JSON object (possibly empty, `{}`), never omitted.
- Any frame that isn't valid JSON, isn't a JSON object, has an unknown/missing
  `type`, or has a non-object `payload` is rejected: the server replies with
  an `error` message (`code: "MALFORMED_MESSAGE"`) and otherwise ignores it —
  the connection is **not** closed for this.

## 3. Connection lifecycle

```
connect ──▶ send "join" (must be the first message) ──▶ recv "joined" or "error"
                                                              │
                                        (if "joined") ────────┘
                                              │
                                   wait in lobby until 4 seats filled
                                              │
                                     "deal" + "bid_request"/"resync" ...
                                              │
                                        normal game messages
                                              │
                                     connection drops / you call close()
```

1. **The very first message you send must be `join`.** Anything else as the
   first message gets a `MALFORMED_MESSAGE` error and the connection is
   dropped (no further messages are read).
2. If `join` succeeds, you get back either:
   - a `joined` message (fresh seat at the table), or
   - a `resync` message (you reconnected to a seat that was mid-game and
     marked disconnected — see §6).
3. From then on it's a normal duplex exchange: the server pushes state-change
   broadcasts, and you send `bid` / `play_card` / `chat` / `rematch` whenever
   it's relevant. There's no polling; everything is push-based.
4. If your connection drops (network issue, tab closed, etc.) mid-game, the
   server marks your seat `disconnected` and broadcasts `connection_status`
   to the other three; the game keeps waiting for that seat's turn. You (or
   anyone) can reconnect later — see §6.

## 4. Client → server messages

| `type`     | Required payload fields                              | Notes |
|------------|-------------------------------------------------------|-------|
| `join`     | `table_key: str`, `player_name: str`                  | Optional `team_name: str`. Must be the first message on a fresh connection. |
| `bid`      | `action: "pass" \| "bid" \| "coinche" \| "surcoinche"` | `action: "bid"` also requires `trump` and `points` (see §7.2). |
| `play_card`| `card: str`                                            | Card in wire format, e.g. `"10♥"`, `"V♠"` (see §7.1). |
| `chat`     | `text: str`                                            | Free-text; server broadcasts it back to everyone verbatim (see §5, `chat`). |
| `rematch`  | *(none)*                                               | Only meaningful once `game_over` was received; otherwise silently ignored. |

Details:

- **`join`** — `table_key` must match `^[A-Za-z0-9]{4,12}$` (case-insensitive;
  the server lower-cases it). The first client to use a given `table_key`
  creates that table; the next up-to-3 clients using the same key join it.
  `player_name` must be non-empty after trimming whitespace, and must be
  unique (case-insensitive) among the players **currently connected** at
  that table. Optional `team_name`: a free-text label (e.g. `"A"`/`"B"`)
  used as a best-effort hint to seat two same-`team_name` players opposite
  each other (N/S or E/W) instead of in arrival order; if that seat is
  already taken it just falls back to normal ordering.
- **`bid`** — only accepted while `phase == "bidding"` and only from the
  seat whose turn it is (`bid_request` tells you who and what's legal —
  don't just guess; see §7.2). Sending it out of turn or with an action not
  currently legal gets an `error` back (`NOT_YOUR_TURN` / `ILLEGAL_BID`),
  not a connection close.
- **`play_card`** — only accepted while `phase == "trick_play"` and only
  from the seat whose turn it is. `card` must be a valid `"<rank><suit>"`
  string from your current `legal_cards` (see §7.1/§7.3). Errors:
  `NOT_YOUR_TURN` / `ILLEGAL_CARD`.
- **`chat`** — accepted at any time (lobby or mid-game), from any connected
  seat; broadcast to all 4 seats (including the sender) as a `chat` message
  with the sender's `seat` attached.
- **`rematch`** — once `game_over` has been broadcast, any seated player can
  send this to restart the table at 0-0 with a fresh deal (same 4 players,
  same seats). Duplicate/late `rematch` messages (e.g. two players clicking
  "play again") are silently ignored once the new game has already started.

Messages sent while `table.game is None` (i.e. before all 4 seats are filled)
other than `join`/`chat` are simply ignored by the server (no error, no
effect) — there's no game to apply them to yet.

## 5. Server → client messages

| `type`               | When | Payload |
|----------------------|------|---------|
| `joined`              | Reply to a successful `join` (new seat) | `table_key, seat, players[], target_score, server_version` |
| `lobby_update`         | Broadcast to the *other* seats whenever someone joins/leaves before the game starts | `players[], seats_filled, waiting_for` |
| `resync`               | Reply to a successful `join` that matched a disconnected seat (reconnect) | Full game-state snapshot — see §6 |
| `connection_status`    | Broadcast when a seated player disconnects or reconnects mid-game | `seat, name, status: "disconnected" \| "reconnected"` |
| `deal`                 | Sent to each of the 4 seats individually, once per round, right after dealing | `hand[], dealer_seat, first_bidder_seat, round_number` (each player only sees their own `hand`) |
| `bid_request`          | Sent to the seat whose turn it is to bid | `current_highest_bid, legal_actions[], can_coinche, can_surcoinche` |
| `bid_update`           | Broadcast after every bid/pass/coinche/surcoinche | `seat, action, trump, points, next_to_act` |
| `bidding_result`       | Broadcast when the auction ends | `outcome: "redeal" \| "contract"` + fields below |
| `play_request`         | Sent to the seat whose turn it is to play a card | `legal_cards[], current_trick[], trump` |
| `card_played`          | Broadcast after every card play | `seat, card, current_trick[], next_to_act, belote_announcement` |
| `trick_result`         | Broadcast once a trick's 4th card lands | `winner_seat, trick[], points_won, tricks_played, tricks_remaining` |
| `trick_cleared`        | Broadcast a short pause after `trick_result`, telling clients to clear the table | `{}` (empty) |
| `round_score`          | Broadcast at the end of a round (8 tricks played) | `team_NS, team_EW, cumulative, next_dealer_seat` |
| `game_over`            | Broadcast once a team reaches the target score | `final_scores, winning_team` |
| `new_game`             | Broadcast after a `rematch` restarts the table | `target_score` |
| `chat`                 | Broadcast for every `chat` message (including the sender's own) | `seat, text` |
| `error`                | Sent to a single client when one of its messages is rejected | `code, message` (see §8) |

Notes on delivery:

- Messages marked "sent to each seat individually" (`deal`) or "sent to the
  seat whose turn it is" (`bid_request`, `play_request`) are **not**
  broadcast to everyone — only that one connection receives that exact frame.
  Every other message type in the table above is broadcast to **all 4**
  currently-connected seats at the table (an `exclude=seat` broadcast for
  `lobby_update`/`connection_status` just skips echoing it back to the
  player who caused the event, since they already know).
- There's no client-facing "server is thinking" delay for the pauses baked
  into `trick_cleared` (~2.5s after `trick_result`, configurable via
  `--trick-pause`) and before the next round's `deal` (~4s after
  `round_score`, configurable via `--round-pause`) — the server just sleeps
  before sending; your client doesn't need to do anything special, just
  render each message as it arrives.

## 6. Reconnection (`resync`)

If a player's connection drops while `table.game is not None` (mid-game),
the server keeps their seat reserved and marks it disconnected instead of
freeing it. To reconnect, simply open a **new** WebSocket connection and
send the **same** `join` message (`table_key` + `player_name`, matched
case-insensitively) — the server detects this matches a disconnected seat
and treats it as a reconnect instead of a fresh join:

- You get a `resync` message instead of `joined`. Its payload is a full
  state snapshot so you can rebuild your UI from scratch without having
  seen any of the messages that happened while you were gone:

  ```json
  {
    "table_key": "...",
    "seat": "N",
    "players": [{"seat": "N", "name": "...", "team_name": null}, ...],
    "hand": ["10♥", "V♠", ...],
    "phase": "bidding" | "trick_play",
    "current_highest_bid": {"team": "NS", "seat": "E", "trump": "♥", "points": 90} | null,
    "bid_history": [{"seat": "N", "action": "bid", "trump": "♥", "points": 80}, ...],
    "current_trick": [{"seat": "N", "card": "10♥"}, ...],
    "trump": "♥" | null,
    "whose_turn": "S",
    "cumulative_scores": {"NS": 120, "EW": 340},
    "round_number": 3,
    "dealer_seat": "W",
    "server_version": "..."
  }
  ```

- `resync` intentionally does **not** include `legal_actions`/`legal_cards`
  (those are only meaningful in the context of a live turn prompt). If it
  happens to be your turn right when you reconnect, the server follows up
  the `resync` with a normal `bid_request` or `play_request` right after, so
  just handle those the same way you always do.
- The other 3 seats get a `connection_status` broadcast
  (`status: "disconnected"` / `"reconnected"`) so their UIs can show/hide a
  "player X disconnected" indicator; the game itself doesn't pause or skip
  turns for a disconnected seat, it just waits — actions for that seat can't
  come from anywhere until it reconnects.
- Reconnecting **before** the game has started (still in the lobby) isn't a
  thing: if you disconnect during the lobby your seat is simply freed
  (`table.remove_player`) and a new `join` with that name is just a normal
  fresh join, not a reconnect.

## 7. Wire formats for game data

### 7.1 Cards

A card is the string `"<rank><suit>"`, e.g. `"10♥"`, `"V♠"`, `"D♦"`, `"R♣"`,
`"A♠"`, `"7♣"`. Note `"10"` is the only two-character rank; suits are the
literal Unicode glyphs below (not letters like `"H"`/`"S"`).

- **Ranks** (low → high in non-trump order): `7 8 9 10 V D R A`
  (`V` = Valet/Jack, `D` = Dame/Queen, `R` = Roi/King, `A` = As/Ace).
  Trump rank order is different (belote rules): `7 8 9 D R A 10 V`, jack
  and 9 of trump are the two highest cards — but you don't need to implement
  this yourself, the server always tells you which cards are legal via
  `legal_cards` in `play_request`/`bid_request`, and resolves trick winners
  itself.
- **Suits**: `♠` (spade), `♥` (heart), `♦` (diamond), `♣` (club).
- A trick entry (in `current_trick`, `trick`, etc.) is
  `{"seat": "N", "card": "10♥"}`.

### 7.2 Bids

A bid action, sent as `bid`'s payload:

```json
{"action": "bid", "trump": "♥", "points": 90}
```

or for capot:

```json
{"action": "bid", "trump": "♠", "points": "capot"}
```

- `action` is one of `"pass"`, `"bid"`, `"coinche"`, `"surcoinche"`.
- `trump` must be one of the 4 suit glyphs above (only present/required when
  `action == "bid"`).
- `points` is either an integer multiple of 10 between 80 and 180 inclusive,
  or the literal string `"capot"` (only present/required when
  `action == "bid"`). Capot always outranks any numeric bid, and only one
  capot bid is allowed per auction.
- `bid_request`'s `legal_actions` is the exhaustive list of currently-legal
  `{"trump", "points"}` combinations for a plain `bid` action (empty once
  someone has bid capot). It does **not** include `"pass"`,
  `"coinche"`/`"surcoinche"` as entries — those are separately signalled by
  `can_coinche`/`can_surcoinche` booleans (`"pass"` is always legal on your
  turn during bidding, so it's not itemized either). Concretely, to build a
  bid menu: always offer "pass"; offer "coinche" iff `can_coinche`; offer
  "surcoinche" iff `can_surcoinche`; and offer one `{"action": "bid", ...}`
  entry for every item in `legal_actions`.
- **You do not need to reimplement bid-legality rules client-side.** Only
  ever construct a `bid` message using one of the exact
  `{trump, points}` pairs handed to you in the latest `legal_actions` (or
  `"pass"`/`"coinche"`/`"surcoinche"` per the booleans) — the server
  re-validates everything anyway and returns `ILLEGAL_BID` if you don't.

### 7.3 Playing a card

`play_request`'s `legal_cards` is the exhaustive list of cards (in wire
format) you're allowed to play right now, already filtered for follow-suit /
must-trump / must-overtrump / "pisser" exceptions — you only need to render
these as the playable options and send back whichever one the user picked:

```json
{"type": "play_card", "payload": {"card": "10♥"}}
```

### 7.4 Seats and teams

- Seats are the 4 letters `"N"`, `"E"`, `"S"`, `"W"`. Turn order (for both
  bidding and trick play) rotates **counter-clockwise**: `N → W → S → E → N`.
- Teams: `N`+`S` are team `"NS"`; `E`+`W` are team `"EW"`. `TEAM_OF` is
  fixed by seat, not configurable.
- A client only ever sees its own `hand`; the other 3 players' hands are
  never sent over the wire (card counts aren't sent either — track it
  yourself from `card_played` events if you want to show "N has 3 cards
  left" type UI).

### 7.5 `bidding_result` payload shapes

`outcome` determines which other fields are present:

- `"redeal"` (all 4 players passed with no bid at all): `dealer_seat`. A
  fresh `deal` for the next dealer follows immediately.
- `"contract"` (three consecutive passes after at least one bid):
  `attacking_team`, `seat` (who won the auction), `trump`, `points`,
  `coinche_level` (`1` = no coinche, `2` = coinched, `4` = surcoinched),
  `first_leader` (who leads the first trick). A `play_request` to
  `first_leader` follows immediately.

### 7.6 `belote_announcement`

`card_played`'s `belote_announcement` is `"belote"`, `"rebelote"`, or `null`.
The server auto-detects which team holds both King+Queen of trump at the
start of the round; when that team's holder plays the King or Queen of
trump during the round (in either order), the *first* one played is
announced `"belote"` and the *second* one `"rebelote"` — this is purely
informational (the point bonus is applied automatically server-side; you
don't need to do anything with it beyond optionally displaying an
announcement banner).

## 8. Error codes

`error` messages (`{"type": "error", "payload": {"code": ..., "message": ...}}`)
are sent to a single connection, never broadcast, and never close the
connection by themselves:

| `code`               | Meaning |
|----------------------|---------|
| `MALFORMED_MESSAGE`   | Bad JSON / wrong envelope / missing required fields / bad `table_key` format / empty `player_name` / first message wasn't `join`. |
| `NAME_TAKEN`          | `join`'s `player_name` (case-insensitive) is already connected at that table. |
| `GAME_IN_PROGRESS`    | `join` for a table whose game already started, from a name that doesn't match any disconnected seat. |
| `TABLE_FULL`          | `join` for a table that already has 4 connected seats. |
| `NOT_YOUR_TURN`       | `bid`/`play_card` sent when it isn't that seat's turn. |
| `ILLEGAL_BID`         | `bid` payload doesn't match a currently-legal action (see §7.2). |
| `ILLEGAL_CARD`        | `play_card`'s card isn't in the current `legal_cards`, or isn't a well-formed card string. |

`MALFORMED_MESSAGE`/`NAME_TAKEN`/`GAME_IN_PROGRESS`/`TABLE_FULL` occur only
during the `join` handshake, before you've received `joined`/`resync` — in
all four cases the server does not proceed to game messages afterwards
(reconnecting requires a brand-new WebSocket connection and a fresh `join`).
`NOT_YOUR_TURN`/`ILLEGAL_BID`/`ILLEGAL_CARD` can happen at any point during
an active game and are simply "try again" signals; the connection and game
state are unaffected.

## 9. Minimal client checklist

To implement a new client from scratch you need to:

1. Open a WebSocket to `ws://host:port`.
2. Prompt for/collect `table_key` and `player_name` (+ optional `team_name`),
   send `join`, and handle the three possible replies: `joined`, `resync`,
   or `error`.
3. Render a lobby view driven by `lobby_update` until the game starts
   (first `deal` arrives).
4. Maintain client-side state for: your `hand`, `phase`, `trump`,
   `current_trick`, `cumulative_scores`, `whose_turn`/`next_to_act`, and the
   4 players' names/seats — all of this is handed to you incrementally via
   the broadcasts in §5 (or all at once via `resync` after a reconnect).
5. When you receive `bid_request` (it's your turn), render the choices
   derived from `legal_actions`/`can_coinche`/`can_surcoinche` and send back
   one `bid` message.
6. When you receive `play_request` (it's your turn), render `legal_cards`
   and send back one `play_card` message.
7. Handle `error` by showing the message and letting the user retry (don't
   treat it as fatal / don't close the connection).
8. Handle `game_over` by showing final scores and offering a `rematch`
   button (only meaningful post-`game_over`).
9. Handle connection drops: if you want reconnect support, remember your own
   `table_key`/`player_name` and simply re-`join` with the same values on a
   fresh WebSocket connection after a disconnect — the server does the rest.

You do **not** need to reimplement any Coinche rules (legal bids, legal
cards, trick-winner determination, scoring) — the server is fully
authoritative and always tells you what's legal via `bid_request`'s
`legal_actions`/`can_coinche`/`can_surcoinche` and `play_request`'s
`legal_cards`. A minimal client can just render whatever menu those imply
and forward the user's choice back verbatim.

For a fully worked reference implementation of this whole flow (in Python,
using `websockets` + `rich` for the terminal UI), see `coinche/client.py`.
