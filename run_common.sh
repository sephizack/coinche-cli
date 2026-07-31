#!/usr/bin/env bash
#
# Logique commune aux lanceurs coinche (client / serveur).
#
# Prépare l'environnement (git pull, venv, dépendances) puis lance le module
# Python demandé. Ne pas appeler directement : utiliser run_client.sh ou
# run_server.sh.
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

VENV_DIR=".venv"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Création du venv dans $VENV_DIR ..."
    python -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

REQUIREMENTS_FILE="requirements.txt"
STAMP_FILE="$VENV_DIR/.requirements.installed"

if [[ ! -f "$STAMP_FILE" || "$REQUIREMENTS_FILE" -nt "$STAMP_FILE" ]]; then
    echo "Installation des dépendances ..."
    pip install --upgrade pip >/dev/null
    pip install -r "$REQUIREMENTS_FILE"
    touch "$STAMP_FILE"
fi

# The "${MODULE_ARGS[@]+...}" form (instead of a bare "${MODULE_ARGS[@]}")
# avoids an "unbound variable" error under `set -u` when MODULE_ARGS is
# empty on bash < 4.4 (e.g. macOS's default /bin/bash 3.2).
exec python -m "$MODULE" "${MODULE_ARGS[@]+"${MODULE_ARGS[@]}"}"
