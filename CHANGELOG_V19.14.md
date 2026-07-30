# V19.14 — La confiance devient un vrai signal, /recalibrer cesse de planter, /apprentissagev18 cesse de régresser

## Demande traitée

Rendre le bot capable de s'auto-améliorer réellement (pas de régresser),
sans API externe (contrainte budget — Forebet uniquement), en corrigeant
le score de confiance qui ne distinguait pas les bons pronostics des
mauvais, et en vérifiant que /apprentissagev18 est bien relié à la même
logique d'amélioration que le reste du bot.

Cette version part d'un audit complet du pipeline confiance → tracking →
calibration → auto-apprentissage. Trois bugs concrets ont été trouvés et
corrigés en plus des deux demandes explicites (poids du modèle, mémoire
d'équipe).

---

## 1. Bug critique trouvé en testant : `/recalibrer` plantait systématiquement

`engine/auto_learning._simulate()` déballait 10 champs par ligne alors que
`_load_settled_rows()` en fournit 11 (le 11e — `predicted_outcome`, toujours
`NULL` dans ce chargeur précis — a été ajouté à une version antérieure sans
mettre à jour cette fonction). Résultat : dès qu'il y avait assez de
prédictions réglées pour tenter un backtest, `run_auto_learning()` levait un
`ValueError` non rattrapé. Reproduit et confirmé sur les 88 prédictions
réglées réelles du zip fourni, avant tout autre changement.

**Corrigé** : déballage à 11 champs dans `_simulate()` et
`_propose_confidence_thresholds()` (nouvelle fonction, voir §3). Revérifié
sur les mêmes données réelles : `/recalibrer` s'exécute maintenant de bout
en bout et rejette correctement un candidat qui régressait.

## 2. Le score de confiance suivi n'était pas celui qu'on croyait

`confidence_v2.py` calcule depuis longtemps un score à 7 composantes
(qualité des données, cohérence Forebet, stabilité de forme, blessures,
variance Monte-Carlo, chaos, H2H). Mais `confidence_pct`/`confidence_label`
— le SEUL signal réellement suivi dans `/fiabilite` et backtesté par
`/recalibrer` — venait d'une fonction séparée et beaucoup plus pauvre,
basée presque uniquement sur le nombre de matchs joués cette saison. Le
score riche n'était affiché qu'en second ("Grade V18"), sans le moindre
effet sur le tri HIGH/MEDIUM/LOW réellement mesuré. C'est la cause directe
mesurée sur les données réelles : HIGH jamais atteint, MEDIUM (44,2%) et
LOW (42,2%) quasi identiques.

**Corrigé** (`engine/predictor.py`) : `confidence_pct` = le score riche
`confidence_v2.score`. Le "Grade" affiché dans `/predict` (`engine/formatting.py`)
n'est plus un second chiffre concurrent, juste le détail du même score.

## 3. Le seuil de confiance ne pouvait que monter, jamais descendre

`compute_calibration()` (engine/calibration.py) ne fait que remonter
`confidence_high_threshold` quand il détecte une surconfiance — aucun
mécanisme ne le redescend si le palier HIGH se retrouve vide. Avec le point
2 corrigé, l'ancien seuil (60%) n'avait plus aucune raison objective d'être
le bon.

**Ajouté** (`engine/auto_learning.py`) :
- `_propose_confidence_thresholds()` — recherche par grille (pas de 2 points,
  30 à 85) sur le lot **calibration** uniquement, qui choisit la paire
  (seuil HIGH, seuil MEDIUM) maximisant l'écart de précision HIGH−LOW, à
  condition que chaque palier compte au moins 8 échantillons. Si aucune
  paire n'est mesurable, les seuils actuels sont conservés (le silence est
  le comportement sûr).
- Le résultat passe par le **même** backtest holdout que tout le reste —
  s'il ne tient pas sur des données jamais vues, il est rejeté comme
  n'importe quel autre changement.
- Nouveau signal de régression dans `_check_regressions()` : la
  discrimination HIGH−LOW du candidat ne doit pas être pire que celle de
  la config active (comparée seulement quand les deux sont mesurables).

## 4. Mémoire d'équipe : le signal de risque que tu décrivais, câblé

`engine/team_memory.py` existait déjà (traits tactiques par équipe), mais
`update_from_result()` n'était appelé **nulle part** — `/memoire` était
vide depuis toujours.

