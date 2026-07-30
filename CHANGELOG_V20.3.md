# V20.3 — Cohérence interne : expliquer plutôt que laisser deviner

## Le problème signalé

"Le bot dit BTTS oui et O2.5 oui alors que le premier scénario dit 2-0" /
"il dit match nul, mais les deux premiers scénarios font 2-1 et 1-2 — si
l'ensemble des pronostics ne va pas dans le même sens, comment le croire ?"

## Diagnostic

Vérifié dans `montecarlo_v5.py` : home_win_prob, draw_prob, away_win_prob,
btts_prob, over25_prob, modal_score et top_scores viennent tous de la
MÊME boucle de simulation, sur le même `score_counts`. Ce n'est donc
jamais un bug de calcul où deux chemins séparés se contrediraient. Deux
mécanismes distincts, tous deux légitimes, produisent l'effet décrit :

1. **Marginal vs modal.** BTTS/O2.5 sont des probabilités cumulées sur
   TOUS les scénarios qui satisfont la condition (1-1, 2-1, 1-2, 2-2,
   3-1…), alors que le "score le plus probable" est UN SEUL scénario pris
   isolément. Il est parfaitement possible que la somme dépasse 50% sans
   qu'aucun scénario individuel ne satisfasse la condition à lui seul.
2. **Le nul est parfois un boost, pas un maximum brut.** `predicted_outcome`
   peut afficher "Nul" parce que `draw_detection_factor` (1.45×, ajouté en
   V17 pour corriger un vrai biais — le modèle ne prédisait jamais nul)
   a fait dépasser `draw_prob` boostée aux deux autres probabilités
   brutes, même si aucune n'était `draw` au départ.

Les deux sont mathématiquement corrects et déjà documentés séparément
dans le code (V17 pour le boost, montecarlo_v5.py pour le calcul commun)
— mais rien ne le disait à l'utilisateur au moment de lire le pronostic.

## Corrigé

Nouveau module `engine/coherence.py` : compare les champs déjà produits
(rien recalculé) et signale, en clair, quand :
- le 1X2 affiché ne correspond pas à la catégorie du score le plus
  probable pris isolément ;
- le nul affiché est un ajustement statistique plutôt que le maximum
  brut (avec les trois probabilités brutes affichées pour vérifier) ;
- BTTS affiché (Oui ou Non) ne correspond pas à ce que le score le plus
  probable, pris isolément, satisferait ;
- Plus/moins 2,5 affiché ne correspond pas à ce que le score le plus
  probable, pris isolément, satisferait.

Ces signaux réutilisent le même mécanisme que `engine/anomaly.py`
(`AnomalyFlag`, sévérité `"info"` — jamais `"warning"`, car ce n'est pas
une alerte de fiabilité, juste une explication) et s'affichent dans le
même bloc "ANOMALIES DÉTECTÉES" déjà présent dans le message Telegram —
aucun nouveau bloc, aucun nouveau champ sur `PredictionResult`.

## Un bug trouvé et corrigé en testant sur données réelles avant livraison

Premier jet : le message affichait toujours le pourcentage de BTTS/O2.5
"Oui", même quand le pronostic affiché était "Non" — ambigu, un lecteur
pouvait croire que le pourcentage se rapportait au "Non". Corrigé pour
toujours préciser explicitement à quoi le pourcentage se rapporte
("probabilité de BTTS Oui à 51%"), dans les deux sens du décalage.

## Vérifié

- Les deux exemples exacts signalés (BTTS+O2.5 "Oui" avec score modal
  2-0 ; "Nul" avec scénarios 2-1/1-2 dominants) reproduits et confirmés
  correctement expliqués.
- 8 tests unitaires (`engine/tests/test_coherence.py`), rejoués à la main
  un par un : 8/8 passent.
- Pipeline complet (`analyse_snapshot` → `prediction_text`) rejoué sur 40
  matchs réels : 40/40 sans erreur, 20/40 (50%) affichent au moins un
  signal de cohérence — fréquence mesurée, pas estimée.
