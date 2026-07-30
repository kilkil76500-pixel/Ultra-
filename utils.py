"""
engine/utils.py — Shared helpers used across the engine.
No Telegram code. No API calls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from engine.providers.base import NormalizedFixture

logger = logging.getLogger(__name__)


# ── Date helpers ──────────────────────────────────────────────────────────────

def today_str() -> str:
    """Return today's date in YYYY-MM-DD, always in UTC regardless of host TZ."""
    return datetime.now(tz=timezone.utc).date().isoformat()


def current_season() -> int:
    """
    Return the most likely current football season year.
    After July 1 the new season is in progress; before that the previous still applies.
    """
    now = datetime.now(tz=timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


# ── Safe data accessors ───────────────────────────────────────────────────────

def safe_get(data: Any, *keys: str | int, default: Any = None) -> Any:
    """Safely traverse nested dicts/lists without raising KeyError / TypeError."""
    current = data
    for key in keys:
        try:
            current = current[key]
        except (KeyError, IndexError, TypeError):
            return default
    return current


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float, returning `default` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Convert a value to int, returning `default` on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Number formatting ─────────────────────────────────────────────────────────

def pct(prob: float) -> str:
    """Format a 0-1 probability as a percentage string, e.g. '63.4%'."""
    return f"{prob * 100:.1f}%"


def odds_fmt(prob: float) -> str:
    """Convert a probability to decimal odds string, e.g. '1.59'."""
    if prob <= 0:
        return "∞"
    return f"{1 / prob:.2f}"


def ev_label(ev: float) -> str:
    """Return a text label for an Expected Value figure."""
    from config import EV_HIGH, EV_MEDIUM, EV_LOW  # noqa: PLC0415
    if ev >= EV_HIGH:
        return "🔥 HIGH"
    if ev >= EV_MEDIUM:
        return "⚡ MEDIUM"
    if ev >= EV_LOW:
        return "✅ LOW"
    return "❌ NO VALUE"


# ── Fixture display helpers ───────────────────────────────────────────────────

def fixture_label(fixture: "NormalizedFixture") -> str:
    """Return 'Home vs Away' from a NormalizedFixture."""
    return f"{fixture.home_name} vs {fixture.away_name}"


def league_label(fixture: "NormalizedFixture") -> str:
    """Return 'League Name, Country' from a NormalizedFixture."""
    country = fixture.league_country
    return f"{fixture.league_name}, {country}".rstrip(", ") if country else fixture.league_name


_EU = ZoneInfo("Europe/Paris")   # CET/CEST selon la saison


def kickoff_label(fixture: "NormalizedFixture") -> str:
    """Return kickoff time converted to European time (Europe/Paris, CET/CEST)."""
    ts = fixture.timestamp
    if not ts:
        return "TBD"
    try:
        utc_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        eu_dt  = utc_dt.astimezone(_EU)
        return eu_dt.strftime("%H:%M (heure fr)")
    except (ValueError, OSError):
        return "TBD"
