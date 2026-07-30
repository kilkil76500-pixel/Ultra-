# V20.9 — Cotes bookmaker enfin captées : value.py et le signal cote/modèle sortent de la mort clinique

## Contexte

Trouvé en croisant les vraies données de `cache/predictions.db` (session
précédente) : les cotes n'avaient JAMAIS été scrapées — `detail.get("odds")`
retombait systématiquement sur `{}`, sur les 54 matchs réglés vérifiés sans
exception. Conséquence en cascade, jamais remarquée avant : `engine/value.py`
(analyse de value bet / EV) est un module entier resté silencieusement mort
depuis le début, et le signal "écart cote/modèle" d'`engine/anomaly.py` ne
pouvait jamais se déclencher.

## Ce qui a été trouvé et vérifié contre du HTML réel (pas deviné)

L'utilisateur a confirmé que Forebet affiche les cotes 1X2 sous un onglet
"Coef", **sur la page liste** (`pronostics-pour-aujourd-hui`), sans
rechargement d'URL au clic — donc déjà présentes dans le HTML statique
livré par le serveur (confirmé via "Afficher le code source", pas
"Inspecter" : c'est bien dans le HTML brut, pas généré après coup par du JS).

Structure confirmée sur 4 matchs réels collés par l'utilisateur :

```html
<div class="bigOnly prmod">
  <span class="lscrsp" onclick="return getHodd(this,2466434);">1.11</span>
  <div class="haodd">
    <span>15.00</span>  <!-- domicile -->
    <span>9.00</span>   <!-- nul -->
    <span>1.11</span>   <!-- extérieur -->
    <span>no</span><span>no</span><span>no</span>  <!-- non identifié, ignoré -->
  </div>
</div>
```

- Les 3 cotes sont dans un **ordre fixe** (1/X/2) — vérifié sur les 4 exemples.
- L'ID numérique dans `getHodd(this,ID)` est **le même** que le `forebet_id`
  déjà extrait par `_MATCH_HREF` (fin de l'URL du match) — jointure exacte,
  aucune position devinée. Vérifié explicitement par l'utilisateur avant
  d'écrire le code.
- Cotes indisponibles → `" - "` sur les 3 spans (match Ho Chi Minh City W
  vs Ha Noi II W dans l'échantillon) → dict vide, jamais de valeur inventée.
- Un second bloc `.la_prmod` avec `getHodd(this,ID,'lp')` (cotes LIVE,
  toujours vides avant coup d'envoi) existe séparément — le point-virgule
  juste après l'ID dans le pattern regex l'exclut naturellement (`'lp'`
  ajoute une virgule et un 3e argument avant la parenthèse fermante).

## Ce qui a été construit

- `engine/web_collector.py::_extract_odds(region, forebet_id)` — nouvelle
  fonction, même style défensif que le reste du fichier (`_extract_teams`,
  `_extract_score`) : retourne `{}` dès qu'une seule des 3 cotes est absente
  ou invalide, jamais de valeur partielle ou interpolée.
- Câblée dans `_extract_list_fixture()` (page liste, où les cotes ont été
  trouvées) — PAS dans le pipeline de la page fiche-match (`detail`),
  jamais confirmé comme les contenant.
- `_source_snapshot()` : `"odds": match.get("odds") or detail.get("odds") or {}`
  — priorité à la source confirmée, repli sur `detail` (n'écrit toujours
  rien aujourd'hui, mais sans risque si une page fiche-match s'avère un
  jour aussi les exposer).
- **Aucun changement dans `scanner.py`, `odds.py` ou `value.py`** : ces
  modules attendaient déjà exactement les clés `home_win`/`draw`/`away_win`
  dans `snapshot["odds"]` — l'intégration est purement en amont, côté
  collecte.

## Vérifié

- `_extract_odds()` testé directement contre le HTML réel fourni par
  l'utilisateur (4 matchs, dont un sans cotes) : cotes correctement
  extraites dans l'ordre 1/X/2, dict vide sur le match sans cotes, aucune
  contamination entre les blocs `.haodd` de deux matchs voisins dans la
  même région de texte.
- Pipeline complet rejoué avec des cotes au format réel : `MatchOdds.
  available` passe à `True`, le signal "écart cote/modèle" d'`anomaly.py`
  se déclenche, `value.py::analyse_value()` produit de vraies
  `BetOpportunity` avec EV calculé sur cotes réelles — les trois étaient
  jusqu'ici inatteignables en production.
- Non-régression : 40 matchs sans cotes (comportement historique) + 10 avec
  cotes réelles, rejoués ensemble via `scanner.analyse_snapshot` puis
  `record_prediction` : 50/50 sans erreur.

## Portée volontairement limitée

Seuls le marchés 1X2 (`home_win`/`draw`/`away_win`) sont couverts : c'est
tout ce que le bloc `.haodd` expose. `over25`/`under25`/`btts_yes`/
`btts_no` restent à `0.0` (non disponibles) — `value.py` continuera de ne
proposer des opportunités que sur le marché 1X2 tant qu'une source pour
ces autres cotes n'aura pas été trouvée et vérifiée de la même façon
(contre du HTML réel, jamais supposée).

## Non vérifié ici, à faire en conditions réelles

- Le HTML fourni provient d'une capture manuelle de l'utilisateur — le
  comportement en scan réel (Playwright, page complète, tous les matchs
  du jour) reste à confirmer au premier `/scan` après déploiement. Si
  `.haodd` n'apparaît pas identique sur 100% des lignes (mise en page
  Forebet non uniforme selon le sport/la ligue, par ex.), `_extract_odds`
  est conçu pour retourner `{}` proprement plutôt que planter — mais le
  taux de couverture réel (quelle proportion de matchs a des cotes vs `-`)
  n'est mesurable qu'en production.
