# V19.4 — Le "Plus" n'est cliqué qu'une fois, le reste est du scroll infini

## Ce que le test manuel a confirmé

En comptant à la main sur le site : plus de 130 matchs à venir aujourd'hui
(jusqu'à 23h30), et surtout — point clé — **cliquer sur « Plus » ne
recharge pas un nouveau lot à chaque clic** : un seul clic suffit à faire
basculer la page en défilement continu, qui révèle ensuite le reste des
matchs au fur et à mesure qu'on descend la page.

## Pourquoi ça plafonnait à ~55-58

La v19.3 cherchait un bouton « Plus » cliquable à **chaque** round de la
boucle. Une fois le premier (et unique) clic fait, ce bouton disparaît
(normal, on est passés en mode scroll) — la boucle ne trouvait donc plus
rien à cliquer et abandonnait après quelques rounds sans progrès, alors
que la page continuait en réalité d'avoir plus de matchs à révéler par
simple scroll.

## Correctif

La boucle fait maintenant les deux à chaque round, dans cet ordre :
1. Clique sur « Plus » s'il est encore visible (opportuniste — ne bloque
   plus si absent).
2. Fait un pas de scroll modeste (1500px, pas un saut direct en bas — un
   saut trop grand peut sauter par-dessus le seuil qui déclenche le
   chargement suivant d'un scroll infini basé sur IntersectionObserver).
3. Attend `networkidle` (ou le délai configuré) avant de recompter les
   matchs.

Stagnation : 4 rounds sans le moindre nouveau match (au lieu de 3) avant
de conclure que c'est la vraie fin du contenu — un scroll infini charge
parfois par petits paquets avec un peu de latence.

## À vérifier au prochain `/scan`

`[web_collector] <label> : chargement headless terminé — X clic(s), Y
défilement(s), Z match(s) au total dans le DOM.`

Cible attendue : proche de 130+ pour la page "aujourd'hui" (compté à la
main). Si Z stagne encore loin en dessous, il faudra revoir l'incrément de
scroll ou le délai réseau — mais cette fois le mécanisme lui-même
(clic unique + scroll continu) colle à ce qui a été observé manuellement
sur le site.
