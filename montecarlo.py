"""
engine/montecarlo.py — Monte Carlo match simulation engine.

V19 — NE PAS fusionner _poisson_draw()/_dixon_coles_tau() avec leurs
homonymes de montecarlo_v5.py : ce sont deux implémentations
DÉLIBÉRÉMENT différentes, pas des doublons accidentels.
  • _poisson_draw ici borne lambda à 30 en dur ; celle de montecarlo_v5
    utilise _LAMBDA_MIN/_LAMBDA_MAX (bornes différentes, calibrées pour
    V5).
  • _dixon_coles_tau ici prend un rho constant et une formule additive
    (1 - rho, 1 + rho*0.5) ; _dc_tau dans montecarlo_v5 dépend aussi des
    lambdas simulées (lh, la) avec une formule multiplicative. Les
    fusionner produirait un changement de comportement silencieux dans
    l'un des deux moteurs. Voir VERSIONS.md pour le détail de la relation
    entre les deux fichiers.

── V2: event-driven, minute-by-minute simulation ──────────────────────────────

The previous version drew a single (home_goals, away_goals) pair per iteration
from a Negative-Binomial distribution. That is statistically fine for 1X2 /
BTTS / O-U markets, but it systematically over-concentrates scorelines around
the symmetric low-scoring basin (1-1, 1-0, 0-1) because a static distribution
has no notion of *how* a match actually unfolds.

V2 instead plays out every simulated match minute by minute (90' + stoppage),
so scoring intensity reacts to the live match state exactly like a real game:

  1. Pre-match day jitter   — a Gaussian multiplier per team (referee bias,
                               weather, squad rotation, motivation) drawn once
                               per simulated match. Wider for teams with poor
                               recent form (form_h/form_a) and for low-data /
                               chaotic leagues (chaos_level).
  2. Catch-up effect        — a team trailing after the 60th minute presses
                               harder the later and further behind it is.
  3. Park-the-bus            — a team leading by 2+ after 75' sits back; the
                               chasing side gets an extra push.
  4. Red-card shocks         — small per-minute hazard (higher in chaotic /
                               ill-disciplined leagues). Once triggered, that
                               team's intensity is cut for the rest of the
                               match and the opponent's is boosted.
  5. Purple patches          — a rare ~10-minute hot streak where one team's
                               intensity spikes, producing the quick-fire
                               multi-goal bursts real matches sometimes have.
                               Which side is more likely to catch fire is
                               weighted by relative attack strength.
  6. Dixon-Coles low-score
     correlation             — home/away goals are slightly anti-correlated
                               at very low scores (0-0/1-0/0-1/1-1 jointly a
                               bit more likely than pure independence would
                               predict), matching empirical football data.

All of this is *emergent* variance driven by discrete, interpretable random
events tied to form / attack / defence / league chaos — not a single tuned
dispersion constant — so realistic tails (3-1, 2-2, 1-2, 3-0, 4-1...) show up
naturally and shift from run to run, instead of the distribution collapsing
onto 1-1 / 2-1 every time.

No Telegram code. No API calls.
"""

from __future__ import annotations

import math
import random
import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)
Progress = Callable[[int, int, str], None]

# ── V4: bounded 100,000-draw Poisson simulation ────────────────────────────────
#
# The predictor supplies a bounded λ in [0.20, 3.00].  This engine samples
# goals directly from Poisson(λ).  A small, mean-preserving match-day jitter
# represents uncertainty without multiplying λ through purple patches,
# red cards, catch-up effects or other cascades.  Those cascades were the
# reason the previous minute-by-minute engine could produce inflated totals.
DEFAULT_ITERATIONS  = 100_000
MAX_ITERATIONS      = 100_000
_BATCH_SIZE         = 10_000
_LAMBDA_MIN         = 0.20
_LAMBDA_MAX         = 3.00

MINUTES_REGULATION = 90


# ── Result dataclass ────────────────────────────────────────────────────────

