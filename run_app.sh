#!/usr/bin/env bash
#
# Lance l'application complète sur une seule machine : le serveur de jeu ET le
# méta-client (front web multi-session) branché dessus, dans un seul terminal.
#
# Le méta-client ouvre une page web protégée par basic auth ; chaque joueur y
# choisit son nom et obtient une session cliente dédiée connectée au serveur.
#
# Usage:
#   ./run_app.sh --auth-pass PASS [options]
#
# Options:
#   --auth-pass PASS     (REQUIS) mot de passe du basic auth de la page web
#   --auth-user USER     utilisateur du basic auth (défaut: coinche)
#   --meta-port PORT     port d'écoute web du méta-client (défaut: 8080)
#   --game-port PORT     port TCP du serveur de jeu (défaut: 8765)
#   --bot-samples N      distributions Monte-Carlo évaluées par coup de bot (défaut: 100)
#   --turn-timeout SEC   délai maximum d'un tour humain (défaut: 300)
#   --idle-timeout SEC   délai de reaping méta-client (défaut: 900)
#   --server-log FICHIER écrit aussi les logs du serveur dans ce fichier
#   --no-pull            ne pas faire de git pull avant de lancer
#
# Ctrl+C arrête proprement les deux process.
#
# Note : pour un seul hôte, ce script suffit. Un orchestrateur type
# docker-compose n'apporterait rien ici (deux process Python, un environnement uv) et
# ajouterait daemon/images/réseau à gérer — inutile tant qu'on reste mono-hôte.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- valeurs par défaut + parsing --------------------------------------------
AUTH_PASS=""
AUTH_USER="coinche"
META_PORT="8080"
GAME_PORT="8765"
BOT_SAMPLES="100"
SERVER_LOG=""
TURN_TIMEOUT="300"
IDLE_TIMEOUT="900"
DO_PULL=1
if [[ -f .env ]]; then
    # `.env` assignments are otherwise shell-local: export them so the game
    # server and meta-client child processes inherit their configuration.
    set -a
    source .env
    set +a
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --auth-pass) AUTH_PASS="$2"; shift 2 ;;
        --auth-user) AUTH_USER="$2"; shift 2 ;;
        --meta-port) META_PORT="$2"; shift 2 ;;
        --game-port) GAME_PORT="$2"; shift 2 ;;
        --bot-samples) BOT_SAMPLES="$2"; shift 2 ;;
        --server-log) SERVER_LOG="$2"; shift 2 ;;
        --turn-timeout) TURN_TIMEOUT="$2"; shift 2 ;;
        --idle-timeout) IDLE_TIMEOUT="$2"; shift 2 ;;
        --no-pull) DO_PULL=0; shift ;;
        -h|--help)
            sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Argument inconnu : $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$AUTH_PASS" ]]; then
    echo "Erreur : --auth-pass est requis (mot de passe du basic auth)." >&2
    echo "Usage : ./run_app.sh --auth-pass PASS [--meta-port N] [--game-port N] [--server-log FICHIER]" >&2
    exit 2
fi

# --- git pull (best-effort) --------------------------------------------------
if [[ "$DO_PULL" -eq 1 ]] && [[ -d "$SCRIPT_DIR/.git" ]] && command -v git >/dev/null 2>&1; then
    if ! PULL_OUTPUT="$(git pull --ff-only 2>&1)"; then
        echo "⚠ git pull a échoué, poursuite avec la version locale :"
        echo "$PULL_OUTPUT"
    elif [[ "$PULL_OUTPUT" != *"Already up to date."* && "$PULL_OUTPUT" != *"Déjà à jour"* ]]; then
        echo "$PULL_OUTPUT"
    fi
fi

