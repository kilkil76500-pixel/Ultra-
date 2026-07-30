# V20.9 (fusion) — V20.6 à V20.9 portées sur ma branche

## Contexte

Cette lignée (V20.6 → V20.9) s'est révélée être une continuation directe
de ma V20.5 livrée précédemment — confirmé par diff : `tracking.py` et
`scanner.py` (hors le split warnings/notes de V20.6) étaient déjà
identiques avant même de commencer cette fusion. Le travail restant était
donc limité aux vraies nouveautés, sans reconstruction de zéro.

## Ce qui a été porté

- **V20.6** : `PredictionResult.anomaly_warnings`/`anomaly_notes` (split
  par sévérité, additif — `anomaly_messages` reste inchangé) ; `anomaly.py`
  compare désormais la pénalité de tier réellement apprise
  (`engine.league_calibration`) au lieu d'une hypothèse figée, et affiche
  le signal dans les deux sens (revu à la hausse ou à la baisse) ;
  `formatting.py` affiche les deux catégories séparément, avec repli sur
  l'ancien rendu si les nouveaux champs sont vides.
- **V20.7** : `engine/strength_ablation.py` (nouveau) + `predictor.predict
  (strength_ablation=...)` + commande `/auditforce` — neutralise chaque
  composante de l'indice de force à tour de rôle et mesure l'effet réel
  par ré-simulation sur l'historique réglé (split calibration/holdout).
- **V20.8** : `predictor.predict(h2h_mode=...)` (`None`/`"off"`/
  `"weighted"`) — branche enfin `home_factor`/`away_factor`
  (`engine.h2h.compute_h2h_weight`), calculés depuis toujours mais jamais
  utilisés nulle part. `engine/h2h_audit.py` (nouveau) + commande
  `/audith2h`.
- **V20.9** : `engine/web_collector.py::_extract_odds()` — les cotes 1X2
  n'avaient jamais été scrapées (confirmé : 0 cote sur 62 vrais snapshots
  et 54 matchs réglés). Extraction depuis la page liste Forebet (bloc
  `.haodd`, jointure par `forebet_id`), jamais depuis la page fiche-match
  (non confirmée comme les contenant). `scanner.py`, `odds.py`, `value.py`
  non touchés — ces modules attendaient déjà le bon format.

## Fichiers copiés tels quels (jamais touchés par mes fusions précédentes)

`engine/anomaly.py`, `engine/formatting.py`, `engine/web_collector.py`,
`engine/tests/test_anomaly.py`, `engine/tests/test_web_collector.py`,
`engine/h2h_audit.py`, `engine/strength_ablation.py` — vérifié par diff
contre la base d'origine avant copie : aucun conflit possible.

## Fichiers fusionnés avec attention (les deux branches y avaient touché)

- **`engine/predictor.py`** : remplacé intégralement par la version de
  cette lignée — vérifié au préalable qu'elle contient bien mon seul ajout
  antérieur (`xg_multiplier_override`) à l'identique, en plus de
  `strength_ablation`/`h2h_mode`.
- **`engine/scanner.py`** : une seule addition (le split warnings/notes)
  appliquée au bloc `check_coherence()` existant, sans toucher à la
  persistance `snapshot_json`/`league_tier` juste à côté.
- **`bot.py`** : `/auditforce` et `/audith2h` insérés juste après
  `/backtestxg`, sans toucher à `/recalibrerligues` ni au reste.

## Vérifications effectuées

- Compilation de l'ensemble du dépôt.
- **29/29 tests unitaires rejoués à la main** (8 coherence + 13
  league_calibration + 9 anomaly + 7 web_collector, hors doublons —
  pytest non installable ici, pas d'accès réseau).
- `_extract_odds()` retesté indépendamment avec le HTML documenté dans
  CHANGELOG_V20.9.md (cotes normales, cotes absentes "-", ID inconnu, non-
  contamination avec le bloc live `.la_prmod`).
- Sur tes vraies données les plus fraîches (68 prédictions, 54 réglées,
  62 snapshots réels du 28/07, empreinte SHA-256 de `predictions.db`
  vérifiée inchangée avant/après) :
  - `analyse_snapshot()` (chemin de production réel) rejoué sur les 62
    snapshots : **62/62 sans erreur**.
  - Confirmé indépendamment : 62/62 snapshots réels avaient bien des cotes
    vides (comportement avant ce correctif).
  - Équivalence stricte du comportement par défaut : sur un vrai match
    avec H2H exploitable (Nejmeh Beirut vs Al Ahed), `h2h_mode=None`
    implicite et explicite donnent un xG strictement identique ; `"off"`
    et `"weighted"` changent bien le résultat (confirme que le paramètre
    agit réellement).
  - `/backtestxg`, `/audith2h`, `/auditforce`, `/recalibrerligues`,
    `/historique`, `/apprentissagev18`, `/valider` tous exécutés sur les
    vraies données réglées : `predictions.db` inchangé par les commandes
    diagnostiques.
- Test de régression complet sur données synthétiques fraîches (50
  matchs, pipeline réel) : les 8 commandes tournent sans erreur ;
  `calibration.json` ne change que lorsqu'un candidat est effectivement
  accepté (cas testés dans les deux sens).

## Non revérifié dans cet environnement

`pytest` non installable ici (pas d'accès réseau) — suite de tests
complète non rejouée avec `pytest -q`. Le taux de couverture réel des
cotes en scan Playwright complet (toutes ligues) reste, comme signalé
dans CHANGELOG_V20.9.md d'origine, à confirmer au premier `/scan` après
déploiement.
