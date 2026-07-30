"""
engine/montecarlo_v5.py — Monte-Carlo V5 : simulation par scénarios de match.

V19 — _poisson_draw()/_dc_tau() ici sont DÉLIBÉRÉMENT différents de leurs
homonymes dans engine/montecarlo.py (bornes de lambda différentes, formule
Dixon-Coles différente). Ne pas les fusionner ni les "nettoyer" en un seul
module : voir la note équivalente en tête de montecarlo.py et VERSIONS.md.

Au lieu de simuler uniquement des buts, chaque itération représente un vrai
déroulement de match avec des événements narratifs :

  Scénarios possibles (tirés aléatoirement au début de chaque simulation) :
  ─────────────────────────────────────────────────────────────────────────
  normal           — match sans événement particulier (60 % des cas)
  early_goal       — but précoce (< 15') → équipe qui mène ferme le jeu
  red_card         — carton rouge → intensité réduite pour l'équipe pénalisée
  penalty          — penalty → but supplémentaire aléatoire
  comeback         — équipe qui perd revient au score (énergie 70'+ boostée)
  defensive_block  — l'équipe favorite "ferme le jeu" après avoir mené
  dominant_sterile — domination sans efficacité (tirs sans buts)
  high_press       — pressing haut des 2 équipes → match ouvert, plus de buts

Chaque scénario modifie les λ de Poisson de manière réaliste et cohérente.
La distribution finale reflète ces vrais scénarios de match.

Compatible avec l'interface de montecarlo.py (MonteCarloResult).
Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import math
import random
import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_ITERATIONS = 100_000
_LAMBDA_MIN = 0.20
_LAMBDA_MAX = 3.20
_BATCH_SIZE  = 5_000

Progress = Callable[[int, int, str], None]


# ── Scénarios ─────────────────────────────────────────────────────────────────

_SCENARIO_WEIGHTS: dict[str, float] = {
    "normal":           0.52,
    "early_goal":       0.10,
    "red_card":         0.07,
    "penalty":          0.06,
    "comeback":         0.06,
    "defensive_block":  0.08,
    "dominant_sterile": 0.07,
    "high_press":       0.04,
}

# Clés et poids cumulés (précalculés pour la performance)
_SCENARIO_KEYS    = list(_SCENARIO_WEIGHTS.keys())
_SCENARIO_CUM     = []
_cum = 0.0
for _k in _SCENARIO_KEYS:
    _cum += _SCENARIO_WEIGHTS[_k]
    _SCENARIO_CUM.append(_cum)


def _draw_scenario(chaos_level: float = 0.0) -> str:
    """Tire un scénario au sort. Le chaos augmente les scénarios extrêmes."""
    r = random.random()
    # Avec du chaos, on booste les scénarios non-normaux
    if chaos_level > 0.3 and random.random() < chaos_level * 0.4:
        # Exclure "normal" et tirer dans les autres
        candidates = [k for k in _SCENARIO_KEYS if k != "normal"]
        return random.choice(candidates)
    for i, key in enumerate(_SCENARIO_KEYS):
        if r <= _SCENARIO_CUM[i]:
            return key
    return "normal"


def _apply_scenario(
    lam_h: float,
    lam_a: float,
    scenario: str,
    home_stronger: bool,
    chaos_level: float = 0.0,
) -> tuple[float, float, str]:
    """
    Applique un scénario aux λ et retourne (lam_h_mod, lam_a_mod, narrative).

    Toutes les modifications sont multiplicatives pour rester dans [λ_min, λ_max].
    """
    # Favori et outsider pour les scénarios asymétriques
    fav_h = lam_h if home_stronger else lam_a
    fav_a = lam_a if home_stronger else lam_h
    _ = fav_h  # utilisé indirectement via les multiplicateurs

    narrative = ""

    if scenario == "normal":
        # Petite jitter aléatoire (±5 %)
        jitter_h = 1.0 + random.gauss(0, 0.05)
        jitter_a = 1.0 + random.gauss(0, 0.05)
        lam_h = lam_h * max(0.80, min(1.20, jitter_h))
        lam_a = lam_a * max(0.80, min(1.20, jitter_a))
        narrative = "match équilibré"

    elif scenario == "early_goal":
        # But précoce pour l'équipe favorite → elle gère le match
        if home_stronger:
            lam_h = lam_h * 0.85   # favoris gèrent
            lam_a = lam_a * 1.10   # adversaire pousse
        else:
            lam_a = lam_a * 0.85
            lam_h = lam_h * 1.10
        # Ajouter le but précoce au résultat via une Poisson supplémentaire
        # On le simule en augmentant légèrement le λ du favori
        if home_stronger:
            lam_h = lam_h + 0.5
        else:
            lam_a = lam_a + 0.5
        narrative = "but précoce, le favori gère"

    elif scenario == "red_card":
        # Carton rouge : équipe pénalisée réduite, adversaire boosté
        # Qui prend le carton ? Plutôt l'outsider (défend plus)
        if random.random() < 0.55:  # outsider
            if home_stronger:
                lam_a = lam_a * 0.65
                lam_h = lam_h * 1.20
            else:
                lam_h = lam_h * 0.65
                lam_a = lam_a * 1.20
        else:  # favori (surprise)
            if home_stronger:
                lam_h = lam_h * 0.65
                lam_a = lam_a * 1.15
            else:
                lam_a = lam_a * 0.65
                lam_h = lam_h * 1.15
        narrative = "carton rouge, déséquilibre numérique"

    elif scenario == "penalty":
        # Penalty → un but supplémentaire pour l'une des équipes
        # Probabilité proportionnelle aux λ
        total_lam = lam_h + lam_a
        if total_lam > 0 and random.random() < lam_h / total_lam:
            lam_h = lam_h + 0.65   # 65 % de conversion
        else:
            lam_a = lam_a + 0.65
        narrative = "penalty décisif dans le match"

    elif scenario == "comeback":
        # L'outsider revient : on équilibre les λ
        if home_stronger:
            lam_h = lam_h * 0.85
            lam_a = lam_a * 1.30
        else:
            lam_a = lam_a * 0.85
            lam_h = lam_h * 1.30
        narrative = "remontée spectaculaire de l'outsider"

    elif scenario == "defensive_block":
        # Le favori a marqué et ferme le jeu → moins de buts total
        if home_stronger:
            lam_h = lam_h * 0.75
            lam_a = lam_a * 0.80
        else:
            lam_a = lam_a * 0.75
            lam_h = lam_h * 0.80
        narrative = "le favori préserve son avantage, jeu fermé"

    elif scenario == "dominant_sterile":
        # Beaucoup de tirs, peu de buts → λ réduit malgré la domination
        if home_stronger:
            lam_h = lam_h * 0.78
            lam_a = lam_a * 0.90
        else:
            lam_a = lam_a * 0.78
            lam_h = lam_h * 0.90
        narrative = "domination stérile, peu d'efficacité devant le but"

    elif scenario == "high_press":
        # Les deux équipes pressent → match très ouvert, λ boostés
        boost = 1.15 + chaos_level * 0.10
        lam_h = lam_h * boost
        lam_a = lam_a * boost
        narrative = "pressing intense des deux équipes, match très ouvert"

    # Clamp final
    lam_h = max(_LAMBDA_MIN, min(_LAMBDA_MAX, lam_h))
    lam_a = max(_LAMBDA_MIN, min(_LAMBDA_MAX, lam_a))

    return lam_h, lam_a, narrative


# ── Résultat ──────────────────────────────────────────────────────────────────

@dataclass
class MonteCarloV5Result:
    """Résultat enrichi de la simulation V5 par scénarios."""
    home_win_prob:       float
    draw_prob:           float
    away_win_prob:       float
    btts_prob:           float
    over25_prob:         float
    under25_prob:        float
    over35_prob:         float
    under35_prob:        float
    btts_yes_over25_prob:  float
    btts_yes_under25_prob: float
    nbtts_over25_prob:     float
    mean_home_goals:     float
    mean_away_goals:     float
    modal_score:         tuple[int, int]
    second_score:        tuple[int, int]
    top_scores:          list
    distinct_scorelines: int
    iterations:          int
    converged:           bool
    convergence_se:      float
    # V5 extras
    scenario_distribution: dict  = field(default_factory=dict)
    scenario_narratives:   list  = field(default_factory=list)
    red_card_match_share:  float = 0.0
    purple_patch_share:    float = 0.0   # gardé pour compatibilité


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate_v5(
    lam_h:       float,
    lam_a:       float,
    n:           int    = DEFAULT_ITERATIONS,
    chaos_level: float  = 0.0,
    max_iterations: int = DEFAULT_ITERATIONS,
    forebet_score:  tuple[int, int] | None = None,
    progress:    Progress | None = None,
) -> MonteCarloV5Result:
    """
    Simulation Monte-Carlo V5 par scénarios de match.

    Chaque itération :
    1. Tire un scénario de match (normal, carton rouge, but précoce, etc.)
    2. Modifie les λ selon ce scénario
    3. Tire les buts depuis Poisson(λ_modifié) + Dixon-Coles

    Compatible avec l'interface de montecarlo.simulate().
    """
    lam_h = max(_LAMBDA_MIN, min(_LAMBDA_MAX, lam_h))
    lam_a = max(_LAMBDA_MIN, min(_LAMBDA_MAX, lam_a))
    n     = min(n, max_iterations)

    home_stronger = lam_h >= lam_a

    score_counts: dict[tuple[int, int], int] = {}
    home_wins = draws = away_wins = 0
    btts = over25 = over35 = btts_over25 = btts_under25 = nbtts_over25 = 0
    total_home_goals = total_away_goals = 0
    red_card_count   = 0
    scenario_tally:  dict[str, int] = {k: 0 for k in _SCENARIO_KEYS}

    # Convergence tracking
    prev_hw_rate = 0.0
    converged    = False
    conv_se      = 0.0

    batches = (n + _BATCH_SIZE - 1) // _BATCH_SIZE

    for batch_idx in range(batches):
        batch_n = min(_BATCH_SIZE, n - batch_idx * _BATCH_SIZE)

        for _ in range(batch_n):
            # Tirer le scénario
            scenario = _draw_scenario(chaos_level)
            scenario_tally[scenario] = scenario_tally.get(scenario, 0) + 1
            if scenario == "red_card":
                red_card_count += 1

            # Modifier les λ selon le scénario
            lh, la, _ = _apply_scenario(lam_h, lam_a, scenario, home_stronger, chaos_level)

            # Buts via Poisson (+ jitter de journée léger)
            day_jitter = 1.0 + random.gauss(0, 0.04)
            lh = max(_LAMBDA_MIN, lh * max(0.85, min(1.15, day_jitter)))
            la = max(_LAMBDA_MIN, la * max(0.85, min(1.15, 1.0 + random.gauss(0, 0.04))))

            hg = _poisson_draw(lh)
            ag = _poisson_draw(la)

            # Dixon-Coles pour les scores faibles
            dc_ok = _dixon_coles_accept(hg, ag, lam_h, lam_a)
            if not dc_ok:
                # Retirer ce tirage et recommencer simplement
                hg = _poisson_draw(lam_h)
                ag = _poisson_draw(lam_a)

            # Accumulation
            score_counts[(hg, ag)] = score_counts.get((hg, ag), 0) + 1
            total = hg + ag
            if hg > ag:
                home_wins += 1
            elif hg == ag:
                draws += 1
            else:
                away_wins += 1

            if hg >= 1 and ag >= 1:
                btts += 1
                if total > 2:
                    btts_over25 += 1
                else:
                    btts_under25 += 1
            else:
                if total > 2:
                    nbtts_over25 += 1

            if total > 2:
                over25 += 1
            if total > 3:
                over35 += 1

            total_home_goals += hg
            total_away_goals  += ag

        # Vérification de convergence toutes les 2 batches
        done = (batch_idx + 1) * _BATCH_SIZE
        if done >= 20_000 and batch_idx % 2 == 1:
            hw_rate = home_wins / done
            delta   = abs(hw_rate - prev_hw_rate)
            conv_se = delta
            if delta < 0.002:
                converged = True
                n = done   # couper court
                break
            prev_hw_rate = hw_rate

        if progress:
            done = min((batch_idx + 1) * _BATCH_SIZE, n)
            progress(30 + int(done / n * 50), 100, f"MC V5 — {done:,} simulations…")

    # ── Agrégation ────────────────────────────────────────────────────────────
    total_sims = home_wins + draws + away_wins
    if total_sims == 0:
        total_sims = 1

    # Top scores
    sorted_scores = sorted(score_counts.items(), key=lambda x: x[1], reverse=True)
    top_scores    = [
        (f"{sc[0]}-{sc[1]}", round(cnt / total_sims, 4))
        for sc, cnt in sorted_scores[:10]
    ]
    modal_score  = sorted_scores[0][0] if sorted_scores else (1, 0)
    second_score = sorted_scores[1][0] if len(sorted_scores) > 1 else (0, 0)

    # Distribution des scénarios (en %)
    scenario_dist = {
        k: round(v / total_sims * 100, 1)
        for k, v in scenario_tally.items()
        if v > 0
    }

    # Top narratifs (scénarios les plus fréquents ≠ normal)
    narratives = [
        k for k in sorted(
            [k for k in scenario_tally if k != "normal" and scenario_tally[k] > 0],
            key=lambda k: scenario_tally[k], reverse=True
        )[:3]
    ]

    return MonteCarloV5Result(
        home_win_prob    = round(home_wins / total_sims, 4),
        draw_prob        = round(draws / total_sims, 4),
        away_win_prob    = round(away_wins / total_sims, 4),
        btts_prob        = round(btts / total_sims, 4),
        over25_prob      = round(over25 / total_sims, 4),
        under25_prob     = round(1.0 - over25 / total_sims, 4),
        over35_prob      = round(over35 / total_sims, 4),
        under35_prob     = round(1.0 - over35 / total_sims, 4),
        btts_yes_over25_prob  = round(btts_over25 / total_sims, 4),
        btts_yes_under25_prob = round(btts_under25 / total_sims, 4),
        nbtts_over25_prob     = round(nbtts_over25 / total_sims, 4),
        mean_home_goals  = round(total_home_goals / total_sims, 3),
        mean_away_goals  = round(total_away_goals / total_sims, 3),
        modal_score      = modal_score,
        second_score     = second_score,
        top_scores       = top_scores,
        distinct_scorelines = len(score_counts),
        iterations       = total_sims,
        converged        = converged,
        convergence_se   = round(conv_se, 5),
        scenario_distribution = scenario_dist,
        scenario_narratives   = narratives,
        red_card_match_share  = round(red_card_count / total_sims, 4),
        purple_patch_share    = 0.0,
    )


# ── Helpers mathématiques ─────────────────────────────────────────────────────

def _poisson_draw(lam: float) -> int:
    """Tire un entier depuis Poisson(lam) via la méthode de Knuth."""
    lam = max(_LAMBDA_MIN, min(_LAMBDA_MAX, lam))
    L   = math.exp(-lam)
    k   = 0
    p   = 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


_RHO = -0.13   # corrélation Dixon-Coles (empirique football)

def _dc_tau(hg: int, ag: int, lh: float, la: float) -> float:
    """Facteur de correction Dixon-Coles pour les scores faibles."""
    if hg == 0 and ag == 0:
        return 1.0 - lh * la * _RHO
    if hg == 1 and ag == 0:
        return 1.0 + la * _RHO
    if hg == 0 and ag == 1:
        return 1.0 + lh * _RHO
    if hg == 1 and ag == 1:
        return 1.0 - _RHO
    return 1.0


def _dixon_coles_accept(hg: int, ag: int, lh: float, la: float) -> bool:
    """Accepte/rejette un score faible via le taux de correction DC."""
    if hg > 1 or ag > 1:
        return True
    tau = _dc_tau(hg, ag, lh, la)
    # Accepter avec probabilité proportionnelle à tau (borné à [0,1.5])
    tau = max(0.0, min(1.5, tau))
    return random.random() < (tau / 1.5)


def format_scenario_distribution(dist: dict[str, float]) -> str:
    """Formatte la distribution des scénarios pour l'affichage Telegram."""
    _EMOJI: dict[str, str] = {
        "normal":           "⚽",
        "early_goal":       "⚡",
        "red_card":         "🟥",
        "penalty":          "🎯",
        "comeback":         "🔄",
        "defensive_block":  "🏰",
        "dominant_sterile": "😤",
        "high_press":       "🔥",
    }
    _LABELS: dict[str, str] = {
        "normal":           "Match normal",
        "early_goal":       "But précoce",
        "red_card":         "Carton rouge",
        "penalty":          "Penalty",
        "comeback":         "Remontée",
        "defensive_block":  "Jeu défensif",
        "dominant_sterile": "Domination stérile",
        "high_press":       "Pressing intense",
    }
    lines = ["🎲 <b>Scénarios simulés (MC V5)</b>"]
    for key, pct in sorted(dist.items(), key=lambda x: x[1], reverse=True):
        if pct >= 1.0:
            emoji = _EMOJI.get(key, "•")
            label = _LABELS.get(key, key)
            lines.append(f"  {emoji} {label:<22} {pct:.0f}%")
    return "\n".join(lines)
