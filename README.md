# coinche-cli

A networked, terminal-based Coinche (belote coinchée) card game: an asyncio
TCP server hosting multiple 4-player tables, and a `rich`-based CLI client
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
                         [--team TEAM] [--web-port PORT]
```

- `--host` — server address (defaults to an interactive prompt, `127.0.0.1`)
- `--port` — server port (defaults to an interactive prompt, `8765`)
- `--name` — player name, must be unique among currently-connected players at
  that table (defaults to an interactive prompt)
- `--table` — table key: 4–12 alphanumeric characters; skips the interactive
  table picker (useful for scripting)
- `--team` — team label (`Equipe 1` or `Equipe 2`); skips the interactive
  team picker
- `--web-port` — port for the optional **web overlay** (default `0` = pick a
  free port automatically)

## Web overlay

Each client also runs a small in-process web server that mirrors your seat's
view of the game to a browser. On start it prints the reachable URL(s), e.g.:

```
Interface web disponible : http://127.0.0.1:52341
Interface web disponible : http://192.168.1.20:52341
```

Open either URL in a browser to follow the game; pin the port with
`--web-port 8080` if you want a stable address. The current page is a
placeholder (the full UI ships in a later unit), but the state feed is live.

Caveats:

- The overlay binds `0.0.0.0` and is **unauthenticated** — anyone who can
  reach that port on your LAN can view (and, once the UI lands, drive) your
  seat. Only expose it on trusted networks.
- The page only ever shows **your own seat**: your hand, the table, the score.
  It never receives another player's private cards — the bridge pushes only
  the same information your terminal already sees.

When `--table` and `--team` are omitted, the client opens a live-updating
two-step lobby screen (`rich`-based, alternate buffer, arrow-key + Enter
navigation) that subscribes to real-time table updates from the server
(`SUBSCRIBE_LOBBY`).  **Step 1** — browse existing tables (locked when
in-progress or full) plus **Nouvelle table** at the top, then
press Enter to select one.  **Step 2** — pick Equipe 1 or Equipe 2 (with
live member lists), then Enter to join; Esc returns to step 1.  When
another player creates a table or joins one, the list and team rosters
update automatically.

Alternatively, `./run_client.sh` creates the `.venv` if it doesn't exist,
activates it, installs/updates `requirements.txt` when needed, then launches
the client — passing through any arguments you give it:

```bash
./run_client.sh --host 127.0.0.1 --port 8765
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

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the setup, the lint/format/test
loop, and the pull-request expectations. Agents should also read
[`AGENTS.md`](AGENTS.md).
