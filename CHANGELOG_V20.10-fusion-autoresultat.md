# V20.10 (fusion 2) — /autoresultat réécrit, bug de contamination trouvé et corrigé

## Contexte

Une archive uploadée (`football-intelligence-v20_10-fusion-portable-1.zip`)
s'est révélée être ma propre V20.10 (déjà livrée) + un correctif
indépendant de `/autoresultat`, déjà réconciliée par une session distincte
(son propre `CHANGELOG_V20.10-fusion.md` documente cette réconciliation).
Vérifié par diff complet : seuls `engine/web_collector.py`,
`engine/tests/test_web_collector.py`, `bot.py` (2 libellés de diagnostic)
et `VERSIONS.md` différaient de ma V20.10 — tout le reste (mes ajouts
coherence/snapshot_json/league_tier/backtestxg/auditforce/audith2h ainsi
que leur propre cote/modèle) déjà identique.

## Le correctif /autoresultat d'origine

`/autoresultat` revisitait individuellement la fiche de chaque pronostic
en attente (une requête HTTP par match), avec un repli par mots-clés
("terminé"/"finished"/"FT" à proximité d'un nombre) qui a produit des
scores absurdes en production (ex. "13-30"). Réécrit pour scraper une
page dédiée (`FOREBET_FINISHED_URL`) listant tous les matchs déjà marqués
"FT" par Forebet lui-même — une seule requête, le statut "terminé" décidé
par la source plutôt que deviné côté bot.

## Bug trouvé en vérifiant indépendamment (avant fusion)

En testant `_extract_finished_score()` avec un HTML construit moi-même
(pas leur fixture) — deux matchs voisins, le premier terminé (FT), le
second encore en direct (67e minute, pas de FT) — le score du match EN
DIRECT était renvoyé comme final :

```
match en cours (999222), score renvoyé : (1, 0)   <- devrait être None
```

Cause : le garde-fou "un statut FT doit être trouvé dans les ~400
caractères précédents" ne bornait pas sa recherche à l'ancre du match
PRÉCÉDENT. Sur une page dense (deux blocs de match à moins de 400
caractères l'un de l'autre — vérifié : 336 caractères dans mon cas), le
"FT" d'un match voisin peut être capté à tort pour un autre match encore
en direct. C'est exactement le type de bug que ce correctif visait à
éliminer (score en cours pris pour un score final), réintroduit par un
chemin différent.

## Corrigé (avant d'intégrer)

`_extract_finished_score()` : la fenêtre de recherche du "FT" est
maintenant bornée à la dernière ancre `getFTEvents` d'un AUTRE match
trouvée dans les 400 caractères précédents (si elle existe) — jamais
au-delà. Un test de non-régression dédié ajouté
(`test_extract_finished_score_never_leaks_ft_from_preceding_match`).

## Vérifié après correction et fusion

- `_extract_finished_score()` retestée sur mon cas adversarial (corrigé)
  ET sur leur fixture d'origine (toujours correcte) : les deux passent.
- **13/13 tests `test_web_collector.py`** rejoués à la main (dont le
  nouveau test de non-régression).
- **37/37 tests au total** rejoués (coherence + league_calibration +
  anomaly + web_collector).
- Signature de `collect_cached_results(snapshots, predictions)` vérifiée
  compatible avec son unique appelant dans `bot.py` (appel positionnel
  inchangé).
- Sur tes vraies données (62 snapshots réels du 28/07, empreinte SHA-256
  de `predictions.db` vérifiée inchangée avant/après) :
  - `analyse_snapshot()` rejoué : 62/62 sans erreur.
  - `/autoresultat` simulé sur du HTML réaliste (match "FT" avec score
    mi-temps différent du score final) : score final correctement
    identifié et réglé, mi-temps jamais confondue avec le score final.

## Non revérifié ici

`pytest -q` complet (pas d'accès réseau) — comme pour toutes les fusions
précédentes. Le taux de couverture réel des cotes ET du statut "FT" en
scan Playwright complet (toutes ligues, page réelle) reste à confirmer
au prochain déploiement — signalé également dans les changelogs
d'origine V20.9/V20.10.
