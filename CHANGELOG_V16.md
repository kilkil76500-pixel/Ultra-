# Football Intelligence Bot — Changelog V16

## Vue d'ensemble

La V16 introduit **six évolutions majeures** qui transforment le bot d'un bon
moteur statistique en un véritable assistant d'analyse footballistique.

---

## 1. Moteur xG V16 ⭐⭐⭐⭐⭐ `engine/xg_v16.py` — NOUVEAU

### Avant (V13)
Le xG était calculé uniquement via l'indice de force :
`xG = league_avg × exp(0.8 × (index − 50) / 50)`

### Maintenant (V16)
7 facteurs combinés par moyenne pondérée :

| Facteur | Poids | Description |
|---|---|---|
| Indice de force (V13) | 35% | Base compatible ascendante |
| Tirs cadrés | 20% | Efficacité offensive réelle |
| Grosses occasions | 15% | Occasions franches créées/concédées |
| Forme offensive récente | 15% | Buts marqués sur 5 derniers matchs |
| H2H | 10% | Historique des confrontations |
| Importance du match | 5% | Matchs décisifs → prudence accrue |

+ Facteurs multiplicatifs : **fatigue**, **tactique** (tactical.py), **blessures**

**Résultat** : le xG reste réaliste (jamais > 3.20) mais reflète
vraiment l'état actuel des équipes. Finies les prédictions 5-5.

---

## 2. Monte-Carlo V5 ⭐⭐⭐⭐⭐ `engine/montecarlo_v5.py` — NOUVEAU

### Avant (V4)
Chaque simulation tirait directement `Poisson(λ)`.

### Maintenant (V5)
Chaque simulation commence par **tirer un scénario de match** :

| Scénario | Freq. | Description |
|---|---|---|
| Normal | 52% | Match sans événement particulier |
| But précoce | 10% | Favori gère → jeu fermé |
| Jeu défensif | 8% | Favori préserve son avantage |
| Domination stérile | 7% | Peu d'efficacité devant le but |
| Carton rouge | 7% | Déséquilibre numérique |
| Remontée | 6% | L'outsider revient au score |
| Penalty | 6% | But supplémentaire aléatoire |
| Pressing intense | 4% | Match très ouvert des 2 côtés |

La distribution des scénarios est affichée dans le résultat :
```
🎲 Scénarios simulés (MC V5)
  ⚽ Match normal          52%
  ⚡ But précoce           10%
  🏰 Jeu défensif          8%
```

---

## 3. Analyse tactique automatique ⭐⭐⭐⭐ `engine/tactical.py` — NOUVEAU

Reconnaît automatiquement le style de jeu de chaque équipe :

| Style | Description | Impact |
|---|---|---|
| `offensive` | 2+ buts/match, gros volume de tirs | BTTS +6pp, O/U +10pp |
| `possession` | >58% de possession | neutre à légèrement positif |
| `pressing` | Peu de tirs concédés | BTTS +4pp |
| `counter_attack` | Peu de possession, peu de tirs | BTTS −3pp |
| `low_block` | Défense solide (<0.7 buts/match) | BTTS −10pp, O/U −12pp |
| `low_intensity` | Peu d'engagement offensif | BTTS −5pp |
| `balanced` | Style équilibré | aucun ajustement |

Les ajustements tactiques sont appliqués sur les probabilités BTTS, O/U 2.5,
et sur le xG (via xg_v16.py).

**Exemple de sortie :**
```
🧠 Analyse tactique V16
  Arsenal ⚔️ Équipe très offensive, match ouvert probable (80% confiance)
    • 🔥 Forte attaque (2.3 buts/match)
    • 🎯 Gros volume de tirs (17/match)
    → BTTS +6pp → O/U2.5 +10pp
  Chelsea 🏰 Bloc bas, défense solide (65% confiance)
    • 🛡️ Défense solide (0.6 concédés/match)
    → BTTS -10pp → O/U2.5 -12pp
```

---

## 4. Indice de confiance IA V16 ⭐⭐⭐⭐ `engine/confidence_v2.py` — NOUVEAU

Remplace le simple HIGH / MEDIUM / LOW par un **score sur 100** :

| Dimension | Max pts | Description |
|---|---|---|
| Qualité des données | 20 | Matchs joués, stats étendues |
| Cohérence des sources | 15 | Alignement Forebet / moteur |
| Stabilité des équipes | 20 | Régularité des résultats |
| Impact des blessures | 15 | Nombre d'absents |
| Variance Monte-Carlo | 15 | Convergence de la simulation |
| Chaos de la ligue | 10 | Imprévisibilité historique |
| Qualité H2H | 5 | Confrontations directes |

