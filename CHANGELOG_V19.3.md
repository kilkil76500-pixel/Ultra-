# V19.3 — Boucle de clics « Plus » plus robuste (headless)

## Contexte

Le navigateur headless (Playwright) trouvait bien plus de matchs qu'avant
(passage de ~43 à 55), mais la boucle de clics sur « Plus » se figeait
toujours bien avant d'avoir tout chargé, alors que Forebet annonce 100+
matchs pour la journée.

## Cause

Un seul sélecteur exact (`get_by_text("Plus", exact=True).last`) et un seul
essai de clic par round : dès qu'un clic échouait une fois (bouton
temporairement masqué par un bandeau, DOM pas encore stable, libellé
légèrement différent après le premier déplioiement…), la boucle
s'arrêtait — sans que ce soit forcément la fin réelle de la pagination.

## Correctifs

- Recherche du bouton élargie à plusieurs libellés (« Plus », « Voir
  plus », « Charger plus », « Afficher plus », « Charger la suite », « Show
  more ») au lieu d'un seul texte exact.
- Défilement en bas de page avant chaque tentative, pour révéler un bouton
  masqué par un élément collant.
- Si le clic natif échoue (élément intercepté par un overlay), tentative
  de déclenchement JS direct (`el.click()`) avant d'abandonner.
- Attente `networkidle` après chaque clic, pour laisser le temps à l'appel
  AJAX de répondre avant de recompter les matchs.
- Seuil de stagnation relevé (3 rounds sans progrès au lieu de 2), pour ne
  pas confondre un round lent avec une vraie fin de contenu.
- Plafond de clics relevé (`WEB_SCAN_MAX_LOAD_MORE_CLICKS`, def. 120) et
  nouveau budget de temps dédié (`WEB_SCAN_LOAD_MORE_MAX_SECONDS`, def.
  180s), pour qu'un jour à 500-1000 matchs ait vraiment le temps de se
  charger en entier plutôt que d'être coupé par un plafond de clics trop
  bas.
- Logs explicites indiquant laquelle des deux limites (clics ou temps) a
  mis fin à la boucle, pour distinguer « il restait des matchs mais on a
  été coupé » de « la page était vraiment épuisée ».

## À vérifier lors du prochain `/scan`

Chercher dans les logs :
`[web_collector] <label> : chargement headless terminé — X clic(s), Y
match(s) au total dans le DOM.`

Si Y stagne encore bien en dessous du total annoncé par Forebet, il faudra
inspecter le bouton réel (DevTools, onglet Éléments) pour récupérer sa
classe CSS exacte — la recherche par texte a ses limites si Forebet
change son wording ou si le bouton n'a pas de texte visible du tout
(icône seule, par exemple).
