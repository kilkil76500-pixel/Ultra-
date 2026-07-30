# V20 — Mémoire statistique, détection d'anomalies

Suite à l'échange sur les limites du bot (feuille de route V20), trois
briques ont été ajoutées cette session. Priorité donnée aux points qui
n'existaient pas encore réellement (voir CHANGELOG précédents pour ce qui
était déjà là : `leagues.py` pour la fiabilité par ligue, `market_edge.py`
pour la fiabilité par marché).

## Ajouté

### `engine/history_query.py` — requête libre sur l'historique réglé
Filtre `predictions.db` (déjà alimenté par `engine.tracking`) par ligue,
plage de confiance, issue prédite, xG modèle total. Retourne le taux de
réussite réel 1X2/BTTS/Over2.5 sur le sous-ensemble filtré, avec seuil
minimal d'échantillon avant d'afficher un pourcentage.

**Limite assumée et documentée dans le code** : le bot ne collecte pas la
possession, les tirs cadrés ou les corners — seulement ce qu'il calcule
lui-même. Une requête façon "matchs à 62% de possession" n'est donc pas
possible sur ces données ; ce module ne le prétend pas.

Nouvelle commande Telegram : `/historique ligue=Premier conf_min=60`.

### `engine/anomaly.py` — détection d'anomalies pré-match
Trois signaux réellement disponibles, combinés :
1. Similarité historique (via `history_query`) : taux d'échec 1X2 réel sur
   les matchs passés à confiance/xG comparables.
2. Écart cote bookmaker vs probabilité modèle sur l'issue favorite.
3. Dispersion du scénario dominant (probabilité du score le plus probable
   issu du Monte-Carlo) — voir note de recalibrage ci-dessous.

Câblé automatiquement dans `scanner.analyse_snapshot` (lecture seule,
try/except non-bloquant, même pattern défensif que `market_edge` — un
échec de ce module n'interrompt jamais un scan). Affiché dans `/predict`
via `formatting.py`.

### Bug évité en testant — signal de dispersion mal calibré
Le premier jet utilisait `distinct_scorelines` (nombre brut de scores
distincts simulés) avec un seuil de 14. Testé en pipeline complet sur 5
profils de match représentatifs (équilibré, favori net, fermé, offensif,
écrasant) : cette valeur varie naturellement de 40 à 92+ sur des matchs
parfaitement normaux — corrélée au nombre de buts attendus, pas à
l'imprévisibilité. Avec un seuil à 14, l'alerte se serait déclenchée sur
la quasi-totalité des matchs. Remplacé par la probabilité normalisée
(0-1) du scénario de score le plus probable (`top_scores[0][1]`),
comparable entre matchs. `mc_convergence_se` a été abandonné comme signal
pour la même raison : quasi constant (~0.0002-0.001) quel que soit le
match car la simulation tourne toujours à 100 000 itérations.

## Tests
`engine/tests/test_history_query.py` (5 tests) et
`engine/tests/test_anomaly.py` (7 tests), plus rejeu manuel du scénario
d'intégration existant (`test_integration_e2e.py`, cycle
scan→predict→resultat sur 40 matchs) et du test scanner existant
(`test_analyse_snapshot_wires_prediction_and_odds`) pour confirmer
l'absence de régression. `pytest` n'étant pas installable dans cet
environnement (pas d'accès réseau), les tests ont été exécutés via un
petit harnais manuel reproduisant le comportement de
`monkeypatch`/`tmp_path` — à revalider avec `pytest -q` en local.

## Non traité cette session
- Pondération dynamique des ligues *apprise* (point 4 de la feuille de
  route) — `leagues.py` reste à tiers fixes, pas encore recalibrés
  automatiquement par ligue comme le fait déjà `auto_learning.py` par
  marché.
- Un vrai moteur d'apprentissage statistique (régression/boosting appris
  sur les données plutôt que des formules à la main) — non entamé,
  demanderait un jeu de données d'entraînement plus grand que les 88
  matchs actuellement réglés pour être fiable.
