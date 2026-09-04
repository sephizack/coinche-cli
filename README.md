# coinche-cli

A networked, terminal-based Coinche (belote coinchée) card game: an asyncio
TCP server hosting multiple 4-player tables, and a `rich`-based CLI client
that joins a table by host/port, table key, and player name, then plays a
full game (deal → bid → trick play → score → repeat until the target score
is reached).

## Install

```bash
brew install uv                 # macOS, une seule fois
uv sync --all-groups
```

Sur un NAS Synology ou une machine Linux sans Homebrew, installe `uv` une seule
fois avec l'installeur officiel, puis ouvre une nouvelle session SSH pour que
`uv` soit dans le `PATH` :

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
uv sync --all-groups
```

Si `uv` est installé ailleurs, indique son chemin au lanceur :

```bash
UV_BIN=/volume1/homes/<utilisateur>/.local/bin/uv ./run_app.sh --auth-pass 'change-me'
```

`uv` crée et gère `.venv` automatiquement. La version `3.14.6` est définie dans
`.python-version` et `uv.lock` fige toutes les dépendances. Sur une autre
machine, `uv` télécharge cette version de Python si elle n'est pas déjà présente.
Après une modification de `pyproject.toml`, exécute `uv lock`, puis
`uv sync --all-groups`. La CI et les lanceurs refusent un lock obsolète.

## Start the server

```bash
uv run -m coinche.server [--host HOST] [--port PORT] [--target-score N] [--turn-timeout SECONDS]
```

- `--host` — address to bind (default `0.0.0.0`)
- `--port` — port to listen on (default `8765`)
- `--target-score` — cumulative points needed to win the game (default `1000`,
  or `COINCHE_TARGET_SCORE` when set)
- `--turn-timeout` — default per-turn timeout for new tables (default `300`)
- `--bot-think` — minimum seconds each bot waits before announcing or playing,
  - `--bot-samples` — plausible hidden-hand distributions evaluated for each bot
    card decision (default: `100`); larger values improve consistency but make
    each bot turn slower
  plus up to one random extra second (default: `1.0`, therefore 1–2 seconds)

To notify Discord when a table is created, set
`DISCORD_NOTIF_CHANNEL_POST_URL` before starting the server:

```bash
export DISCORD_NOTIF_CHANNEL_POST_URL='https://discord.com/api/webhooks/...'
```

Set `COINCHE_PUBLIC_URL` as well to include three Discord link buttons in each
notification: **Avec** the table creator, **Contre** the table creator, or
**Regarder la partie** as a spectator. It must be the public URL of the
méta-client, for example `https://coinche.example.org`.

Set `COINCHE_TARGET_SCORE` to change the default target score for new tables;
`--target-score` overrides it. A table creator can also choose a positive
target score in the terminal with `--target-score` or in the web lobby's
**Options de table**. A table's score target is immutable after creation.

Example:

```bash
uv run -m coinche.server --port 8765 --target-score 1000
```

## Start a client

```bash
uv run -m coinche.client [--host HOST] [--port PORT] [--table KEY] [--name NAME]
                         [--team TEAM] [--score-mode MODE] [--target-score N] [--turn-timeout SECONDS] [--web-port PORT]
```

- `--host` — server address (defaults to an interactive prompt, `127.0.0.1`)
- `--port` — server port (defaults to an interactive prompt, `8765`)
- `--name` — player name, must be unique among currently-connected players at
  that table (defaults to an interactive prompt)
- `--table` — table key: 4–12 alphanumeric characters; skips the interactive
  table picker (useful for scripting)
- `--team` — team label (`Equipe 1` or `Equipe 2`); skips the interactive
  team picker
- `--score-mode` — scoring convention for a table you create: `points_made`,
  `points_announced`, or `points_made_plus_announced` (default)
- `--target-score` — cumulative score for a table you create (default: server
  setting)
  - `--turn-timeout` — per-turn timeout in seconds for a table you create
    (default: server setting)
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

