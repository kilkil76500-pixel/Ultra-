"""
engine/value.py — Expected Value (EV) calculation and bet flagging.

Upgrades over v1:
  • analyse_value() accepts a LeagueInfo and applies a tier EV penalty.
    Lower-tier leagues → EV is deflated to avoid overvaluing weak data.
  • Confidence filter: bets with confidence_pct below the threshold are
    excluded from the value report to reduce noise.
  • value_score() incorporates confidence_pct so the scanner ranks
    high-confidence opportunities above equally-EV low-confidence ones.

Formula: EV = (model_prob × decimal_odds) − 1
Positive EV means the model sees an edge over the bookmaker's implied prob.

No API calls. No Telegram code.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import config
from engine.odds import MatchOdds, implied_probability
from engine.predictor import PredictionResult
from engine.utils import ev_label

if TYPE_CHECKING:
    from engine.leagues import LeagueInfo

logger = logging.getLogger(__name__)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class BetOpportunity:
    label:         str    # e.g. "Home Win", "BTTS Yes"
    model_prob:    float  # 0-1 probability from our model
    bm_odds:       float  # bookmaker decimal odds
    bm_prob:       float  # implied probability from bookmaker
    ev:            float  # raw expected value
    adj_ev:        float  # EV after tier penalty
    market_edge:   float  # model_prob − bm_prob (positive = model sees edge)
    rating:        str    # "HIGH" / "MEDIUM" / "LOW" / "NO VALUE"
    passes_filter: bool   # True if confidence threshold is met

    @property
    def is_value(self) -> bool:
        """True when adj_ev is positive AND confidence filter is met."""
        return self.adj_ev >= config.EV_LOW and self.passes_filter


@dataclass
class ValueReport:
    opportunities: list[BetOpportunity]
    best_ev:       float  # highest adj_ev across all bets
    best_label:    str    # label of the best bet
    tier:          int    # league tier this report is for

    @property
    def has_value(self) -> bool:
        return any(o.is_value for o in self.opportunities)


# ── Core EV helpers ───────────────────────────────────────────────────────────

def _calc_ev(model_prob: float, decimal_odds: float) -> float:
    """Return raw EV for one bet."""
    if decimal_odds <= 0 or model_prob <= 0:
        return -1.0
    return round(model_prob * decimal_odds - 1, 4)


def _rating(adj_ev: float) -> str:
    if adj_ev >= config.EV_HIGH:
        return "HIGH"
    if adj_ev >= config.EV_MEDIUM:
        return "MEDIUM"
    if adj_ev >= config.EV_LOW:
        return "LOW"
    return "NO VALUE"


# ── Public API ────────────────────────────────────────────────────────────────

def analyse_value(
    prediction:     PredictionResult,
    match_odds:     MatchOdds,
    league_info:    "LeagueInfo | None" = None,
) -> ValueReport:
    """
    Compare model probabilities against bookmaker odds and flag value bets.

    Parameters
    ----------
    prediction  : PredictionResult from predictor.predict()
    match_odds  : MatchOdds from odds.parse_odds()
    league_info : LeagueInfo for this match (None → no tier penalty, no filter)

    Returns
    -------
    ValueReport with all bet opportunities sorted by adj_ev descending.
    """
    tier       = league_info.tier       if league_info else 1
    ev_retain  = league_info.ev_penalty if league_info else 1.0
    conf_pct   = prediction.confidence_pct

    passes_filter = (
        conf_pct >= config.CONFIDENCE_THRESHOLD
        if config.CONFIDENCE_THRESHOLD > 0
        else True
    )

    # ── Core markets ──────────────────────────────────────────────────────────
    candidates: list[tuple[str, float, float]] = [
        ("Home Win",  prediction.home_win_prob,       match_odds.home_win),
        ("Draw",      prediction.draw_prob,            match_odds.draw),
        ("Away Win",  prediction.away_win_prob,        match_odds.away_win),
        ("Over 2.5",  prediction.over25_prob,          match_odds.over25),
        ("Under 2.5", prediction.under25_prob,         match_odds.under25),
        ("Over 3.5",  prediction.over35_prob,          match_odds.over35),
        ("Under 3.5", prediction.under35_prob,         match_odds.under35),
        ("BTTS Yes",  prediction.btts_prob,            match_odds.btts_yes),
        ("BTTS No",   1.0 - prediction.btts_prob,      match_odds.btts_no),
    ]

    # ── BTTS combo markets (reference methodology) ────────────────────────────
    # Compound markets: BTTS outcome × Over/Under 2.5.
    #
    # Model probability: true joint co-occurrence counts from Monte Carlo,
    # tracked per-iteration inside montecarlo._run_simulation_loop().  These
    # are NOT products of marginals — BTTS and O/U share the same goal draws
    # so they are strongly correlated (e.g. BTTS Yes + Under 2.5 is only
    # possible with exactly 1-1, which has much lower probability than the
    # product P(BTTS Yes) × P(Under 2.5) would suggest).
    #
    # Bookmaker odds: synthesised from constituent implied probabilities as
    # 1 / (bm_implied_A × bm_implied_B).  This assumes the bookie prices
    # the combo legs independently, so the synthetic odds slightly over-
    # estimate the true fair-value combo odds, giving our model a natural
    # edge when EV is genuinely positive.
    #
    # Combos are only added when both constituent odds are available (> 0).

    if match_odds.btts_yes > 0 and match_odds.over25 > 0:
        bm_prob = (1.0 / match_odds.btts_yes) * (1.0 / match_odds.over25)
        bm_odds = round(1.0 / bm_prob, 2) if bm_prob > 0 else 0.0
        candidates.append((
            "BTTS+Over 2.5",
            prediction.btts_yes_over25_prob,   # true joint from MC
            bm_odds,
        ))

    if match_odds.btts_yes > 0 and match_odds.under25 > 0:
        bm_prob = (1.0 / match_odds.btts_yes) * (1.0 / match_odds.under25)
        bm_odds = round(1.0 / bm_prob, 2) if bm_prob > 0 else 0.0
        candidates.append((
            "BTTS+Under 2.5",
            prediction.btts_yes_under25_prob,  # true joint from MC (≈ P(1-1))
            bm_odds,
        ))

    if match_odds.btts_no > 0 and match_odds.over25 > 0:
        bm_prob = (1.0 / match_odds.btts_no) * (1.0 / match_odds.over25)
        bm_odds = round(1.0 / bm_prob, 2) if bm_prob > 0 else 0.0
        candidates.append((
            "No BTTS+Over 2.5",
            prediction.nbtts_over25_prob,      # true joint from MC
            bm_odds,
        ))

    opps: list[BetOpportunity] = []
    for label, model_prob, bm_odds in candidates:
        if bm_odds <= 0:
            continue  # market not available or not offered by bookmaker
        ev          = _calc_ev(model_prob, bm_odds)
        adj_ev      = round(ev * ev_retain, 4)
        bm_prob     = implied_probability(bm_odds)
        market_edge = round(model_prob - bm_prob, 4)   # positive = model edge
        rating      = _rating(adj_ev)
        opps.append(BetOpportunity(
            label=label,
            model_prob=model_prob,
            bm_odds=bm_odds,
            bm_prob=bm_prob,
            ev=ev,
            adj_ev=adj_ev,
            market_edge=market_edge,
            rating=rating,
            passes_filter=passes_filter,
        ))

    opps.sort(key=lambda o: o.adj_ev, reverse=True)

    best = opps[0] if opps else None
    return ValueReport(
        opportunities=opps,
        best_ev=best.adj_ev if best else -1.0,
        best_label=best.label if best else "—",
        tier=tier,
    )


def value_score(
    report:         ValueReport,
    confidence_pct: float = 0.0,
) -> float:
    """
    Single scalar ranking score for the global scanner.

    Combines:
      • maximum adj_ev of bets that clear the SCAN_MIN_EV threshold
      • count of such bets (breadth bonus: more opportunities = higher rank)
      • confidence_pct as a multiplier (high-confidence beats low-confidence
        at the same raw EV)

    Bets below SCAN_MIN_EV are excluded so the scanner only surfaces
    opportunities with a meaningful edge (≥ 5 % by default).
    Higher score = better global ranking.
    """
    qualifying = [o.adj_ev for o in report.opportunities
                  if o.adj_ev >= config.SCAN_MIN_EV and o.passes_filter]
    if not qualifying:
        return 0.0
    # Log-scale breadth bonus: each additional qualifying market adds diminishing
    # value (log vs linear), preventing matches with many marginal bets from
    # outranking a single strong edge.
    # Examples: 1 bet → ×1.10, 3 bets → ×1.26, 9 bets → ×1.45
    breadth_bonus = 1.0 + 0.15 * math.log1p(len(qualifying))
    raw = max(qualifying) * breadth_bonus
    # conf_factor: floor at 10 % so very-early-season matches aren't zeroed
    conf_factor = max(confidence_pct / 100.0, 0.10)
    return round(raw * conf_factor, 6)
