# V20.4 — Fusion des deux branches V20.3

## Contexte

Deux V20.3 indépendantes avaient été livrées à partir de la même base
v20.2.1, sans collision de nom résolue :

- **"coherence-seuils-calibres"** : `engine/coherence.py` (explique les
  écarts marginal/modal et le boost du nul dans le message déjà affiché,
  sans rien recalculer) + `engine/history_query.py` corrigé pour comparer
  BTTS/O2.5 aux seuils réellement calibrés (`calibration.json`) plutôt qu'à
  0.5 en dur.
- **"xg-backtest-snapshot-persistant"** (la mienne) : persistance du
  snapshot brut dans `predictions.db` (`snapshot_json`) + câblage de la
  commande `/backtestxg`, jusque-là orpheline.

Cette version fusionne les deux, en partant de la branche "coherence"
(la plus complète des deux sur son sujet) et en y portant mon correctif,
sans rien retirer de part et d'autre.

## Ce qui est resté strictement intact (vérifié par diff avant fusion)

- `engine/coherence.py`, `engine/tests/test_coherence.py` : non touchés.
- Le correctif de seuils calibrés dans `engine/history_query.py` : non touché.
- Le câblage de `check_coherence()` dans `engine/scanner.py` (bloc distinct
  de celui modifié pour la persistance du snapshot) : non touché.
- `engine/anomaly.py`, `engine/predictor.py`
  (`xg_multiplier_override`, déjà présent depuis V20.1) : non touchés.

## Ce qui a été ajouté par-dessus (portage de mon V20.3)

- **`engine/tracking.py`** : colonne `snapshot_json` (migration additive,
  `NULL` par défaut), paramètre `snapshot_json` sur `record_prediction()`.
- **`engine/scanner.py`** : `record_prediction()` sérialise et persiste
  `result.snapshot` — ajouté dans la même fonction que le reste, sans toucher
  au bloc `check_coherence()` qui vit ailleurs dans le fichier.
- **`engine/xg_backtest.py`** : `_load_settled_with_snapshots()` utilise en
  priorité `snapshot_json`, avec repli sur la jointure `forebet_url` <->
  cache local existante pour les lignes créées avant ce correctif (le split
  chronologique calibration/holdout et `apply_candidate()` n'ont pas été
  touchés).
- **`bot.py`** : commande `/backtestxg` ajoutée (même philosophie de
  sécurité que `/recalibrer` : application automatique seulement si le
  candidat ne régresse pas sur le holdout jamais vu), ajoutée à `/help`.

## Pourquoi ce correctif reste nécessaire

Rappel du problème, confirmé une nouvelle fois sur tes vraies données dans
cette fusion : sur les 88 matchs déjà réglés (sans `snapshot_json`),
`/backtestxg` retrouve toujours **0 match**. `engine.cache_store.
prune_expired_snapshots()` purge le snapshot d'un match dès son coup
d'envoi passé, or un match n'est réglable qu'après avoir été joué — sans
persistance à la source, son snapshot a quasi toujours disparu au moment
utile. Les 88 matchs déjà réglés restent irrécupérables (rien à migrer),
mais toute nouvelle prédiction en profitera.

## Vérifications effectuées

- Compilation de l'ensemble du dépôt (`bot.py` + tout `engine/`).
- **8/8 tests unitaires de `engine/tests/test_coherence.py` rejoués à la
  main** (pytest non installable ici, pas d'accès réseau) : tous passent,
  confirmant que le portage n'a rien cassé côté cohérence.
- Test d'équivalence stricte du pipeline de production : `analyse_snapshot()`
  produit toujours les mêmes `home_xg`/`confidence`, et affiche bien un
  signal de cohérence (`anomaly_messages`) quand pertinent.
- `/historique` avec seuils calibrés : toujours fonctionnel après le
  portage (vérifié sur un jeu de 20 matchs synthétiques réglés).
- Sur tes vraies données (copie de travail, empreinte SHA-256 de
  `predictions.db`/`calibration.json` originaux vérifiée inchangée avant/
  après) :
  - État natif (88 réglés, sans snapshot) : `attempted=False, n_matched=0`
    — comportement inchangé, rien de cassé.
  - Flux réel avec tes 36 vrais snapshots du 27/07 : 36 matchés
    (25 calibration / 11 holdout), candidat rejeté car il régresse sur le
    holdout → `calibration.json` non modifié (vérifié).
  - Preuve d'indépendance au cache : après suppression totale du dossier
    de cache correspondant, toujours 36/36 retrouvés.
- Test de régression complet sur données synthétiques fraîches (45 matchs,
  pipeline réel) : `/recalibrer`, `/apprentissagev18`, `/valider`,
  `/historique`, `/backtestxg` (jusqu'à `apply_candidate`, cas accepté
  cette fois) tournent tous sans erreur ; `calibration.json` ne change que
  lorsque le candidat est effectivement accepté.

## Non revérifié dans cet environnement

`pytest` n'est pas installable ici (pas d'accès réseau) — la suite de
tests complète du dépôt (au-delà des 8 tests de `test_coherence.py` rejoués
à la main un par un) n'a pas été exécutée avec `pytest -q`. À revalider en
local avant déploiement.