In the Web lobby, **Options de table** opens the settings for a new table:
table name, target score, Discord notification, and the default bot type for
newly created bots, the scoring convention, and whether a Coinche/Contre
blocks later bids.
The scoring choices are **Points faits**, **Points annoncés**, and **Points
faits + annoncés** (the default); the blocking rule is enabled by default.
Each bot keeps its own type; click its `BOT <type>`
badge at the table to cycle through the available strategies. `Maestro` uses
the same information-safe card play as the bot
but announces one extra trick when it holds firm trump control. `Cloclo` is an
offensive AI opponent: with the Jack, 9, and two side aces it announces one
more trick than Maestro, then preserves points when its team is already winning
a trick. When disabled,
a later valid bid cancels the Coinche and returns the contract multiplier to
normal.

Alternatively, `./run_client.sh` synchronizes the locked `uv` environment then
launches the client — passing through any arguments you give it:

```bash
./run_client.sh --host 127.0.0.1 --port 8765
```

To play a full 4-player game, start the server once, then run the client
4 times (in 4 terminals, or on 4 machines that can reach the server), giving
each a distinct `--name` and the same `--table` key:

```bash
uv run -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Alice
uv run -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Bob
uv run -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Carol
uv run -m coinche.client --host 127.0.0.1 --port 8765 --table demo1 --name Dave
```

(or `./run_client.sh --table demo1 --name Alice`, etc.)

If fewer than 4 people are available, any seated player can press **F** in the
terminal (or click **Remplir avec des bots** in the web interface) while the
table is waiting. The server fills every free seat with a bot and starts the
game. Bots evaluate their hand before bidding. For card play, they simulate
Bots evaluate their hand before bidding. For card play, they simulate
multiple plausible distributions of the unseen cards, play each legal choice
to the end of the round, and select the best average result. The distributions
use their own cards plus the complete public auction and play history—not the
real hidden hands. Hosts choose the search depth with `--bot-samples`; use a
smaller value for quicker turns or a larger value for more stable decisions.
Their opening bids are deliberately conservative: trump control sets the base
contract, and only strong J-9 four-trump hands add points for side-suit aces.
They wait a random one to two seconds before each decision by default, so
their turns remain visible and do not feel instantaneous; hosts can adjust the
minimum with `--bot-think` (or use `0` to disable waiting).
At the end of each round, each bot posts its original eight-card hand in the
chat so its bidding decisions can be reviewed; human hands are never revealed.
All bot actions pass through the same server-side legality checks as human moves.

Once all 4 seats are filled, by people or bots, the server deals a hand and the game begins.
If a client's connection drops mid-game, relaunching it with the same
`--table` and `--name` reconnects to the same seat and resumes play.

A table that has bots can be joined mid-game: in the lobby it shows up as
"🤖 N bot(s) — remplaçable" (rather than locked), and selecting it lets you
pick which bot to replace. You take over that seat exactly as it stands — its
hand, its turn — and the bot steps aside. In the web interface, click a bot's
chip on a running table (or its "🤖 Remplacer un bot" button); from the command
line, `--table KEY --seat N|E|S|W` joins straight into a bot's chair.

## Méta-client (multi-session web front door)

For hosting several players from a **single process** — e.g. one small VM that
serves everyone through a browser, with no terminal client to install — run the
**méta-client**. It exposes one authenticated web page; each visitor picks a
name and gets their own dedicated, isolated client session (its own connection
to the game server, its own state, its own seat).

```bash
uv run -m coinche.meta \
  --host 127.0.0.1 --port 8765 \        # the game server to connect sessions to
  --listen-host 0.0.0.0 --listen-port 8080 \
  --auth-user coinche --auth-pass 'change-me'   # HTTP basic auth (password required)
```

or, with the launcher (syncs the locked environment, then runs it):

```bash
./run_meta.sh --auth-pass 'change-me' --host 127.0.0.1 --port 8765
```

Flow for a player:

1. Open `http://<meta-host>:8080/` — the browser prompts for the basic-auth
  user/password you configured once. A valid login creates a 30-day browser
  cookie, so later page loads do not need to repeat the Basic Auth prompt.
2. Enter a name and submit → the méta-client spins up a fresh session and
   redirects to `/s/<random-id>` (an unguessable per-session URL).
