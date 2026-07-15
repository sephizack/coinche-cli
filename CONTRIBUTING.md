# Contribuer à coinche-cli

Merci de contribuer ! Ce dépôt est petit et volontairement simple ; garde les
changements ciblés, testés, et cohérents avec l'architecture en couches décrite
dans [`AGENTS.md`](AGENTS.md).

## Mise en place

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+ est requis.

## La boucle de vérification

Avant de proposer un changement, lance les trois vérifications et assure-toi
qu'elles passent (ce sont exactement celles que la CI exécute sur Python 3.10 et
3.13) :

```bash
ruff check .                              # lint (E, F, I, B, UP)
ruff format --check coinche demo_table.py # formatage
python -m pytest                          # tests (~1,5 s)
```

Corrections automatiques : `ruff check . --fix` et `ruff format coinche demo_table.py`.

## Attentes sur les changements

- Tout changement de comportement des **règles, du score ou du protocole** doit
  s'accompagner d'un test correspondant. Les tests qui passent sont le filet de
  sécurité principal (il n'y a pas de vérificateur de types séparé).
- Respecte les **frontières d'architecture** : pas d'I/O ni de réseau dans
  `game.py`/`rules.py`/`cards.py` ; le serveur reste autoritaire sur la
  validation des coups. Voir [`AGENTS.md`](AGENTS.md).
- Chaînes fournies par l'utilisateur (noms, chat) : toujours via
  `rich.text.Text(value)`, jamais interpolées dans une chaîne de markup.
- Garde la doc synchronisée : si tu modifies une commande, un check ou une règle
  d'archi, mets à jour `README.md` et `AGENTS.md`.

## Pull requests

- Travaille sur une branche et ouvre une PR ; la CI (lint + formatage + tests)
  doit passer avant le merge sur `main`.
- Le [modèle de PR](.github/pull_request_template.md) fournit une check-list
  (vérifications + responsabilité). Remplis-la.
- **Responsabilité de l'auteur, y compris pour le code assisté par IA :** tu dois
  avoir lu et compris l'intégralité de la diff que tu proposes. La description et
  les tests sont les tiens, pas une simple sortie générée non relue. Un
  reviewer (ou toi, en auto-revue) doit pouvoir compter sur le fait que l'auteur
  comprend et assume le changement.
