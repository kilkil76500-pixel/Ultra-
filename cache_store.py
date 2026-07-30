"""Persistent match cache used as the boundary between collection and analysis.

The collector is the only module that talks to the Internet.  Every analysis
path reads these JSON files and therefore cannot silently trigger a web request.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from engine.providers.base import NormalizedFixture

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()


def cache_root() -> Path:
    root = Path(config.WEB_CACHE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slug(value: str) -> str:
    value = value.encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "match"


def match_key(home: str, away: str, timestamp: int | None) -> str:
    stamp = str(timestamp or "tbd")
    return f"{_slug(home)}-vs-{_slug(away)}-{stamp}"


def path_for(snapshot: dict[str, Any]) -> Path:
    day = str(snapshot.get("date") or datetime.now(timezone.utc).date().isoformat())
    competition = _slug(str(snapshot.get("competition") or "football"))
    key = str(snapshot.get("cache_key") or match_key(
        str(snapshot.get("home") or "home"),
        str(snapshot.get("away") or "away"),
        snapshot.get("timestamp"),
    ))
    return cache_root() / day / competition / f"{key}.json"


def save_snapshot(snapshot: dict[str, Any]) -> Path:
    target = path_for(snapshot)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def clear_day(day: str | None = None) -> int:
    """Remove the snapshots and scan index for one UTC day.

    Kept for an explicit full reset (e.g. a future /viderlecache-du-jour
    command), but collect_window() no longer calls this on every /scan —
    see prune_expired_snapshots() for the accumulating behaviour used
    there instead.
    """
    day = day or datetime.now(timezone.utc).date().isoformat()
    target = cache_root() / day
    with _LOCK:
        if not target.exists():
            return 0
        count = sum(1 for path in target.rglob("*.json") if path.is_file())
        shutil.rmtree(target)
    logger.info("Day cache cleared: %s (%d files)", day, count)
    return count


def prune_expired_snapshots(day: str, floor_timestamp: int) -> int:
    """Remove only the snapshots whose kickoff is older than *floor_timestamp*.

    V19.5 — un /scan headless ne charge parfois qu'une partie des matchs
    du jour avant de stagner (défilement infini qui n'atteint pas le bas de
    la page en un seul passage). Avant, collect_window() appelait
    clear_day() au début de chaque scan : un scan partiel écrasait donc
    purement et simplement les matchs trouvés par le scan précédent, sans
    jamais accumuler. Cette fonction ne retire que ce qui est vraiment
    expiré (hors de la fenêtre même après le tampon "en direct"), et laisse
    tout le reste intact — un second /scan peut alors ajouter de nouveaux
    matchs à l'archive du jour au lieu de repartir de zéro.
    """
    target = cache_root() / day
    removed = 0
    if not target.exists():
        return 0
    with _LOCK:
        for path in target.rglob("*.json"):
            if path.name == "scan-index.json":
                continue
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            raw_ts = item.get("timestamp")
            try:
                ts = int(raw_ts) if raw_ts is not None else None
            except (TypeError, ValueError):
                ts = None
            if ts is not None and ts < floor_timestamp:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        logger.info(
            "[cache_store] %d ancien(s) match(s) expiré(s) purgé(s) de "
            "l'archive %s (kickoff avant %s).",
            removed, day, datetime.fromtimestamp(floor_timestamp, tz=timezone.utc).isoformat(),
        )
    return removed


def save_scan_index(
    day: str,
    keys: list[str],
    *,
    window_start: str,
    window_end: str,
    sources: dict[str, str],
) -> Path:
    """Persist the exact /today ordering used by /predict N."""
    target = cache_root() / day / "scan-index.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "date": day,
        "keys": keys,
        "window_start": window_start,
        "window_end": window_end,
        "sources": sources,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with _LOCK:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_scan_index(day: str | None = None) -> dict[str, Any] | None:
    day = day or datetime.now(timezone.utc).date().isoformat()
    target = cache_root() / day / "scan-index.json"
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring invalid scan index %s: %s", target, exc)
        return None


def load_snapshots(day: str | None = None) -> list[dict[str, Any]]:
    """Return all cached match snapshots.

    When *day* is given, load only that day's directory (used internally).
    When *day* is None (the normal case called from scanner / bot), scan every
    day directory whose name is >= today.  This is necessary because
    ``web_collector`` stores each snapshot under the match's own date (e.g.
    tomorrow's matches land in ``cache/2026-07-25/…``) while the bot may be
    running on the previous calendar day — a mismatch that used to produce an
    empty list after a successful scan.
    """
    today = datetime.now(timezone.utc).date().isoformat()

    if day is not None:
        # Explicit day: single-directory load (unchanged behaviour).
        directories = [cache_root() / day]
        index_day = day
    else:
        # Automatic mode: collect all day directories that are today or later,
        # so that matches stored under tomorrow's date are always visible.
        root = cache_root()
        directories = sorted(
            d for d in root.iterdir()
            if d.is_dir() and d.name >= today
        )
        # Use the most recent scan-index that has one (usually today or the
        # only populated day folder).
        index_day = today

    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    with _LOCK:
        for directory in directories:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*/*.json")):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    ck = str(item.get("cache_key") or path.stem)
                    if ck in seen:
                        continue
                    seen.add(ck)
                    item["_path"] = str(path)
                    result.append(item)
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("Ignoring invalid cache file %s: %s", path, exc)

    result.sort(key=lambda item: (int(item.get("timestamp") or 2**31), str(item.get("home", ""))))
    index = load_scan_index(index_day)
    if index:
        positions = {
            str(key): position
            for position, key in enumerate(index.get("keys") or [])
        }
        result.sort(key=lambda item: (
            positions.get(str(item.get("cache_key")), len(positions)),
            int(item.get("timestamp") or 2**31),
            str(item.get("home", "")),
        ))
    return result


def load_by_key(key: str) -> dict[str, Any] | None:
    for item in load_snapshots():
        if item.get("cache_key") == key:
            return item
    return None


_DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def delete_cache() -> int:
    """Delete only the daily match-snapshot directories (``AAAA-MM-JJ/…``).

    ``predictions.db`` (tracked pronostics — /resultat, /fiabilite,
    /apprentissage), ``calibration.json`` and ``calibration_history/``
    (from /recalibrer) live directly under the same cache root but must
    survive a "vider le cache" — they are not day-snapshot data, they are
    the bot's learning history. Only directories that look like a scan
    date are removed.
    """
    root = Path(config.WEB_CACHE_DIR)
    count = 0
    with _LOCK:
        if root.exists():
            for entry in root.iterdir():
                if entry.is_dir() and _DAY_DIR_RE.match(entry.name):
                    count += sum(1 for path in entry.rglob("*.json") if path.is_file())
                    shutil.rmtree(entry)
        root.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Cache jour supprimé : %s (%d fichier(s)) — predictions.db et calibration.json préservés",
        root, count,
    )
    return count


def update_sources(snapshot: dict[str, Any], source: str, source_data: dict[str, Any]) -> None:
    sources = list(snapshot.get("sources") or [])
    if source not in sources:
        sources.append(source)
    snapshot["sources"] = sources
    details = dict(snapshot.get("source_details") or {})
    details[source] = source_data
    snapshot["source_details"] = details
    snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()


def fixture_from_snapshot(snapshot: dict[str, Any]) -> NormalizedFixture:
    return NormalizedFixture(
        fixture_id=str(snapshot.get("cache_key") or ""),
        home_id=str(snapshot.get("home_id") or _slug(str(snapshot.get("home") or ""))),
        away_id=str(snapshot.get("away_id") or _slug(str(snapshot.get("away") or ""))),
        home_name=str(snapshot.get("home") or "Home"),
        away_name=str(snapshot.get("away") or "Away"),
        league_id=int(snapshot.get("league_id") or 0),
        league_name=str(snapshot.get("competition") or "Unknown competition"),
        league_country=str(snapshot.get("country") or ""),
        timestamp=int(snapshot["timestamp"]) if snapshot.get("timestamp") else None,
        provider="web-cache",
    )


def snapshot_for_fixture(fixture_id: str) -> dict[str, Any] | None:
    for item in load_snapshots():
        if str(item.get("cache_key")) == str(fixture_id):
            return item
    return None