Grades : **A+ (90-100)** / **A (75-89)** / **B (55-74)** / **C (35-54)** / **D (0-34)**

**Exemple :**
```
🎯 Indice de confiance V16
  🟦🟦🟦🟦🟦🟦🟦🟦░░  83/100  [A]
  🟢 HIGH  |  Risque : FAIBLE
  Données solides, modèle convergent, fiabilité élevée.
```

---

## 5. Auto-apprentissage V2 ⭐⭐⭐⭐ `engine/learning_v2.py` — NOUVEAU

Extension de learning.py (V15) avec analyse segmentée :

- **Par championnat** : taux de réussite par ligue, biais domicile/extérieur
- **Par équipe** : équipes les mieux prédites par le bot
- **Par mois** : saisonnalité des erreurs

Commande Telegram : `/apprentissage2`

**Exemple de sortie :**
```
🧠 Auto-apprentissage V2
  Analyse de 47 prédictions réglées.
  Meilleure ligue : Premier League. Ligue difficile : Ligue 2.

  📋 Par championnat
  • Premier League (12 matchs) — 1X2 58% | bon taux 1X2
  • Ligue 1 (9 matchs) — 1X2 44% | surestime domicile

  📅 Par mois
  • 2026-03 (8 matchs) — 1X2 62%
  • 2026-01 (6 matchs) — 1X2 38% | ⚠️ taux 1X2 faible
```

---

## 6. Mémoire des équipes V16 ⭐⭐⭐⭐ `engine/team_memory.py` — NOUVEAU

Crée un profil évolutif pour chaque équipe, mis à jour à chaque `/resultat` :

| Trait | Description |
|---|---|
| Fragilité mentale | Perd des matchs où elle menait |
| Buteur tardif | Marque souvent après la 70e minute |
| Fort finisseur | Meilleure en 2e mi-temps |
| Difficultés contre bloc bas | Peine face aux défenses regroupées |
| Domination à domicile | Grand écart dom./ext. |
| Capacité à remonter | Revient souvent au score |

Commande Telegram : `/memoire [équipe]`

**Exemple de sortie :**
```
🧠 Mémoire des équipes V16
  Paris Saint-Germain (23 matchs en mémoire)
    🧠 Solidité mentale : gère bien ses avantages
    ⏰ Buteur tardif : marque souvent après la 70e minute
    🏠 Très fort à domicile
  Lyon (19 matchs en mémoire)
    😰 Fragilité mentale : souvent muet après avoir mené
    🏰 Difficultés contre les blocs bas
```

---

## Commandes Telegram V16

| Commande | Description |
|---|---|
| `/apprentissage2` | Rapport V2 segmenté (ligue / équipe / mois) |
| `/memoire` | Voir la mémoire de toutes les équipes |
| `/memoire PSG` | Voir le profil mémoire d'une équipe |
| `/tactique` | Infos sur les styles tactiques reconnus |

Les commandes V15 (`/apprentissage`, `/recalibrer`, `/valider`, `/versions`)
restent disponibles et inchangées.

---

## Fichiers modifiés

| Fichier | Changement |
|---|---|
| `engine/xg_v16.py` | **NOUVEAU** — moteur xG multi-facteurs |
| `engine/montecarlo_v5.py` | **NOUVEAU** — Monte-Carlo par scénarios |
| `engine/tactical.py` | **NOUVEAU** — analyse tactique automatique |
| `engine/confidence_v2.py` | **NOUVEAU** — indice de confiance sur 100 |
| `engine/team_memory.py` | **NOUVEAU** — mémoire des équipes |
| `engine/learning_v2.py` | **NOUVEAU** — apprentissage segmenté |
| `engine/predictor.py` | Intégration V16 (xG, MC V5, tactique, confiance) |
| `bot.py` | Commandes V16 + menu mis à jour |

## Aucun changement (rétrocompatibilité)

- `engine/montecarlo.py` — conservé intact (MC V4)
- `engine/scenarios.py` — conservé intact (scénarios V15)
- `engine/learning.py` — conservé intact (rapport de base V15)
- `engine/calibration.py` — conservé intact
- `engine/tracking.py` — conservé intact (base SQLite inchangée)
- `config.py` — conservé intact
- `requirements.txt` — inchangé (aucune nouvelle dépendance)

## Compatibilité

- **100% rétrocompatible** avec la V15.
- Sans `/resultat`, la mémoire des équipes reste vide → aucun impact.
- Sans données étendues, les facteurs tirs/occasions restent neutres (1.0).
- Le Monte-Carlo V5 est utilisé par défaut à la place du V4.
- L'indice de confiance V16 remplace le score V15 dans les sorties.
