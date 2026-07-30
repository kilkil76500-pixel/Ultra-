"""
engine/data.py — Caching facade over the local match cache.

All other engine modules call this module. They never talk to the HTTP layer
directly. This module owns the TTL cache layer and reads from
engine.cache_store, which is populated exclusively by engine.web_collector
(Forebet scraping). There is no paid-API fallback: the multi-provider router
(Football-Data.org / API-Football) that used to be described here was dead
code — never instantiated — and was removed in V17.1.

Caching TTLs
------------
  fixture_cache  — 60 s     (today's schedule; re-fetched when cache expires)
  stats_cache    — 15 min   (season aggregates change slowly)
  odds_cache     — 120 s    (odds drift closer to kick-off; kept fresh)
  h2h_cache      — 1 hour   (historical record never changes intra-day)
  injury_cache   — 30 min   (squads can update up to kick-off)

Return types
------------
All public functions return normalized typed objects from engine.providers.base,
never raw scraper dicts. This keeps the prediction engine decoupled from the
collector's HTML parsing.
"""

from __future__ import annotations

import logging

from engine import cache_store
from engine.cache import fixture_cache, odds_cache, stats_cache, h2h_cache, injury_cache
from engine.odds import MatchOdds
from engine.providers.base import (
    NormalizedFixture,
    NormalizedH2HFixture,
    NormalizedInjury,
    NormalizedTeamStats,
)
from engine.utils import today_str, current_season

logger = logging.getLogger(__name__)


# ── Diagnostics ───────────────────────────────────────────────────────────────

def provider_status() -> list[dict]:
    return [{"name": "public-web-cache", "available": True, "supports_odds": False,
             "supports_h2h": False, "supports_injuries": False}]


def active_provider_name() -> str:
    return "public-web-cache"


def get_daily_call_count() -> int:
    """
    Return today's (UTC) API-Football call count (0 if API-Football is not configured).
    Kept for backward compatibility; may be removed in a future version.
    """
    return 0


# ── Fixtures ──────────────────────────────────────────────────────────────────

def fetch_all_fixtures_today(force_refresh: bool = False) -> list[NormalizedFixture]:
    """
    Return all fixtures for today (UTC) across every available competition.
    Cached for 60 s.  Pass force_refresh=True to bypass.
    """
    date_key = today_str()
    if not force_refresh:
        cached = fixture_cache.get(date_key)
        if cached is not None:
            return cached

    result = [cache_store.fixture_from_snapshot(item) for item in cache_store.load_snapshots(date_key)]
    fixture_cache.set(date_key, result)
    logger.info("[data] Cached %d fixtures for %s.", len(result), date_key)
    return result


def fetch_fixtures_today(league_id: int | None = None) -> list[NormalizedFixture]:
    """Return today's fixtures, optionally filtered to one league_id."""
    all_fixtures = fetch_all_fixtures_today()
    if league_id is None:
        return all_fixtures
    return [f for f in all_fixtures if f.league_id == league_id]


# ── Team statistics ───────────────────────────────────────────────────────────

def fetch_team_statistics(
    team_id: str, league_id: int, season: int | None = None,
) -> NormalizedTeamStats:
    """
    Return team stats for team_id in league_id for the given season.
    Cached for 15 minutes.
    """
    ssn = season or current_season()
    key = f"stats:{team_id}:{league_id}:{ssn}"
    cached = stats_cache.get(key)
    if cached is not None:
        return cached

    result = _stats_from_cache(team_id)
    stats_cache.set(key, result)
    return result


# ── Odds ──────────────────────────────────────────────────────────────────────

def fetch_odds(fix: NormalizedFixture) -> MatchOdds:
    """
    Return bookmaker odds for the fixture.
    Only available when the fixture's provider supports odds (API-Football does;
    Football-Data.org does not).  Returns empty MatchOdds when unavailable.
    Cached for 120 s.
    """
    # Namespace by provider: fixture IDs are provider-specific; without the
    # provider prefix a failover switch could serve cached IDs from the wrong
    # provider and silently corrupt odds lookups.
    key = f"odds:{fix.provider}:{fix.fixture_id}"
    cached = odds_cache.get(key)
    if cached is not None:
        return cached

    # Use the SAME provider that generated the fixture; odds fixture IDs are
    # provider-specific and cannot be cross-referenced.
    snapshot = cache_store.snapshot_for_fixture(fix.fixture_id) or {}
    raw = snapshot.get("odds") or {}
    result = MatchOdds(**{k: float(v) for k, v in raw.items() if k in {
        "home_win", "draw", "away_win", "over25", "under25", "over35",
        "under35", "btts_yes", "btts_no"
    }})
    odds_cache.set(key, result)
    return result


