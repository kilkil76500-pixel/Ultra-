# V3 — Monte-Carlo adaptatif 4000→8000 + indice Forebet réel

> Cette version historique est remplacée par V4. Voir
> `MONTE_CARLO_V4.md` : xG additif plafonné à 3.0, Monte-Carlo à 100 000
> tirages et fusion Forebet 1X2 à 30 %. Les règles V3 ci-dessous ne doivent
> pas être réactivées sans recalibrage.

## 1. Monte-Carlo à précision adaptative (engine/montecarlo.py)
- Le moteur ne tourne plus sur un nombre fixe d'itérations. Il simule par lots
  de 500, avec un plancher de 4000 et un plafond de 8000, et continue tant que
  l'erreur standard sur l'issue 1X2 n'est pas descendue sous 0.6 point de %.
- Résultat : un match très déséquilibré s'arrête souvent à 4000-5000 sims
  (l'estimation est déjà stable), un match serré ou chaotique va chercher
  jusqu'à 7000-8000 — la précision est mise là où elle sert vraiment.
- `MonteCarloResult` expose maintenant `iterations`, `converged` et
  `convergence_se` pour que le bot affiche honnêtement le niveau de précision
  atteint plutôt qu'un nombre de simulations sans contexte.

## 2. Fenêtres de buts justifiées par les simulations elles-mêmes
- Chaque but simulé est loggé minute par minute avec la mécanique en jeu au
  moment où il tombe (purple patch / état post-carton rouge / pression de fin
  de match). Les fenêtres de 9 minutes affichées à l'utilisateur viennent
  désormais de l'agrégation de ces événements réels, avec le "driver"
  dominant de chaque fenêtre — ce n'est plus seulement une courbe théorique
  pré-calculée, mais une lecture a posteriori de ce que le moteur a produit.
- L'ancienne fonction analytique `compute_goal_timing_windows` reste en place
  comme filet de sécurité si jamais le journal d'événements est vide.

## 3. Indice Forebet réellement exploité (au lieu d'être toujours vide)
- `engine/web_collector.py` parse maintenant la page de pronostics Forebet et
  en extrait le score exact prédit par fixture, en le rattachant au bon match
  par proximité d'horaire de coup d'envoi (le nom des équipes n'est
  volontairement pas utilisé pour ce rapprochement — trop fragile à extraire
  fiablement du HTML de Forebet — donc un rattachement ambigu est ignoré
  plutôt que deviné).
- Le parseur est conçu de façon défensive :
  s'il ne trouve rien d'univoque, il ne retourne rien, et le bot affiche
  honnêtement « indice non disponible » — jamais une valeur inventée.
- `engine/predictor.py` blend ce score dans les λ (lambdas) du modèle avec un
  poids **plafonné à 35%** (le seuil qui était déjà annoncé dans les
  messages du bot mais jamais réellement appliqué), et divise ce poids par
  deux si le pronostic Forebet contredit la propre lecture interne du bot.
  Ce n'est donc jamais une copie du pronostic Forebet — juste un indice
  borné, conformément à la philosophie déjà affichée dans bot.py.
- Le résultat de simulation rapporte en plus un diagnostic « alignment » :
  la probabilité que la simulation du bot assigne elle-même au score
  Forebet, et son rang parmi tous les scores simulés — un vrai contrôle
  croisé, pas une confirmation automatique.

## Fichiers modifiés
- engine/montecarlo.py   (moteur de simulation)
- engine/predictor.py    (pipeline de prédiction + blend Forebet)
- engine/web_collector.py (parseur Forebet + rattachement par horaire)
- engine/scanner.py      (transmet l'indice Forebet du cache au predictor)
- bot.py                 (affichage des nouveaux diagnostics)

## À vérifier après déploiement
- Le HTML de forebet.com n'est pas documenté publiquement ; si le parseur
  `_forebet_matches()` ne remonte aucune prédiction en production, comparez
  le motif `_SCORE_PAIR` / `_FOREBET_DATETIME` à la page live et ajustez.
  Le log `source_status["forebet"]` indique combien de prédictions ont été
  extraites et rattachées à chaque scan (`predictions=N; matched=M`).
- Vérifiez les conditions d'utilisation / robots.txt de forebet.com avant un
  déploiement en production : ce scraper est best-effort et respecte déjà les
  codes 403/429 en reculant, mais la responsabilité du respect des CGU du
  site reste la vôtre.
