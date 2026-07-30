# V19.11 — Scan complet figé et export de fiabilité

## Scan

- Le navigateur Chromium headless est désormais obligatoire pour les trois
  pages Forebet (aujourd'hui, en direct, demain).
- L'ancien repli GET statique est désactivé : il ne peut plus produire un scan
  « réussi » avec seulement les premières lignes de la page.
- Chaque fiche de match est téléchargée une seule fois. La réponse déjà
  utilisée pour la validation est réutilisée pour l'enrichissement détaillé.
- Le plafond historique `GLOBAL_SCAN_MAX_FIXTURES` n'est pas appliqué : tous
  les matchs retenus dans la fenêtre sont traités et écrits progressivement.

## Fiabilité et export

- `cache/predictions.db` reste la source persistante de `/fiabilite`,
  `/resultat` et de l'apprentissage.
- `python export_data.py` crée `cache/exports/` avec les snapshots de matchs,
  les pronostics de fiabilité en JSON et CSV, un résumé statistique et une
  copie SQLite.
- Les données existantes sont conservées dans la release ; aucun secret
  Telegram n'est inclus.