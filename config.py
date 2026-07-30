"""Runtime configuration for the autonomous Telegram football bot.

The bot uses public web pages and a persistent local cache. No paid sports API
credentials are required for the core engine.
"""

from __future__ import annotations

import os
import sys


def _safe_int(env_key: str, default: int) -> int:
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"WARNING config.py: {env_key}={raw!r} is invalid; using {default}",
            file=sys.stderr,
        )
        return default


def _safe_bool(env_key: str, default: bool) -> bool:
    raw = os.environ.get(env_key, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    print(
        f"WARNING config.py: {env_key}={raw!r} is invalid; using {default}",
        file=sys.stderr,
    )
    return default


TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# The /scan window is deliberately relative to the moment the command starts.
WEB_SCAN_HOURS: int = _safe_int("WEB_SCAN_HOURS", 10)
WEB_REQUEST_TIMEOUT: int = _safe_int("WEB_REQUEST_TIMEOUT", 20)
WEB_CACHE_DIR: str = os.environ.get("WEB_CACHE_DIR", "cache")
FOREBET_DETAIL_CONCURRENCY: int = _safe_int("FOREBET_DETAIL_CONCURRENCY", 6)

# V19.8 — le bot doit prédire AVANT le coup d'envoi, pas pendant ni juste
# avant. Deux conséquences :
# 1. On ne remonte plus dans le passé pour récupérer les matchs déjà en
#    cours (Live, mi-temps…) : ce n'était utile que si l'objectif était de
#    suivre des matchs en direct, ce qui n'est plus le cas. Un match dont le
#    coup d'envoi est déjà passé est désormais toujours exclu, quel que soit
#    WEB_SCAN_LIVE_BUFFER_HOURS (conservé ci-dessous pour compatibilité mais
#    plus utilisé dans le filtre de collect_window()).
# 2. On exclut aussi les matchs dont le coup d'envoi est TROP proche de
#    l'instant du scan (< WEB_SCAN_MIN_LEAD_HOURS) : un match qui démarre
#    dans quelques minutes ne laisse plus le temps de produire un pronostic
#    pré-match utile, et risque même d'avoir déjà démarré le temps que la
#    collecte (jusqu'à WEB_SCAN_LOAD_MORE_MAX_SECONDS) se termine. Exemple
#    demandé : scan lancé à 17h → on saute volontairement 17h et on ne
#    retient que les matchs à partir de 18h (17h + WEB_SCAN_MIN_LEAD_HOURS).
WEB_SCAN_LIVE_BUFFER_HOURS: int = _safe_int("WEB_SCAN_LIVE_BUFFER_HOURS", 3)  # legacy, voir ci-dessus
WEB_SCAN_MIN_LEAD_HOURS: int = _safe_int("WEB_SCAN_MIN_LEAD_HOURS", 1)

# V19.3 — les pages de listing Forebet ("aujourd'hui" / "en direct") ne
# rendent côté serveur qu'une quarantaine de lignes ; le reste (jusqu'à
# 400+ matchs certains jours) ne se charge que via le bouton "Plus", en
# JavaScript. Passer par un vrai navigateur headless (Playwright/Chromium)
# permet de cliquer ce bouton en boucle et de récupérer la liste complète.
# Mettre à `false` (ou installer Chromium et laisser à `true` mais sans
# Playwright dispo) fait retomber automatiquement sur un simple GET —
# fenêtre plus étroite, mais /scan continue de fonctionner.
WEB_SCAN_USE_HEADLESS_BROWSER: bool = _safe_bool("WEB_SCAN_USE_HEADLESS_BROWSER", True)
# Nombre max. de clics sur "Plus" par page de listing. La boucle s'arrête de
# toute façon plus tôt si 3 clics d'affilée n'ajoutent plus rien — ce
# plafond n'intervient donc que si Forebet a vraiment beaucoup de matchs
# (ex. 1000+) ; mieux vaut un plafond haut que de couper une journée chargée.
WEB_SCAN_MAX_LOAD_MORE_CLICKS: int = _safe_int("WEB_SCAN_MAX_LOAD_MORE_CLICKS", 120)
# Pause (ms) après chaque clic "Plus", le temps que l'appel AJAX de Forebet
# réponde et que le DOM se mette à jour, avant de recompter les matchs.
WEB_SCAN_LOAD_MORE_DELAY_MS: int = _safe_int("WEB_SCAN_LOAD_MORE_DELAY_MS", 700)
# V19.3 — budget de temps (secondes) pour toute la boucle de clics "Plus"
# d'une page. Complète le plafond de clics : sur un jour à 1000 matchs, le
# bot doit pouvoir prendre son temps plutôt que de s'arrêter prématurément.
# 180s laisse de la marge même avec beaucoup de rounds lents (networkidle
# qui traîne, DOM pas encore stable, etc.).
# V19.6 — 180s s'est avéré trop juste un jour à 130+ matchs (la page
# "aujourd'hui" est triée chronologiquement : les matchs tardifs, ex. 23h,
# sont tout en bas et n'ont jamais le temps d'être révélés par le scroll
# avant que le budget n'expire). Relevé à 300s.
# V19.7 — toujours pas assez marge un jour très chargé ; relevé à 500s sur
# demande explicite pour garantir qu'aucun match n'est oublié faute de temps.
WEB_SCAN_LOAD_MORE_MAX_SECONDS: int = _safe_int("WEB_SCAN_LOAD_MORE_MAX_SECONDS", 500)
# Timeout (ms) de navigation/attente initiale du navigateur headless.
WEB_HEADLESS_NAV_TIMEOUT_MS: int = _safe_int("WEB_HEADLESS_NAV_TIMEOUT_MS", 20000)

# V19 — les horaires affichés sur les pages Forebet sont interprétés comme
# de l'UTC pur par web_collector._find_datetime(). Si les logs de /scan
# (voir le résumé "[web_collector] Forebet today: ...") montrent beaucoup
# de matchs "hors fenêtre" alors qu'ils devraient être dans les prochaines
# WEB_SCAN_HOURS heures, c'est probablement que Forebet affiche ses horaires
# dans un autre fuseau que UTC. Ajustez cette valeur (en heures, positive ou
# négative) pour corriger l'écart observé — 0 = aucune correction (comportement
# historique, inchangé par défaut).
# V19.7 — décalage constaté en usage réel : un scan lancé à 17h (heure de
# l'utilisateur) sautait systématiquement les matchs affichés par Forebet à
# 17h et ne remontait qu'à partir de 18h — signe que Forebet affichait ses
# horaires 1h en avance sur l'UTC réel utilisé par le bot. Mis à 1 pour
# corriger. À revérifier au prochain /scan (voir le log de calibration
# "[web_collector] Calibration horaire" ajouté dans collect_window) : si le
# décalage persiste ou s'inverse, ajuster cette valeur en conséquence.
FOREBET_TZ_OFFSET_HOURS: int = _safe_int("FOREBET_TZ_OFFSET_HOURS", 1)

EV_HIGH: float = 0.10
EV_MEDIUM: float = 0.05
EV_LOW: float = 0.00
SCAN_MIN_EV: float = 0.05
CONFIDENCE_THRESHOLD: float = 20.0
GLOBAL_SCAN_MAX_FIXTURES: int = 100
TOP_N_OPPORTUNITIES: int = 100
TIER_SCAN_MAX: dict[int, int] = {1: 100, 2: 100, 3: 100}


def validate() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise EnvironmentError("Missing required environment variable: TELEGRAM_BOT_TOKEN")
    if WEB_SCAN_HOURS <= 0:
        raise EnvironmentError("WEB_SCAN_HOURS must be greater than zero")
    if FOREBET_DETAIL_CONCURRENCY <= 0:
        raise EnvironmentError("FOREBET_DETAIL_CONCURRENCY must be greater than zero")