@dataclass
class MonteCarloResult:
    """Aggregated probabilities from N simulated matches."""
    home_win_prob:   float
    draw_prob:       float
    away_win_prob:   float
    btts_prob:       float          # both teams score ≥ 1 goal each
    over25_prob:     float          # total goals > 2.5  (i.e. ≥ 3)
    under25_prob:    float          # total goals ≤ 2.5  (i.e. ≤ 2)
    over35_prob:     float          # total goals > 3.5  (i.e. ≥ 4)
    under35_prob:    float          # total goals ≤ 3.5  (i.e. ≤ 3)
    mean_home_goals: float          # simulated xG home
    mean_away_goals: float          # simulated xG away
    iterations:      int            # total iterations actually used
    # Most frequent scoreline (capped at 9 per side for display only)
    modal_score:     tuple[int, int] = field(default_factory=lambda: (0, 0))
    # Second most frequent scoreline — gives bettors an alternative scenario
    second_score:    tuple[int, int] = field(default_factory=lambda: (0, 0))
    # Five most frequent simulated scorelines, with their empirical probabilities
    top_scores:      list = field(default_factory=list)
    # ── True joint (co-occurrence) probabilities ──────────────────────────────
    btts_yes_over25_prob:  float = 0.0   # BTTS Yes ∩ total goals ≥ 3
    btts_yes_under25_prob: float = 0.0   # BTTS Yes ∩ total goals ≤ 2  (≡ 1-1 only)
    nbtts_over25_prob:     float = 0.0   # BTTS No  ∩ total goals ≥ 3  (one team scores 3+)
    # ── Compatibility diagnostics ─────────────────────────────────────────────
    # V4 keeps these fields for older Telegram/report consumers, but does not
    # inject red cards or purple patches into the expected-goals distribution.
    red_card_match_share:   float = 0.0
    purple_patch_share:     float = 0.0
    distinct_scorelines:    int   = 0    # how many different scorelines occurred (0-9x0-9)
    # ── Precision + empirical timing diagnostics ───────────────────────────────
    converged:          bool  = False   # True when the requested budget completed
    convergence_se:     float = 0.0     # standard error at the end of the budget
    timing_windows:     list = field(default_factory=list)
    goal_minutes:       list = field(default_factory=list)
    forebet_score_prob: float = 0.0     # sim-assigned probability of forebet's exact scoreline (0 if n/a)
    forebet_score_rank: int   = 0       # rank of that scoreline among all simulated outcomes (1 = modal); 0 if n/a
    forebet_alignment:  str   = "n/a"   # "aligned" / "plausible" / "diverges" / "n/a"


# ── Poisson sampler (Knuth) ───────────────────────────────────────────────────

def _poisson_draw(lam: float) -> int:
    """Draw a random Poisson(λ) variate using Knuth's algorithm."""
    if lam <= 0:
        return 0
    if lam > 30:
        lam = 30
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while p > L:
        k += 1
        p *= random.random()
    return k - 1


def _bounded_day_multiplier(form: float, chaos_level: float) -> float:
    """Return a small, bounded match-day multiplier around 1.0."""
    sigma = min(0.08, 0.035 + 0.025 * (1.0 - form) + 0.02 * chaos_level)
    return max(0.85, min(1.15, 1.0 + random.gauss(0.0, sigma)))


def _sample_goal_minute() -> int:
    """Sample a goal minute from the empirical timing curve."""
    total = sum(_BASE_WEIGHTS)
    draw = random.random() * total
    cumulative = 0.0
    for start, end, weight in zip(_WINDOW_STARTS, _WINDOW_ENDS, _BASE_WEIGHTS):
        cumulative += weight
        if draw <= cumulative:
            return random.randint(start, end)
    return 90


# ── Dixon-Coles low-score adjustment ───────────────────────────────────────────

def _dixon_coles_tau(hg: int, ag: int, rho: float) -> float:
    """
    tau(x,y): rho < 0 slightly boosts 0-0/1-1 and slightly suppresses 1-0/0-1
    relative to independence — the classic empirical football correction.
    """
    if hg == 0 and ag == 0:
        return 1 - rho
    if hg == 0 and ag == 1:
        return 1 + rho * 0.5
    if hg == 1 and ag == 0:
        return 1 + rho * 0.5
    if hg == 1 and ag == 1:
        return 1 - rho
    return 1.0


