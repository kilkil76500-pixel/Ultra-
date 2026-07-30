"""Lance collect_window() directement, sans Telegram."""
import logging
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)

# --- Env vars (avant d'importer config) ---
os.environ.setdefault("WEB_CACHE_DIR", str(SCRIPT_DIR / "cache"))
os.environ.setdefault("WEB_SCAN_MIN_LEAD_HOURS", "3")
os.environ.setdefault("WEB_SCAN_HOURS", "14")
os.environ.setdefault("WEB_SCAN_LOAD_MORE_MAX_SECONDS", "900")
os.environ.setdefault("WEB_SCAN_MAX_LOAD_MORE_CLICKS", "300")
os.environ.setdefault("FOREBET_TZ_OFFSET_HOURS", "1")
os.environ.setdefault("FOREBET_DETAIL_CONCURRENCY", "6")
os.environ.setdefault("WEB_SCAN_USE_HEADLESS_BROWSER", "true")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    stream=sys.stdout,
)

sys.path.insert(0, str(SCRIPT_DIR))
# V19.10 — la préparation des libs Nix (LD_LIBRARY_PATH, libgbm isolée) est
# maintenant faite dynamiquement par engine/headless_env.py, appelée
# directement par web_collector.py avant chaque lancement de Chromium. Plus
# besoin de la dupliquer ici avec des chemins /nix/store/<hash> codés en dur
# (qui se cassaient à chaque nouveau déploiement Replit).
from engine import web_collector

def progress(step, total, label):
    print(f"[PROGRESS {step}/{total}] {label}", flush=True)

print("=== SCAN DÉMARRÉ ===", flush=True)
result = web_collector.collect_window(progress)

total    = result.get("written", result.get("count", result.get("total", "?")))
nouveaux = result.get("new_this_scan", "?")

print("\n=== RÉSULTAT ===", flush=True)
print(f"  total matchs retenus  : {total}")
print(f"  nouveaux ce scan      : {nouveaux}")
print(f"  fenêtre              : {result.get('window_start','?')} → {result.get('window_end','?')}")
print(f"  hors fenêtre         : {result.get('out_of_window','non retourné')}")
for k, v in result.get("sources", {}).items():
    print(f"  [{k}] {v}")
print("=== SCAN TERMINÉ ===", flush=True)
