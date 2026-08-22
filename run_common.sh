#!/usr/bin/env bash
#
# Logique commune aux lanceurs coinche (client / serveur).
#
# Prépare l'environnement verrouillé (git pull, uv sync) puis lance le module
# demandé. Ne pas appeler directement : utiliser run_client.sh ou run_server.sh.
#
# Usage interne:
#   run_common.sh <module.python> [args...]
# Les args sont transmis tels quels au module. --no-pull désactive le git pull.

set -euo pipefail

MODULE="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DO_PULL=1
MODULE_ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--no-pull" ]]; then
        DO_PULL=0
    else
        MODULE_ARGS+=("$arg")
    fi
done

if [[ "$DO_PULL" -eq 1 ]] && [[ -d "$SCRIPT_DIR/.git" ]] && command -v git >/dev/null 2>&1; then
    if ! PULL_OUTPUT="$(git pull --ff-only 2>&1)"; then
        echo "⚠ git pull a échoué, poursuite avec la version locale :"
        echo "$PULL_OUTPUT"
    elif [[ "$PULL_OUTPUT" != *"Already up to date."* && "$PULL_OUTPUT" != *"Déjà à jour"* ]]; then
        echo "$PULL_OUTPUT"
    fi
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "Erreur : uv est requis. Installation : https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

echo "Synchronisation de l'environnement verrouillé..."
uv sync --locked --all-groups
exec uv run --no-sync -m "$MODULE" "${MODULE_ARGS[@]+"${MODULE_ARGS[@]}"}"
