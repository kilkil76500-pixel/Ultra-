# V20.2.1 — Correction barre de progression étape 3

## Le problème

Pendant le scan (étape 3/3 — enrichissement des données match par match),
la barre de progression restait bloquée à ~50 % quelle que soit l'avancée
réelle, puis sautait directement à 100 % en fin d'étape.

## Cause

`web_collector` émet des labels de la forme :

```
Étape 3/3 — 12/24 · PSG – Lyon (+8 stats étendues)
```

La fonction `_parse_scan_label` (dans `bot.py`) extrayait correctement
les noms d'équipes (`PSG`, `Lyon`) depuis ce label, mais **pas le compteur
`12/24`** (matchs traités / total).

Le compteur n'était récupéré que pour deux formats :
- `validation : 5/24` (étape 2 — regex `:\s*(\d+)/(\d+)`)
- `18 matchs` (étape 1 — regex `(\d+)\s+match`)

Aucun des deux ne correspondait au format `— 12/24 ·` de l'étape 3. La
variable `processed` restait donc à 0 pendant toute l'étape 3, et
`_build_scan_progress` calculait :

```
pct_val = 50 + int((0 / total) * 48) = 50
```

… jusqu'à la fin, où `pct_val` passait à 100.

## Corrigé

Ajout d'un bloc conditionnel dans `_parse_scan_label` (étape 3 non terminée
uniquement) avec la regex `—\s*(\d+)/(\d+)\s*·` :

```python
if info["step"] == 3 and not info["done"]:
    m = _re.search(r"—\s*(\d+)/(\d+)\s*·", label)
    if m:
        info["processed"] = int(m.group(1))
        info["total"]     = int(m.group(2))
```

Ce regex cible exclusivement le séquence `— X/Y ·` spécifique à l'étape 3 :
il ne peut pas matcher `3/3` (pas de tiret en-tête ni de `·` en suffixe),
ni aucun label d'étape 1 ou 2.

## Résultat

La barre de progression avance maintenant de 50 % à 98 % au fil de
l'enrichissement (1 match traité → ~52 %, 12/24 → ~74 %, 24/24 → ~98 %),
puis passe à 100 % à la fin.

## Tests

Deux nouveaux cas paramétrés ajoutés dans
`engine/tests/test_bot_commands.py::test_scan_progress_label_parser` :

| Label | processed | total | home | away |
|---|---|---|---|---|
| `Étape 3/3 — 12/24 · PSG – Lyon (+8 stats étendues)` | 12 | 24 | PSG | Lyon |
| `Étape 3/3 — 1/24 · Arsenal – Chelsea (+5 stats étendues)` | 1 | 24 | Arsenal | Chelsea |

Non-régressif : les 3 cas existants (étapes 1, 2, Terminé) passent inchangés.

## Suite complète

228 tests passent · couverture 58.53 % (seuil 55 %)
