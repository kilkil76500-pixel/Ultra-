# V19.2 — Cohérence pronostic 1X2 / scénario principal / O-U

## Corrigé (bug de cohérence réel, pas cosmétique)

Symptôme signalé : le rapport peut afficher **"Pronostic du bot : Nul"** en
tête, puis plus bas dans 🔮 **SCÉNARIOS**, un "scénario principal" à un score
comme **2-1** (qui n'est pas un nul), pendant que le marché Over/Under penche
vers **Under** — trois signaux qui semblent se contredire dans le même
rapport.

### Cause

Deux calculs indépendants, jamais réconciliés à l'affichage :

- `predictor.py` décide du pronostic 1X2 (`predicted_outcome`) à partir des
  probabilités 1X2, **après amplification calibrée de la probabilité de nul**
  (`draw_detection_factor = 1.45`, en place depuis la V17 pour corriger un
  bot qui ne prédisait jamais de nul). Cette amplification n'affecte QUE la
  décision affichée — jamais les probabilités elles-mêmes, ni la simulation.
- `scenarios.py` (🔮 SCÉNARIOS) recalculait de son côté un "vainqueur prédit"
  par simple argmax des probabilités **brutes, non amplifiées** — donc
  potentiellement différent du pronostic déjà affiché juste au-dessus — et
  choisissait pour "scénario principal" le scoreline isolé le plus fréquent
  de la simulation, sans aucun lien avec quelque pronostic que ce soit.

Le "Nul" affiché et le "2-1" affiché étaient donc chacun corrects
individuellement (le nul cumule plusieurs scorelines à faible probabilité
chacun ; 2-1 est simplement le scoreline unique le plus fréquent — un fait
statistique réel, pas un bug de calcul), mais leur juxtaposition sans lien
donnait l'impression d'un bot qui se contredit.

### Changement

Aucune probabilité, aucune simulation Monte-Carlo, aucune calibration n'est
modifiée. Seul l'**ordre d'affichage** est corrigé :

- `scenarios.build_scenarios()` accepte deux nouveaux paramètres optionnels,
  `predicted_outcome` et `ou25_yes` — le pronostic 1X2 et O/U **déjà décidés**
  par le prédicteur. Quand fournis, tout le bundle (favorable / défavorable
  / score principal) s'aligne dessus au lieu de recalculer un pronostic
  indépendant.
- Le "scénario principal" n'est plus systématiquement `top_scores[0]` : on
  choisit désormais, parmi les scorelines déjà classés par probabilité, le
  plus probable qui **ne contredit pas** le pronostic 1X2 affiché (et, à
  égalité de pertinence, qui ne contredit pas non plus O/U). Si aucun des
  10 scorelines les plus fréquents ne correspond au pronostic (cas limite),
  on retombe sur le scoreline global le plus probable comme avant, mais le
  narratif le signale désormais explicitement au lieu de laisser une
  contradiction silencieuse.
- `scanner.plausible_scenarios_for()` transmet maintenant
  `pred.predicted_outcome` et `pred.ou25_yes` à `build_scenarios()`.

Comportement inchangé pour tout appelant qui ne fournit pas ces deux
nouveaux paramètres (rétrocompatibilité testée).

## Tests

- `engine/tests/test_scenarios.py` (nouveau, 5 tests) : verrouille le cas
  signalé (pronostic "Nul" + score principal cohérent, jamais une victoire),
  le cas combiné avec O/U, l'alignement du bundle complet sur le pronostic
  fourni, le repli avec note explicite quand aucun scoreline ne correspond,
  et la compatibilité ascendante sans `predicted_outcome`.
