# V20.10 — Le signal "écart cote/modèle" comparait le mauvais favori

## Contexte

En vérifiant la V20.9 (extraction des cotes 1X2 depuis la page liste
Forebet — enfin fonctionnelle, testée contre le vrai HTML fourni par
l'utilisateur, 4/4 tests + 5 cas limites supplémentaires ajoutés ici,
tous passent) avec de VRAIES cotes injectées sur un vrai match du cache
récent, un deuxième bug est apparu — invisible jusqu'ici uniquement parce
que ce chemin n'avait jamais été atteignable en production (les cotes
étaient toujours vides avant V20.9).

## Le bug

`anomaly.py::detect_anomalies()` calculait l'écart cote/modèle en
comparant `max(probabilités du modèle)` à `max(probabilités implicites
du marché)` — sans vérifier qu'il s'agit de la MÊME issue des deux côtés.

Exemple réel rencontré en testant : modèle favori = Extérieur à 52% ;
marché favori = Domicile à 54% implicite. Comparer les deux maximums
(52% vs 54%) ne montre presque aucun écart — silence total. Mais le
marché ne crédite l'Extérieur (le favori du MODÈLE) que de 24% : un
écart réel de 28 points sur l'issue qui compte, complètement invisible
avec l'ancienne comparaison. `engine/value.py` calculait déjà
correctement cet écart par issue (`market_edge`) — `anomaly.py` mesurait
autre chose sans s'en rendre compte.

## Corrigé

- `detect_anomalies()` : `bookmaker_fav_prob` (un seul scalaire) remplacé
  par `bookmaker_home_prob`/`bookmaker_draw_prob`/`bookmaker_away_prob`
  (une probabilité implicite par issue).
- La comparaison se fait maintenant entre la probabilité du modèle pour
  SON favori et la probabilité implicite du marché pour cette MÊME
  issue — pas le favori du marché.
- `scanner.py` mis à jour pour calculer et transmettre les trois
  probabilités implicites au lieu du seul maximum.

## Vérifié

- **V20.9 (extraction des cotes)** : les 4 tests fournis (construits sur
  du vrai HTML utilisateur) rejoués à la main : 4/4 passent. 5 cas
  limites supplémentaires ajoutés et testés : deux matchs adjacents avec
  IDs différents (aucune contamination), séparateur décimal virgule, cote
  ≤ 1.0 rejetée, une seule cote manquante rejette tout le triplet, grande
  région réaliste (0,1ms, aucun risque de lenteur) — 5/5 passent.
- Reproduit le bug exact sur un vrai match du cache récent (cotes
  injectées manuellement, puisque les cotes réelles pré-V20.9 sont
  toujours vides) : confirmé silencieux avant correctif, signale
  correctement "28 points" après.
- 11/11 tests `test_anomaly.py` (9 existants + 2 nouveaux ciblant
  précisément ce bug) rejoués à la main.
- Pipeline complet rejoué sur 40 vrais matchs récents (moitié avec cotes
  injectées, moitié sans, pour couvrir les deux chemins) : 40/40 sans
  erreur.

## Non vérifié (comme indiqué en V20.9)

Le taux de couverture réel des cotes en scan live (Playwright, toutes
les lignes du jour) reste à confirmer au prochain `/scan` — la structure
HTML a été vérifiée sur les échantillons fournis, pas sur l'intégralité
d'une page réelle.
