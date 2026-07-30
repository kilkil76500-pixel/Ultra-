# Football Intelligence Bot — Changelog V17

## Vue d'ensemble

La V17 ajoute une couche d'**intelligence narrative** propulsée par **OpenAI GPT** sur
le moteur statistique V16, sans rien casser. Toutes les fonctionnalités GPT sont
**optionnelles** : si `OPENAI_API_KEY` n'est pas défini, le bot V16 fonctionne
exactement comme avant.

---

## 1. Narration des pronostics ⭐⭐⭐⭐⭐ `engine/gpt.py` + `bot.py` — NOUVEAU

### Avant (V16)
Le bot sortait des chiffres bruts : `67% domicile, xG 1.8–1.1, BTTS 58%`.

### Maintenant (V17)
Après chaque analyse, GPT génère automatiquement un **paragraphe narratif en français**
qui explique le pronostic en termes humains :

```
🤖 Analyse GPT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Arsenal arrive dans ce match avec une dynamique offensive dominante,
comme en témoigne son xG de 1.82 — bien au-dessus de la moyenne de
la ligue. Chelsea, en revanche, a récemment adopté un bloc bas qui
réduit les espaces : le modèle anticipe un match fermé en première
mi-temps. La probabilité BTTS de 58% reflète la capacité des Gunners
à forcer des situations dangereuses même contre des défenses regroupées.
Le scénario le plus fréquent dans les 100 000 simulations est une
victoire 2–1 d'Arsenal, portée par leurs ailiers en contre.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Données transmises à GPT :** probabilités 1X2, xG V16, BTTS, O/U 2,5,
style tactique (tactical.py), scénarios Monte-Carlo V5, blessures, H2H.

---

## 2. Analyse post-résultat ⭐⭐⭐⭐ `engine/gpt.py` + `bot.py` — NOUVEAU

### Avant (V16)
Après `/resultat 42 2-1`, le bot affichait ✅/❌ et un score de Brier.

### Maintenant (V17)
GPT génère une **explication en langage naturel** de l'erreur (ou du succès),
avec détection de patterns sur les erreurs récentes :

```
🤖 Analyse GPT post-résultat
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Le modèle surestimait l'attaque de Lyon pour ce type de déplacement :
avec seulement 0.9 xG réalisé, l'équipe a reproduit exactement le
schéma observé sur ses 3 derniers déplacements difficiles. L'analyse
tactique V16 avait bien détecté un style "counter_attack" pour Lyon,
mais le facteur "jeu défensif de l'adversaire à domicile" a été
sous-pondéré. Pour les prochains matchs de Lyon en déplacement contre
un bloc haut, il sera pertinent de pénaliser davantage leur xG.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 3. Interface conversationnelle `/ask` ⭐⭐⭐⭐⭐ `engine/gpt.py` + `bot.py` — NOUVEAU

Plutôt que de filtrer des tableaux, tu poses des questions en langage naturel :

| Question | Exemple de réponse |
|---|---|
| `/ask Quels matchs HIGH ce week-end ?` | Liste des pronostics HIGH en attente |
| `/ask Résume mes erreurs du mois` | Analyse narrative des 20 dernières erreurs |
| `/ask Quelle est ma précision sur la Ligue 1 ?` | Stats filtrées par ligue |
| `/ask Quelle est mon équipe la mieux prédite ?` | Classement des équipes |

GPT interroge la base SQLite en temps réel et répond en texte structuré.

**Commande Telegram :** `/ask <question>`

---

## Commandes Telegram V17

| Commande | Description |
|---|---|
| `/ask <question>` | Interface conversationnelle GPT (V17) |

Toutes les commandes V16 restent disponibles et inchangées.

---

## Configuration

### Variable d'environnement requise
```
OPENAI_API_KEY=sk-...      # Clé OpenAI (optionnelle — GPT désactivé si absente)
```

### Modèle utilisé
`gpt-4o-mini` — rapide, peu coûteux, suffisant pour de la narration sportive.

### Coût estimé
- Narration pronostic : ~300 tokens/analyse ≈ $0.00045
- Analyse post-résultat : ~350 tokens ≈ $0.00052
- Question /ask : ~400 tokens ≈ $0.00060

---

## Fichiers modifiés

| Fichier | Changement |
|---|---|
| `engine/gpt.py` | **NOUVEAU** — module OpenAI (narration, erreur, chat) |
| `config.py` | Ajout de `OPENAI_API_KEY` (optionnel, avec validation douce) |
| `bot.py` | Narration GPT après pronostic · Analyse après `/resultat` · `/ask` |
| `requirements.txt` | Ajout `openai>=1.54.0` |

## Aucun changement (rétrocompatibilité)

Tous les fichiers V16 sont conservés intacts :
- `engine/xg_v16.py`, `engine/montecarlo_v5.py`, `engine/tactical.py`
- `engine/confidence_v2.py`, `engine/team_memory.py`, `engine/learning_v2.py`
- `engine/predictor.py`, `engine/tracking.py`, et tous les autres

## Compatibilité

- **100% rétrocompatible** avec la V16.
- Sans `OPENAI_API_KEY`, le bot se comporte exactement comme la V16.
- La commande `/ask` affiche un message d'information si GPT n'est pas configuré.
- Aucune erreur n'est propagée depuis le module GPT — tous les échecs sont silencieux.
- Le modèle `gpt-4o-mini` est utilisé par défaut (remplaçable via le code).

## Comparaison V15 → V16 → V17

| Aspect | V15 | V16 | V17 |
|---|---|---|---|
| xG | V13 (1 facteur) | V16 (7 facteurs) | V16 (inchangé) |
| Monte-Carlo | V4 (Poisson direct) | V5 (scénarios) | V5 (inchangé) |
| Confiance | HIGH/MEDIUM/LOW | Score /100 + grade | Score /100 + grade |
| Tactique | — | detect_style() | detect_style() |
| Mémoire équipes | — | team_memory.py | team_memory.py |
| Narration | — | — | **GPT** ✨ |
| Analyse erreurs | Chiffres | Chiffres | **GPT narratif** ✨ |
| Interface Q&A | — | — | **/ask** ✨ |