3. Play from the same web UI as the overlay (lobby → join a table/team → game).

**Accès voiture sans ressaisir le Basic Auth.** Depuis un téléphone déjà
authentifié, ouvrez la page d'accueil puis cliquez sur « Ouvrir sur un autre
appareil ». Elle produit un lien court, à ouvrir sur la voiture dans les
10 minutes. Le lien est à usage unique et crée sur la voiture un cookie
`HttpOnly` valable 30 jours ; les visites suivantes ne demandent plus le mot
de passe. Le mot de passe Basic Auth reste nécessaire pour créer de nouveaux
liens. Pour ne taper que le code court, enregistrez une fois
`http://<meta-host>:8080/a` dans les favoris de la voiture : cette page publique
demande le code affiché sur le téléphone. Ce mécanisme est prévu pour un réseau
local de confiance : sans HTTPS, ne partagez pas le lien ni le réseau avec des
personnes non fiables. Les accès appairé et Basic Auth sont conservés en
mémoire : un redémarrage du méta-client les révoque et demande une nouvelle
authentification ou un nouvel appairage.

Each session runs as an independent set of asyncio tasks on the one event loop
(no per-session thread), reconnecting to the game server on its own with
backoff if the connection drops. The terminal client (`coinche.client`) and the
single-session overlay are unchanged and still work exactly as before.

**Session recovery (browser).** The SPA stores its per-session id in
`localStorage`. On the landing page it probes `GET /api/session?id=<id>`; if
that session is still live it jumps straight back into it, so a page refresh or
an accidentally-closed tab returns the player to their seat instead of spawning
a new, orphaned session. A stale id — the session was reaped, or the server
restarted — reports `{"alive": false}`, the browser drops it, and the name form
is shown for a fresh start.

**Idle timeout / reaping.** A background reaper on the méta-client kicks any
session that has had **no browser attached and no activity** for longer than
the idle timeout (default 900s). A kicked session first sends `LEAVE` — so the
game server frees its seat pre-game, or hands it to a bot mid-game, and tears
down a now-empty table — then it is removed. This keeps abandoned sessions from
pinning seats forever and doubles as table housekeeping. A session with a live
browser attached is never reaped.

**Turn timeout.** A connected human who does not act for 300 seconds is
replaced by a bot for the rest of that game. The per-turn timeout must remain
strictly below the meta-client idle timeout: $T_{\mathrm{tour}} < T_{\mathrm{kick\ global}}$
(defaults: $300 < 900$). `run_app.sh` validates this relation when either
`--turn-timeout` or `--idle-timeout` is supplied.

### One-command launch (server + méta-client)

To bring up the whole thing on one host — the game server *and* the méta-client
wired to it — in a single terminal:

```bash
./run_app.sh --auth-pass 'change-me'                 # server on 8765, web on 8080
./run_app.sh --auth-pass 'change-me' --meta-port 9000 --game-port 8790 \
             --server-log game.log                   # custom ports + server log file
```

`--auth-pass` is **required**. `run_app.sh` synchronizes the locked `uv` environment (like the other
launchers), starts `coinche.server` then `coinche.meta` pointed at it, and
supervises both: Ctrl+C stops them together, and if either process dies the
other is torn down too. This is deliberately a plain shell script — for a
single host with two Python processes and one `uv` environment, a docker-compose-style
orchestrator would add a daemon, images, and a network to manage for no real
gain.

Caveats:

- Basic auth is transmitted in cleartext over plain HTTP — put the méta-client
  behind a TLS-terminating reverse proxy (or an SSH tunnel) if it's reachable
  from an untrusted network.
- Anyone with the auth credentials can create sessions and join tables; the
  per-session URL is a capability, so don't share it.

## Running the tests

```bash
uv run pytest
```

CI also requires 100% line and branch coverage for the default and Maestro bot strategies:

```bash
uv run coverage run --branch -m pytest
uv run coverage report --fail-under=100 coinche/bot_types/default.py
uv run coverage report --fail-under=100 coinche/bot_types/maestro.py
```

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the setup, the lint/format/test
loop, and the pull-request expectations. Agents should also read
[`AGENTS.md`](AGENTS.md).
