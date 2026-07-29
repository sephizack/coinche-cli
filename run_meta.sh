#!/usr/bin/env bash
#
# Lance le méta-client coinche (front web multi-session). Voir run_common.sh
# pour les détails (git pull, venv, installation des dépendances).
#
# Un seul process héberge plusieurs sessions clientes : chaque joueur ouvre la
# page web (protégée par basic auth), choisit son nom, et se voit attribuer une
# session cliente dédiée connectée au serveur de jeu indiqué au démarrage.
#
# Usage:
#   ./run_meta.sh [--no-pull] --auth-pass PASS [--host GAME_HOST] [--port GAME_PORT] \
#                 [--auth-user USER] [--listen-host HOST] [--listen-port PORT]

exec "$(dirname "${BASH_SOURCE[0]}")/run_common.sh" coinche.meta "$@"
