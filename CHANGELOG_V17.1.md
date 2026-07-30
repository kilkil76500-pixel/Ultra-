# V17.1

## Supprimé
- `engine/providers/router.py`, `apifootball.py`, `footballdata.py` : code mort,
  jamais instancié. Le bot n'utilise et n'a jamais utilisé que le scraping
  Forebet (`engine/web_collector.py`) — pas d'API payante, pas de clé à
  configurer. `engine/providers/base.py` est conservé mais réduit aux seuls
  types de données partagés (`NormalizedFixture`, `NormalizedTeamStats`, …),
  qui restent utilisés dans tout le moteur.
- La classe `ProviderInterface` et la hiérarchie d'exceptions
  `ProviderError`/`ProviderUnavailableError`/`ProviderRateLimitError`
  (jamais utilisées en dehors des fichiers supprimés).

## Corrigé
- **`/delete` ne supprime plus tout le répertoire de cache.** Auparavant,
  `cache_store.delete_cache()` faisait un `shutil.rmtree()` sur tout
  `WEB_CACHE_DIR` — qui contient aussi `predictions.db` (historique des
  pronostics, utilisé par `/resultat`, `/fiabilite`, `/apprentissage`) et
  `calibration.json`/`calibration_history/` (issus de `/recalibrer`).
  Résultat : vider le cache du jour effaçait aussi tout l'historique
  d'apprentissage. Désormais, seuls les dossiers datés (`AAAA-MM-JJ/…`)
  sont supprimés ; `predictions.db` et la calibration sont préservés.
- `/delete` affiche maintenant le nombre de matchs du jour effacés *et*
  un rappel de ce qui est conservé (nombre de pronostics enregistrés/réglés,
  état de la calibration), pour que ce soit visible directement dans Telegram.

## Documentation
- Le docstring de `engine/data.py` (qui prétendait déléguer à un
  `ProviderRouter`) reflète maintenant la réalité : toutes les données
  viennent de `cache_store`, alimenté uniquement par le scraping Forebet.