**Ajouté** :
- `TeamMemoryProfile.model_predictions_seen` / `model_correct_1x2` →
  propriété `model_error_rate` (neutre sous 5 échantillons, jamais de bonus,
  seulement une pénalité une fois qu'on sait).
- `TeamMemoryManager.record_model_outcome()`, appelé automatiquement depuis
  `engine.tracking.settle()` — donc à chaque `/resultat` ou `/autoresultat`,
  sans dépendre d'un appelant pour s'en souvenir.
- `confidence_v2.py` : nouvelle pénalité "mémoire équipe" (0 à −8 pts) basée
  sur `max(home_team_risk, away_team_risk)`.
- `/memoire` affiche désormais un trait "difficile à prévoir" / "bien cerné"
  quand l'historique est suffisant.

Purement interne — aucune source externe à Forebet, comme demandé.

## 5. Incohérence de mesure entre `/resultat` et `/fiabilite`

`tracking.settle()` recalculait le 1X2 par argmax naïf sur les probabilités
brutes (ignorant `draw_detection_factor`) et comparait BTTS/O2.5 à un seuil
fixe de 0.5 — alors que la décision réellement affichée à l'utilisateur
utilise `draw_detection_factor` et les seuils calibrés (0.65/0.58
actuellement). Le message affiché juste après `/resultat` pouvait donc
contredire ce que `/fiabilite` calculait ensuite pour la même ligne.

**Corrigé** : `settle()` et `calibration_report()` utilisent maintenant le
`predicted_outcome` stocké (retombent sur l'argmax naïf uniquement pour les
lignes antérieures à V19.12, `NULL`) et les seuils BTTS/O2.5 réellement
actifs via `calibration.load_calibration()`.

## 6. `/apprentissagev18` était un générateur de régression

`apply_v18_calibration()` écrivait **directement** dans `calibration.json`,
sans le moindre backtest :
- La plupart des champs (`btts_threshold`, `ou25_threshold`, seuils de
  confiance, multiplicateurs) venaient d'un **instantané figé** d'une
  mesure ponctuelle passée sur 88 matchs (`V18CalibrationValues()`), pas
  d'un calcul sur les données actuelles — malgré le message affiché
  prétendant "Corrections V18 appliquées ✅".
- `draw_detection_factor` était **forcé à 1.45 à chaque exécution**,
  écrasant silencieusement toute valeur que `/recalibrer` aurait validée
  entre-temps par holdout.
- `run_v18_analysis()` évaluait lui-même l'exactitude passée avec ces mêmes
  constantes figées (1.45 / 0.56 / 0.54) plutôt qu'avec la calibration
  réellement active — un rapport mesurant un bot qui n'existe plus.

**Corrigé** :
- `run_v18_analysis()` charge maintenant la calibration active
  (`draw_detection_factor`, seuils BTTS/O2.5) pour son calcul de précision.
- `apply_v18_calibration()` n'écrit plus rien. Seul `xg_global_multiplier`
  reste calculé depuis les données (il ne peut pas être backtesté a
  posteriori — il agit avant la simulation Monte-Carlo, voir le
  commentaire déjà présent dans `auto_learning.py`) ; il est maintenant
  seulement **suggéré**, à valider manuellement.
- `/apprentissagev18` (bot.py) devient une commande purement informative
  (catalogue de scénarios + biais xG) — toute recalibration passe
  exclusivement par `/recalibrer`, qui recalcule et backteste avant
  d'appliquer.

---

## Ce qui n'a PAS été changé, et pourquoi

- Les poids de `_compute_strength_index()` (predictor.py, 25/20/15/10/10/10/5/5
  points) restent réglés à la main. Les ajuster par régression demande un
  volume de données réglées nettement supérieur à 88-116 pour ne pas
  sur-ajuster au bruit — à envisager une fois ce volume atteint,
  probablement via une extension du même mécanisme holdout que
  `/recalibrer` plutôt qu'un réglage manuel de plus.
- `xg_global_multiplier` reste sous contrôle humain exclusif (limite
  documentée, pas un oubli) : aucune API externe ne fournissant de xG
  indépendant du xG déjà utilisé pour produire les probabilités
  enregistrées, il n'existe aucune façon de le rejouer a posteriori sans
  refaire tourner la simulation Monte-Carlo complète — hors de portée de
  cette passe.
- Aucun test automatisé n'a pu être exécuté dans cet environnement (pas
  d'accès réseau pour installer `pytest`). La suite existante
  (`engine/tests/test_auto_learning.py` en particulier) devrait être
  lancée après déploiement — c'est le fichier qui couvre le plus
  directement les changements de ce correctif.

## Vérification recommandée après déploiement

```
pytest engine/tests/test_auto_learning.py engine/tests/test_confidence_v2.py -v
```

Puis en usage réel : lancer `/recalibrer` une fois pour confirmer qu'il ne
plante plus et qu'il produit un rapport (accepté ou rejeté, peu importe —
l'important est qu'il s'exécute), et surveiller `/fiabilite` sur les
prochains résultats pour voir si HIGH commence à se peupler avec une
précision réellement supérieure à LOW.