@dataclass
class _MatchParams:
    jitter_h:           float
    jitter_a:            float
    chaos_level:          float
    red_card_hazard:      float   # per-minute hazard, per team
    purple_prob:          float   # probability a hot streak occurs at all
    purple_home_weight:   float   # 0..1 — how much of the purple-patch chance goes to home


def _play_one_match(
    lam_h: float, lam_a: float, p: _MatchParams,
) -> tuple[int, int, bool, bool, list[tuple[int, str, frozenset]]]:
    """
    Simulate one match with two bounded Poisson draws.

    The timing log is sampled after the score is known.  Timing therefore
    describes *when* goals tend to happen without changing the score
    distribution.  No in-match mechanic is allowed to change the expected
    goal total.
    """
    day_mult_h = _bounded_day_multiplier(1.0 - p.jitter_h / 0.42, p.chaos_level)
    day_mult_a = _bounded_day_multiplier(1.0 - p.jitter_a / 0.42, p.chaos_level)
    hg = _poisson_draw(max(_LAMBDA_MIN, min(_LAMBDA_MAX, lam_h * day_mult_h)))
    ag = _poisson_draw(max(_LAMBDA_MIN, min(_LAMBDA_MAX, lam_a * day_mult_a)))
    goal_log: list[tuple[int, str, frozenset]] = []
    for _ in range(hg):
        goal_log.append((_sample_goal_minute(), "H", frozenset()))
    for _ in range(ag):
        goal_log.append((_sample_goal_minute(), "A", frozenset()))
    return hg, ag, False, False, goal_log


# ── Public simulation API ─────────────────────────────────────────────────────

