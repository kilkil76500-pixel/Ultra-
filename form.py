"""
engine/form.py — Form variance analysis and dynamic chaos computation.

Implements the adaptive chaos model from the reference methodology:

    chaos = tier_base
          + h2h_instability × 0.05          (H2H goal/diff variance)
          + 0.05  (when key players are missing)

Why variance-based chaos?
  • The existing engine used a fixed per-tier constant (0.05 / 0.15 / 0.30).
    This treats all Tier-2 matches identically even though a routine mid-table
    clash has very different unpredictability to a top-of-table thriller.
  • Variance in H2H total goals and goal-differentials is a proven proxy for
    matchup volatility — high variance = historically unpredictable fixture.
  • This module supplies compute_dynamic_chaos() to predictor.predict() which
    replaces the static _CHAOS_LEVEL lookup with a per-fixture value.

No Telegram code.  No API calls.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

# ── FormStats ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FormStats:
    """Variance metrics derived from recent match scorelines."""
    goal_diff_variance:   float   # pvariance of (goals_scored − goals_conceded)
    total_goals_variance: float   # pvariance of (goals_scored + goals_conceded)
    avg_goals_scored:     float   # mean goals scored per match
    avg_goals_conceded:   float   # mean goals conceded per match
    sample_size:          int     # number of matches used


# Returned when no scoreline data is available — neutral, non-zero avoidance.
NEUTRAL_FORM = FormStats(0.0, 0.0, 1.20, 1.20, 0)


def compute_form_stats(recent_scores: list[tuple[int, int]]) -> FormStats:
    """
    Compute variance stats from a list of (goals_for, goals_against) tuples.

    Each tuple represents one recent match from a single team's perspective:
        (goals that team scored, goals that team conceded)

    This is used to characterise match-level variance rather than a simple
    W/D/L string.  Higher goal_diff_variance indicates an inconsistent team
    that swings between big wins and heavy losses.

    Returns NEUTRAL_FORM when sample is empty or has only one entry
    (pvariance is undefined for n < 2).
    """
    n = len(recent_scores)
    if n == 0:
        return NEUTRAL_FORM

    goals_for     = [g for g, _ in recent_scores]
    goals_against = [c for _, c in recent_scores]
    goal_diffs    = [g - c for g, c in recent_scores]
    total_goals   = [g + c for g, c in recent_scores]

    return FormStats(
        goal_diff_variance   = statistics.pvariance(goal_diffs)   if n > 1 else 0.0,
        total_goals_variance = statistics.pvariance(total_goals) if n > 1 else 0.0,
        avg_goals_scored     = sum(goals_for)     / n,
        avg_goals_conceded   = sum(goals_against) / n,
        sample_size          = n,
    )


# ── H2H volatility ────────────────────────────────────────────────────────────

def compute_h2h_volatility(
    h2h_scores: tuple[tuple[int, int], ...],
) -> tuple[float, float]:
    """
    Return (total_goals_variance, goal_diff_variance) from H2H match scores.

    Each score tuple is (home_goals, away_goals) already corrected for
    orientation (i.e. from the current home team's perspective) by h2h.py.

    A high total_goals_variance means some H2H meetings are 0-0 while others
    are 5-3 — genuinely hard to predict total goals.
    A high goal_diff_variance means the rivalry swings both ways — no dominant
    side — so the outcome is volatile.

    Returns (0.0, 0.0) when the sample has fewer than 2 entries.
    """
    n = len(h2h_scores)
    if n < 2:
        return 0.0, 0.0
    total_goals = [a + b for a, b in h2h_scores]
    goal_diffs  = [a - b for a, b in h2h_scores]
    return (
        statistics.pvariance(total_goals),
        statistics.pvariance(goal_diffs),
    )


# ── Dynamic chaos computation ─────────────────────────────────────────────────

# Structural base per tier — reflects irreducible data-quality uncertainty
# even when form and H2H look stable.
_TIER_CHAOS_BASE: dict[int, float] = {1: 0.05, 2: 0.12, 3: 0.25}


def compute_dynamic_chaos(
    h2h_scores:          tuple[tuple[int, int], ...],
    tier:                int,
    key_players_missing: bool = False,
) -> float:
    """
    Compute a per-fixture adaptive chaos coefficient.

    Replaces the static ``_CHAOS_LEVEL`` tier lookup with a value that adapts
    to the specific historical volatility of this matchup.

    Formula (from reference methodology):
        base            = _TIER_CHAOS_BASE[tier]        (structural floor)
        h2h_instability = normalised H2H variance        (from scorelines)
        player_flag     = +0.05 when key players are absent

        chaos = base + h2h_instability × 0.05 [+ player_flag]

    The normalisation divisor (15.0) maps realistic football pvariance ranges
    (~0–6 for total_goals, ~0–8 for goal_diffs) into the ~0–0.10 contribution
    band so the tier base remains the primary driver.

    Output: clamped to [0.04, 0.45]

    Parameters
    ----------
    h2h_scores          : orientation-corrected (home_g, away_g) tuples
    tier                : league tier  1=elite, 2=strong, 3=low-data
    key_players_missing : True when injury data confirms multiple key absences
    """
    base = _TIER_CHAOS_BASE.get(tier, 0.20)

    h2h_goals_var, h2h_diff_var = compute_h2h_volatility(h2h_scores)
    # Normalise: realistic pvariance sits in 0–8 range for football,
    # dividing by 15 maps the sum of both variances into ~0–1.1 band,
    # then scaling by 0.05 keeps the contribution below 0.055 extra chaos.
    h2h_instability = (h2h_goals_var + h2h_diff_var) / 15.0

    chaos = base + h2h_instability * 0.05

    if key_players_missing:
        chaos += 0.05

    return round(min(max(chaos, 0.04), 0.45), 4)


# ── Mental strength proxy ─────────────────────────────────────────────────────

def compute_mental_strength(form_str: str) -> float:
    """
    Derive a mental/momentum score (0.0–1.0) from a team's form string.

    The mental dimension (from the reference AdaptiveWeights model) captures
    psychological momentum: a team winning their last 3 but who lost the two
    before that has positive momentum.  A team drawing everything has neutral
    mental.

    Method:
      • Split the 5-match window into "late" (last 3) and "early" (preceding 2).
      • Score each segment: W=1, D=0.5, L=0.
      • Blend: 70 % late form + 30 % upward-trend bonus.

    Returns 0.5 when fewer than 3 results are available.
    """
    if not form_str or len(form_str) < 3:
        return 0.5

    recent = form_str[-5:] if len(form_str) >= 5 else form_str

    def _segment_score(seg: str) -> float:
        if not seg:
            return 0.5
        pts = sum(1.0 if c == "W" else 0.5 if c == "D" else 0.0 for c in seg)
        return pts / len(seg)

    if len(recent) >= 5:
        late  = _segment_score(recent[-3:])    # most recent 3
        early = _segment_score(recent[-5:-3])  # 2 matches before those
        trend = max(0.0, late - early)          # positive = improving momentum
        mental = late * 0.70 + trend * 0.30
    else:
        mental = _segment_score(recent)

    return round(min(max(mental, 0.0), 1.0), 3)
