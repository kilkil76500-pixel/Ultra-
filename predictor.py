"""
engine/predictor.py — V16 Football Intelligence Engine.

Pipeline (per match)
--------------------
1. Build a 0-100 Strength Index for each team from 8 weighted dimensions:
      Attaque 25% · Défense adverse 20% · Forme récente 15%
      H2H 10% · Terrain domicile/extérieur réel 10%
      Classement 10% · Motivation 5% · Forme physique 5%
2. Map each team's index to an xG via exponential scaling calibrated on
   football averages: index=50 → league_avg, index=70 → 1.49×avg, etc.
3. Clamp the final Poisson λ to [0.20, 3.00].
4. Run a 100,000-draw Monte Carlo (Poisson + Dixon-Coles + day jitter).
5. Blend Forebet's validated 1X2 at 30% (70% when local data is sparse).
6. Return full PredictionResult with all diagnostics.

No Telegram code. No API calls.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from engine import montecarlo
from engine import form as form_module
from engine.h2h import H2HWeight
from engine.injuries import InjuryImpact
from engine.providers.base import NormalizedTeamStats, TeamExtendedStats

# ── V16 imports ───────────────────────────────────────────────────────────────
from engine import montecarlo_v5
from engine import xg_v16
from engine import tactical as tactical_module
from engine import confidence_v2 as confidence_v2_module
from engine import team_memory as team_memory_module

if TYPE_CHECKING:
    from engine.leagues import LeagueInfo

logger = logging.getLogger(__name__)
Progress = Callable[[int, int, str], None]

_DEFAULT_AVG_HOME = 1.50
_DEFAULT_AVG_AWAY = 1.15
_PRIOR_WEIGHT     = 6.0

_MC_MAX_ITERATIONS: dict[int, int] = {1: 100_000, 2: 100_000, 3: 100_000}

# Forebet 1X2 blend weights
FOREBET_1X2_WEIGHT       = 0.30
FOREBET_LOW_DATA_WEIGHT  = 0.70

XG_MIN = 0.20
XG_MAX = 3.00

# V13 Strength index scaling constant
# xG = league_avg × exp(K × (index − 50) / 50)
# K=0.8 → index 70 = 1.49×avg, index 30 = 0.67×avg, index 90 = 2.23×avg
_INDEX_SCALE_K = 0.8

# V20.7 — poids maximum de chaque composante de l'indice de force (voir
# _compute_strength_index) et leur valeur "neutre" (moitié du max) utilisée
# par l'ablation — voir engine/strength_ablation.py.
COMPONENT_MAX_POINTS: dict[str, float] = {
    "attaque": 25.0, "défense_adverse": 20.0, "forme": 15.0, "h2h": 10.0,
    "terrain": 10.0, "classement": 10.0, "motivation": 5.0, "forme_physique": 5.0,
}
_COMPONENT_NEUTRAL: dict[str, float] = {k: v / 2.0 for k, v in COMPONENT_MAX_POINTS.items()}


# ── Intermediate data structures ──────────────────────────────────────────────

@dataclass
class TeamStrength:
    attack:  float
    defence: float
    form:    float
    played:  float
    mental:  float = 0.5
    fitness: float = 0.5


@dataclass
class PredictionResult:
    home_xg:               float = 0.0
    away_xg:               float = 0.0
    home_win_prob:         float = 0.0
    draw_prob:             float = 0.0
    away_win_prob:         float = 0.0
    # V18 : probabilités 1X2 AVANT application des multiplicateurs de
    # calibration (prob_multiplier_home/draw/away). Conservées pour permettre
    # à engine.auto_learning de rejouer d'anciennes prédictions avec une
    # calibration candidate sans jamais appliquer un multiplicateur deux fois.
    home_win_prob_raw:     float = 0.0
    draw_prob_raw:         float = 0.0
    away_win_prob_raw:     float = 0.0
    calibration_version:   int   = 0
    btts_prob:             float = 0.0
    over25_prob:           float = 0.0
    under25_prob:          float = 0.0
    over35_prob:           float = 0.0
    under35_prob:          float = 0.0
    btts_yes_over25_prob:  float = 0.0
    btts_yes_under25_prob: float = 0.0
    nbtts_over25_prob:     float = 0.0
    home_name:             str   = "Home"
    away_name:             str   = "Away"
    confidence:            str   = "LOW"
    confidence_pct:        float = 0.0
    risk_level:            str   = "HIGH"
    mc_iterations:         int   = 0
    modal_score:           str   = "?"
    second_score:          str   = "?"
    top_scores:            list  = field(default_factory=list)
    xg_breakdown:          dict  = field(default_factory=dict)
    strength_index:        dict  = field(default_factory=dict)   # V13 index breakdown
    goal_minutes:          list  = field(default_factory=list)
    mc_mean_home_goals:    float = 0.0
    mc_mean_away_goals:    float = 0.0
    power_home:            float = 0.0
    power_away:            float = 0.0
    crowd_emotion:         float = 0.0
    chaos_level:           float = 0.0
    h2h_matches:           int   = 0
    injuries_home:         int   = 0
    injuries_away:         int   = 0
    red_card_match_share:  float = 0.0
    purple_patch_share:    float = 0.0
    distinct_scorelines:   int   = 0
    mc_converged:           bool  = False
    mc_convergence_se:      float = 0.0
    timing_windows:         list  = field(default_factory=list)
    forebet_score:          str   = ""
    forebet_weight_applied: float = 0.0
    forebet_score_prob:     float = 0.0
    forebet_score_rank:     int   = 0
    forebet_alignment:      str   = "n/a"
    forebet_detail_xg_weight: float = 0.0
    forebet_detail_prob_weight: float = 0.0
    local_data_quality:       str   = "unknown"
    # V13 extended data flags
    extended_data_used:     bool  = False   # True when TeamExtendedStats enriched the index
    home_index:             float = 50.0    # Raw 0-100 strength index for home team
    away_index:             float = 50.0    # Raw 0-100 strength index for away team
    # V16 fields
    confidence_v2_score:    int   = 0       # 0-100 reliability score
    confidence_v2_grade:    str   = "D"     # A+/A/B/C/D
    confidence_v2_label:    str   = "LOW"   # ELITE/HIGH/MEDIUM/LOW/TRÈS BAS
    confidence_v2_breakdown: dict = field(default_factory=dict)
    confidence_v2_explanation: str = ""
    home_tactical_style:    str   = "balanced"
    away_tactical_style:    str   = "balanced"
    home_tactical_desc:     str   = ""
    away_tactical_desc:     str   = ""
    tactical_btts_adj:      float = 0.0
    tactical_over25_adj:    float = 0.0
    xg_v16_breakdown:       dict  = field(default_factory=dict)
    mc_v5_scenario_dist:    dict  = field(default_factory=dict)
    mc_v5_narratives:       list  = field(default_factory=list)
    mc_engine:              str   = "v4"    # "v4" | "v5"
    # V17 : résultat prédit avec détection améliorée des nuls
    predicted_outcome:      str   = ""      # "home" | "draw" | "away"
    # V17 : recommandation binaire BTTS / O2.5 calibrée
    btts_yes:               bool  = False
    ou25_yes:               bool  = False
    # V19.13 — marché conseillé pour CE match, calculé après coup par
    # engine.market_edge à partir de l'historique réglé (nécessite un accès
    # DB, donc jamais rempli ici dans predictor.py — voir scanner.py). Champs
    # additifs à défaut vide : rien qui lit PredictionResult sans connaître
    # ces champs n'est affecté.
    recommended_market:        str   = ""      # "1x2" | "btts" | "over25" | ""
    recommended_market_label:  str   = ""
    recommended_market_reason: str   = ""
    recommended_market_data_ok: bool = False
    # V20 — signaux d'anomalie pré-match (engine.anomaly), lecture seule sur
    # l'historique déjà réglé : ne modifie jamais une probabilité ni un
    # pronostic. Champs additifs à défaut vide/liste vide, voir scanner.py.
    anomaly_messages:          list  = field(default_factory=list)
    anomaly_has_alert:         bool  = False
    # V20.6 — anomaly_messages mélangeait sans distinction des signaux de
    # fiabilité réelle (severity="warning" : taux d'échec historique, écart
    # cote/modèle) et des notes purement explicatives (severity="info" :
    # dispersion des scénarios, tier de ligue, cohérence marginal/modal —
    # voir engine/coherence.py). Un utilisateur lisant 5 lignes "⚠️/ℹ️" à la
    # suite ne pouvait pas distinguer "sois prudent" de "ces deux chiffres
    # corrects ne se contredisent pas vraiment". Champs additifs (défaut
    # liste vide) : anomaly_messages reste inchangé pour compatibilité,
    # scanner.py peuple maintenant aussi ces deux listes séparément.
    anomaly_warnings:          list  = field(default_factory=list)
    anomaly_notes:             list  = field(default_factory=list)


# ── Bayesian strength extraction ──────────────────────────────────────────────

def _extract_strength(stats: NormalizedTeamStats, avg_h: float, avg_a: float) -> TeamStrength:
    played = max(float(stats.played), 0.0)
    gf     = max(float(stats.goals_for), 0.0)
    ga     = max(float(stats.goals_against), 0.0)

    if played <= 0:
        if not 0.0 < gf <= 6.0:
            gf = avg_h
        if not 0.0 < ga <= 6.0:
            ga = avg_a

    total_w = played + _PRIOR_WEIGHT
    shrunk_gf = (gf * played + avg_h * _PRIOR_WEIGHT) / total_w
    shrunk_ga = (ga * played + avg_a * _PRIOR_WEIGHT) / total_w

    attack  = max(shrunk_gf / avg_h if avg_h > 0 else 1.0, 0.1)
    defence = max(shrunk_ga / avg_a if avg_a > 0 else 1.0, 0.1)

    form_str = stats.form or ""
    recent   = form_str[-5:] if len(form_str) >= 5 else form_str
    form     = (
        (recent.count("W") + 0.5 * recent.count("D")) / max(len(recent), 1)
        if recent else 0.5
    )

    mental  = form_module.compute_mental_strength(form_str)
    fitness = round(min(1.0, max(0.30, attack * 0.50 + form * 0.50)), 3)

    return TeamStrength(
        attack=attack, defence=defence, form=form,
        played=played, mental=mental, fitness=fitness,
    )


# ── V13 Strength Index ────────────────────────────────────────────────────────

def _form_score(form_str: str) -> float:
    """Convert a W/D/L string to a 0-1 score. W=1, D=0.5, L=0."""
    if not form_str:
        return 0.5
    recent = form_str[-5:] if len(form_str) >= 5 else form_str
    return (recent.count("W") + 0.5 * recent.count("D")) / max(len(recent), 1)


def _compute_strength_index(
    team: TeamStrength,
    opponent: TeamStrength,
    side: str,
    league_avg: float,
    h2h_component: float,
    injury_multiplier: float,
    extended: TeamExtendedStats | None,
    opp_extended: TeamExtendedStats | None,
    ablate: str | None = None,
) -> tuple[float, dict[str, float]]:
    """
    Build a 0-100 strength index for one team's scoring probability.

    Components (total 100 pts):
    1. Attaque          — 25 pts
    2. Défense adverse  — 20 pts
    3. Forme récente    — 15 pts
    4. H2H              — 10 pts
    5. Terrain réel     — 10 pts  (home/away split record, not static +10%)
    6. Classement       — 10 pts
    7. Motivation       —  5 pts
    8. Forme physique   —  5 pts

    Returns (index 0-100, component_breakdown dict).

    V20.7 — `ablate` : nom d'une composante à neutraliser (voir
    engine.strength_ablation). Remplace la valeur normalement calculée par
    la moitié de son poids maximum (ex. attaque → 12.5/25), pour les deux
    équipes symétriquement — donc cette composante ne discrimine plus rien
    entre domicile et extérieur, sans changer les autres. Permet de
    mesurer, par ré-simulation complète sur l'historique réglé, si chaque
    composante contribue vraiment à la précision ou si elle dilue le
    signal des autres.
    """
    components: dict[str, float] = {}

    # ── 1. Attaque (25 pts max) ───────────────────────────────────────────────
    # Base: attack ratio clamped to [0.2, 2.0] → maps to [2.5, 25] pts
    atk_base = min(2.0, max(0.2, team.attack)) / 2.0 * 25.0
    # Over 2.5 historical % adjusts the attack signal (max ±3 pts)
    ou_adj = 0.0
    if extended and extended.over25_pct >= 0:
        ou_adj = (extended.over25_pct / 100.0 - 0.50) * 6.0
    # Failed to score % penalises attack (each 10% of FTS → -1 pt, max -4 pts)
    fts_pen = 0.0
    if extended and extended.failed_score_pct >= 0:
        fts_pen = min(4.0, extended.failed_score_pct / 100.0 * 10.0)
    components["attaque"] = round(max(2.0, min(25.0, atk_base + ou_adj - fts_pen)), 2)

    # ── 2. Défense adverse (20 pts max) ──────────────────────────────────────
    # High opponent.defence → opponent concedes more → easier to score
    opp_def_base = min(2.0, max(0.2, opponent.defence)) / 2.0 * 20.0
    # Opponent clean sheet % penalises the scorer (max -4 pts)
    cs_pen = 0.0
    if opp_extended and opp_extended.clean_sheet_pct >= 0:
        cs_pen = min(4.0, opp_extended.clean_sheet_pct / 100.0 * 8.0)
    components["défense_adverse"] = round(max(2.0, min(20.0, opp_def_base - cs_pen)), 2)

    # ── 3. Forme récente (15 pts max) ────────────────────────────────────────
    # Prefer split form when available
    global_form = team.form  # already 0-1 from TeamStrength
    if extended:
        split_str = extended.home_form if side == "home" else extended.away_form
        if split_str:
            split_score = _form_score(split_str)
            # Blend: 40% global, 60% split (split is more relevant)
            form_score = 0.40 * global_form + 0.60 * split_score
        else:
            form_score = global_form
    else:
        form_score = global_form
    components["forme"] = round(form_score * 15.0, 2)

    # ── 4. H2H (10 pts max) ──────────────────────────────────────────────────
    # h2h_component is the league-blended historical goal rate from h2h.py
    h2h_ratio = h2h_component / max(0.1, league_avg)
    # Clamp ratio to [0.4, 2.0] then scale to [2, 10] pts
    h2h_pts = (min(2.0, max(0.4, h2h_ratio)) - 0.4) / (2.0 - 0.4) * 8.0 + 2.0
    components["h2h"] = round(h2h_pts, 2)

    # ── 5. Terrain réel (10 pts max) ─────────────────────────────────────────
    # Actual split record, NOT a static bonus
    if extended:
        split_str = extended.home_form if side == "home" else extended.away_form
        if split_str:
            split_score = _form_score(split_str)
            terrain_pts = split_score * 10.0
        elif side == "home":
            # No split data available → conservative home boost
            terrain_pts = 5.5
        else:
            terrain_pts = 4.5
    else:
        # Fallback static values (smaller than the old fixed +10%)
        terrain_pts = 5.5 if side == "home" else 4.5
    components["terrain"] = round(max(0.0, min(10.0, terrain_pts)), 2)

    # ── 6. Classement (10 pts max) ───────────────────────────────────────────
    if extended and extended.has_position:
        pos_pts = extended.position_ratio * 10.0
    else:
        pos_pts = 5.0  # neutral assumption
    components["classement"] = round(max(0.0, min(10.0, pos_pts)), 2)

    # ── 7. Motivation / Mental (5 pts max) ───────────────────────────────────
    mental_score = team.mental  # 0-1 from form_module
    # Current streak adjusts motivation (±1 pt, max ±3 consecutive)
    streak_adj = 0.0
    if extended and extended.current_streak != 0:
        streak_adj = max(-1.0, min(1.0, extended.current_streak / 3.0))
    motivation_pts = (0.70 * mental_score + 0.30 * (0.5 + 0.5 * streak_adj)) * 5.0
    components["motivation"] = round(max(0.0, min(5.0, motivation_pts)), 2)

    # ── 8. Forme physique / Absences (5 pts max) ─────────────────────────────
    effective_fitness = max(0.3, min(1.0, team.fitness * injury_multiplier))
    components["forme_physique"] = round(effective_fitness * 5.0, 2)

    # ── V20.7 : neutralisation (ablation) d'une composante, si demandée ──────
    if ablate is not None and ablate in components:
        components[ablate] = _COMPONENT_NEUTRAL[ablate]

    # ── Total ─────────────────────────────────────────────────────────────────
    total = sum(components.values())
    # Clamp to [5, 95] — no team should be completely zero or guaranteed
    total = max(5.0, min(95.0, total))

    return round(total, 2), components


def _index_to_xg(index: float, league_avg: float) -> float:
    """
    Map a 0-100 strength index to an expected goals value via exponential scaling.

    Calibration (K=0.8):
      index=50  → xG = league_avg      (neutral)
      index=60  → xG = 1.17 × avg
      index=70  → xG = 1.38 × avg
      index=80  → xG = 1.62 × avg
      index=90  → xG = 1.90 × avg
      index=40  → xG = 0.85 × avg
      index=30  → xG = 0.73 × avg
      index=20  → xG = 0.62 × avg

    The exponential is more stable than the old weighted-sum formula: it
    avoids xG above 3.0 for realistic index values, and handles sparse
    data gracefully (index ≈ 50 → league_avg).
    """
    raw = league_avg * math.exp(_INDEX_SCALE_K * (index - 50.0) / 50.0)
    return max(XG_MIN, min(XG_MAX, raw))


# ── H2H component helper ──────────────────────────────────────────────────────

def _h2h_components(
    h2h_weight: H2HWeight | None,
    avg_h: float,
    avg_a: float,
) -> tuple[float, float]:
    if h2h_weight is None or not h2h_weight.h2h_scores:
        return avg_h, avg_a
    scores = h2h_weight.h2h_scores[-10:]
    h2h_h = sum(min(max(float(home), 0.0), XG_MAX) for home, _ in scores) / len(scores)
    h2h_a = sum(min(max(float(away), 0.0), XG_MAX) for _, away in scores) / len(scores)
    # Blend: 70% league average, 30% H2H — keep H2H from dominating on small samples
    return (
        0.70 * avg_h + 0.30 * h2h_h,
        0.70 * avg_a + 0.30 * h2h_a,
    )


# ── Power score (for display only) ───────────────────────────────────────────

def _compute_power(index: float) -> float:
    """Map 0-100 index to the legacy power score format (display only)."""
    return round(index, 1)


# ── Crowd emotion ─────────────────────────────────────────────────────────────

def _compute_crowd_emotion(tier: int, h2h_matches: int) -> float:
    _IMPORTANCE: dict[int, float] = {1: 0.80, 2: 0.55, 3: 0.30}
    home_pressure    = 0.65
    match_importance = _IMPORTANCE.get(tier, 0.40)
    rivalry_level    = min(0.60, 0.10 + h2h_matches * 0.05)
    emotion = (
        home_pressure    * 0.4
        + match_importance * 0.3
        + rivalry_level    * 0.3
    )
    return round(min(max(emotion, 0.0), 1.0), 4)


# ── Confidence scoring ────────────────────────────────────────────────────────

def _base_confidence(played: float) -> float:
    checkpoints = [(0, 10.0), (5, 40.0), (10, 65.0), (20, 85.0), (30, 95.0)]
    if played <= 0:
        return 10.0
    if played >= 30:
        return 95.0
    for i in range(len(checkpoints) - 1):
        n0, c0 = checkpoints[i]
        n1, c1 = checkpoints[i + 1]
        if n0 <= played <= n1:
            t = (played - n0) / (n1 - n0)
            return c0 + t * (c1 - c0)
    return 95.0


def _compute_confidence(
    played_h: float, played_a: float, tier_penalty: float,
    extended_used: bool,
) -> tuple[float, str, str]:
    base = _base_confidence(min(played_h, played_a))
    # Small bonus when extended data enriched the prediction
    ext_bonus = 5.0 if extended_used else 0.0
    pct  = round(max(5.0, min(base * tier_penalty + ext_bonus, 98.0)), 1)
    if pct >= 65:
        return pct, "HIGH",   "LOW"
    if pct >= 40:
        return pct, "MEDIUM", "MEDIUM"
    return pct, "LOW", "HIGH"


# ── V18 : application des multiplicateurs de calibration ─────────────────────

def _apply_prob_multipliers(
    home: float, draw: float, away: float, calib,
) -> tuple[float, float, float]:
    """
    Applique prob_multiplier_home/draw/away puis renormalise pour que les
    3 probabilités continuent de sommer à 1.

    Ces multiplicateurs sont calculés par engine.calibration /
    engine.auto_learning à partir des biais observés dans les résultats
    réels (ex. victoires extérieures systématiquement sous-estimées).
    Avant V18 ils étaient calculés mais jamais appliqués ici.
    """
    mh = max(0.0, home * calib.prob_multiplier_home)
    md = max(0.0, draw * calib.prob_multiplier_draw)
    ma = max(0.0, away * calib.prob_multiplier_away)
    total = mh + md + ma
    if total <= 0:
        return home, draw, away
    return mh / total, md / total, ma / total


# ── Forebet probability blend ─────────────────────────────────────────────────

def _blend_forebet_probabilities(
    home: float,
    draw: float,
    away: float,
    detail: dict,
    *,
    weight: float = FOREBET_1X2_WEIGHT,
) -> tuple[float, float, float, float]:
    raw = detail.get("probabilities")
    if not isinstance(raw, dict):
        return home, draw, away, 0.0
    values: list[float] = []
    for key in ("home_win", "draw", "away_win"):
        try:
            value = float(raw[key])
        except (KeyError, TypeError, ValueError):
            return home, draw, away, 0.0
        if not 0.0 <= value <= 100.0:
            return home, draw, away, 0.0
        values.append(value / 100.0)
    total = sum(values)
    if total <= 0:
        return home, draw, away, 0.0
    page_h, page_d, page_a = (value / total for value in values)
    weight = max(0.0, min(1.0, float(weight)))
    return (
        home * (1.0 - weight) + page_h * weight,
        draw * (1.0 - weight) + page_d * weight,
        away * (1.0 - weight) + page_a * weight,
        weight,
    )


# ── Public prediction API ─────────────────────────────────────────────────────

def predict(
    home_stats:       NormalizedTeamStats,
    away_stats:       NormalizedTeamStats,
    home_name:        str                  = "Home",
    away_name:        str                  = "Away",
    league_info:      "LeagueInfo | None"  = None,
    h2h_weight:       H2HWeight | None     = None,
    injury_impact:    InjuryImpact | None  = None,
    forebet_hint:     dict | None          = None,
    home_extended:    TeamExtendedStats | None = None,  # V13
    away_extended:    TeamExtendedStats | None = None,  # V13
    progress:         Progress | None      = None,
    xg_multiplier_override: float | None   = None,  # V19.16 — voir engine.xg_backtest
    strength_ablation: str | None          = None,  # V20.7 — voir engine.strength_ablation
    h2h_mode: str | None                   = None,  # V20.8 — voir engine.h2h_audit
) -> PredictionResult:
    """
    V13 full prediction pipeline.

    1. Extract TeamStrength via Bayesian shrinkage.
    2. Build V13 Strength Index (8 components, 0-100) for each team.
    3. Map index → xG via exponential scaling.
    4. Clamp λ to [0.20, 3.00].
    5. Run 100,000-draw Monte Carlo.
    6. Blend Forebet 1X2 at 30% (70% when sparse data).
    """
    avg_h  = league_info.avg_home_goals     if league_info else _DEFAULT_AVG_HOME
    avg_a  = league_info.avg_away_goals     if league_info else _DEFAULT_AVG_AWAY
    t_pen  = league_info.confidence_penalty if league_info else 1.0
    tier   = league_info.tier               if league_info else 1

    if progress:
        progress(8, 100, "Lecture des statistiques du match…")

    home_s = _extract_strength(home_stats, avg_h, avg_a)
    away_s = _extract_strength(away_stats, avg_h, avg_a)

    h2h_matches_used  = h2h_weight.matches_used if h2h_weight else 0
    crowd_emotion     = _compute_crowd_emotion(tier, h2h_matches_used)
    # V20.8 — h2h_mode (voir engine.h2h_audit) :
    #   None (défaut, comportement inchangé pour tout appelant existant) :
    #     mélange 70% moyenne ligue / 30% moyenne de buts H2H brute — seul
    #     canal H2H actif aujourd'hui, alimente juste la composante "h2h"
    #     (10 pts) de l'indice de force.
    #   "off" : H2H complètement neutralisé — avg_h/avg_a inchangées, comme
    #     s'il n'y avait aucun historique face-à-face.
    #   "weighted" : utilise EN PLUS home_factor/away_factor (calculés par
    #     engine.h2h.compute_h2h_weight — pondération par récence + ratio de
    #     buts, bornée [0.88, 1.12]) comme multiplicateur direct sur lambda
    #     après conversion indice→xG. Ces facteurs sont calculés depuis la
    #     création du module h2h.py mais n'ont jamais été branchés nulle
    #     part ailleurs dans le pipeline — trouvé en auditant le H2H.
    if h2h_mode == "off":
        h2h_comp_h, h2h_comp_a = avg_h, avg_a
    else:
        h2h_comp_h, h2h_comp_a = _h2h_components(h2h_weight, avg_h, avg_a)

    injury_home_mult = injury_impact.home_multiplier if injury_impact else 1.0
    injury_away_mult = injury_impact.away_multiplier if injury_impact else 1.0

    extended_used = home_extended is not None or away_extended is not None

    if progress:
        progress(20, 100, "Calcul de l'indice de force V13…")

    # ── V13 Strength Index ────────────────────────────────────────────────────
    home_index, home_components = _compute_strength_index(
        team=home_s, opponent=away_s, side="home",
        league_avg=avg_h, h2h_component=h2h_comp_h,
        injury_multiplier=injury_home_mult,
        extended=home_extended, opp_extended=away_extended,
        ablate=strength_ablation,
    )
    away_index, away_components = _compute_strength_index(
        team=away_s, opponent=home_s, side="away",
        league_avg=avg_a, h2h_component=h2h_comp_a,
        injury_multiplier=injury_away_mult,
        extended=away_extended, opp_extended=home_extended,
        ablate=strength_ablation,
    )

    # ── Index → xG ────────────────────────────────────────────────────────────
    # NB : ce lam_h/lam_a est du code mort depuis la V16 — voir xg_v16.py
    # ("Remplace le calcul _index_to_xg() de V13 par un modèle multi-facteurs").
    # Il n'alimente plus la simulation finale (voir plus bas, lam_h est
    # réaffecté depuis xg_profile), seulement le message de progression
    # ci-dessous. Trouvé en auditant le H2H (V20.8) : la pondération
    # home_factor/away_factor doit être appliquée au VRAI lam_h final, pas ici.
    lam_h = _index_to_xg(home_index, avg_h)
    lam_a = _index_to_xg(away_index, avg_a)

    if progress:
        progress(30, 100, f"xG calculés · {home_name} {lam_h:.2f} — {away_name} {lam_a:.2f}")

    xg_breakdown = {
        "home": {k: v for k, v in home_components.items()},
        "away": {k: v for k, v in away_components.items()},
    }
    strength_index_display = {
        "home": {"total": home_index, "composantes": home_components},
        "away": {"total": away_index, "composantes": away_components},
    }

    # ── Forebet exact score (report-only) ─────────────────────────────────────
    forebet_score_tuple: tuple[int, int] | None = None
    forebet_detail = (forebet_hint or {}).get("detail") or {}
    if forebet_hint and forebet_hint.get("score"):
        raw_score = forebet_hint["score"]
        try:
            forebet_score_tuple = (int(raw_score[0]), int(raw_score[1]))
        except (TypeError, ValueError, IndexError):
            forebet_score_tuple = None

    key_players_missing = (
        injury_impact is not None
        and injury_impact.has_data
        and (injury_impact.home_out >= 2 or injury_impact.away_out >= 2)
    )
    h2h_scores_for_chaos: tuple[tuple[int, int], ...] = (
        h2h_weight.h2h_scores if h2h_weight is not None else ()
    )
    chaos = form_module.compute_dynamic_chaos(
        h2h_scores=h2h_scores_for_chaos,
        tier=tier,
        key_players_missing=key_players_missing,
    )

    # ── V16 : Analyse tactique ────────────────────────────────────────────────
    home_extended_for_tactical = home_extended
    away_extended_for_tactical = away_extended

    home_tactical = tactical_module.detect_style(
        avg_goals_scored=home_s.attack * (league_info.avg_home_goals if league_info else 1.5) if home_s.played > 0 else -1,
        avg_goals_conceded=home_s.defence * (league_info.avg_away_goals if league_info else 1.15) if home_s.played > 0 else -1,
        avg_shots=home_extended_for_tactical.avg_shots_total if home_extended_for_tactical and hasattr(home_extended_for_tactical, "avg_shots_total") else -1,
        clean_sheet_pct=home_extended_for_tactical.clean_sheet_pct if home_extended_for_tactical and home_extended_for_tactical.clean_sheet_pct >= 0 else -1,
        failed_score_pct=home_extended_for_tactical.failed_score_pct if home_extended_for_tactical and home_extended_for_tactical.failed_score_pct >= 0 else -1,
        over25_pct=home_extended_for_tactical.over25_pct if home_extended_for_tactical and home_extended_for_tactical.over25_pct >= 0 else -1,
        btts_pct=home_extended_for_tactical.btts_pct if home_extended_for_tactical and home_extended_for_tactical.btts_pct >= 0 else -1,
        team_name=home_name,
    )
    away_tactical = tactical_module.detect_style(
        avg_goals_scored=away_s.attack * (league_info.avg_away_goals if league_info else 1.15) if away_s.played > 0 else -1,
        avg_goals_conceded=away_s.defence * (league_info.avg_home_goals if league_info else 1.5) if away_s.played > 0 else -1,
        avg_shots=away_extended_for_tactical.avg_shots_total if away_extended_for_tactical and hasattr(away_extended_for_tactical, "avg_shots_total") else -1,
        clean_sheet_pct=away_extended_for_tactical.clean_sheet_pct if away_extended_for_tactical and away_extended_for_tactical.clean_sheet_pct >= 0 else -1,
        failed_score_pct=away_extended_for_tactical.failed_score_pct if away_extended_for_tactical and away_extended_for_tactical.failed_score_pct >= 0 else -1,
        over25_pct=away_extended_for_tactical.over25_pct if away_extended_for_tactical and away_extended_for_tactical.over25_pct >= 0 else -1,
        btts_pct=away_extended_for_tactical.btts_pct if away_extended_for_tactical and away_extended_for_tactical.btts_pct >= 0 else -1,
        team_name=away_name,
    )

    # ── V16 : xG amélioré ────────────────────────────────────────────────────
    if progress:
        progress(28, 100, "Calcul xG V16 (tirs, grosses occasions, tactique)…")

    xg_profile = xg_v16.compute_xg_v16(
        home_index=home_index,
        away_index=away_index,
        home_goals_for=home_s.attack * (avg_h or 1.5),
        away_goals_for=away_s.attack * (avg_a or 1.15),
        home_goals_against=home_s.defence * (avg_a or 1.15),
        away_goals_against=away_s.defence * (avg_h or 1.5),
        home_games_played=int(home_s.played),
        away_games_played=int(away_s.played),
        home_injury_mult=injury_home_mult,
        away_injury_mult=injury_away_mult,
        tier=tier,
        match_importance=0.6 if tier == 1 else 0.4,
        home_tactical=home_tactical,
        away_tactical=away_tactical,
    )
    # ── V17 : Calibration — chargée ici pour appliquer le multiplicateur xG ──
    from engine.calibration import load_calibration as _load_calib
    _calib = _load_calib()

    # V19.16 — xg_multiplier_override permet à engine.xg_backtest de rejouer
    # ce match avec une valeur CANDIDATE sans toucher à calibration.json (donc
    # sans jamais risquer d'affecter une prédiction réelle en cours pendant le
    # backtest). None (comportement par défaut, tous les appelants existants)
    # = valeur active inchangée, comme avant.
    _xg_mult = xg_multiplier_override if xg_multiplier_override is not None else _calib.xg_global_multiplier

    # Use V16 xG values + V17 multiplicateur global de correction biais buts
    lam_h = xg_profile.home_xg * _xg_mult
    lam_a = xg_profile.away_xg * _xg_mult
    # Clamp pour éviter des valeurs absurdes
    lam_h = max(montecarlo_v5._LAMBDA_MIN, min(montecarlo_v5._LAMBDA_MAX, lam_h))
    lam_a = max(montecarlo_v5._LAMBDA_MIN, min(montecarlo_v5._LAMBDA_MAX, lam_a))

    # V20.8 — h2h_mode="weighted" : applique ICI (sur le VRAI lambda qui
    # alimente la simulation) le multiplicateur home_factor/away_factor
    # calculé par engine.h2h.compute_h2h_weight — jamais branché nulle part
    # avant cet audit, malgré le docstring de H2HWeight qui décrit
    # explicitement ces facteurs comme destinés à ajuster lambda.
    if h2h_mode == "weighted" and h2h_weight is not None and h2h_weight.has_data:
        lam_h = max(montecarlo_v5._LAMBDA_MIN, min(montecarlo_v5._LAMBDA_MAX, lam_h * h2h_weight.home_factor))
        lam_a = max(montecarlo_v5._LAMBDA_MIN, min(montecarlo_v5._LAMBDA_MAX, lam_a * h2h_weight.away_factor))

    if progress:
        progress(32, 100, f"xG V17 · {home_name} {lam_h:.2f} — {away_name} {lam_a:.2f}")

    # ── V16 : Monte-Carlo V5 par scénarios ───────────────────────────────────
    mc_ceiling = _MC_MAX_ITERATIONS.get(tier, montecarlo.MAX_ITERATIONS)

    if progress:
        progress(35, 100, "Monte-Carlo V5 — simulation par scénarios…")

    mc_v5 = montecarlo_v5.simulate_v5(
        lam_h,
        lam_a,
        n=montecarlo_v5.DEFAULT_ITERATIONS,
        chaos_level=chaos,
        max_iterations=mc_ceiling,
        forebet_score=forebet_score_tuple,
        progress=progress,
    )

    # Créer un wrapper compatible avec l'interface mc de V4 pour le reste du pipeline
    class _MCCompat:
        def __getattr__(self, name):
            return getattr(mc_v5, name, None)

    mc = _MCCompat()
    # Patches pour les attributs non présents dans MonteCarloV5Result
    mc.home_win_prob = mc_v5.home_win_prob
    mc.draw_prob     = mc_v5.draw_prob
    mc.away_win_prob = mc_v5.away_win_prob
    mc.btts_prob     = mc_v5.btts_prob
    mc.over25_prob   = mc_v5.over25_prob
    mc.under25_prob  = mc_v5.under25_prob
    mc.over35_prob   = mc_v5.over35_prob
    mc.under35_prob  = mc_v5.under35_prob
    mc.btts_yes_over25_prob  = mc_v5.btts_yes_over25_prob
    mc.btts_yes_under25_prob = mc_v5.btts_yes_under25_prob
    mc.nbtts_over25_prob     = mc_v5.nbtts_over25_prob
    mc.mean_home_goals       = mc_v5.mean_home_goals
    mc.mean_away_goals       = mc_v5.mean_away_goals
    mc.modal_score           = mc_v5.modal_score
    mc.second_score          = mc_v5.second_score
    mc.top_scores            = mc_v5.top_scores
    mc.distinct_scorelines   = mc_v5.distinct_scorelines
    mc.iterations            = mc_v5.iterations
    mc.converged             = mc_v5.converged
    mc.convergence_se        = mc_v5.convergence_se
    mc.red_card_match_share  = mc_v5.red_card_match_share
    mc.purple_patch_share    = mc_v5.purple_patch_share
    mc.timing_windows        = []
    mc.goal_minutes          = []
    mc.forebet_score_prob    = 0.0
    mc.forebet_score_rank    = 0
    mc.forebet_alignment     = "n/a"

    if progress:
        progress(94, 100, "Simulation terminée — finalisation des probabilités…")

    low_data = (
        min(home_s.played, away_s.played) <= 0
        and not home_stats.form
        and not away_stats.form
    )
    blend_weight = FOREBET_LOW_DATA_WEIGHT if low_data else FOREBET_1X2_WEIGHT
    home_win_prob, draw_prob, away_win_prob, forebet_detail_prob_weight = (
        _blend_forebet_probabilities(
            mc.home_win_prob, mc.draw_prob, mc.away_win_prob,
            forebet_detail if isinstance(forebet_detail, dict) else {},
            weight=blend_weight,
        )
    )

    # ── V18 : application RÉELLE des multiplicateurs de calibration ─────────
    # Avant V18 ces multiplicateurs étaient calculés par /recalibrer mais
    # jamais utilisés — la calibration domicile/extérieur/nul n'avait donc
    # aucun effet sur les prédictions. On les applique ici puis on
    # renormalise pour que les 3 probabilités somment toujours à 1.
    # Les valeurs pré-multiplicateur sont conservées (raw) pour permettre à
    # engine.auto_learning de rejouer l'historique en toute sécurité.
    home_win_prob_raw, draw_prob_raw, away_win_prob_raw = (
        home_win_prob, draw_prob, away_win_prob
    )
    home_win_prob, draw_prob, away_win_prob = _apply_prob_multipliers(
        home_win_prob, draw_prob, away_win_prob, _calib
    )

    # ── V16 : Ajustements tactiques sur BTTS / O/U ───────────────────────────
    tact_adj = tactical_module.apply_tactical_adjustments(
        btts_prob=mc.btts_prob,
        over25_prob=mc.over25_prob,
        under25_prob=mc.under25_prob,
        home_profile=home_tactical,
        away_profile=away_tactical,
    )

    # ── V16 : Indice de confiance sur 100 ────────────────────────────────────
    # V19.14 — signal de risque tiré UNIQUEMENT de la mémoire interne
    # (aucune API externe) : taux d'erreur historique du modèle sur CETTE
    # équipe précise, construit au fil des /resultat. Neutre (0.0) tant qu'il
    # n'y a pas assez d'historique pour être fiable — voir team_memory.py.
    try:
        from engine import team_memory as team_memory_module
        _mgr = team_memory_module.get_manager()
        home_team_risk = _mgr.get(home_name).model_error_rate
        away_team_risk = _mgr.get(away_name).model_error_rate
    except Exception:
        home_team_risk = 0.0
        away_team_risk = 0.0

    # _calib already loaded above (before MC simulation)
    conf_v2 = confidence_v2_module.compute_confidence_v2(
        home_games_played=int(home_s.played),
        away_games_played=int(away_s.played),
        extended_data_used=extended_used,
        local_data_quality="sparse" if low_data else "available",
        forebet_alignment=mc.forebet_alignment or "n/a",
        forebet_weight_applied=forebet_detail_prob_weight,
        home_form_str=home_stats.form or "",
        away_form_str=away_stats.form or "",
        home_injuries_out=injury_impact.home_out if injury_impact else 0,
        away_injuries_out=injury_impact.away_out if injury_impact else 0,
        mc_convergence_se=mc.convergence_se,
        mc_converged=mc.converged,
        mc_distinct_scorelines=mc.distinct_scorelines,
        home_win_prob=home_win_prob,
        draw_prob=draw_prob,
        away_win_prob=away_win_prob,
        chaos_level=chaos,
        tier=tier,
        h2h_matches_used=h2h_matches_used,
        calibration_quality=_calib.calibration_quality,
        home_team_risk=home_team_risk,
        away_team_risk=away_team_risk,
    )

    # V19.14 — jusqu'ici conf_pct/conf_label (le SEUL signal réellement suivi
    # dans /fiabilite et backtesté par /recalibrer) venait de _compute_confidence(),
    # qui ne regarde que le nombre de matchs joués + le palier de ligue. Le
    # score riche conf_v2 (qualité des données, cohérence Forebet, stabilité
    # de forme, blessures, variance Monte-Carlo, chaos, H2H, mémoire
    # d'équipe) n'était affiché qu'en second ("Grade V18") sans aucun effet
    # sur le tri HIGH/MEDIUM/LOW réellement mesuré — cause directe du fait
    # que ces trois catégories ne se distinguaient pas en précision réelle.
    # conf_v2.score devient donc LA confiance trackée ; risk_level suit.
    conf_pct = float(conf_v2.score)
    risk     = conf_v2.risk_level
    # V17 : recalibrer le label de confiance avec les seuils calibrés empiriquement
    if conf_pct >= _calib.confidence_high_threshold:
        conf_label = "HIGH"
    elif conf_pct >= _calib.confidence_medium_threshold:
        conf_label = "MEDIUM"
    else:
        conf_label = "LOW"

    modal_str  = f"{mc.modal_score[0]}-{mc.modal_score[1]}"
    second_str = f"{mc.second_score[0]}-{mc.second_score[1]}"

    # ── V17 : résultat prédit avec détection améliorée des nuls ──────────────
    # Le bot ne prédisait jamais "nul" (0/22 nuls réels détectés).
    # On applique un facteur amplificateur sur draw_prob pour la DÉCISION uniquement
    # (les probabilités affichées restent intactes).
    _draw_eff = draw_prob * _calib.draw_detection_factor
    _outcome_map = {"home": home_win_prob, "draw": _draw_eff, "away": away_win_prob}
    _predicted_outcome = max(_outcome_map, key=_outcome_map.get)

    # ── V17 : recommandation binaire calibrée BTTS / O2.5 ────────────────────
    _btts_prob_final  = tact_adj.get("btts_prob", mc.btts_prob)
    _ou25_prob_final  = tact_adj.get("over25_prob", mc.over25_prob)
    _btts_yes  = _btts_prob_final  >= _calib.btts_threshold
    _ou25_yes  = _ou25_prob_final  >= _calib.ou25_threshold

    return PredictionResult(
        home_xg                = round(lam_h, 2),
        away_xg                = round(lam_a, 2),
        home_win_prob          = round(home_win_prob, 4),
        draw_prob              = round(draw_prob, 4),
        away_win_prob          = round(away_win_prob, 4),
        home_win_prob_raw      = round(home_win_prob_raw, 4),
        draw_prob_raw          = round(draw_prob_raw, 4),
        away_win_prob_raw      = round(away_win_prob_raw, 4),
        calibration_version    = _calib.version,
        btts_prob              = tact_adj.get("btts_prob", mc.btts_prob),
        over25_prob            = tact_adj.get("over25_prob", mc.over25_prob),
        under25_prob           = tact_adj.get("under25_prob", mc.under25_prob),
        over35_prob            = mc.over35_prob,
        under35_prob           = mc.under35_prob,
        btts_yes_over25_prob   = mc.btts_yes_over25_prob,
        btts_yes_under25_prob  = mc.btts_yes_under25_prob,
        nbtts_over25_prob      = mc.nbtts_over25_prob,
        home_name              = home_name,
        away_name              = away_name,
        confidence             = conf_label,
        confidence_pct         = conf_pct,
        risk_level             = risk,
        mc_iterations          = mc.iterations,
        modal_score            = modal_str,
        second_score           = second_str,
        top_scores             = list(getattr(mc, "top_scores", [])),
        xg_breakdown           = xg_breakdown,
        strength_index         = strength_index_display,
        mc_mean_home_goals     = mc.mean_home_goals,
        mc_mean_away_goals     = mc.mean_away_goals,
        power_home             = _compute_power(home_index),
        power_away             = _compute_power(away_index),
        crowd_emotion          = crowd_emotion,
        chaos_level            = chaos,
        h2h_matches            = h2h_matches_used,
        injuries_home          = injury_impact.home_out if injury_impact else 0,
        injuries_away          = injury_impact.away_out if injury_impact else 0,
        red_card_match_share   = getattr(mc, "red_card_match_share", 0.0),
        purple_patch_share     = getattr(mc, "purple_patch_share", 0.0),
        distinct_scorelines    = getattr(mc, "distinct_scorelines", 0),
        mc_converged           = getattr(mc, "converged", False),
        mc_convergence_se      = getattr(mc, "convergence_se", 0.0),
        timing_windows         = getattr(mc, "timing_windows", []),
        goal_minutes           = getattr(mc, "goal_minutes", []),
        forebet_score          = f"{forebet_score_tuple[0]}-{forebet_score_tuple[1]}" if forebet_score_tuple else "",
        forebet_weight_applied = 0.0,
        forebet_score_prob     = getattr(mc, "forebet_score_prob", 0.0),
        forebet_score_rank     = getattr(mc, "forebet_score_rank", 0),
        forebet_alignment      = getattr(mc, "forebet_alignment", "n/a"),
        forebet_detail_xg_weight = 0.0,
        forebet_detail_prob_weight = forebet_detail_prob_weight,
        local_data_quality      = "sparse" if low_data else "available",
        extended_data_used      = extended_used,
        home_index              = home_index,
        away_index              = away_index,
        # ── V16 ──────────────────────────────────────────────────────────────
        confidence_v2_score     = conf_v2.score,
        confidence_v2_grade     = conf_v2.grade,
        confidence_v2_label     = conf_v2.label,
        confidence_v2_breakdown = conf_v2.breakdown,
        confidence_v2_explanation = conf_v2.explanation,
        home_tactical_style     = home_tactical.style,
        away_tactical_style     = away_tactical.style,
        home_tactical_desc      = home_tactical.description,
        away_tactical_desc      = away_tactical.description,
        tactical_btts_adj       = tact_adj.get("btts_delta", 0.0),
        tactical_over25_adj     = tact_adj.get("over25_delta", 0.0),
        xg_v16_breakdown        = xg_profile.breakdown,
        mc_v5_scenario_dist     = mc_v5.scenario_distribution,
        mc_v5_narratives        = mc_v5.scenario_narratives,
        mc_engine               = "v5",
        # ── V17 ──────────────────────────────────────────────────────────────
        predicted_outcome       = _predicted_outcome,
        btts_yes                = _btts_yes,
        ou25_yes                = _ou25_yes,
    )
