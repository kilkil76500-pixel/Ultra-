"""
engine/providers/base.py — Normalized data types shared across the engine.

V17.1 : ce module ne contient plus qu'un jeu de types partagés
(NormalizedFixture, NormalizedTeamStats, etc.). Les implémentations
API-Football / Football-Data.org et le routeur multi-providers ont été
retirés (code mort — jamais instanciés, ce bot n'utilise aucune API
payante). Toutes les données viennent de engine.web_collector (scraping
Forebet) via engine.cache_store ; ces types restent la frontière typée
entre la collecte et le moteur de prédiction.
"""

from __future__ import annotations

from dataclasses import dataclass


# ── Normalized data structs ───────────────────────────────────────────────────

@dataclass(frozen=True)
class NormalizedFixture:
    """One upcoming/live match in provider-agnostic terms."""
    fixture_id:     str        # cache key, always stored as string
    home_id:        str        # home team ID, always string
    away_id:        str        # away team ID, always string
    home_name:      str
    away_name:      str
    league_id:      int        # competition ID
    league_name:    str
    league_country: str
    timestamp:      int | None # UTC unix timestamp; None if unknown
    provider:       str        # always "web-cache" (Forebet)


@dataclass(frozen=True)
class NormalizedTeamStats:
    """Season / recent-match aggregates for one team."""
    played:        int
    goals_for:     float
    goals_against: float
    form:          str   # "WWDLW…" oldest-to-newest; empty string when unavailable


@dataclass(frozen=True)
class TeamExtendedStats:
    """
    Extended statistics scraped from Forebet — enriches the V13 strength index.
    All percentage fields are 0-100. -1.0 means the value was not available.
    """
    # Historical match-outcome percentage stats
    btts_pct:           float = -1.0   # Both teams scored in X% of recent matches
    over25_pct:         float = -1.0   # Over 2.5 goals in X% of recent matches
    over15_pct:         float = -1.0   # Over 1.5 goals in X% of recent matches
    clean_sheet_pct:    float = -1.0   # Kept a clean sheet in X% of matches
    failed_score_pct:   float = -1.0   # Failed to score in X% of matches
    # Split form strings (oldest→newest, uppercase W/D/L)
    home_form:          str   = ""     # Form when playing specifically at home
    away_form:          str   = ""     # Form when playing specifically away
    # League table context
    league_position:    int   = 0      # Position in league table (0 = unknown)
    league_size:        int   = 20     # Total teams in the league
    # Current streak: +N = N consecutive wins, −N = N consecutive losses, 0 = neutral
    current_streak:     int   = 0

    @property
    def has_position(self) -> bool:
        return self.league_position > 0

    @property
    def position_ratio(self) -> float:
        """0.0 = bottom of table, 1.0 = top of table."""
        if self.league_position <= 0:
            return 0.5  # neutral assumption
        total = max(self.league_size, max(self.league_position, 2))
        return 1.0 - (self.league_position - 1) / max(total - 1, 1)


@dataclass(frozen=True)
class NormalizedH2HFixture:
    """One historical H2H match in the provider's raw home/away orientation."""
    home_id:    str
    away_id:    str
    goals_home: int
    goals_away: int


@dataclass(frozen=True)
class NormalizedInjury:
    """One injured or suspended player linked to their team."""
    team_id:  str
    position: str   # "goalkeeper" | "attacker" | "midfielder" | "defender" | ""
