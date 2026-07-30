"""
engine/h2h.py — Head-to-head weighting system.

Accepts normalized H2H fixtures (NormalizedH2HFixture) from any provider
and produces multiplicative λ adjustment factors for the prediction engine.

The orientation of each historical match (which team played at home) may differ
from today's fixture.  _reorient() corrects for this so all goal statistics
are expressed from today's home team's perspective.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from engine.providers.base import NormalizedH2HFixture

logger = logging.getLogger(__name__)

_MIN_FACTOR = 0.88
_MAX_FACTOR  = 1.12
_H2H_WINDOW  = 10


@dataclass(frozen=True)
class H2HWeight:
    """
    Multiplicative adjustments for home / away λ values.

    home_factor > 1 → home team historically dominates this matchup.
    away_factor > 1 → away team historically dominates.
    Both bounded to [0.88, 1.12].

    h2h_scores : orientation-corrected (home_goals, away_goals) tuples
                 from today's home team's perspective — fed to
                 form.compute_dynamic_chaos() for per-fixture chaos.
    goal_power : blended win-rate × goal-ratio power score (ref h2h_power).
    """
    home_factor:  float = 1.0
    away_factor:  float = 1.0
    matches_used: int   = 0

    h2h_scores: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    goal_power: float = 1.0

    @property
    def has_data(self) -> bool:
        return self.matches_used > 0


# ── Public API ────────────────────────────────────────────────────────────────

def compute_h2h_weight(
    h2h_fixtures:  list[NormalizedH2HFixture],
    home_team_id:  str,
    away_team_id:  str,
) -> H2HWeight:
    """
    Compute H2H multipliers from normalized H2H history.

    Parameters
    ----------
    h2h_fixtures  : list from data.fetch_h2h() — may be empty
    home_team_id  : string ID of today's home team
    away_team_id  : string ID of today's away team
    """
    if not h2h_fixtures:
        return H2HWeight()

    recent = h2h_fixtures[-_H2H_WINDOW:]

    # Reorient each historical fixture to today's perspective
    parsed: list[tuple[str, tuple[int, int]]] = []
    for fix in recent:
        outcome, score = _reorient(fix, home_team_id, away_team_id)
        if outcome != "unknown":
            parsed.append((outcome, score))

    if not parsed:
        return H2HWeight(matches_used=0)

    n = len(parsed)

    # Recency-weighted outcome counting (oldest → weight 1.0, newest → 2.0)
    home_w = draw_w = away_w = 0.0
    home_goals_total = conceded_total = 0.0

    for rank, (outcome, (hg, ag)) in enumerate(parsed):
        w = 1.0 + rank / max(n - 1, 1)
        if   outcome == "home": home_w += w
        elif outcome == "draw": draw_w += w
        elif outcome == "away": away_w += w
        home_goals_total += hg
        conceded_total   += ag

    total_w = home_w + draw_w + away_w

    home_win_rate = home_w / total_w
    away_win_rate = away_w / total_w

    # Goal-power (reference h2h_power formula)
    avg_gf     = home_goals_total / n
    avg_ga     = conceded_total   / n
    goal_ratio = avg_gf / max(0.5, avg_ga)
    goal_power = home_win_rate * 0.6 + min(goal_ratio, 3.0) / 3.0 * 0.4

    # λ adjustment factors
    home_factor = _MIN_FACTOR + (_MAX_FACTOR - _MIN_FACTOR) * (
        home_win_rate * 0.70 + goal_power * 0.30
    )
    away_factor = _MIN_FACTOR + (_MAX_FACTOR - _MIN_FACTOR) * (
        away_win_rate * 0.70 + (1.0 - goal_power) * 0.30
    )
    home_factor = max(_MIN_FACTOR, min(_MAX_FACTOR, home_factor))
    away_factor = max(_MIN_FACTOR, min(_MAX_FACTOR, away_factor))

    h2h_scores: tuple[tuple[int, int], ...] = tuple(score for _, score in parsed)

    weight = H2HWeight(
        home_factor  = round(home_factor,  3),
        away_factor  = round(away_factor,  3),
        matches_used = n,
        h2h_scores   = h2h_scores,
        goal_power   = round(goal_power,   3),
    )
    logger.debug(
        "H2H weight: %s (home_rate=%.2f, away_rate=%.2f, goal_power=%.3f, n=%d)",
        weight, home_win_rate, away_win_rate, goal_power, n,
    )
    return weight


# ── Internal helpers ──────────────────────────────────────────────────────────

def _reorient(
    fix:          NormalizedH2HFixture,
    home_team_id: str,
    away_team_id: str,
) -> tuple[str, tuple[int, int]]:
    """
    Return (outcome, (home_goals, away_goals)) from today's home/away perspective.

    NormalizedH2HFixture stores the raw historical home/away orientation.
    We flip goals when the historical sides were the reverse of today's fixture.
    """
    hg, ag = fix.goals_home, fix.goals_away

    if fix.home_id == home_team_id and fix.away_id == away_team_id:
        # Same orientation as today
        pass
    elif fix.home_id == away_team_id and fix.away_id == home_team_id:
        # Historically reversed — swap to today's perspective
        hg, ag = ag, hg
    else:
        return "unknown", (0, 0)

    if   hg > ag: outcome = "home"
    elif hg < ag: outcome = "away"
    else:         outcome = "draw"

    return outcome, (hg, ag)