# --- environnement verrouillé (même logique que run_common.sh) --------------
UV_BIN="${UV_BIN:-uv}"
# The official installer uses ~/.local/bin, which non-interactive NAS shells
# may not include in PATH.
if [[ "$UV_BIN" == "uv" && -x "$HOME/.local/bin/uv" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v "$UV_BIN" >/dev/null 2>&1; then
    echo "Erreur : uv est requis. Ajoutez ~/.local/bin au PATH ou définissez UV_BIN=/chemin/vers/uv." >&2
    echo "Installation : https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

echo "Synchronisation de l'environnement verrouillé..."
"$UV_BIN" sync --locked --all-groups

if ! "$UV_BIN" run --no-sync python - "$TURN_TIMEOUT" "$IDLE_TIMEOUT" <<'PY'
import sys

from coinche.timeouts import validate_timeout_order

validate_timeout_order(float(sys.argv[1]), float(sys.argv[2]))
PY
then
    echo "Erreur : --turn-timeout doit être strictement inférieur à --idle-timeout." >&2
    exit 2
fi

# --- libère les ports d'éventuels process fantômes ---------------------------
# Restes d'un lancement précédent tué brutalement (kill -9, crash, terminal
# fermé) : des process Python orphelins peuvent garder les ports occupés. On les
# arrête avant de démarrer pour ne pas rester bloqué au bind.
free_port() {
    local port="$1"
    command -v lsof >/dev/null 2>&1 || return 0
    local pids
    pids="$(lsof -ti "tcp:$port" 2>/dev/null || true)"
    [[ -z "$pids" ]] && return 0
    echo "Port $port déjà occupé (process $pids) — arrêt de ces process fantômes."
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
}

# Tue tous les process coinche restants d'un lancement précédent (serveur et
# méta lancés avec `uv run -m coinche.*`). Ils n'occupent pas
# forcément les ports libérés par free_port mais continuent de tourner ; on les
# nettoie ici.
#
# On n'utilise pas `pgrep` : absent de busybox (NAS). On liste via `ps` et on
# filtre avec awk. `ps -o pid= -o args=` marche sur GNU/BSD/macOS ; si ça ne
# donne rien (busybox), on retombe sur `ps w` puis `ps` nu. Le PID est le 1er
# champ dans ces trois formats. Le pattern détecte le wrapper `uv run` et son
# enfant Python, donc il marche pour les lanceurs actuels comme pour les anciens
# lancements directs ; `!/awk/` exclut awk.
kill_coinche_processes() {
    local ps_out pids
    ps_out="$(ps -o pid= -o args= 2>/dev/null || true)"
    [[ -z "$ps_out" ]] && ps_out="$(ps w 2>/dev/null || true)"
    [[ -z "$ps_out" ]] && ps_out="$(ps 2>/dev/null || true)"
    pids="$(printf '%s\n' "$ps_out" | awk '
        !/awk/ && tolower($0) ~ /python.*-m coinche\./ {print $1}
        !/awk/ && $0 ~ /(^|[[:space:]])uv[[:space:]].*run.*-m coinche\./ {print $1}
    ' || true)"
    [[ -z "$pids" ]] && return 0
    # shellcheck disable=SC2086
    echo "Process coinche encore actifs ($(echo $pids | tr '\n' ' ')) — arrêt avant de démarrer."
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
}

kill_coinche_processes
free_port "$GAME_PORT"
free_port "$META_PORT"

# --- lancement supervisé -----------------------------------------------------
SERVER_PID=""
META_PID=""

# Tue un process proprement (TERM) puis, s'il résiste, brutalement (KILL).
kill_hard() {
    local pid="$1"
    [[ -z "$pid" ]] && return 0
    kill "$pid" 2>/dev/null || return 0
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.1
    done
    kill -9 "$pid" 2>/dev/null || true
}

cleanup() {
    # Arrêt des deux enfants avec escalade TERM -> KILL ; idempotent.
    kill_hard "$META_PID"
    kill_hard "$SERVER_PID"
    wait 2>/dev/null || true
}
# HUP couvre la fermeture brutale du terminal, absente du trap précédent.
trap cleanup EXIT INT TERM HUP

SERVER_ARGS=(--host 0.0.0.0 --port "$GAME_PORT" --bot-samples "$BOT_SAMPLES" --turn-timeout "$TURN_TIMEOUT")
if [[ -n "$SERVER_LOG" ]]; then
    SERVER_ARGS+=(--log-file "$SERVER_LOG")
fi

echo "Démarrage du serveur de jeu sur le port $GAME_PORT ..."
"$UV_BIN" run --no-sync -m coinche.server "${SERVER_ARGS[@]}" &
SERVER_PID=$!

echo "Attente du serveur de jeu ..."
SERVER_READY=0
for _ in $(seq 1 300); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "Erreur : le serveur de jeu s'est arrêté avant de devenir disponible." >&2
        exit 1
    fi

    if "$UV_BIN" run --no-sync python - "$GAME_PORT" <<'PY'
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
    connection.settimeout(0.1)
    sys.exit(0 if connection.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
    then
        SERVER_READY=1
        break
    fi
    sleep 0.1
done

if [[ "$SERVER_READY" -ne 1 ]]; then
    echo "Erreur : le serveur de jeu n'est pas disponible après 30 secondes." >&2
    exit 1
fi

echo "Démarrage du méta-client (web) sur le port $META_PORT ..."
"$UV_BIN" run --no-sync -m coinche.meta \
    --host 127.0.0.1 --port "$GAME_PORT" \
    --listen-host 0.0.0.0 --listen-port "$META_PORT" \
    --auth-user "$AUTH_USER" --auth-pass "$AUTH_PASS" --idle-timeout "$IDLE_TIMEOUT" &
META_PID=$!

echo "Application lancée. Ctrl+C pour tout arrêter."

# Supervision portable (macOS ships bash 3.2, sans `wait -n`) : on sonde les
# deux enfants toutes les secondes ; dès que l'un meurt, le trap EXIT arrête
# l'autre. `kill -0` teste l'existence du process sans lui envoyer de signal.
while kill -0 "$SERVER_PID" 2>/dev/null && kill -0 "$META_PID" 2>/dev/null; do
    sleep 1
done
echo "Un des process s'est arrêté — arrêt de l'application."
