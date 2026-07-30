# V20.6 — Point 1 : anomaly.py aligné sur league_calibration ; Point 2 : lisibilité du message

## Point 1 — La note de tier était figée, indépendante de ce que league_calibration mesure

`anomaly.league_info_chaos_note()` affichait "Tier 3, données moins
fiables" comme une hypothèse fixe, sans jamais regarder si
`engine.league_calibration` (V20.5) avait déjà mesuré une vraie pénalité
de confiance pour ce tier précis. `engine.leagues.classify()` peuple
pourtant déjà `info.confidence_penalty` avec la valeur apprise quand elle
existe — cette fonction se contentait de l'ignorer.

**Corrigé** : la note compare maintenant la pénalité active à la
constante par défaut (`_TIER_CONF_PENALTY`). Si elles diffèrent (une
mesure existe), le message le dit explicitement et donne le vrai chiffre
mesuré — dans les deux sens (confiance réduite OU revue à la hausse, pas
uniquement "c'est pire"). Sinon, comportement identique à avant (tier >= 2
uniquement, pas de nouveau bruit tant que rien n'est mesuré).

Vérifié : les 2 tests existants (tier 1 → rien, tier 3 non mesuré →
message par défaut) toujours au vert, + 2 nouveaux tests couvrant le cas
mesuré (à la baisse et à la hausse) — rejoués à la main, 9/9 passent au
total sur `test_anomaly.py`.

## Point 2 — Le message devenait illisible sur un match à signaux multiples

Recherché dans les vrais matchs en cache le cas le plus chargé : un match
Tier 3 cumulant 5 signaux (fiabilité historique + dispersion des
scénarios + note de tier + boost du nul + décalage marginal/modal) les
affichait tous à plat sous un seul "🔍 ANOMALIES DÉTECTÉES", sans
distinction visuelle entre "sois prudent" (1 seul des 5, en réalité) et
"ces deux chiffres corrects ne se contredisent pas vraiment" (les 4
autres). Un utilisateur pressé pouvait raisonnablement lire "5 alertes" et
perdre confiance dans tout le pronostic.

**Corrigé** : split par sévérité (`warning` vs `info`, déjà présentes
dans `AnomalyFlag`, rien à ajouter côté détection) en deux blocs
distincts :
- `⚠️ SIGNAUX DE FIABILITÉ` — uniquement les vrais signaux de prudence
  (taux d'échec historique, écart cote/modèle).
- `ℹ️ NOTES EXPLICATIVES` — avec un sous-titre explicite ("rien
  d'alarmant") pour tout le reste.

Nouveaux champs additifs sur `PredictionResult` : `anomaly_warnings`,
`anomaly_notes` (l'ancien `anomaly_messages`, liste à plat, reste
inchangé pour compatibilité). `formatting.py` se replie sur l'ancien
rendu à liste unique si jamais ces deux nouveaux champs ne sont pas
peuplés (vérifié en isolant la logique de branchement, sans dépendance
Telegram, sur les 5 combinaisons possibles).

## Vérifié

- `test_anomaly.py` : 9/9 (harnais monkeypatch/tmp_path reconstruit à la
  main, comme d'habitude — pas de pytest disponible ici).
- Pipeline complet rejoué sur 40 vrais matchs : 40/40 sans erreur, et sur
  chacun, `anomaly_warnings ∪ anomaly_notes == anomaly_messages` vérifié
  explicitement (aucun message perdu ou dupliqué dans le split).
- Le cas le plus chargé trouvé dans les vrais snapshots (Remo PA vs
  Vitória, Tier 3, 5 signaux) confirme le résultat concret : 1 ligne sous
  "SIGNAUX DE FIABILITÉ", 4 sous "NOTES EXPLICATIVES" avec le sous-titre
  rassurant — au lieu de 5 lignes indifférenciées.