def _window_index_for_minute(minute: int) -> int:
    """Map a match minute (including stoppage time, folded into the last
    window) to its index in _WINDOW_STARTS/_WINDOW_ENDS (0-9)."""
    if minute >= 82:
        return 9
    return min(9, (minute - 1) // 9)


def _standard_error(successes: int, n: int) -> float:
    """Binomial standard error of a proportion — the classic sqrt(p(1-p)/n)."""
    if n <= 0:
        return 1.0
    prob = successes / n
    return math.sqrt(max(prob * (1.0 - prob), 1e-9) / n)


def _build_empirical_timing_windows(
    window_goal_counts: list[int],
    window_cause_counts: list["dict[str, int]"],
    total_goals: int,
    top_n: int = 4,
) -> list["GoalTimingWindow"]:
    """
    Turn the per-window goal tally collected across every simulated match into
    the top-N GoalTimingWindow objects, each carrying the dominant *mechanic*
    (purple patch / red-card chaos / late urgency chase / plain baseline tempo)
    that produced most of that window's goals — i.e. the "why", grounded in
    what the simulation itself did rather than a pre-set curve.
    """
    if total_goals <= 0:
        return []

    windows: list[GoalTimingWindow] = []
    _LABELS = {
        "purple_patch":    "hot-streak burst (purple patch)",
        "red_card_state":  "post-red-card imbalance",
        "urgency_chase":   "late chase / catch-up press",
        "baseline":        "baseline tempo (no special trigger)",
    }
    for i in range(len(_WINDOW_STARTS)):
        goals_here = window_goal_counts[i]
        if goals_here == 0:
            windows.append(GoalTimingWindow(
                start=_WINDOW_STARTS[i], end=_WINDOW_ENDS[i],
                probability=0.0, label=f"{_WINDOW_STARTS[i]}'–{_WINDOW_ENDS[i]}'",
                sample_goals=0, dominant_driver=_LABELS["baseline"], driver_share=0.0,
            ))
            continue
        # Each goal was pre-classified into exactly one bucket (see the
        # aggregation loop in simulate()), so these tallies sum to goals_here.
        tally = window_cause_counts[i]
        dominant_key = max(tally, key=tally.get) if tally else "baseline"
        dominant_share = tally.get(dominant_key, 0) / goals_here
        windows.append(GoalTimingWindow(
            start=_WINDOW_STARTS[i], end=_WINDOW_ENDS[i],
            probability=round(goals_here / total_goals, 4),
            label=f"{_WINDOW_STARTS[i]}'–{_WINDOW_ENDS[i]}'",
            sample_goals=goals_here,
            dominant_driver=_LABELS.get(dominant_key, dominant_key),
            driver_share=round(dominant_share, 3),
        ))
    windows.sort(key=lambda w: w.probability, reverse=True)
    return windows[:top_n]


def simulate(
    lam_h:          float,
    lam_a:          float,
    n:              int   = DEFAULT_ITERATIONS,
    variance_boost: float = 1.0,
    attack_h:    float = 1.0,   # home attack ratio vs league avg (1.0 = avg)
    defence_h:   float = 1.0,   # home defence weakness ratio (lower = stronger)
    attack_a:    float = 1.0,   # away attack ratio vs league avg
    defence_a:   float = 1.0,   # away defence weakness ratio
    form_h:      float = 0.5,   # home trailing-5 form score  0–1
    form_a:      float = 0.5,   # away trailing-5 form score  0–1
    chaos_level: float = 0.0,   # extra variance for low-data/Tier 3 leagues 0–1
    rho:         float = -0.06, # Dixon-Coles low-score correlation coefficient
    max_iterations: int = MAX_ITERATIONS,   # hard budget ceiling
    forebet_score: "tuple[int, int] | None" = None,  # Forebet's exact-score pick, for cross-check only
    progress: Progress | None = None,
) -> MonteCarloResult:
    """
    Run a bounded Poisson Monte Carlo and return aggregated match probabilities.
    Each iteration draws one home score and one away score from the supplied
    λ values. The default production budget is exactly 100,000 iterations.

    `forebet_score`, if supplied, is used *only* to report how much probability
    mass the simulation itself assigns to Forebet's predicted scoreline (a
    cross-check "indice"), never to steer the simulation's own random draws.
    Any actual influence Forebet should have belongs upstream, blended into
    lam_h/lam_a by the caller (see engine/predictor.py), bounded and capped —
    never copied in as the final prediction.

    Parameters
    ----------
    lam_h, lam_a    : bounded expected goals / Poisson λ values
    n               : requested simulation budget (default 100,000)
    variance_boost  : retained for API compatibility; no longer changes λ
    attack_h/a      : retained for API compatibility
    defence_h/a     : retained for API compatibility
    form_h/a        : controls only a small bounded day-jitter
    chaos_level     : controls only a small bounded day-jitter
    rho             : Dixon-Coles low-score correlation (negative = classic)
    max_iterations  : hard ceiling for the requested budget
    forebet_score   : optional (home, away) Forebet correct-score pick

    Returns
    -------
    MonteCarloResult with all probabilities rounded to 4 decimal places.
    """
    if n < 100:
        n = 100
    target_iterations = min(max(n, 100), max_iterations, MAX_ITERATIONS)
    if progress:
        progress(40, 100, "Simulation Monte-Carlo — préparation des 100 000 tirages…")

    # Defensive boundary for direct callers. The predictor applies the same
    # [0.20, 3.00] rule before reaching this function.
    eff_h = max(_LAMBDA_MIN, min(_LAMBDA_MAX, float(lam_h)))
    eff_a = max(_LAMBDA_MIN, min(_LAMBDA_MAX, float(lam_a)))

    # ── Pre-match jitter σ ─────────────────────────────────────────────────────
    # Poor recent form and a leaky opposing defence both widen the day-to-day
    # variance in how a team's expected output actually shows up on the pitch.
    jitter_h = min(0.42, 0.10
                   + 0.12 * (1.0 - form_h)
                   + 0.05 * max(0.0, defence_a - 1.0)
                   + 0.06 * chaos_level)
    jitter_a = min(0.42, 0.10
                   + 0.12 * (1.0 - form_a)
                   + 0.05 * max(0.0, defence_h - 1.0)
                   + 0.06 * chaos_level)

    total_attack    = max(0.2, attack_h + attack_a)
    purple_home_w   = max(0.15, min(0.85, attack_h / total_attack))

    params = _MatchParams(
        jitter_h=jitter_h, jitter_a=jitter_a, chaos_level=max(0.0, min(1.0, chaos_level)),
        red_card_hazard=0.0, purple_prob=0.0,
        purple_home_weight=purple_home_w,
    )

    score_counts: dict[tuple[int, int], int] = {}
    home_wins = draws = away_wins = 0
    btts = over25 = over35 = 0
    btts_o25 = btts_u25 = nbtts_o25 = 0
    sum_h = sum_a = 0.0
    red_card_matches = purple_patch_matches = 0

    # Empirical goal-timing aggregation — filled from real simulated events.
    window_goal_counts = [0] * len(_WINDOW_STARTS)
    window_cause_counts: list[dict[str, int]] = [dict() for _ in _WINDOW_STARTS]
    total_goals_logged = 0
    minute_goal_counts = [0] * (MINUTES_REGULATION + 1)

    # ── Fixed-precision batch loop ────────────────────────────────────────────
    # Batches keep aggregation predictable; they do not stop early.
    ran = 0
    achieved_se = 1.0
    converged_early = False
    while ran < target_iterations:
        this_batch = min(_BATCH_SIZE, target_iterations - ran)
        if this_batch <= 0:
            break

        for _ in range(this_batch):
            hg, ag, had_red_card, had_purple, goal_log = _play_one_match(eff_h, eff_a, params)

            final_h, final_a = hg, ag
            tau = _dixon_coles_tau(min(hg, 1), min(ag, 1), rho)
            drop_home_goal = tau < 1 and hg == 1 and ag == 0 and random.random() < (1 - tau)
            drop_away_goal = tau < 1 and hg == 0 and ag == 1 and random.random() < (1 - tau)
            if drop_home_goal:
                final_h = 0
            if drop_away_goal:
                final_a = 0

            sum_h += final_h
            sum_a += final_a

            if final_h > final_a:
                home_wins += 1
            elif final_h < final_a:
                away_wins += 1
            else:
                draws += 1

            tg = final_h + final_a
            is_btts = final_h >= 1 and final_a >= 1
            is_over25 = tg > 2

            if is_btts:
                btts += 1
            if is_over25:
                over25 += 1
            if tg > 3:
                over35 += 1
            if is_btts and is_over25:
                btts_o25 += 1
            if is_btts and not is_over25:
                btts_u25 += 1
            if not is_btts and is_over25:
                nbtts_o25 += 1

            if had_red_card:
                red_card_matches += 1
            if had_purple:
                purple_patch_matches += 1

            key = (min(final_h, 9), min(final_a, 9))
            score_counts[key] = score_counts.get(key, 0) + 1

            # Reconcile the goal log with the (rare) Dixon-Coles low-score
            # correction above: if a goal was statistically "undone", drop the
            # matching late event so the timing windows stay consistent with
            # the final scoreline actually counted.
            if drop_home_goal or drop_away_goal:
                side_to_drop = "H" if drop_home_goal else "A"
                for idx in range(len(goal_log) - 1, -1, -1):
                    if goal_log[idx][1] == side_to_drop:
                        del goal_log[idx]
                        break

            for minute, _side, causes in goal_log:
                w_idx = _window_index_for_minute(minute)
                window_goal_counts[w_idx] += 1
                total_goals_logged += 1
                minute_goal_counts[min(90, max(1, minute))] += 1
                if "purple_patch" in causes:
                    bucket = "purple_patch"
                elif "red_card_state" in causes:
                    bucket = "red_card_state"
                elif "urgency_chase" in causes:
                    bucket = "urgency_chase"
                else:
                    bucket = "baseline"
                window_cause_counts[w_idx][bucket] = window_cause_counts[w_idx].get(bucket, 0) + 1

        ran += this_batch
        if progress:
            progress(
                40 + int(50 * ran / max(target_iterations, 1)),
                100,
                f"Simulation Monte-Carlo — {ran:,}/{target_iterations:,} tirages",
            )

        if ran >= target_iterations:
            achieved_se = _standard_error(home_wins, ran)

    n = ran
    converged_early = n >= DEFAULT_ITERATIONS

    modal = max(score_counts, key=score_counts.get) if score_counts else (0, 0)
    if len(score_counts) >= 2:
        second = max((k for k in score_counts if k != modal), key=score_counts.get)
    else:
        second = modal

    over25_prob = over25 / n
    over35_prob = over35 / n

    timing_windows = _build_empirical_timing_windows(
        window_goal_counts, window_cause_counts, total_goals_logged,
        top_n=5,
    )
    goal_minutes = [
        (minute, round(count / max(total_goals_logged, 1), 4))
        for minute, count in sorted(
            enumerate(minute_goal_counts), key=lambda item: item[1], reverse=True
        )
        if minute > 0 and count > 0
    ][:5]
    ranked_scores = sorted(score_counts.items(), key=lambda item: item[1], reverse=True)
    top_scores = [
        (score, round(count / n, 4))
        for score, count in ranked_scores[:5]
    ]

    # ── Forebet exact-score cross-check (report-only, never steers the sim) ──
    forebet_prob = 0.0
    forebet_rank = 0
    forebet_alignment = "n/a"
    if forebet_score is not None:
        fkey = (min(forebet_score[0], 9), min(forebet_score[1], 9))
        forebet_prob = round(score_counts.get(fkey, 0) / n, 4)
        ranked = sorted(score_counts.items(), key=lambda item: item[1], reverse=True)
        for idx, (sc, _count) in enumerate(ranked, start=1):
            if sc == fkey:
                forebet_rank = idx
                break
        if forebet_rank == 0:
            forebet_alignment = "diverges"
        elif forebet_rank <= 2:
            forebet_alignment = "aligned"
        elif forebet_rank <= 5:
            forebet_alignment = "plausible"
        else:
            forebet_alignment = "diverges"

    return MonteCarloResult(
        home_win_prob          = round(home_wins       / n, 4),
        draw_prob              = round(draws            / n, 4),
        away_win_prob          = round(away_wins         / n, 4),
        btts_prob              = round(btts              / n, 4),
        over25_prob            = round(over25_prob,             4),
        under25_prob           = round(1.0 - over25_prob,       4),
        over35_prob            = round(over35_prob,             4),
        under35_prob           = round(1.0 - over35_prob,       4),
        mean_home_goals        = round(sum_h             / n, 2),
        mean_away_goals        = round(sum_a             / n, 2),
        iterations              = n,
        modal_score             = modal,
        second_score            = second,
        top_scores              = top_scores,
        btts_yes_over25_prob    = round(btts_o25    / n, 4),
        btts_yes_under25_prob   = round(btts_u25    / n, 4),
        nbtts_over25_prob       = round(nbtts_o25   / n, 4),
        red_card_match_share    = round(red_card_matches / n, 4),
        purple_patch_share      = round(purple_patch_matches / n, 4),
        distinct_scorelines     = len(score_counts),
        converged                = converged_early,
        convergence_se           = round(achieved_se, 5),
        timing_windows           = timing_windows,
        goal_minutes            = goal_minutes,
        forebet_score_prob       = forebet_prob,
        forebet_score_rank       = forebet_rank,
        forebet_alignment        = forebet_alignment,
    )


def draw_scenario(
    lam_h: float,
    lam_a: float,
    form_h: float = 0.5,
    form_a: float = 0.5,
    chaos_level: float = 0.0,
    rho: float = -0.06,
) -> dict:
    """Draw one fresh match realization from the aggregate model."""
    eff_h = max(_LAMBDA_MIN, min(_LAMBDA_MAX, float(lam_h)))
    eff_a = max(_LAMBDA_MIN, min(_LAMBDA_MAX, float(lam_a)))
    jitter_h = min(0.42, 0.10 + 0.12 * (1.0 - form_h) + 0.06 * chaos_level)
    jitter_a = min(0.42, 0.10 + 0.12 * (1.0 - form_a) + 0.06 * chaos_level)
    day_mult_h = _bounded_day_multiplier(1.0 - jitter_h / 0.42, chaos_level)
    day_mult_a = _bounded_day_multiplier(1.0 - jitter_a / 0.42, chaos_level)

    hg = _poisson_draw(max(_LAMBDA_MIN, min(_LAMBDA_MAX, eff_h * day_mult_h)))
    ag = _poisson_draw(max(_LAMBDA_MIN, min(_LAMBDA_MAX, eff_a * day_mult_a)))
    tau = _dixon_coles_tau(min(hg, 1), min(ag, 1), rho)
    if tau < 1 and hg == 1 and ag == 0 and random.random() < (1 - tau):
        hg = 0
    if tau < 1 and hg == 0 and ag == 1 and random.random() < (1 - tau):
        ag = 0

    goal_minutes: list[tuple[int, str]] = []
    for _ in range(hg):
        goal_minutes.append((_sample_goal_minute(), "H"))
    for _ in range(ag):
        goal_minutes.append((_sample_goal_minute(), "A"))
    goal_minutes.sort(key=lambda goal: goal[0])
    return {
        "home_goals": hg,
        "away_goals": ag,
        "goal_minutes": goal_minutes,
        "day_mult_home": round(day_mult_h, 3),
        "day_mult_away": round(day_mult_a, 3),
    }


def simulate_batch(
    fixtures: list[tuple[float, float]],
    n:        int = DEFAULT_ITERATIONS,
) -> list[MonteCarloResult]:
    """
    Simulate a list of (lam_h, lam_a) pairs in one call.
    Useful for scanner pre-computation; uses neutral strength defaults.
    """
    return [simulate(lam_h, lam_a, n) for lam_h, lam_a in fixtures]
# ── Goal timing windows ───────────────────────────────────────────────────────

@dataclass
class GoalTimingWindow:
    """A single 9-minute goal timing window with its probability estimate."""
    start:       int     # e.g. 1
    end:         int     # e.g. 9
    probability: float   # fraction of match goals expected in this window
    label:       str     # e.g. "1'–9'"
    # ── V3: populated when this window comes from simulate()'s real event log
    # (compute_goal_timing_windows() below, the pre-simulation formula-based
    # estimate, leaves these at their defaults) ──────────────────────────────
    sample_goals:    int   = 0     # how many simulated goals actually fell here
    dominant_driver: str   = ""    # main mechanic behind this window's goals
    driver_share:    float = 0.0   # share of this window's goals from that mechanic


# Empirical base weights per 9-minute window (from football goal timing research).
# Goals cluster at end of first half (37-45') and end of match (82-90').
# Sums to ~10.5; normalised to 1.0 inside the function.
_WINDOW_STARTS  = [1, 10, 19, 28, 37, 46, 55, 64, 73, 82]
_WINDOW_ENDS    = [9, 18, 27, 36, 45, 54, 63, 72, 81, 90]
_BASE_WEIGHTS   = [
    0.90,   # 1'–9'   : settling phase — below-average goal rate
    0.90,   # 10'–18' : first creative period
    0.95,   # 19'–27'
    1.00,   # 28'–36' : mid first-half build-up
    1.30,   # 37'–45' : pre-halftime pressure + injury time surge
    1.00,   # 46'–54' : post-restart reset
    1.00,   # 55'–63'
    1.05,   # 64'–72' : mid second-half push
    1.10,   # 73'–81' : teams chasing results
    1.30,   # 82'–90' : closing pressure, late goals, injury time
]


def compute_goal_timing_windows(
    lam_h:         float,
    lam_a:         float,
    chaos_level:   float = 0.10,
    crowd_emotion: float = 0.50,
    top_n:         int   = 4,
) -> list[GoalTimingWindow]:
    """
    Estimate the most probable goal-timing windows across 90 minutes.

    Model — non-homogeneous Poisson intensity approach:
      1. Start from an empirical base weight curve (10 × 9-minute windows).
      2. Scale each team's contribution by their λ (xG), giving heavier weight
         to high-scoring teams but preserving the temporal shape.
      3. Chaos flattens the curve toward uniform (unknown leagues are
         unpredictable; we cannot pin the timing as confidently).
      4. Crowd emotion adds a small first-half home-pressure boost: a loud
         home crowd tends to drive early intensity, shifting a fraction of
         first-half probability toward the 19'–36' band.
      5. Top-N windows by probability are returned.

    Parameters
    ----------
    lam_h         : home expected goals (Poisson λ)
    lam_a         : away expected goals
    chaos_level   : 0 (structured Tier 1) → 1 (chaotic Tier 3); flattens curve
    crowd_emotion : 0–1 crowd-pressure index from predictor; boosts first-half
    top_n         : number of windows to return (default 4)

    Returns
    -------
    List of GoalTimingWindow sorted by probability (highest first), length = top_n.
    """
    # ── Step 1: start from base weights ──────────────────────────────────────
    weights = list(_BASE_WEIGHTS)  # mutable copy

    # ── Step 2: home/away goal-rate shift ─────────────────────────────────────
    # Higher lam_h → home team dominates → early pressure boosts first-half
    # Higher lam_a → counter-attacking away team → presses harder in 2nd half
    total_xg = max(0.10, lam_h + lam_a)
    home_ratio = lam_h / total_xg   # fraction driven by home attack
    away_ratio = lam_a / total_xg

    for i in range(len(weights)):
        if i < 5:   # first half windows 0-4
            weights[i] *= (0.85 + 0.30 * home_ratio)
        else:       # second half windows 5-9
            weights[i] *= (0.85 + 0.30 * away_ratio)

    # ── Step 3: xG concentration — high-scoring games have sharper timing peaks ─
    # A high-xG match (lam_h+lam_a ≫ 2.5) means both teams attack relentlessly;
    # their pressure concentrates into known high-intensity windows (pre-halftime,
    # closing phase), amplifying the peaks relative to quiet spells.
    # A low-xG game (total < 1.5) is more random — the single goal can arrive
    # anywhere, so we flatten the curve toward uniform.
    #
    # Implementation: scale each window's *deviation from the mean* by a
    # concentration factor.  At factor=1.0 (neutral) the shape is unchanged.
    # factor > 1 → peaks taller, valleys deeper (more concentrated).
    # factor < 1 → peaks shorten, valleys rise (more uniform).
    # This is mathematically distinct from a global multiplier because it
    # changes the *shape* of the distribution, not just an overall level that
    # would cancel in normalisation.
    concentration = max(0.6, min(1.8, total_xg / 2.5))
    mean_w        = sum(weights) / len(weights)
    weights = [
        max(0.01, mean_w + (w - mean_w) * concentration)
        for w in weights
    ]

    # ── Step 4: chaos flattens toward uniform ────────────────────────────────
    chaos_clamped = max(0.0, min(1.0, chaos_level))
    uniform_w     = sum(weights) / len(weights)
    weights = [
        w * (1.0 - chaos_clamped) + uniform_w * chaos_clamped
        for w in weights
    ]

    # ── Step 5: crowd emotion — early-pressure boost ────────────────────────
    # Crowd pushes the home team harder in the first 36 minutes; shift a
    # fraction proportional to crowd_emotion from late first-half into 19'–36'.
    crowd_shift = 0.06 * max(0.0, min(1.0, crowd_emotion))
    # Take from windows 37-45 (index 4) and give to 19-27 & 28-36 (indices 2,3)
    donate = weights[4] * crowd_shift
    weights[4]  -= donate
    weights[2]  += donate * 0.4
    weights[3]  += donate * 0.6

    # ── Step 6: normalise to probabilities ───────────────────────────────────
    total = sum(weights)
    probs = [w / total for w in weights]

    # ── Step 7: build and return top-N windows ───────────────────────────────
    windows = [
        GoalTimingWindow(
            start       = _WINDOW_STARTS[i],
            end         = _WINDOW_ENDS[i],
            probability = round(probs[i], 4),
            label       = f"{_WINDOW_STARTS[i]}'–{_WINDOW_ENDS[i]}'",
        )
        for i in range(len(probs))
    ]
    windows.sort(key=lambda w: w.probability, reverse=True)
    return windows[:top_n]

