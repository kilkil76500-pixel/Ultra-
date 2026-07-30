"""
engine/injuries.py — Injury impact system.

Accepts normalized injury records (NormalizedInjury) from any provider
and reduces team Poisson λ based on missing players.

Design principles (unchanged):
  • Key positions (GK, Attacker) carry a larger penalty than squad players.
  • Total penalty capped at 30 % (multiplier floor = 0.70).
  • Returns InjuryImpact(1.0, 1.0) safely when no data is available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from engine.providers.base import NormalizedInjury

logger = logging.getLogger(__name__)

_POSITION_PENALTY: dict[str, float] = {
    "goalkeeper": 0.07,
    "attacker":   0.06,
    "forward":    0.06,
    "midfielder": 0.04,
    "defender":   0.03,
}
_DEFAULT_PENALTY: float = 0.03
_MIN_MULTIPLIER:  float = 0.70


@dataclass(frozen=True)
class InjuryImpact:
    """
    Multiplicative penalties applied to each team's Poisson λ.
    Values in [0.70, 1.00].  1.0 = no impact.
    """
    home_multiplier: float = 1.0
    away_multiplier: float = 1.0
    home_out:        int   = 0
    away_out:        int   = 0

    @property
    def has_data(self) -> bool:
        return (self.home_out + self.away_out) > 0


def compute_injury_impact(
    injuries:     list[NormalizedInjury],
    home_team_id: str,
    away_team_id: str,
) -> InjuryImpact:
    """
    Compute injury multipliers from normalized injury records.

    Parameters
    ----------
    injuries      : list from data.fetch_injuries() — may be empty
    home_team_id  : string ID of the home team
    away_team_id  : string ID of the away team
    """
    if not injuries:
        return InjuryImpact()

    home_pen = away_pen = 0.0
    home_out = away_out = 0

    for inj in injuries:
        penalty = _POSITION_PENALTY.get(inj.position, _DEFAULT_PENALTY)
        if inj.team_id == home_team_id:
            home_pen += penalty
            home_out += 1
        elif inj.team_id == away_team_id:
            away_pen += penalty
            away_out += 1

    home_mult = max(_MIN_MULTIPLIER, 1.0 - home_pen)
    away_mult = max(_MIN_MULTIPLIER, 1.0 - away_pen)

    impact = InjuryImpact(
        home_multiplier = round(home_mult, 3),
        away_multiplier = round(away_mult, 3),
        home_out        = home_out,
        away_out        = away_out,
    )
    logger.debug("Injury impact: %s", impact)
    return impact
