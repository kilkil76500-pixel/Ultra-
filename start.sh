#!/usr/bin/env bash
# Wrapper de démarrage pour Football Intelligence Bot
#
# V19.10 — la préparation des libs Nix pour Chromium headless (LD_LIBRARY_PATH,
# libgbm isolée) se fait maintenant dynamiquement dans engine/headless_env.py,
# appelée par web_collector.py juste avant chaque lancement de Chromium — donc
# ce script n'a plus besoin de le faire lui-même, et surtout ça marche aussi
# si le process est démarré autrement que via ce script (Procfile, Dockerfile,
# bouton "Run" de Replit qui lance bot.py directement, etc.).

set -euo pipefail

# Toujours exécuter depuis le dossier du bot, même si la plateforme lance
# le script depuis un répertoire différent.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  echo "Erreur : Python est requis pour démarrer Football Intelligence." >&2
  exit 1
}

# Charger automatiquement une configuration locale si elle existe. En
# production, les plateformes peuvent continuer à injecter les variables
# directement dans l'environnement.
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/.env"
  set +a
fi

# --- Paramètres de scan ---
# MIN_LEAD=0 : la fenêtre commence à "maintenant" → [now, now+14h]
export WEB_SCAN_MIN_LEAD_HOURS="${WEB_SCAN_MIN_LEAD_HOURS:-0}"
export WEB_SCAN_HOURS="${WEB_SCAN_HOURS:-14}"
export WEB_SCAN_LOAD_MORE_MAX_SECONDS="${WEB_SCAN_LOAD_MORE_MAX_SECONDS:-900}"
export WEB_SCAN_MAX_LOAD_MORE_CLICKS="${WEB_SCAN_MAX_LOAD_MORE_CLICKS:-300}"
export FOREBET_TZ_OFFSET_HOURS="${FOREBET_TZ_OFFSET_HOURS:-1}"
export FOREBET_DETAIL_CONCURRENCY="${FOREBET_DETAIL_CONCURRENCY:-6}"
export WEB_SCAN_USE_HEADLESS_BROWSER="${WEB_SCAN_USE_HEADLESS_BROWSER:-true}"

# Un chemin relatif fourni par la plateforme reste attaché au bot, pas au
# répertoire arbitraire depuis lequel le processus est lancé.
CACHE_DIR="${WEB_CACHE_DIR:-cache}"
case "$CACHE_DIR" in
  /*) ;;
  *) CACHE_DIR="$SCRIPT_DIR/$CACHE_DIR" ;;
esac
export WEB_CACHE_DIR="$CACHE_DIR"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

mkdir -p "$WEB_CACHE_DIR"

# --- Lancer le bot ---
exec "$PYTHON_BIN" "$SCRIPT_DIR/bot.py"
