# V20.5 — Fusion des trois branches V20.3

## Contexte

Trois V20.3 avaient été développées indépendamment à partir de la même
base v20.2.1, sans collision de nom résolue entre elles :

1. **"xg-backtest-snapshot-persistant"** (la mienne, V20.3) : persistance
   du snapshot brut dans `predictions.db` + `/backtestxg` câblé.
2. **"coherence-seuils-calibres"** (fusionnée en V20.4) : `engine/coherence.py`
   + seuils calibrés dans `/historique`.
3. **"league_calibration"** (celle-ci) : `engine/league_calibration.py` —
   pénalité de confiance par tier de ligue apprise depuis la précision
   réellement mesurée, au lieu de constantes fixes — + `/recalibrerligues`.

Cette version porte la troisième par-dessus ma V20.4 (qui contenait déjà
les deux premières), sans rien retirer d'aucune des trois.

## Ce qui est resté strictement intact (vérifié par diff avant fusion)

- `engine/coherence.py`, `engine/tests/test_coherence.py`,
  `engine/xg_backtest.py`, `engine/tracking.py` (colonne `snapshot_json`),
  `bot.py` (`/backtestxg`) : non touchés par cette fusion, seulement
  complétés en un point d'insertion distinct pour chacun (voir ci-dessous).
- Le correctif de seuils calibrés dans `engine/history_query.py` : non touché.

## Ce qui a été ajouté par-dessus

- **`engine/league_calibration.py`** + **`engine/tests/test_league_calibration.py`**
  : copiés tels quels (module autonome, aucune dépendance aux fichiers qui
  avaient divergé entre les branches).
- **`engine/leagues.py`** : copié tel quel (base non touchée par mes deux
  fusions précédentes — aucun conflit possible, vérifié par diff avant copie).
- **`engine/tracking.py`** : migration additive `league_tier` (colonne +
  paramètre `record_prediction` + INSERT) ajoutée à côté de `snapshot_json`
  déjà présent — 24 colonnes, 24 `?`, vérifié.
- **`engine/scanner.py`** : `league_tier=result.tier` ajouté au seul appel
  `tracking.record_prediction(...)`, au même point d'insertion relatif que
  dans la branche d'origine — sans toucher au bloc `check_coherence()` ni à
  la persistance de `snapshot_json`, tous deux ailleurs dans la fonction.
- **`bot.py`** : import, commande `cmd_league_calibration`, handler
  `/recalibrerligues` et entrée d'aide ajoutés aux mêmes points d'ancrage
  qu'à l'origine — sans toucher à `/backtestxg` (V20.4) ni au reste.

## Vérifications effectuées

- Compilation de l'ensemble du dépôt (`bot.py` + tout `engine/`).
- Nombre de colonnes de l'INSERT SQL de `record_prediction()` vérifié égal
  au nombre de `?` (24 = 24) après fusion des deux migrations additives.
- **13/13 tests unitaires de `test_league_calibration.py` rejoués à la main**
  avec un harnais minimal reproduisant `monkeypatch`/`tmp_path` (pytest non
  installable ici, pas d'accès réseau) : tous passent, y compris les 5 qui
  utilisent ces fixtures.
- **8/8 tests de `test_coherence.py`** rejoués à nouveau après cette fusion :
  toujours au vert (non affectés, fichier non touché).
- Sur tes vraies données (copie de travail, empreinte SHA-256 de
  `predictions.db`/`calibration.json` originaux vérifiée inchangée) : un
  vrai match du 27/07 traité par le pipeline complet fusionné — `tier`,
  `confidence_penalty`, signal de cohérence (nul ajusté par
  `draw_detection_factor`) et persistance simultanée de `league_tier` ET
  `snapshot_json` en base, tous corrects et cohabitant sans conflit.
- État natif (88 réglés d'origine, sans `snapshot_json`) : `/backtestxg`
  toujours à `attempted=False, n_matched=0` — comportement inchangé.
- Test de régression complet sur données synthétiques fraîches (45 matchs,
  pipeline réel) : `/recalibrer`, `/apprentissagev18`, `/valider`,
  `/historique`, `/recalibrerligues`, `/backtestxg` (jusqu'à
  `apply_candidate`) tournent tous sans erreur ; `calibration.json` ne
  change que lorsqu'un candidat est effectivement accepté.

## Non revérifié dans cet environnement

`pytest` n'est pas installable ici (pas d'accès réseau) — la suite de
tests complète du dépôt (au-delà des 21 tests rejoués à la main : 8
cohérence + 13 league_calibration) n'a pas été exécutée avec `pytest -q`.
À revalider en local avant déploiement.
