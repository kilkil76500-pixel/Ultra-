"""Tests de la façade de données et de ses caches TTL."""

from __future__ import annotations

from engine import data
from engine.odds import MatchOdds
from engine.providers.base import NormalizedFixture


def fixture(name="Paris", away="Lyon"):
    return NormalizedFixture("f1", "1", "2", name, away, 61, "Ligue 1",
                             "France", 100, "web-cache")


def test_fixture_fetch_is_cached_and_filterable(monkeypatch):
    data.invalidate_all_caches()
    calls = []
    monkeypatch.setattr(data, "today_str", lambda: "2099-01-01")
    monkeypatch.setattr(data.cache_store, "load_snapshots",
                        lambda day=None: [{"home": "Paris", "away": "Lyon",
                                           "cache_key": "f1", "league_id": 61}] )
    monkeypatch.setattr(data.cache_store, "fixture_from_snapshot",
                        lambda row: fixture())

    first = data.fetch_all_fixtures_today(force_refresh=True)
    second = data.fetch_all_fixtures_today()
    assert first == second
    assert data.fetch_fixtures_today(61) == first
    assert data.fetch_fixtures_today(99) == []


def test_stats_and_odds_use_snapshot_cache(monkeypatch):
    data.invalidate_all_caches()
    monkeypatch.setattr(data, "current_season", lambda: 2099)
    monkeypatch.setattr(data.cache_store, "load_snapshots", lambda day=None: [
        {"home_id": "1", "stats": {"home": {"played": 5, "goals_for": 8,
                                             "goals_against": 3, "form": "WWDLW"}},
         "cache_key": "f1", "odds": {"home_win": 2.1, "draw": 3.2}}
    ])
    stats = data.fetch_team_statistics("1", 61)
    assert stats.played == 5
    odds = data.fetch_odds(fixture())
    assert odds.home_win == 2.1
    assert odds.draw == 3.2


def test_h2h_and_fixture_lookup(monkeypatch):
    data.invalidate_all_caches()
    fix = fixture()
    monkeypatch.setattr(data, "fetch_all_fixtures_today", lambda: [fix])
    assert data.fetch_fixture_by_teams("par", "LYO") == fix
    monkeypatch.setattr(data.cache_store, "snapshot_for_fixture", lambda _: {
        "h2h": [{"home_id": "1", "away_id": "2", "goals_home": 2, "goals_away": 1}]
    })
    history = data.fetch_h2h("1", "2", "f1")
    assert len(history) == 1
    assert history[0].goals_home == 2
