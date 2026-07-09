# coinche-cli

A networked, terminal-based Coinche (belote coinchée) card game: an asyncio
WebSocket server hosting multiple 4-player tables, and a `rich`-based CLI client
that joins a table by host/port, table key, and player name, then plays a
full game (deal → bid → trick play → score → repeat until the target score
is reached).

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(Python 3.10+ is required; the codebase uses `from __future__ import annotations`
and modern type-hint syntax.)

## Start the server

```bash
python -m coinche.server [--host HOST] [--port PORT] [--target-score N]
```

- `--host` — address to bind (default `0.0.0.0`)
- `--port` — port to listen on (default `8765`)
- `--target-score` — cumulative points needed to win the game (default `1000`)

Example:

```bash
python -m coinche.server --port 8765 --target-score 1000
```

## Start a client

```bash
python -m coinche.client [--host HOST] [--port PORT] [--table KEY] [--name NAME]
```

- `--host` — server address (defaults to an interactive prompt, `127.0.0.1`)
- `--port` — server port (defaults to an interactive prompt, `8765`)
- `--table` — table key: 4–12 alphanumeric characters, shared by all 4 players
  at the same table; the first player to use a key creates that table
  (defaults to an interactive prompt)
- `--name` — player name, must be unique among currently-connected players at
  that table (defaults to an interactive prompt)

Any flag you omit falls back to an interactive prompt at startup.

Alternatively, `./run_client.sh` creates the `.venv` if it doesn't exist,
activates it, installs/updates `requirements.txt` when needed, then launches
the client — passing through any arguments you give it:

```bash
./run_client.sh --host 127.0.0.1 --port 8765 --table demo1 --name Alice
```

To play a full 4-player game, start the server once, then run the client
4 times (in 4 terminals, or on 4 machines that can reach the server), giving
each a distinct `--name` and the same `--table` key:

```bash
python -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Alice
python -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Bob
python -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Carol
python -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Dave
```

(or `./run_client.sh --table demo1 --name Alice`, etc.)

Once all 4 seats are filled, the server deals a hand and the game begins.
If a client's connection drops mid-game, relaunching it with the same
`--table` and `--name` reconnects to the same seat and resumes play.

## Running the tests

```bash
python -m pytest
```

## Writing a new client

Want to build a different client (a web app, a bot, another CLI)? See
[`PROTOCOL.md`](PROTOCOL.md) for the full WebSocket message protocol — every
message type, payload shape, error code, and the join/reconnect flow — so
you can implement one without reading `coinche/client.py`'s `rich`-based UI
code at all.
