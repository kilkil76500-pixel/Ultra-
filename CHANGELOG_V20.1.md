# V20.1 — Fusion : V20 (historique + anomalies) + backtest réel de xg_global_multiplier

## Contexte

Deux branches distinctes avaient divergé :
- La branche "portable" (V19.15 → V20) : déploiement Docker/apt standard,
  pagination du clavier de matchs, test de cohérence commande/aide,
  fuseau Europe/Paris, `/historique`, détection d'anomalies pré-match —
  et la suppression volontaire du bloc "Scénarios V18" (décision produit
  confirmée, pas une régression).
- Ma branche V19.16 : `engine/xg_backtest.py`, le seul mécanisme capable
  de valider `xg_global_multiplier` par ré-simulation complète plutôt que
  par relecture de probabilités déjà stockées.

Cette version fusionne les deux : rien n'a été retiré de la V20, le
backtest xG a été réintégré par-dessus.

## Fusion effectuée

- `engine/predictor.py` : paramètre `xg_multiplier_override` réintégré
  (défaut `None`, comportement inchangé pour tout appelant existant,
  y compris `market_edge`, `anomaly`, `history_query` qui ne le passent
  jamais).
- `engine/scanner.py` : `build_prediction_inputs()` refactorisée en tête
  de fichier, réutilisée par `analyse_snapshot()` — le câblage
  `market_edge` + `anomaly` de la V20, situé après l'appel à
  `predictor.predict()`, est intégralement préservé, dans le même ordre.
- `engine/xg_backtest.py` copié tel quel depuis la V19.16.
- `RELEASE_VERSION` / `RELEASE_MANIFEST.txt` mis à jour (ils indiquaient
  encore "19.15" alors que le contenu réel était déjà en V20 — corrigé
  au passage).

Décisions produit confirmées et donc conservées sans modification :
fuseau Europe/Paris (`engine/utils.py`), suppression du bloc "Scénarios
V18" dans `formatting.py`/`bot.py`.

## Vérification — tout exécuté réellement, pas relu

Environnement de test : les 88 vrais matchs réglés (avec `forebet_url`)
+ les 248 snapshots réels correspondants, dans un cache isolé.

| Test | Résultat |
|---|---|
| Pipeline complet (`analyse_snapshot` → `prediction_text`), 40 matchs réels | 40/40 OK, `anomaly_messages` et `recommended_market` tous deux présents et cohérents sur chaque match |
| `/fiabilite` | n=88, accuracy_1x2=40,9% (inchangé, comme attendu) |
| `/recalibrer` | s'exécute, candidat accepté (seuils de confiance — même limite de fiabilité déjà documentée en V19.15 : jeu de données encore trop petit) |
| `/apprentissagev18` | n=88, `apply_v18_calibration()` retourne toujours `False` |
| `/historique` (conf_min=50) | 43 matchs, rendu correct |
| `/resultat` + `/memoire` | réglage + mémoire d'équipe câblés correctement |
| Cohérence commande/aide (test AST rejoué à la main) | 20 commandes enregistrées, toutes async valides, aucune manquante dans `/help` |
| `engine.xg_backtest.backtest_xg_multiplier()` | s'exécute (~150s sur 88 matchs), lit la calibration mise à jour par `/recalibrer` sans conflit, ne modifie `calibration.json` à aucun moment (vérifié par empreinte SHA-256 isolée) |

`predictions.db` : seules les écritures attendues (les `/resultat` de
test) l'ont modifié — vérifié par empreinte avant/après l'ensemble de la
batterie de tests.

## Ce qui reste vrai, sans changement

Les limites déjà documentées en V19.15/V19.16/V20 restent valables telles
quelles : les 88 matchs sont encore trop peu pour qu'un recalibrage
(seuils de confiance ou `xg_global_multiplier`) soit pleinement fiable ;
le taux d'alerte "anomalie" est actuellement élevé (reflet honnête de la
précision 1X2 encore faible, pas un bug de `anomaly.py`) ; `/historique`
et `anomaly.py` utilisent un seuil fixe de 0,5 pour BTTS/O2.5 plutôt que
les seuils calibrés actifs (`btts_threshold`/`ou25_threshold`) — écart
mineur de cohérence avec `/fiabilite`, à revoir si ça devient gênant en
pratique.
