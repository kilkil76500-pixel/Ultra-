# V20.8 — Audit du H2H : un signal calculé mais jamais branché, trouvé en creusant

## Ce qui a été trouvé

`engine/h2h.py::compute_h2h_weight()` calcule `home_factor`/`away_factor`
— des multiplicateurs de lambda pondérés par récence et ratio de buts,
bornés [0.88, 1.12] — que son propre docstring décrit explicitement comme
destinés à ajuster le xG. Recherche exhaustive dans tout le dépôt : **ces
facteurs n'étaient utilisés nulle part**, ni dans `predictor.py`, ni
ailleurs. Le seul canal H2H réellement actif était un mélange simple
70% moyenne ligue / 30% moyenne de buts H2H brute
(`predictor._h2h_components()`), sans tenir compte de qui a gagné chaque
confrontation ni de la récence — bien plus pauvre que ce que `h2h.py`
calcule déjà et jette.

## Un bug trouvé dans mon propre premier correctif, corrigé avant livraison

En branchant `home_factor`/`away_factor`, je les ai d'abord appliqués à
un `lam_h`/`lam_a` calculé par `_index_to_xg()` — qui s'est avéré être du
**code mort depuis la V16** (confirmé par le docstring de `xg_v16.py` :
*"Remplace le calcul `_index_to_xg()` de V13 par un modèle
multi-facteurs"*). Ce lambda n'alimentait plus rien du tout, écrasé 90
lignes plus loin par le vrai lambda (issu de `xg_v16` +
`xg_global_multiplier`) qui sert réellement à la simulation. Repéré en
testant : le premier essai ne changeait rien au xG affiché. Corrigé pour
appliquer la pondération au bon endroit, sur le lambda qui compte
vraiment.

## Ce qui a été construit

- `predictor.predict(h2h_mode=...)` — nouveau paramètre optionnel
  (`None` par défaut, comportement inchangé pour tout appelant
  existant) :
  - `None` : comportement actuel (mélange 70/30, sans home_factor/away_factor).
  - `"off"` : H2H complètement neutralisé.
  - `"weighted"` : mélange actuel + home_factor/away_factor appliqué au
    vrai lambda final.
- `engine/h2h_audit.py` (nouveau) : même architecture que
  `xg_backtest`/`strength_ablation` — snapshots via `snapshot_json`
  (repli cache), split chronologique calibration/holdout, purement
  diagnostique (n'applique jamais rien).
- Nouvelle commande `/audith2h`.

## Vérifié

- Test de fumée sur un vrai match (10 confrontations H2H,
  home_factor=0,886) : le mode `weighted` fait maintenant baisser le xG
  domicile de 2,090 à 1,850 (cohérent avec 2,090×0,886≈1,852) — confirme
  que la pondération s'applique réellement, contrairement au premier essai.
- Pipeline complet (mode par défaut, sans aucun override) rejoué sur 40
  vrais matchs après ces changements : 40/40 sans erreur — aucune
  régression sur le comportement de production.
- Cohérence commande/aide : 24 commandes, `/audith2h` enregistrée, câblée,
  documentée.

## Résultat de l'audit réel sur les 88 matchs (61 calibration / 27 holdout)

| Mode | Calibration | Holdout |
|---|---|---|
| Actuel (mélange 70/30 seul) | 58,5% | 39,5% |
| H2H désactivé | 57,9% | 40,7% |
| Mélange + home_factor/away_factor | 55,2% | **42,0%** |

**Lecture honnête :** le mode `weighted` (le signal jusqu'ici inutilisé)
montre la meilleure précision holdout des trois — mais aussi la pire en
calibration. Direction incohérente entre les deux lots, comme pour
"défense adverse" dans l'audit de l'indice de force (V20.7) : plus
probablement du bruit qu'un vrai effet à ce stade. Le mode "H2H
désactivé" fait légèrement mieux que l'actuel sur le holdout aussi — sur
27 matchs, ça représente environ 1 match d'écart, largement dans la
marge de bruit.

**Un facteur d'incertitude supplémentaire découvert en comparant deux
mesures qui auraient dû concorder :** `montecarlo_v5.py` utilise le
module `random` de Python **sans seed fixe** — chaque appel à
`predict()` tire ses 50 000 simulations indépendamment. Deux mesures de
référence (celle de `/auditforce` en V20.7 et le mode "Actuel" de cet
audit) qui auraient dû être identiques diffèrent de 0,6 point
(57,9% vs 58,5%) — uniquement du bruit d'échantillonnage Monte-Carlo,
pas un changement de comportement. Ça n'invalide aucune conclusion tirée
jusqu'ici (chaque audit compare toujours ses modes/composantes entre eux,
dans le même run), mais ça s'ajoute à la prudence déjà de mise sur la
taille d'échantillon : ne jamais comparer un chiffre d'un run à un
chiffre d'un autre run, seulement les écarts mesurés au sein d'un même
run.

**Aucune conclusion à appliquer maintenant** — comme pour `/auditforce`
et `/backtestxg`, c'est un signal à surveiller (le mode `weighted` reste
la piste la plus prometteuse, vu que c'est le plus riche en information),
pas une preuve suffisante avec seulement 27 matchs au holdout.
