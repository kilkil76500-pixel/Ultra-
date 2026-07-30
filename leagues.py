"""
engine/leagues.py — Global league classification system.

Every competition discovered from the API response is mapped to:
  • Tier 1  — elite international / domestic (highest data quality)
  • Tier 2  — strong secondary competitions and continental cups
  • Tier 3  — all remaining leagues (smaller, lower data reliability)

The tier drives:
  • league-calibrated goal averages for the Poisson model
  • confidence penalty applied to the prediction score
  • EV penalty applied during value analysis

No Telegram code. No API calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── LeagueInfo dataclass ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class LeagueInfo:
    id:                 int
    name:               str
    country:            str
    tier:               int    # 1 | 2 | 3
    avg_home_goals:     float  # calibrated for Poisson λ_home baseline
    avg_away_goals:     float  # calibrated for Poisson λ_away baseline
    confidence_penalty: float  # 0-1 multiplier on raw confidence score
    tier_label:         str    # "TIER 1" | "TIER 2" | "TIER 3"
    tier_emoji:         str    # display emoji

    @property
    def ev_penalty(self) -> float:
        """Fraction of EV to retain. Lower tiers → retain less."""
        _EV_RETAIN = {1: 1.00, 2: 0.90, 3: 0.75}
        return _EV_RETAIN.get(self.tier, 0.75)


# ── Goal-average calibration per tier ────────────────────────────────────────
# Tier 1: well-documented averages from historical data
# Tier 2: slightly higher variance; use slightly raised values
# Tier 3: wide variance; regress toward global mean

_TIER_GOALS: dict[int, tuple[float, float]] = {
    1: (1.50, 1.15),
    2: (1.45, 1.10),
    3: (1.40, 1.08),
}
_TIER_CONF_PENALTY: dict[int, float] = {1: 1.00, 2: 0.85, 3: 0.65}
_TIER_LABEL:        dict[int, str]   = {1: "TIER 1", 2: "TIER 2", 3: "TIER 3"}
_TIER_EMOJI:        dict[int, str]   = {1: "🌟", 2: "⚡", 3: "🔵"}


# ── Known league-ID → tier mapping ───────────────────────────────────────────
# These IDs come from the API-Football v3 league IDs.
# Anything not listed here falls through to name-based heuristics → Tier 3.

TIER_1_IDS: frozenset[int] = frozenset({
    1,   # World Cup
    2,   # UEFA Champions League
    9,   # Copa América
    39,  # England Premier League
    61,  # France Ligue 1
    78,  # Germany Bundesliga
    135, # Italy Serie A
    140, # Spain La Liga
})

TIER_2_IDS: frozenset[int] = frozenset({
    3,   # UEFA Europa League
    4,   # UEFA Euro Championship
    5,   # UEFA Nations League
    6,   # FIFA Friendlies
    10,  # CONMEBOL Libertadores
    11,  # CONMEBOL Sudamericana
    13,  # CONCACAF Champions League
    71,  # Brazil Série A
    72,  # Brazil Série B
    94,  # Portugal Primeira Liga
    88,  # Netherlands Eredivisie
    144, # Belgium Pro League
    203, # Turkey Süper Lig
    207, # Switzerland Super League
    218, # Austria Bundesliga
    235, # Russia Premier League
    262, # Mexico Liga MX
    128, # Argentina Primera División
    253, # USA MLS
    848, # UEFA Europa Conference League
})

# ── Name-pattern heuristics for unknown leagues ───────────────────────────────
# Applied when league_id is not in TIER_1_IDS or TIER_2_IDS.
# Matches are case-insensitive substring checks on (name + country).

_TIER2_NAME_HINTS: list[str] = [
    "champions league", "europa league", "conference league",
    "libertadores", "sudamericana", "nations league",
    "serie a", "primera division", "super lig",
    "eredivisie", "primeira liga", "pro league",
    "premier liga", "bundesliga", "primera",
]

_TIER1_NAME_HINTS: list[str] = [
    "world cup", "euro ", "copa america",
    "premier league", "la liga", "ligue 1",
]


# ── Public classification function ───────────────────────────────────────────

def classify(
    league_id: int,
    name:      str = "",
    country:   str = "",
) -> LeagueInfo:
    """
    Return a LeagueInfo for any competition discovered from the API.
    Never raises — always falls back to Tier 3.

    Parameters
    ----------
    league_id : integer ID from the API fixture['league']['id']
    name      : competition name string (optional but improves classification)
    country   : country string (optional)
    """
    tier = _determine_tier(league_id, name, country)
    avg_h, avg_a = _TIER_GOALS[tier]
    confidence_penalty = _TIER_CONF_PENALTY[tier]
    # V20 — remplace la pénalité fixe par la valeur apprise (engine.
    # league_calibration) quand une recalibration a été acceptée pour ce
    # tier. Import local pour éviter un cycle (league_calibration importe
    # _TIER_CONF_PENALTY depuis ce module). Ne lève jamais : un souci de
    # lecture ne doit jamais casser la classification d'un match.
    try:
        from engine.league_calibration import load_league_calibration
        learned = load_league_calibration().tier_confidence_penalty
        if tier in learned:
            confidence_penalty = learned[tier]
    except Exception as exc:
        logger.warning("[leagues] pénalité apprise indisponible : %s", exc)
    return LeagueInfo(
        id=league_id,
        name=name or f"League {league_id}",
        country=country,
        tier=tier,
        avg_home_goals=avg_h,
        avg_away_goals=avg_a,
        confidence_penalty=confidence_penalty,
        tier_label=_TIER_LABEL[tier],
        tier_emoji=_TIER_EMOJI[tier],
    )


def _determine_tier(league_id: int, name: str, country: str) -> int:
    """Return 1, 2, or 3."""
    if league_id in TIER_1_IDS:
        return 1
    if league_id in TIER_2_IDS:
        return 2

    # Heuristic name matching (case-insensitive)
    combined = (name + " " + country).lower()
    for hint in _TIER1_NAME_HINTS:
        if hint in combined:
            return 1
    for hint in _TIER2_NAME_HINTS:
        if hint in combined:
            return 2

    return 3


def tier_display(tier: int) -> str:
    """Return a formatted tier label with emoji."""
    return f"{_TIER_EMOJI.get(tier, '🔵')} {_TIER_LABEL.get(tier, 'TIER 3')}"


def all_tier_ids(tier: int) -> frozenset[int]:
    """Return the set of known IDs for a given tier (for informational use)."""
    if tier == 1:
        return TIER_1_IDS
    if tier == 2:
        return TIER_2_IDS
    return frozenset()  # Tier 3 is the open catch-all