# ── Head-to-head ──────────────────────────────────────────────────────────────

def fetch_h2h(
    home_id:    str,
    away_id:    str,
    fixture_id: str | None = None,
    last:       int        = 10,
) -> list[NormalizedH2HFixture]:
    """
    Return up to `last` H2H fixtures between two teams.
    Cached for 1 hour (historical record doesn't change intra-day).

    fixture_id is required by Football-Data.org's head2head endpoint; ignored
    by API-Football which uses the team pair directly.
    """
    sorted_ids = sorted([home_id, away_id])
    key = f"h2h:{sorted_ids[0]}:{sorted_ids[1]}:{last}"
    cached = h2h_cache.get(key)
    if cached is not None:
        return cached

    snapshot = cache_store.snapshot_for_fixture(fixture_id or "") or {}
    result = []
    for item in snapshot.get("h2h", [])[:last]:
        from engine.providers.base import NormalizedH2HFixture
        result.append(NormalizedH2HFixture(
            home_id=str(item.get("home_id", "")), away_id=str(item.get("away_id", "")),
            goals_home=int(item.get("goals_home", 0)), goals_away=int(item.get("goals_away", 0)),
        ))
    h2h_cache.set(key, result)
    return result


# ── Injuries ──────────────────────────────────────────────────────────────────

def fetch_injuries(fix: NormalizedFixture) -> list[NormalizedInjury]:
    """
    Return injury / suspension list for the fixture.
    Only available when the fixture's provider supports injuries (API-Football does;
    Football-Data.org does not).  Returns [] when unavailable.
    Cached for 30 minutes.
    """
    # Namespace by provider for the same reason as odds cache above.
    key = f"injuries:{fix.provider}:{fix.fixture_id}"
    cached = injury_cache.get(key)
    if cached is not None:
        return cached

    result = []
    injury_cache.set(key, result)
    return result


# ── Team search / fixture lookup ──────────────────────────────────────────────

def search_team(name: str) -> list[dict]:
    """Search for teams by name. Returns provider-specific raw results."""
    return []


def fetch_fixture_by_teams(
    home_name: str, away_name: str,
) -> NormalizedFixture | None:
    """
    Find today's fixture for the given team name queries.

    Fast path: fuzzy substring match against the cached fixture list (works
    for all providers).
    Slow path: API team-search then ID-based match (API-Football only;
    Football-Data.org returns no search results, so this is a no-op there).
    """
    all_fixtures = fetch_all_fixtures_today()
    home_lower   = home_name.lower()
    away_lower   = away_name.lower()

    # Fast path — case-insensitive substring match on team names
    for fix in all_fixtures:
        if home_lower in fix.home_name.lower() and away_lower in fix.away_name.lower():
            return fix

    return None


# ── Cache control ─────────────────────────────────────────────────────────────

def invalidate_all_caches() -> None:
    """Flush every cache (useful after midnight or in tests)."""
    for c in (fixture_cache, odds_cache, stats_cache, h2h_cache, injury_cache):
        c.clear()
    logger.info("[data] All caches cleared.")


def _stats_from_cache(team_id: str) -> NormalizedTeamStats:
    for snapshot in cache_store.load_snapshots():
        stats = snapshot.get("stats") or {}
        for side in ("home", "away"):
            if str(snapshot.get(f"{side}_id")) == str(team_id):
                raw = stats.get(side) or {}
                return NormalizedTeamStats(
                    played=int(raw.get("played") or 0),
                    goals_for=float(raw.get("goals_for") or 0.0),
                    goals_against=float(raw.get("goals_against") or 0.0),
                    form=str(raw.get("form") or ""),
                )
    return NormalizedTeamStats(played=0, goals_for=0.0, goals_against=0.0, form="")
