# V20.7 — Audit d'ablation des composantes de l'indice de force

## Pourquoi

Les 8 composantes de l'indice de force (attaque 25pts, défense adverse
20pts, forme 15pts, H2H 10pts, terrain 10pts, classement 10pts,
motivation 5pts, forme physique 5pts) ont des poids fixes jamais mesurés
depuis leur mise en place. Avant de retoucher le moteur à l'intuition —
exactement l'erreur qu'on a évitée pour `xg_global_multiplier` — ce
module mesure, composante par composante, si elle contribue vraiment à
la précision ou si elle dilue le signal des autres.

## Comment

- `predictor.predict()` accepte un nouveau paramètre optionnel
  `strength_ablation` (défaut `None`, aucun changement pour tout appelant
  existant) : neutralise la composante nommée à la moitié de son poids
  maximum, pour les deux équipes symétriquement.
- `engine/strength_ablation.py` (nouveau) : réutilise
  `xg_backtest._load_settled_with_snapshots()` telle quelle (même source
  fiable depuis V20.4 : `snapshot_json`, repli sur cache), rejoue le
  pipeline complet pour chaque composante + une référence sans ablation,
  sur le même split chronologique calibration/holdout que le reste du
  projet.
- Contrairement à `xg_global_multiplier` ou aux seuils de confiance, une
  composante de l'indice de force n'est PAS un paramètre qu'on retirerait
  en production — ce module ne propose et n'applique rien. Purement
  diagnostique.
- Nouvelle commande `/auditforce` (coûteuse — plusieurs minutes, prévient
  l'utilisateur avant de lancer).

## Vérifié

- Mécanisme validé sur un match isolé avant de lancer l'audit complet :
  chaque composante ablatée produit bien un changement distinct et
  plausible de l'indice/xG (ex. neutraliser "classement" fait baisser
  l'indice des deux équipes de façon cohérente avec leur position
  respective).
- Pipeline normal (sans ablation) rejoué sur 40 vrais matchs après ces
  changements : 40/40 sans erreur — aucune régression sur le comportement
  par défaut.
- Cohérence commande/aide : 23 commandes, `/auditforce` enregistrée,
  câblée, documentée.

## Résultat de l'audit réel sur les 88 matchs réglés

61 matchs (calibration) / 27 matchs (holdout). Impact mesuré (précision
combinée 1X2+BTTS+O2.5, holdout jamais vu pendant le calcul) :

| Composante | Calibration | Holdout | Impact holdout |
|---|---|---|---|
| Référence (aucune ablation) | 57.9% | 39.5% | — |
| Classement | 60.1% | 50.6% | **-9.9%** (ablation améliore) |
| Défense adverse | 55.7% | 48.1% | **-7.4%** (ablation améliore) |
| Forme récente | 62.3% | 42.0% | -1.2% |
| H2H | 57.4% | 39.5% | -1.2% |
| Attaque / Terrain / Motivation / Forme physique | ~58-62% | 39.5% | 0.0% (aucun effet mesurable) |

**Lecture honnête, pas une conclusion à appliquer :**

- "Classement" est le signal le plus intéressant : son ablation améliore
  la précision **dans le lot calibration ET dans le lot holdout** (60,1%
  et 50,6%, tous deux au-dessus de la référence) — une direction
  cohérente sur les deux échantillons est plus rassurante qu'un résultat
  qui ne tiendrait que sur l'un des deux.
- "Défense adverse" est moins fiable : son ablation empire la
  calibration (55,7% < 57,9%) mais améliore le holdout (48,1% > 39,5%) —
  directions opposées entre les deux lots, signe classique de bruit
  plutôt que d'un vrai effet.
- Les composantes à 0,0% ne veulent pas dire "aucun effet" — avec 27
  matchs au holdout, chaque prédiction qui change de sens déplace la
  précision d'environ 3,7 points ; un effet réel mais petit peut très
  bien ne jamais franchir ce seuil de résolution.
- **88 matchs (27 au holdout) restent bien trop peu pour trancher sur 8
  composantes à la fois.** Ce résultat est un signal à surveiller — en
  particulier "classement", dont la direction tient sur les deux lots —
  pas une preuve suffisante pour retoucher les poids de l'indice de force
  dès maintenant. À relancer une fois l'historique étoffé.
