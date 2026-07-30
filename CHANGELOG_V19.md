# V19 — Auto-apprentissage plus robuste, couverture de tests élargie

## Ajouté

- **Validation walk-forward multi-fenêtres** (`engine/auto_learning.py`) :
  un split unique 70/30 peut, par hasard, tomber sur une tranche récente non
  représentative et faire accepter — ou rejeter — un candidat à tort. Quand
  le holdout est assez grand (≥ 15 lignes réglées), il est maintenant
  redécoupé en 3 fenêtres chronologiques distinctes et un candidat n'est
  accepté que s'il n'est pas moins bon sur **chacune** d'elles, pas
  seulement sur l'agrégat. Vérifié sur un cas réel construit pour l'occasion :
  un candidat dont l'agrégat holdout semblait acceptable (précision 1X2
  égale, Brier légèrement meilleur) régressait en fait nettement sur la
  fenêtre la plus récente — l'ancienne logique single-split l'aurait
  accepté, la nouvelle le rejette. Avec un holdout trop petit pour former
  des fenêtres fiables, le comportement reste strictement identique aux
  versions précédentes (`n_folds == 1` = tout le holdout, aucun changement).
- **Suivi des rejets consécutifs** : `CalibrationConfig.consecutive_rejections`
  s'incrémente à chaque cycle rejeté pour cause de régression et se remet à
  0 dès qu'un candidat est accepté. Persisté sans jamais déclencher de
  nouvelle version ni d'archive (un rejet ne change aucun paramètre du
  modèle). À partir de 3 rejets consécutifs, `/apprentissage2` affiche une
  alerte explicite — signal de plafonnement du modèle ou de dérive des
  données méritant une revue humaine.
- **`engine/tests/test_formatting.py`** (nouveau) : premiers tests dédiés à
  la couche présentation Telegram (`bar`, `pct_int`, `chunk`, `divider`,
  `outcome_badge`, `kickoff`) — module le plus visible pour l'utilisateur
  final, qui n'avait jusqu'ici aucun test dédié.
- **`engine/tests/test_calibration.py`** (nouveau) : verrouille le
  comportement déjà présent mais non testé de `load_calibration()` face à
  un fichier manquant, corrompu, ou contenant des champs inconnus (mise à
  niveau future) ; ajoute les tests du nouveau compteur de rejets
  consécutifs.
- **`engine/tests/test_integration_e2e.py`** (nouveau) : test bout-en-bout
  `/scan → /predict → /resultat → /recalibrer` en cache-only, qui manquait
  totalement — chaque module était testé isolément mais rien ne garantissait
  que la chaîne complète fonctionne après un changement dans un module
  intermédiaire. Couvre aussi le cas réaliste où seule une partie des
  pronostics est réglée au moment du recalibrage.
- **`pytest.ini`** (nouveau) : seuil de couverture minimal
  (`--cov-fail-under=55`, volontairement prudent — à relever au fil des
  prochaines versions à mesure que la couverture augmente, jamais à
  baisser sans le documenter ici). Avant V19, la commande de couverture du
  README pouvait baisser silencieusement sans jamais faire échouer la CI.

## Clarifié (pas de changement de comportement)

- **`engine/montecarlo.py` / `engine/montecarlo_v5.py`** : ajout d'une note
  explicite en tête de chaque fichier (et dans `VERSIONS.md`) précisant que
  leurs fonctions `_poisson_draw` / Dixon-Coles sont *intentionnellement*
  différentes (bornes de lambda différentes, formules différentes) et ne
  doivent jamais être fusionnées "pour supprimer la duplication" — un
  examen approfondi en V19 a montré que malgré des noms et des rôles
  similaires, elles ne calculent pas la même chose, et une fusion naïve
  aurait introduit une régression silencieuse dans l'un des deux moteurs.

## Non fait (décision assumée)

- Le découpage physique de `engine/web_collector.py` (1276 lignes,
  responsabilité unique de scraping Forebet) envisagé initialement a été
  écarté pour cette version : le module n'a aucun test dédié dans la suite
  actuelle, et le séparer sans filet de tests spécifique aurait été plus
  risqué qu'utile. À reconsidérer une fois des tests ciblés sur
  `web_collector` en place.
