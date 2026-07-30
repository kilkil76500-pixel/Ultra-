# Dépendances entre fichiers versionnés

Ce document explique pourquoi les fichiers « ancienne version » coexistent avec les versions récentes
et **ne doivent pas être supprimés** sans vérification préalable.

---

## montecarlo.py  ←  montecarlo_v5.py

| Fichier | Rôle | Utilisé par |
|---|---|---|
| `engine/montecarlo.py` | Définit `MonteCarloResult` (dataclass de base) + `_poisson_draw` | `engine/scenarios.py`, `engine/predictor.py` (import direct) |
| `engine/montecarlo_v5.py` | Simulation complète V5 par scénarios (Early Goal, Red Card…) | `engine/predictor.py`, `bot.py` |

**Pourquoi les deux coexistent :**  
`montecarlo.py` est toujours importé par `scenarios.py` et par `predictor.py` (pour `MonteCarloResult`).
`montecarlo_v5.py` étend le moteur sans supprimer la compatibilité ascendante.
→ Supprimer `montecarlo.py` casserait `scenarios.py`.

**V19 — ne pas dédupliquer `_poisson_draw`/Dixon-Coles entre les deux :**  
Ce sont deux implémentations *intentionnellement* différentes, pas des
doublons de copier-coller. `montecarlo._poisson_draw` borne lambda à 30 en
dur ; `montecarlo_v5._poisson_draw` utilise ses propres `_LAMBDA_MIN`/
`_LAMBDA_MAX`. `montecarlo._dixon_coles_tau` prend un `rho` constant avec une
formule additive ; `montecarlo_v5._dc_tau` dépend en plus des lambdas
simulées avec une formule multiplicative. Un refactor qui les fusionnerait
« pour supprimer la duplication » changerait silencieusement le comportement
de l'un des deux moteurs — voir les docstrings de tête de chaque fichier.

---

## learning.py  ←  learning_v2.py

| Fichier | Rôle | Utilisé par |
|---|---|---|
| `engine/learning.py` | `LearningReport` + `analyse_errors` (erreurs globales) | `engine/learning_v2.py`, `engine/calibration.py`, `bot.py` |
| `engine/learning_v2.py` | `LearningReportV2` + `analyse_v2` (segmenté par ligue/équipe/mois) | `bot.py` |

**Pourquoi les deux coexistent :**  
`learning_v2.py` **importe** `learning.py` (il réutilise `LearningReport` comme sous-structure).
`calibration.py` importe aussi `learning.py` directement.
→ Supprimer `learning.py` casserait `learning_v2.py` et `calibration.py`.

---

## confidence_v2.py (pas de v1 visible)

`confidence_v2.py` est la seule version du module de confiance dans ce dépôt.
Il n'y a pas de `confidence.py` v1 — la numérotation `_v2` reflète la version
du **système de confiance** (qui a évolué en même temps que xG V16),
pas la présence d'un fichier ancêtre.

---

## Règle pour les futures refactorisations

Avant de supprimer un fichier "ancien" :

1. `grep -r "from engine.X" bot/`  pour trouver tous les imports
2. `grep -r "import engine.X" bot/`  idem
3. Vérifier aussi les imports **à l'intérieur des fonctions** (lazy imports)
4. Si le fichier est encore importé → le conserver ou consolider en une seule étape

---

*Mis à jour : juillet 2026*
