# Football Intelligence

Bot Telegram d'analyse footballistique en français. Il collecte les matchs dans
un cache local, calcule des probabilités 1X2/xG/BTTS/Over 2.5, conserve les
pronostics et mesure leur fiabilité après saisie des résultats.

## Fonctionnalités

- `/scan` collecte les matchs dans `WEB_CACHE_DIR`.
- `/today`, `/predict`, `/match` affichent et analysent les matchs en cache.
- Monte-Carlo V5, xG V16, analyse tactique et indice de confiance à 8
  composantes (attaque, défense adverse, forme, H2H, terrain, classement,
  motivation, forme physique).
- Chaque prédiction affiche automatiquement ses signaux de fiabilité :
  cohérence marginal/modal, écart cote/modèle (comparé sur la même issue),
  historique similaire, dispersion Monte-Carlo, fiabilité par tier de
  ligue — séparés en `⚠️ signaux` (à vérifier) et `ℹ️ notes` (explicatifs).
- `/resultat`, `/autoresultat`, `/fiabilite`, `/apprentissage` et
  `/recalibrer` suivent la qualité réelle des prédictions.
- `/historique` interroge librement l'historique réglé (filtres ligue,
  confiance, issue, etc.).
- `/memoire` conserve un profil par équipe (fragilité mentale, buts
  tardifs, remontadas...) alimenté par `/resultat`.
- Diagnostics d'audit, purement informatifs (n'appliquent jamais rien
  automatiquement sauf mention contraire) :
  - `/backtestxg` — backtest de `xg_global_multiplier` par ré-simulation
    complète, validé par split calibration/holdout ; applique le candidat
    à `calibration.json` s'il ne régresse pas sur le holdout.
  - `/recalibrerligues` — pénalité de confiance par tier de ligue apprise
    depuis la précision réellement mesurée (au lieu de constantes fixes) ;
    applique de la même façon si le holdout valide l'ordre proposé.
  - `/auditforce` — ablation de chaque composante de l'indice de force,
    une par une, pour mesurer son effet réel.
  - `/audith2h` — compare le canal H2H actuel, désactivé, et une variante
    pondérée par récence/écart de buts (`home_factor`/`away_factor`).
- Chaque prédiction persiste son snapshot d'origine (`snapshot_json` dans
  `predictions.db`) : les audits ci-dessus restent utilisables même après
  que le match ait quitté le cache journalier.
- Les cotes 1X2 bookmaker sont scrapées depuis la page liste Forebet et
  alimentent `engine/value.py` (analyse EV) et le signal écart cote/modèle.
- Le scan V19.11 utilise obligatoirement Chromium headless et réutilise la
  même fiche HTTP pour la validation et l'enrichissement : aucun repli statique
  incomplet n'est autorisé.
- `python export_data.py` exporte les matchs en cache et l'historique
  `/fiabilite` dans `cache/exports/` (JSON, CSV et SQLite).

Les probabilités sont des estimations statistiques et ne constituent pas des
conseils de paris.

## Installation locale

Python 3.11 ou plus récent est recommandé.

```bash
cd bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
cp .env.example .env
# renseigner TELEGRAM_BOT_TOKEN dans .env
./start.sh
```

Le bot utilise le fichier SQLite `WEB_CACHE_DIR/predictions.db` et les snapshots
JSON du même répertoire. Le cache par défaut est le dossier `cache` situé à côté
de `start.sh`, quel que soit le dossier depuis lequel le script est appelé.

## Variables d'environnement

Voir `.env.example`. `TELEGRAM_BOT_TOKEN` est obligatoire.

## Tests

Les tests n'appellent ni Telegram, ni les sites de collecte.

```bash
cd bot
pytest -q
```

`pytest.ini` (depuis V19) active automatiquement la couverture avec un seuil
minimal (`--cov-fail-under=55`, volontairement prudent) : `pytest -q` échoue
désormais si la couverture baisse en dessous de ce seuil. Pour l'affichage
détaillé ligne par ligne :

```bash
pytest --cov=engine --cov=bot --cov-report=term-missing
```

Voir `CHANGELOG_V19.md` pour le détail des tests ajoutés dans cette version
(formatting, calibration, intégration bout-en-bout), et `CHANGELOG_V20.*.md`
(notamment `CHANGELOG_V20.9-fusion.md` et `CHANGELOG_V20.10-fusion-autoresultat.md`)
pour les tests ajoutés depuis — coherence, league_calibration, anomaly,
web_collector (cotes + scores finaux). Ces derniers ont été rejoués à la main
(sans `pytest`, indisponible dans certains environnements de préparation) ;
voir chaque changelog pour le détail exact de ce qui a été vérifié ainsi.

## Exploitation

### Docker

```bash
docker build -t football-intelligence .
docker run --env-file .env -v "$PWD/cache:/app/cache" football-intelligence
```

### Procfile

Le processus `worker` démarre `./start.sh`, qui applique les paramètres de scan
et prépare le cache avant de lancer le bot. Le fichier peut être utilisé par
les plateformes qui prennent en charge les Procfiles.

### Redéploiement sur une autre plateforme

Cette archive est autonome et ne contient aucun secret, cache ou fichier
temporaire. Depuis le dossier du bot :

```bash
cp .env.example .env
# renseigner TELEGRAM_BOT_TOKEN dans .env
pip install -r requirements.txt
python -m playwright install chromium
./start.sh
```

`start.sh` charge automatiquement le fichier `.env` lorsqu’il est présent. En
production, il est préférable d’injecter `TELEGRAM_BOT_TOKEN` comme variable
secrète de la plateforme.

Pour un environnement Docker, `Dockerfile` installe aussi les bibliothèques
système et Chromium nécessaires à la collecte headless. Pour un hébergeur
acceptant un Procfile, utiliser le processus `worker`.

Le répertoire `cache/` doit être conservé sur un volume persistant si l’on veut
préserver les pronostics, les résultats et la calibration après redémarrage.

## Commandes principales

| Commande | Usage |
| --- | --- |
| `/scan` | Charger les matchs et leurs données |
| `/today` | Lister les matchs en cache |
| `/predict 2` | Analyser le match numéro 2 |
| `/match A vs B` | Rechercher un match du dernier scan |
| `/example` | Analyser un match aléatoire du cache |
| `/resultat 42 2-1` | Régler le pronostic 42 |
| `/autoresultat` | Vérifier et régler les matchs marqués terminés par Forebet |
| `/fiabilite` | Voir la précision mesurée |
| `/historique ligue=... conf_min=60` | Requête libre sur l'historique réglé |
| `/apprentissage` | Analyser les erreurs |
| `/apprentissage2` | Analyse V2 et auto-amélioration |
| `/apprentissagev18` | Analyse profonde des scénarios |
| `/recalibrer` | Recalculer la calibration avec validation |
| `/recalibrerforce` | Recalculer immédiatement sans backtest |
| `/recalibrerligues` | Pénalité de confiance par tier de ligue (appris, holdout sécurisé) |
| `/valider` | Valider la calibration sur un lot holdout |
| `/backtestxg` | Backtest de xg_global_multiplier (holdout sécurisé) |
| `/auditforce` | Audit d'ablation de l'indice de force (diagnostique) |
| `/audith2h` | Audit du canal H2H (diagnostique) |
| `/versions` | Voir ou restaurer les calibrations |
| `/memoire [équipe]` | Voir la mémoire des équipes |
| `/delete` | Supprimer le cache du jour |

## Export des données

Depuis le dossier `bot` :

```bash
python export_data.py
```

Les fichiers sont créés dans `cache/exports/`. La base originale
`cache/predictions.db` n'est pas modifiée.
