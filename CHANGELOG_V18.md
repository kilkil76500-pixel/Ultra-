# V18 — Auto-amélioration sécurisée (jamais de régression)

## Ajouté
- **`engine/auto_learning.py`** — nouvelle boucle d'auto-apprentissage :
  1. Sépare les prédictions réglées en lot **calibration** (≈70%, les plus
     anciennes) et lot **holdout** (≈30%, les plus récentes, jamais vu).
  2. Propose une calibration candidate à partir du lot calibration
     uniquement (`engine.calibration.compute_calibration`).
  3. Rejoue **l'actif** et **le candidat** sur le même lot holdout jamais
     vu, en réappliquant les seuils/multiplicateurs aux probabilités déjà
     enregistrées.
  4. N'applique le candidat que s'il n'est **strictement pas moins bon**
     que l'actif sur ce holdout (précision 1X2 ≥, Brier/BTTS/O-U pas
     dégradés au-delà d'une tolérance de bruit). Sinon : rejet, la
     calibration active reste identique au bit près.
  - Le candidat accepté est sauvegardé via `calibration.save_calibration`,
    qui versionne et archive automatiquement — `/versions` permet donc
    toujours un retour manuel, même après une auto-amélioration acceptée.
  - Limite assumée et documentée : `xg_global_multiplier` agit avant la
    simulation Monte-Carlo et ne peut pas être rejoué à partir des seules
    probabilités enregistrées. Il est donc figé par cette boucle
    automatique et ne peut évoluer que via `/recalibrerforce` (manuel).

## Corrigé
- **Bug de calibration silencieux** : `prob_multiplier_home` /
  `prob_multiplier_away` / `prob_multiplier_draw` étaient calculés par
  `/recalibrer` et affichés à l'utilisateur, mais **jamais appliqués** aux
  prédictions (`engine/predictor.py` ne les lisait jamais). Le recalibrage
  du biais domicile/extérieur/nul n'avait donc, en pratique, aucun effet.
  Corrigé : les multiplicateurs sont désormais réellement appliqués aux
  probabilités 1X2 (puis renormalisés) avant toute décision en aval
  (label de confiance, détection du nul, `PredictionResult`).
- **`/recalibrer` appliquait ses changements immédiatement**, sans jamais
  vérifier sur des données jamais vues si le nouveau réglage était
  vraiment meilleur — c'était la porte d'entrée principale pour une
  régression silencieuse de la calibration. `/recalibrer` passe désormais
  par `engine.auto_learning` : un changement n'est appliqué que s'il est
  prouvé non régressif. L'ancien comportement immédiat reste disponible
  explicitement via **`/recalibrerforce`**, pour un diagnostic manuel.

## Modifié
- `/apprentissage2` déclenche maintenant, après son rapport segmenté
  habituel (ligue/équipe/mois), un cycle complet d'auto-amélioration
  sécurisée et en affiche le résultat (candidat appliqué ou rejeté, et
  pourquoi).
- `engine/tracking.py` : migration additive de `predictions` avec
  `home_win_prob_raw`, `draw_prob_raw`, `away_win_prob_raw` (probabilités
  1X2 *avant* multiplicateur de calibration) et `calibration_version`.
  Rétrocompatible : sur les lignes créées avant V18 ces colonnes sont
  NULL et `auto_learning` retombe alors sur les probabilités déjà
  enregistrées, qui étaient déjà "raw" à l'époque puisque le bug ci-dessus
  empêchait tout multiplicateur d'être appliqué.
- `engine/learning.py` : `analyse_errors()` accepte désormais un
  paramètre optionnel `rows` (réutilisé par `auto_learning` pour analyser
  uniquement le lot calibration d'un découpage temporel, sans dupliquer
  la logique d'analyse). Comportement par défaut inchangé.

## Tests
- `engine/tests/test_auto_learning.py` (nouveau) : données insuffisantes
  → aucune action ; biais confirmé sur le holdout → candidat accepté ;
  biais contredit par le holdout → candidat rejeté et calibration
  strictement inchangée ; aucun changement pertinent → no-op sûr.