- L'auto-apprentissage ne rejoue toujours pas `xg_global_multiplier` (limite
  documentée depuis la V18 — il agit avant la simulation Monte-Carlo et ne
  peut pas être reconstruit a posteriori à partir des seules probabilités
  enregistrées). Reste sous contrôle humain exclusif via `/recalibrerforce`.

## Tests

- `engine/tests/test_auto_learning.py` : + 4 tests (fallback single-window,
  détection walk-forward d'une régression cachée par l'agrégat, montée et
  remise à zéro du compteur de rejets consécutifs).
- `engine/tests/test_calibration.py` (nouveau, 6 tests).
- `engine/tests/test_formatting.py` (nouveau, 15 tests).
- `engine/tests/test_integration_e2e.py` (nouveau, 2 tests).

## V19.1 — Apparence, cohérence des libellés, diagnostic du scan

### Corrigé (bug d'apparence réel)

- **31 en-têtes dans `bot.py` + 3 dans `engine/formatting.py`** utilisaient des
  cadres ASCII (`╔══╗ / ║ texte ║ / ╚══╝`) envoyés en `parse_mode="HTML"`
  **sans balise `<pre>`/`<code>`**. Telegram affiche le HTML en police
  proportionnelle par défaut : ces cadres ne s'alignaient donc jamais
  correctement (le bord droit ne tombe pas au bon endroit), quel que soit
  l'appareil ou la longueur du titre. Remplacés par le style à ligne de
  séparation simple (`divider()` / `_divider()`) déjà utilisé ailleurs dans
  le bot (`/help`, `/apprentissage2`, `/fiabilite`...) — ne dépend d'aucun
  alignement de caractères, rend proprement en toutes circonstances, et
  uniformise enfin le style visuel de TOUTES les commandes.

### Renommé

- Toutes les étiquettes d'interface affichant "V16" (ANALYSE, MÉMOIRE,
  APPRENTISSAGE, SCÉNARIOS, VALIDATION HOLDOUT, INDICE DE FORCE, GRADE...)
  → V18, dans `bot.py` et `engine/formatting.py`.
- **Volontairement laissés inchangés** (exactitude technique, pas un oubli) :
  - `xG V16` : nom réel du sous-module `engine/xg_v16.py` (version propre du
    moteur xG, indépendante de la version globale du bot — même logique que
    `montecarlo_v5.py`).
  - Le commentaire `# V16 imports` et le docstring "Ancien comportement V16"
    (`/recalibrerforce`) : repères historiques exacts décrivant un
    comportement d'une version passée, pas des étiquettes à mettre à jour.
  - `/apprentissage2` affiche "APPRENTISSAGE V2" — nom lié au module
    `learning_v2.py`, distinct de la numérotation générale.

### Diagnostic — pourquoi /scan ne retient parfois qu'une fraction des
matchs disponibles

`engine/web_collector.py` ne journalisait jusqu'ici que le total final de
matchs retenus, sans jamais dire où les autres étaient perdus. Ajouté :

- Un résumé de diagnostic loggé à la fin de chaque `/scan` :
  `[web_collector] Forebet today: N lien(s) de match détecté(s) -> M
  retenu(s) dans la fenêtre de Xh, Y hors fenêtre, Z rejeté(s) (raisons)`.
- Un log explicite (`WARNING`) quand un match est ignoré faute de date/heure
  exploitable dans le HTML — ce cas était auparavant totalement silencieux.
- **`config.FOREBET_TZ_OFFSET_HOURS`** (0 par défaut, comportement inchangé) :
  les horaires Forebet sont actuellement interprétés comme de l'UTC pur,
  ce qui est probablement inexact (le site a son propre fuseau par défaut,
  visible dans son sélecteur "Fuseau horaire"). Cause la plus probable de
  l'écart observé (42 matchs retenus alors que Forebet affichait ~103-120
  matchs pour la journée, confirmé par recherche web). Ajustez cette valeur
  une fois le décalage réel identifié dans les nouveaux logs.
