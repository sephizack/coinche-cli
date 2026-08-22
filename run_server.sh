#!/usr/bin/env bash
#
# Lance le serveur coinche. Voir run_common.sh pour les détails
# (git pull, uv sync).
#
# Usage:
#   ./run_server.sh [--no-pull] [--host HOST] [--port PORT] [--target-score N]

exec "$(dirname "${BASH_SOURCE[0]}")/run_common.sh" coinche.server "$@"
