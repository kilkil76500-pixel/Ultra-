# V19.7 — Budget de collecte à 500s + calibration du décalage horaire Forebet

## Demandes traitées

1. Budget de scroll insuffisant sur un jour très chargé → relevé à 500s.
2. Les matchs de l'heure en cours du scan (ex. 17h si le scan tourne à 17h)
   étaient sautés, seuls ceux à partir de 18h+10h apparaissaient.

## 1. Budget de collecte

`WEB_SCAN_LOAD_MORE_MAX_SECONDS` : 300 → 500 secondes (config.py). Toujours
surchargeable via la variable d'environnement du même nom sans toucher au
code.

## 2. Décalage horaire Forebet

Ce n'était pas un problème de fenêtre de scan (`WEB_SCAN_HOURS=10` couvre
largement un match à +1h), ni un problème de tri/chargement — c'est un
décalage de fuseau horaire entre l'heure affichée par Forebet et l'UTC
utilisé en interne par le bot pour comparer "maintenant" au coup d'envoi.

Le site Forebet propose lui-même un sélecteur de fuseau horaire (visible
dans son menu "Settings"), ce qui confirme que l'heure qu'il affiche par
défaut au scraper n'est pas forcément l'UTC pur que `_find_datetime()`
suppose. Le paramètre `FOREBET_TZ_OFFSET_HOURS` existait déjà précisément
pour ce cas de figure (voir la note V19 dans `config.py`) mais était resté à
0 (aucune correction) par défaut.

D'après le décalage rapporté (un scan à 17h saute les matchs de 17h et ne
remonte qu'à partir de 18h — soit 1h de retard), `FOREBET_TZ_OFFSET_HOURS`
est mis à **1**.

⚠️ Cette valeur est une première estimation calibrée sur ce que tu as
observé, pas une certitude absolue — je n'ai pas d'accès direct au flux
Forebet en temps réel pour la confirmer moi-même.

## Vérification / recalibrage

Un nouveau log a été ajouté dans `collect_window()` :
```
[web_collector] Calibration horaire — now(UTC)=..., FOREBET_TZ_OFFSET_HOURS=1, fenêtre appliquée=[..., ...]
```
Au prochain `/scan`, compare le coup d'envoi affiché par le bot pour un
match que tu connais à l'heure réelle affichée sur forebet.com pour ce même
match :
- Si ça correspond maintenant → c'est calibré, rien à faire.
- Si l'écart persiste ou s'inverse → ajuste `FOREBET_TZ_OFFSET_HOURS` (env
  var, pas besoin de recompiler) en conséquence : +1 de plus si le décalage
  est encore dans le même sens, ou une valeur négative s'il s'est inversé.
