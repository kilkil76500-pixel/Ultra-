# V19.6 — Le scroll abandonne avant d'atteindre les matchs tardifs

## La demande

Un `/scan` lancé à 20h50 remonte bien les matchs en cours (18h50→20h50)
mais rate une partie des matchs plus tardifs (23h) un jour où il y a
beaucoup de matchs — comme si le bot « prenait ce qu'il voulait ».

## Ce qui bloquait ça

La fenêtre de collecte elle-même n'était pas en cause : avec
`WEB_SCAN_HOURS=10`, un match à 23h est largement dans la fenêtre d'un
scan à 20h50. Le vrai goulot d'étranglement est le chargement de la page
"aujourd'hui" — triée chronologiquement, révélée par scroll infini côté
navigateur headless. Deux limites arrêtaient ce scroll avant d'atteindre
le bas de la liste (donc les matchs tardifs) un jour chargé :

- `WEB_SCAN_LOAD_MORE_MAX_SECONDS=180` : un budget de 180s suffit pour
  une journée normale mais peut s'épuiser avant la fin du scroll un jour
  à 130+ matchs, surtout si Forebet répond lentement (Cloudflare,
  latence réseau).
- Le seuil de stagnation à 4 rounds sans nouveau match pouvait déclencher
  un arrêt prématuré sur un simple ralentissement réseau ponctuel, pas
  une vraie fin de contenu.

Résultat : les matchs tardifs n'étaient jamais présents dans le HTML
récupéré — le filtre de fenêtre horaire ne les voyait donc jamais, ce
n'était pas un choix délibéré mais une interruption du scroll dépendante
du timing réseau du moment.

## Correctif

- `WEB_SCAN_LOAD_MORE_MAX_SECONDS` : 180 → 300 secondes (config.py,
  toujours surchageable via la variable d'environnement du même nom).
- Seuil de stagnation avant abandon : 4 → 7 rounds sans nouveau match
  (web_collector.py, `_fetch_match_list_headless`).

## À vérifier au prochain `/scan`

Regarder la ligne de log :
```
[web_collector] <label> : chargement headless terminé — X clic(s), Y
défilement(s), Z match(s) au total dans le DOM.
```
et si un message "plafond de N défilement(s) atteint" ou "budget temps
de Ns épuisé" apparaît juste avant — ça indique si une des limites est
encore trop juste sur une journée particulièrement chargée (auquel cas
il faut les relever encore, via les variables d'environnement
`WEB_SCAN_LOAD_MORE_MAX_SECONDS` / `WEB_SCAN_MAX_LOAD_MORE_CLICKS`, sans
toucher au code).
