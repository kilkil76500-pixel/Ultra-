# Monte-Carlo V4 — méthode utilisée par le bot

## Objectif

Le moteur doit produire des probabilités stables sans transformer une donnée
extrême en score impossible. Le Monte-Carlo ne corrige pas un mauvais xG :
il simule exactement les λ reçus. La première protection est donc la
construction additive du xG dans `engine/predictor.py`.

## 1. Construction du xG

Pour chaque équipe, le bot calcule sept estimations en buts par match, puis
fait une moyenne pondérée :

| Composant | Poids |
|---|---:|
| Force offensive actuelle | 35 % |
| Faiblesse défensive adverse | 25 % |
| Forme récente | 15 % |
| H2H | 10 % |
| Avantage domicile / extérieur | 5 % |
| Absences | 5 % |
| Motivation / mental | 5 % |

Chaque composant est borné avant la moyenne. Le H2H est en plus régressé vers
la moyenne de la ligue (70 % moyenne de ligue, 30 % H2H), afin que d'anciennes
confrontations ne dominent pas la forme actuelle.

Le résultat final est toujours borné :

```text
0.20 <= xG_home <= 3.00
0.20 <= xG_away <= 3.00
```

Les absences, le H2H et l'émotion du public sont donc des composants limités,
pas des multiplicateurs successifs. C'est la différence essentielle avec
l'ancienne méthode qui pouvait pousser λ jusqu'à 6.0 avant la simulation.

## 2. Monte-Carlo

Le moteur lance 100 000 simulations indépendantes. À chaque simulation :

```text
home_goals ~ Poisson(lambda_home)
away_goals ~ Poisson(lambda_away)
```

Une petite variation de jour de match, bornée entre 0.85 et 1.15, représente
l'incertitude de forme et de contexte. Elle ne peut jamais dépasser le plafond
du λ ni ajouter un nouveau multiplicateur de momentum, carton rouge ou
« purple patch ».

Les scores 4-0, 4-1 ou 5-1 restent mathématiquement possibles lorsque λ le
justifie. Les scores 8-1 et 9-1 deviennent des événements de queue, pas le
résultat normal d'un λ gonflé artificiellement.

Le moteur agrège :

- victoire domicile, nul, victoire extérieur ;
- BTTS ;
- Over / Under 2.5 et 3.5 ;
- score modal et score alternatif ;
- fenêtres temporelles des buts ;
- probabilité et rang du score exact annoncé par Forebet, comme contrôle.

## 3. Forebet 1X2

Quand la fiche Forebet contient les trois probabilités 1X2, la sortie finale
utilise une pondération adaptée à la qualité des données locales :

```text
avec données locales suffisantes :
  probabilité finale = 70 % moteur interne + 30 % Forebet

avec données locales absentes ou invalides :
  probabilité finale = 30 % moteur interne + 70 % Forebet
```

Les trois valeurs sont d'abord normalisées pour totaliser 100 %. Si Forebet ne
fournit pas les trois probabilités, le bot conserve uniquement son calcul
interne. Le score exact Forebet et son éventuel xG de page ne modifient pas λ :
ils servent uniquement au contrôle d'alignement affiché. Le score modal affiché
par le Monte-Carlo est donc un score local, distinct du score exact Forebet.

## 4. Règles à ne pas casser

1. Ne jamais remettre des multiplicateurs H2H, forme, absences ou score
   Forebet en chaîne après la moyenne additive.
2. Ne jamais relever `XG_MAX` au-delà de 3.0 sans recalibrage statistique.
3. Ne jamais utiliser un score exact Forebet pour fabriquer un λ.
4. Si un nouveau facteur est ajouté, l'ajouter comme composant borné dont le
   poids est explicite, puis vérifier que les poids totalisent 100 %.