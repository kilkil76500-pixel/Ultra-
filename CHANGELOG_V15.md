# Football Intelligence Bot — Changelog V15

## Vue d'ensemble

La V15 introduit deux grandes évolutions indépendantes :

1. **Scénarios plausibles** — remplacement du tirage Monte-Carlo aléatoire par
   trois scénarios construits et narrativisés.
2. **Module d'apprentissage** — analyse automatique des erreurs, recalibrage
   progressif, validation holdout et historique des versions.

### Résultats automatiques

La commande `/autoresultat` réutilise à chaque appel les matchs déjà présents
dans le cache du jour après `/scan`. Elle ouvre uniquement les pages Forebet
des matchs dont le coup d'envoi est passé, puis règle seulement les pronostics
du bot dont l'URL Forebet et les deux équipes correspondent. Les pages bloquées
ou les scores finaux ambigus restent non réglés.

---

## 1. Scénarios plausibles (`engine/scenarios.py`)

### Avant (V14)
Le bot tirait un seul score aléatoire à chaque appel (« tirage aléatoire —
change à chaque appel »). Ce score n'était pas interprétable : il pouvait
correspondre à n'importe quel contexte.

### Maintenant (V15)
Le bot génère **trois scénarios construits** :

| Scénario | Description |
|---|---|
| ⭐ **Principal** | Score le plus probable issu des 100 000 tirages |
| 📈 **Favorable** | λ du favori boosté (+15 %), λ de l'adversaire réduit (−12 %) |
| ⚠️ **Défavorable plausible** | λ du favori réduit (−25 %), λ adverse boosté (+15 %) + narratif contextuel |

Le scénario défavorable est accompagné d'une explication basée sur :
- L'écart de force entre les deux équipes (indice de force V13)
- Les retournements observés en confrontation directe (H2H)
- Le niveau de chaos de la ligue
- La pression offensive potentielle de l'adversaire

**Exemple de sortie :**
```
🔮 SCÉNARIOS V15 (construits, non tirés au hasard)
────────────────────────────────────
   ⭐ PRINCIPAL
      PSG 2 – 1 Lyon  (31 %)  🏠
      💬 Score le plus fréquent dans la simulation de 100 000 tirages.

   📈 FAVORABLE
      PSG 3 – 0 Lyon  (18 %)  🏠
      💬 PSG en grande forme offensive, Lyon en difficulté défensive.

   ⚠️ DÉFAVORABLE PLAUSIBLE
      PSG 1 – 2 Lyon  (12 %)  ✈️
      💬 Scénario construit à partir de contextes similaires où PSG
         a sous-performé : faible écart de force (8 pts) ;
         2 retournements déjà observés en H2H.
```

---

## 2. Module d'apprentissage

### `engine/learning.py` — Analyse des erreurs

Interroge les prédictions réglées (base SQLite existante) et produit :
- Taux d'erreur par marché : **1X2, BTTS, Over/Under 2.5, score exact modal**
- Causes d'erreurs identifiées : surprise totale, pari risqué, biais nul,
  surestimation BTTS, sous-estimation extérieur
- Biais systématiques par bucket de confiance (HIGH / MEDIUM / LOW)
- Recommandation actionnable

Commande Telegram : `/apprentissage`

### `engine/calibration.py` — Recalibrage progressif

À partir du rapport d'apprentissage, calcule des multiplicateurs de correction :
- **Seuils de confiance** (HIGH / MEDIUM) — relevés si HIGH est moins précis que MEDIUM
- **Multiplicateurs 1X2** — corrige les biais domicile / extérieur / nul
- **Seuil BTTS** — ajusté si le marché BTTS est systématiquement mal calibré

La calibration est sauvegardée dans `cache/calibration.json` et archivée
automatiquement dans `cache/calibration_history/`.

Commande Telegram : `/recalibrer` (nécessite ≥ 15 résultats réglés)

### `engine/validation.py` — Validation holdout temporelle

Sépare les prédictions réglées en deux ensembles :
- **70 % les plus anciennes** → jeu de calibration
- **30 % les plus récentes** → jeu holdout (jamais vus pendant la calibration)

Calcule accuracy 1X2, Brier Score, accuracy BTTS et Over/Under sur chaque
ensemble, et détecte les régressions (holdout < calibration − 8 %).

Commande Telegram : `/valider`

### `engine/versioning.py` — Historique des versions

Archive chaque calibration dans `cache/calibration_history/v<N>_<ts>.json`.
Permet de lister toutes les versions et de revenir à une version précédente
si la nouvelle calibration est moins performante.

Commandes Telegram :
- `/versions` — liste les versions archivées
- `/versions 3` — restaure la version v3 comme calibration active

---

## Fichiers modifiés

| Fichier | Changement |
|---|---|
| `engine/scenarios.py` | **NOUVEAU** — 3 scénarios plausibles |
| `engine/learning.py` | **NOUVEAU** — analyse des erreurs par marché |
| `engine/calibration.py` | **NOUVEAU** — recalibrage progressif |
| `engine/validation.py` | **NOUVEAU** — validation holdout |
| `engine/versioning.py` | **NOUVEAU** — historique des versions |
| `engine/scanner.py` | Ajout de `plausible_scenarios_for()` |
| `bot.py` | Remplacement de `_scenario_text` par `_scenarios_text` ; ajout des commandes `/apprentissage`, `/recalibrer`, `/valider`, `/versions` ; menu V15 |

## Aucun changement

- `engine/montecarlo.py` — non modifié (la simulation de base reste identique)
- `engine/predictor.py` — non modifié (le pipeline V13 reste intact)
- `engine/tracking.py` — non modifié (la base SQLite existante est réutilisée)
- `config.py` — non modifié
- `requirements.txt` — non modifié (aucune nouvelle dépendance)

---

## Compatibilité

- Aucun changement de schéma de base de données.
- Les prédictions existantes sont automatiquement utilisées par l'apprentissage.
- La calibration est optionnelle : sans `/recalibrer`, le bot se comporte
  exactement comme en V14 (valeurs par défaut = multiplicateurs à 1.0).
