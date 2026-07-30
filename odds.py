"""
engine/odds.py — Parse and normalise bookmaker odds from API-Football.

Responsibility: convert raw odds API response into clean dicts.
No prediction logic. No Telegram code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from engine.utils import safe_float, safe_get

logger = logging.getLogger(__name__)

# Bet names as used by API-Football
_MATCH_WINNER   = "Match Winner"
_GOALS_OVER_UND = "Goals Over/Under"
_BTTS           = "Both Teams Score"


@dataclass
class MatchOdds:
    """Normalised decimal odds for a single fixture."""
    home_win: float = 0.0
    draw:     float = 0.0
    away_win: float = 0.0
    over25:   float = 0.0
    under25:  float = 0.0
    over35:   float = 0.0   # Goals Over 3.5 market
    under35:  float = 0.0   # Goals Under 3.5 market
    btts_yes: float = 0.0
    btts_no:  float = 0.0

    @property
    def available(self) -> bool:
        return self.home_win > 0 and self.away_win > 0


def _find_bet(bookmakers: list[dict], bet_name: str) -> list[dict]:
    """Return the values list for the named bet from the first bookmaker that has it."""
    for bm in bookmakers:
        for bet in safe_get(bm, "bets", default=[]):
            if safe_get(bet, "name", default="") == bet_name:
                return safe_get(bet, "values", default=[])
    return []


def _value_by_label(values: list[dict], label: str) -> float:
    """Return odd (float) for a specific value label."""
    for v in values:
        raw = safe_get(v, "value", default=None)
        if not isinstance(raw, str):
            continue  # guard: skip null / non-string entries from the API
        if raw.lower() == label.lower():
            return safe_float(safe_get(v, "odd"), 0.0)
    return 0.0


def parse_odds(raw_odds_response: list[dict]) -> MatchOdds:
    """
    Parse the list returned by data.fetch_odds() into a MatchOdds object.
    Returns a MatchOdds with all zeros if the response is empty or malformed.
    """
    if not raw_odds_response:
        return MatchOdds()

    # Collect all bookmakers from the first odds entry (fixture-level)
    bookmakers: list[dict] = []
    for entry in raw_odds_response:
        bookmakers.extend(safe_get(entry, "bookmakers", default=[]))

    if not bookmakers:
        return MatchOdds()

    # Match Winner
    mw = _find_bet(bookmakers, _MATCH_WINNER)
    home_win = _value_by_label(mw, "Home")
    draw     = _value_by_label(mw, "Draw")
    away_win = _value_by_label(mw, "Away")

    # Goals Over/Under (2.5 and 3.5 from the same market)
    gu      = _find_bet(bookmakers, _GOALS_OVER_UND)
    over25  = _value_by_label(gu, "Over 2.5")
    under25 = _value_by_label(gu, "Under 2.5")
    over35  = _value_by_label(gu, "Over 3.5")
    under35 = _value_by_label(gu, "Under 3.5")

    # BTTS
    bt = _find_bet(bookmakers, _BTTS)
    btts_yes = _value_by_label(bt, "Yes")
    btts_no  = _value_by_label(bt, "No")

    odds = MatchOdds(
        home_win=home_win,
        draw=draw,
        away_win=away_win,
        over25=over25,
        under25=under25,
        over35=over35,
        under35=under35,
        btts_yes=btts_yes,
        btts_no=btts_no,
    )
    logger.debug("Parsed odds: %s", odds)
    return odds


def implied_probability(decimal_odd: float) -> float:
    """Convert a decimal odd to its implied (bookmaker) probability."""
    if decimal_odd <= 0:
        return 0.0
    return round(1 / decimal_odd, 4)


def odds_available(match_odds: MatchOdds) -> bool:
    """Return True only if we have at least the 1X2 market."""
    return match_odds.available
