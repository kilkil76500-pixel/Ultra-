# V19.15 — `/memoire` ne pouvait jamais afficher son propre trait phare

## Contexte

Suite demandée : exécuter `/recalibrer` sur les 88 matchs réels et vérifier
toutes les commandes de bout en bout, sans régression. `/recalibrer` et
`/apprentissagev18` ont été testés directement (pas juste relus) sur les 88
prédictions réglées réelles, avec empreinte SHA-256 de `predictions.db` et
`calibration.json` avant/après chaque commande pour confirmer qu'aucune ne
modifie autre chose que ce qu'elle est censée modifier.

## Bug trouvé en testant `/memoire`

`TeamMemoryProfile.describe()` retournait un early-return sur
`total_matches < 3` **avant** d'évaluer le trait de fiabilité du modèle
basé sur `model_predictions_seen` (ajouté en V19.14). Or `total_matches`
n'est incrémenté que par `update_from_result()` — qui n'est appelé **nulle
part** dans le code actuel, avant comme après V19.14.

Conséquence concrète, reproduite avant correctif : une équipe avec 7
`/resultat` réels réglés (`model_predictions_seen=7`, taux d'erreur mesuré
86%) ne faisait jamais remonter le trait "🎯⚠️ Équipe difficile à prévoir" —
`describe()` renvoyait systématiquement `["⚠️ Données insuffisantes"]`,
quel que soit le nombre de résultats traités pour cette équipe.

`format_team_memory()` avait le **même bug en double** : son propre garde
`if profile.total_matches == 0: ...` court-circuitait avant même d'appeler
`describe()`, donc corriger `describe()` seul n'aurait pas suffi — le
`/memoire` réellement envoyé à l'utilisateur serait resté vide.

## Corrigé

- `describe()` : le trait de fiabilité du modèle est maintenant évalué
  avant le early-return sur `total_matches`, donc indépendant de ce
  compteur jamais alimenté.
- `format_team_memory()` : le garde "aucun historique" ne se déclenche
  désormais que si **ni** `total_matches` **ni** `model_predictions_seen`
  n'ont de données ; l'en-tête affiche le compteur pertinent selon lequel
  des deux est renseigné.

Revérifié après correctif : une équipe avec `model_predictions_seen=7` et
`total_matches=0` affiche maintenant correctement son trait de fiabilité
dans `/memoire` ; une équipe sans aucun historique affiche toujours
"aucun historique enregistré" (pas de régression sur ce cas).

## Résultat des tests demandés

- **`/recalibrer`** : ne plante plus (confirmé sur les 88 matchs réels,
  cf. le fix V19.14). Candidat accepté sur ce jeu de données, mais la
  paire de seuils de confiance proposée (HIGH=38/MEDIUM=36) ne discrimine
  PAS réellement HIGH de LOW sur l'ensemble des 88 matchs (41,3% vs 40,5%
  d'exactitude — écart non significatif) : le jeu de données actuel est
  trop petit pour qu'une recherche de seuils par grille trouve un signal
  stable. De plus, ces 88 matchs portent l'ancien `confidence_pct`
  (pré-V19.14, basé sur les matchs joués) — pas le nouveau score
  `confidence_v2.score` câblé dans ce correctif. **Il faudra relancer
  `/recalibrer` une fois qu'un historique de prédictions utilisant le
  nouveau score se sera accumulé** pour que la recherche de seuils ait un
  sens.
- Repéré en creusant ce point (pas corrigé, documenté pour vigilance) :
  le garde-fou anti-régression sur la discrimination HIGH−LOW
  (`_check_regressions()`, auto_learning.py) ne compare que lorsque la
  config ACTIVE a, elle aussi, des échantillons mesurables dans les deux
  paliers. Sur ce jeu de données, la config active n'avait aucun
  échantillon HIGH (seuil à 72%, jamais atteint) : la comparaison était
  donc impossible, et un candidat dont le panier HIGH était mesurablement
  *pire* que son panier LOW sur un pli de test a pu passer sans être
  bloqué. À surveiller une fois plus de données disponibles — un plancher
  absolu (rejeter si discrimination du candidat < 0, indépendamment de la
  config active) serait plus sûr qu'une comparaison uniquement relative.
- **`/apprentissagev18`** : n'écrit plus rien (`apply_v18_calibration()`
  retourne `False`), confirmé. Le rapport affiché correspond exactement
  aux chiffres calculés indépendamment sur ces mêmes 88 matchs (40,9% 1X2,
  59,1% BTTS, 58,0% O2.5).
- **`/resultat`** (via `tracking.settle()`) : câblage `team_memory`
  vérifié de bout en bout — chaque règlement incrémente bien
  `model_predictions_seen`/`model_correct_1x2` pour les deux équipes.
- **Pipeline complet `analyse_snapshot` → `prediction_text`** : 40
  snapshots réels rejoués sans erreur, `confidence_pct` reflète bien
  `confidence_v2.score`, `recommended_market` (V19.13) toujours
  fonctionnel et non affecté par le changement de barème de confiance.
- Aucun fichier sensible (`predictions.db`, `calibration.json`) modifié
  par une commande qui n'est pas censée écrire (vérifié par empreinte
  SHA-256 avant/après chaque commande testée).
