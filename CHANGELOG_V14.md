# V14 — Suivi de fiabilité + scénario simulé réellement aléatoire

## Suivi de fiabilité

- Chaque analyse générée par `/predict`, `/match` ou `/example` est enregistrée dans `cache/predictions.db`.
- La commande `/resultat <id> <score>` permet d'ajouter manuellement le score final.
- La commande `/fiabilite` affiche la précision réelle 1X2, le score de Brier, BTTS, Over/Under 2,5 et la ventilation par confiance.

## Scénario simulé

- Chaque analyse affiche un tirage frais du modèle avec un score et les minutes des buts.
- Ce tirage illustratif est séparé des probabilités agrégées Monte Carlo, qui restent la mesure statistique principale.

La récupération automatique des résultats n'est pas incluse : les scores sont saisis manuellement avec `/resultat`.