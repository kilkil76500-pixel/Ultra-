# V20.2 — Identifiant de pronostic restauré (perdu par effet de bord de la suppression des Scénarios V18)

## Le problème signalé

Après `/predict`, le message ne montrait plus nulle part le numéro
d'identification du pronostic (utile pour `/resultat <id> <score>` plus
tard). L'utilisateur devait relancer `/resultat` à vide pour retrouver
l'ID dans la liste des pronostics en attente.

## Cause

L'identifiant (`🆔 Pronostic n°<id>` + l'exemple `/resultat <id> 2-1`
prêt à copier) n'était pas un bloc indépendant — il vivait comme pied de
page DANS `scenarios_text()` (le bloc "Scénarios V18"). Quand ce bloc a
été retiré (décision produit confirmée — la partie narrative
principal/favorable/défavorable ne devait plus s'afficher), le pied de
page identifiant est parti avec, sans que ce soit l'intention : la
suppression visait le récit des scénarios, pas le suivi de l'ID.

## Corrigé

- `engine/formatting.py` : `prediction_text()` accepte maintenant un
  paramètre optionnel `pred_id` (défaut `None`, rétrocompatible — aucun
  appelant existant qui ne le passe pas n'est affecté). Quand fourni, un
  petit pied de page indépendant s'affiche : identifiant + exemple
  `/resultat` avec l'ID déjà rempli. Le bloc "Scénarios V18" reste retiré,
  comme décidé.
- `bot.py` : les deux points d'appel (`_predict_index`, utilisé par
  `/predict` et `/exemple` ; et `cmd_match`, utilisé par `/match`)
  passent maintenant `pred_id=pred_id` — la variable existait déjà
  (retournée par `scanner.record_prediction()`), elle n'était simplement
  jamais transmise à l'affichage.

## Vérifié

- Rejoué sur un vrai snapshot : le pied de page apparaît, avec le bon ID,
  et l'exemple `/resultat <id> 2-1` est correct.
- Sans `pred_id` fourni : aucun pied de page, aucune erreur — non-régressif
  pour tout appelant qui ne le passe pas.
- 25 snapshots réels rejoués via le pipeline complet
  (`analyse_snapshot` → `record_prediction` → `prediction_text`) : 25/25
  OK, ID correct à chaque fois.
