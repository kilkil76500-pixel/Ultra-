# V19.5 — L'archive du jour s'accumule d'un /scan à l'autre

## La demande

Un premier `/scan` s'arrête à 59 matchs (jusqu'à 20h15) alors qu'il y en a
130+ dans la journée. Demande : archiver ce premier scan, et faire en
sorte qu'un second `/scan` reprenne là où le premier s'était arrêté au
lieu de repartir de zéro.

## Ce qui bloquait ça

`collect_window()` appelait `cache_store.clear_day()` en tout début de
scan — ça supprimait purement et simplement tous les fichiers de matchs
déjà archivés pour la journée avant même de recommencer à scraper. Un
second scan, même s'il trouvait des matchs différents (plus tard dans la
soirée, par exemple), écrasait donc systématiquement les 59 du premier
scan au lieu de s'ajouter à eux.

## Correctif

- `cache_store.clear_day()` n'est plus appelée automatiquement à chaque
  scan (elle reste disponible pour un futur reset manuel explicite).
- Nouvelle fonction `cache_store.prune_expired_snapshots(day,
  floor_timestamp)` : ne retire que les matchs dont le coup d'envoi est
  antérieur à la fenêtre "en direct" (`WEB_SCAN_LIVE_BUFFER_HOURS`) — tout
  le reste survit d'un scan à l'autre.
- `collect_window()` fusionne maintenant systématiquement, à la fin de
  chaque scan : les matchs de CE scan + tout ce qui est encore archivé
  d'un scan précédent (et pas expiré), triés chronologiquement. C'est ce
  qui permet à un second `/scan` d'ajouter ses trouvailles à la suite du
  premier.
- En cas d'échec total de récupération (Cloudflare, réseau…), l'index du
  jour n'est plus écrasé par une liste vide : l'archive existante est
  republiée telle quelle.
- Le message Telegram de fin de scan affiche maintenant séparément
  « X nouveaux ce scan » et le total archivé pour la journée, pour que la
  progression d'un scan à l'autre soit visible directement.

## Limite à connaître

Ça résout le problème d'archivage/accumulation demandé, mais ça ne
garantit pas qu'un second scan atteindra forcément des matchs *différents*
du premier — ça dépend de ce que le chargement par défilement (V19.4)
arrive à récupérer à chaque tentative. Si deux scans consécutifs
retombent exactement sur le même sous-ensemble de matchs (parce que le
défilement stagne toujours au même point), l'archive n'avancera pas.
Mais dans la pratique, le temps qui passe change ce qui est "à venir"
(des matchs commencent, sortent de la liste "prochain"), donc des scans
espacés dans le temps ont de bonnes chances de couvrir des créneaux
horaires différents et de faire grossir l'archive au fil de la journée.
