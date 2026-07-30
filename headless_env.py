"""engine/headless_env.py — Prépare l'environnement pour Chromium headless
sur les hôtes NixOS (Replit).

Playwright lance un binaire Chromium classique, qui va chercher ses
bibliothèques partagées (nss, nspr, glib, X11, …) via le chemin système
habituel. Sur NixOS, ces libs ne vivent pas dans /usr/lib : elles sont
éparpillées sous des chemins à hachage de contenu, du type
"/nix/store/<hash>-<paquet>-<version>/lib". Ce hachage est propre à UN
instantané précis du store Nix — rien ne garantit qu'il soit identique sur
un autre Replit, même créé depuis le même modèle.

Les premières versions (V19.9) codaient ces chemins en dur dans
run_scan.py/start.sh. Ça fonctionne tant qu'on reste sur l'environnement où
ils ont été relevés, puis se casse silencieusement au premier nouveau
déploiement : les chemins n'existent plus, Chromium ne trouve plus ses
libs, `chromium.launch()` échoue, et `_fetch_match_list_headless()`
rattrape l'exception et retombe sur un GET statique (~40 matchs au lieu de
la liste complète) — sans rien de plus voyant qu'un log WARNING. Rien ne le
signalait à l'utilisateur du bot.

Ce module cherche les libs par NOM sur la machine qui tourne réellement
maintenant, au lieu de chemins figés — donc ça survit à un nouveau
déploiement sans qu'il faille toucher au code. Il est appelé directement
depuis `_fetch_match_list_headless()` (pas seulement depuis run_scan.py /
start.sh), pour que le correctif s'applique quel que soit le point d'entrée
utilisé pour lancer le process (bot.py, run_scan.py, autre).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

_LIB_NAME_PATTERNS = [
    "libnspr4.so*",
    "libnss3.so*",
    "libglib-2.0.so*",
    "libatspi.so*",
    "libdbus-1.so*",
    "libX11.so*",
    "libXcomposite.so*",
    "libXdamage.so*",
    "libXext.so*",
    "libXfixes.so*",
    "libXrandr.so*",
    "libxcb.so*",
    "libxkbcommon.so*",
    "libasound.so*",
    "libdrm.so*",
    "libexpat.so*",
    "libudev.so*",
]

_GBM_LIB_PATTERN = "libgbm.so*"
_ISOLATED_GBM_DIR = "/tmp/pw-libs"
_NIX_STORE_ROOT = "/nix/store"
# Ne pas parcourir /nix/store avec glob : dans Replit ce répertoire peut
# contenir plusieurs dizaines de milliers d'entrées et le scan bloquait le
# premier /scan avant même le lancement de Chromium. ldconfig connaît déjà
# les bibliothèques disponibles et renvoie directement leurs chemins.
_CACHE_FILE = "/tmp/pw-nix-libdirs.cache"
_ENV_READY_FLAG = "_HEADLESS_LIBS_READY"


def _discover_lib_dirs() -> list[str]:
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, encoding="utf-8") as fh:
                cached = [line.strip() for line in fh if line.strip()]
            if cached and all(os.path.isdir(d) for d in cached):
                return cached
        except OSError:
            pass

    if not os.path.isdir(_NIX_STORE_ROOT):
        return []  # pas un hôte Nix — rien à faire ici

    names = {pattern.removeprefix("lib").split(".so", 1)[0] for pattern in _LIB_NAME_PATTERNS}
    found_dirs: list[str] = []
    seen: set[str] = set()
    try:
        output = subprocess.run(
            ["ldconfig", "-p"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("[headless_env] Impossible de lire ldconfig (%s).", exc)
        output = ""

    for line in output.splitlines():
        if " => " not in line:
            continue
        library_path = line.rsplit(" => ", 1)[-1].strip()
        library_name = os.path.basename(library_path)
        if any(library_name.startswith(name) for name in names) and os.path.dirname(library_path).startswith(_NIX_STORE_ROOT):
            lib_dir = os.path.dirname(library_path)
            if lib_dir not in seen:
                seen.add(lib_dir)
                found_dirs.append(lib_dir)

    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
            fh.write("\n".join(found_dirs))
    except OSError:
        pass
    return found_dirs


def _ensure_isolated_gbm() -> str | None:
    """libgbm doit être isolée dans son propre dossier : sur Replit, le
    paquet qui la fournit embarque aussi des libc/libm qui entrent en
    conflit avec celles utilisées par Python si on ajoute tout son
    dossier lib/ à LD_LIBRARY_PATH. On ne copie donc que ce fichier-là."""
    target = f"{_ISOLATED_GBM_DIR}/libgbm.so.1"
    if os.path.exists(target):
        return _ISOLATED_GBM_DIR
    if not os.path.isdir(_NIX_STORE_ROOT):
        return None

    source = None
    try:
        output = subprocess.run(
            ["ldconfig", "-p"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        for line in output.splitlines():
            if "libgbm.so" not in line or " => " not in line:
                continue
            candidate = line.rsplit(" => ", 1)[-1].strip()
            if candidate.startswith(_NIX_STORE_ROOT) and os.path.isfile(candidate):
                source = candidate
                break
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("[headless_env] Impossible de trouver libgbm via ldconfig (%s).", exc)
    if source is None:
        logger.warning(
            "[headless_env] libgbm introuvable sous %s — Chromium headless "
            "risque de ne pas démarrer sur cet hôte.", _NIX_STORE_ROOT,
        )
        return None

    os.makedirs(_ISOLATED_GBM_DIR, exist_ok=True)
    dst = f"{_ISOLATED_GBM_DIR}/{os.path.basename(source)}"
    try:
        shutil.copy2(source, dst)
    except OSError as exc:
        logger.warning("[headless_env] Copie de libgbm impossible (%s).", exc)
        return None
    for name in ("libgbm.so.1", "libgbm.so"):
        link = f"{_ISOLATED_GBM_DIR}/{name}"
        if not os.path.exists(link):
            try:
                os.symlink(dst, link)
            except OSError:
                pass
    return _ISOLATED_GBM_DIR


def ensure_headless_browser_libs() -> None:
    """À appeler avant tout `chromium.launch()`. Idempotent (ne refait le
    travail qu'une fois par process) et sans effet sur un hôte non-Nix
    (Docker classique avec `playwright install-deps` : Chromium trouve ses
    libs par les chemins système normaux, il n'y a rien à ajouter ici)."""
    if os.environ.get(_ENV_READY_FLAG) == "1":
        return

    # Replit fournit déjà un ensemble cohérent de bibliothèques natives pour
    # son navigateur Playwright. Il faut le préférer au navigateur téléchargé
    # par `playwright install`, qui peut dépendre de libs absentes du runtime
    # Python (notamment libnspr4.so).
    replit_libs = os.environ.get("REPLIT_PYTHON_LD_LIBRARY_PATH", "").strip()
    if replit_libs:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = (
            f"{replit_libs}:{existing}" if existing else replit_libs
        )
        logger.info("[headless_env] Bibliothèques natives Replit utilisées pour Chromium.")
        os.environ[_ENV_READY_FLAG] = "1"
        return

    lib_dirs = _discover_lib_dirs()
    gbm_dir = _ensure_isolated_gbm()
    if gbm_dir:
        lib_dirs.append(gbm_dir)

    if lib_dirs:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        new_path = ":".join(lib_dirs)
        os.environ["LD_LIBRARY_PATH"] = f"{new_path}:{existing}" if existing else new_path
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH",
            os.path.expanduser("~/.cache/ms-playwright"),
        )
        logger.info(
            "[headless_env] LD_LIBRARY_PATH prêt (%d dossier(s) Nix détecté(s) "
            "dynamiquement, dont libgbm isolée=%s).",
            len(lib_dirs), "oui" if gbm_dir else "non",
        )

    os.environ[_ENV_READY_FLAG] = "1"
