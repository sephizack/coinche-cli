"""Méta-client Coinche : un unique process qui héberge plusieurs sessions
clientes indépendantes derrière une seule page web protégée par basic auth.

Chaque session (`MetaSession`) est une instance cliente *headless* : sa propre
connexion TCP au serveur de jeu, son propre `ClientState`/`ClientLink`, et un
pont WebSocket vers le(s) navigateur(s) qui la pilotent. Le client terminal
(`coinche.client`) et l'overlay web mono-session (`coinche.web`) restent
inchangés — le méta-client vit à côté.

Voir `coinche/meta/session.py` et `coinche/meta/server.py`.
"""

from __future__ import annotations

from coinche.meta.server import MetaClientServer
from coinche.meta.session import MetaSession

__all__ = ["MetaClientServer", "MetaSession"]
