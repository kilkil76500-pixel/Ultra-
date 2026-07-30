"""
engine/confidence_v2.py — V16 : Indice de confiance IA sur 100.

Remplace le simple HIGH / MEDIUM / LOW par un score de fiabilité sur 100
basé sur 7 dimensions :

  1. Qualité des données (matchs joués, disponibilité des stats étendues)
  2. Cohérence des sources (alignement Forebet vs moteur interne)
  3. Stabilité des équipes (régularité des résultats, pas de séries erratiques)
  4. Blessures (nombre et criticité des absents)
  5. Variance Monte-Carlo (écart-type des simulations)
  6. Niveau de chaos de la ligue (imprévisibilité historique)
  7. Qualité H2H (nombre et pertinence des confrontations directes)

Score 0-100 avec thresholds :
  90-100 : 🟢🟢 Elite — données excellentes, modèle très stable
  75-89  : 🟢   HIGH  — bonne confiance, pari recommandé
  55-74  : 🟡   MEDIUM — confiance modérée, prudence recommandée
  35-54  : 🟠   LOW   — incertitude élevée
  0-34   : 🔴   TRÈS BAS — données insuffisantes, éviter

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Structure du score de confiance ──────────────────────────────────────────

@dataclass
class ConfidenceV2:
    """Score de confiance V16 sur 100."""
    score:          int    = 0          # 0-100
    label:          str    = "LOW"      # ELITE / HIGH / MEDIUM / LOW / VERY_LOW
    risk_level:     str    = "HIGH"
    grade:          str    = "D"        # A+ / A / B / C / D / F
    # Composantes
    data_quality:   float  = 0.0        # 0-20 pts
    source_consistency: float = 0.0     # 0-15 pts
    team_stability: float  = 0.0        # 0-20 pts
    injury_impact:  float  = 0.0        # 0-15 pts (négatif = pénalité)
    mc_variance:    float  = 0.0        # 0-15 pts
    chaos_penalty:  float  = 0.0        # 0-10 pts (négatif = pénalité)
    h2h_quality:    float  = 0.0        # 0-5 pts
    memory_risk:    float  = 0.0        # -8 à 0 pts (V19.14, pénalité mémoire d'équipe)
    breakdown:      dict   = field(default_factory=dict)
    explanation:    str    = ""


# ── Calcul principal ──────────────────────────────────────────────────────────

def compute_confidence_v2(
    # Données de base
    home_games_played:     int   = 0,
    away_games_played:     int   = 0,
    extended_data_used:    bool  = False,
    local_data_quality:    str   = "unknown",     # "available" | "sparse" | "unknown"
    # Cohérence des sources
    forebet_alignment:     str   = "n/a",         # "strong" | "moderate" | "weak" | "n/a"
    forebet_weight_applied: float = 0.0,
    # Stabilité des équipes
    home_form_str:         str   = "",            # ex: "WWDLW"
    away_form_str:         str   = "",
    # Blessures
    home_injuries_out:     int   = 0,
    away_injuries_out:     int   = 0,
    # Variance Monte-Carlo
    mc_convergence_se:     float = 0.0,
    mc_converged:          bool  = False,
    mc_distinct_scorelines: int  = 0,
    home_win_prob:         float = 0.33,
    draw_prob:             float = 0.33,
    away_win_prob:         float = 0.33,
    # Chaos
    chaos_level:           float = 0.0,
    tier:                  int   = 1,
    # H2H
    h2h_matches_used:      int   = 0,
    # Calibration actuelle (depuis calibration.py)
    calibration_quality:   str   = "UNKNOWN",
    # V19.14 — mémoire interne (aucune API externe) : taux d'erreur historique
    # du modèle sur cette équipe précise (0.0 = neutre/inconnu, voir team_memory.py)
    home_team_risk:        float = 0.0,
    away_team_risk:        float = 0.0,
) -> ConfidenceV2:
    """
    Calcule le score de confiance V16 sur 100 points.

    Chaque dimension est notée indépendamment puis sommée.
    Le score final est plafonné à 100 et ne peut pas être négatif.
    """

    # ── 1. Qualité des données (0-20 pts) ─────────────────────────────────────
    min_games = min(home_games_played, away_games_played)
    if min_games <= 0:
        dq = 2.0
    elif min_games < 5:
        dq = 5.0
    elif min_games < 10:
        dq = 10.0
    elif min_games < 20:
        dq = 15.0
    else:
        dq = 20.0

    # Bonus données étendues
    if extended_data_used:
        dq = min(20.0, dq + 3.0)
    # Malus données éparses
    if local_data_quality == "sparse":
        dq = max(0.0, dq - 5.0)
    elif local_data_quality == "unknown":
        dq = max(0.0, dq - 3.0)

    # ── 2. Cohérence des sources (0-15 pts) ───────────────────────────────────
    _ALIGNMENT_SCORES = {
        "strong":   15.0,
        "moderate": 10.0,
        "weak":      4.0,
        "n/a":       8.0,   # Forebet non disponible → neutre
    }
    sc = _ALIGNMENT_SCORES.get(forebet_alignment, 8.0)
    # Bonus si Forebet a été intégré avec un poids significatif
    if forebet_weight_applied > 0.15:
        sc = min(15.0, sc + 2.0)

    # ── 3. Stabilité des équipes (0-20 pts) ───────────────────────────────────
    home_var = _form_variance(home_form_str)
    away_var = _form_variance(away_form_str)
    avg_var  = (home_var + away_var) / 2.0
    # Variance 0 = forme parfaitement stable, 1 = totalement erratique
    ts = 20.0 * (1.0 - avg_var)
    ts = max(0.0, min(20.0, ts))

    # ── 4. Impact des blessures (0-15 pts, réduit si blessures importantes) ──
    max_injuries = max(home_injuries_out, away_injuries_out)
    if max_injuries == 0:
        inj = 15.0
    elif max_injuries == 1:
        inj = 12.0
    elif max_injuries == 2:
        inj = 9.0
    elif max_injuries == 3:
        inj = 6.0
    else:
        inj = max(0.0, 15.0 - max_injuries * 2.5)

    # ── 5. Variance Monte-Carlo (0-15 pts) ────────────────────────────────────
    if mc_converged:
        mc_pts = 12.0
    else:
        mc_pts = 6.0

    # Bonus si convergence rapide (SE très faible)
    if mc_convergence_se < 0.001:
        mc_pts = min(15.0, mc_pts + 3.0)
    elif mc_convergence_se > 0.01:
        mc_pts = max(0.0, mc_pts - 4.0)

    # Bonus si le nombre de scorelines distincts est élevé (simulation riche)
    if mc_distinct_scorelines > 50:
        mc_pts = min(15.0, mc_pts + 2.0)

    # Pénalité si résultat trop incertain (3 issues quasi-équiprobables)
    max_prob = max(home_win_prob, draw_prob, away_win_prob)
    if max_prob < 0.38:
        mc_pts = max(0.0, mc_pts - 4.0)

    # ── 6. Chaos de la ligue (0-10 pts, c'est une pénalité) ──────────────────
    # chaos_level ∈ [0, 1] → pénalité proportionnelle
    # Tier 3 = pénalité supplémentaire
    tier_penalty = {1: 0.0, 2: 1.5, 3: 3.5}.get(tier, 3.5)
    chaos_pts = max(0.0, 10.0 - chaos_level * 10.0 - tier_penalty)

    # ── 7. Qualité H2H (0-5 pts) ──────────────────────────────────────────────
    if h2h_matches_used >= 6:
        h2h_pts = 5.0
    elif h2h_matches_used >= 3:
        h2h_pts = 3.0
    elif h2h_matches_used >= 1:
        h2h_pts = 1.5
    else:
        h2h_pts = 0.0

    # ── 8. Mémoire d'équipe (0 à -8 pts, pénalité uniquement) ─────────────────
    # V19.14 — home_team_risk/away_team_risk viennent de team_memory.py :
    # le taux d'erreur RÉEL du modèle sur cette équipe précise, mesuré sur
    # l'historique de /resultat (0.0 = pas assez de matchs pour juger, donc
    # neutre — jamais de bonus, seulement une pénalité quand on SAIT que le
    # modèle se trompe souvent sur cette équipe). Purement interne, aucune
    # donnée externe à Forebet.
    team_risk = max(home_team_risk, away_team_risk)
    memory_pts = -round(min(8.0, team_risk * 12.0), 2)

    # ── Assemblage ────────────────────────────────────────────────────────────
    raw_score = dq + sc + ts + inj + mc_pts + chaos_pts + h2h_pts + memory_pts

    # Bonus calibration de qualité
    calib_bonus = {"HIGH": 3.0, "MEDIUM": 1.0, "LOW": -2.0}.get(calibration_quality, 0.0)
    raw_score += calib_bonus

    score = max(0, min(100, int(round(raw_score))))

    # ── Labels et grades ──────────────────────────────────────────────────────
    if score >= 90:
        label, risk, grade = "ELITE",    "TRÈS FAIBLE", "A+"
    elif score >= 75:
        label, risk, grade = "HIGH",     "FAIBLE",      "A"
    elif score >= 55:
        label, risk, grade = "MEDIUM",   "MODÉRÉ",      "B"
    elif score >= 35:
        label, risk, grade = "LOW",      "ÉLEVÉ",       "C"
    else:
        label, risk, grade = "TRÈS BAS", "TRÈS ÉLEVÉ",  "D"

    explanation = _build_explanation(score, label, dq, sc, ts, inj, mc_pts, chaos_pts, h2h_pts)

    breakdown = {
        "données (sur 20)":      round(dq, 1),
        "sources (sur 15)":      round(sc, 1),
        "stabilité (sur 20)":    round(ts, 1),
        "blessures (sur 15)":    round(inj, 1),
        "simulation (sur 15)":   round(mc_pts, 1),
        "ligue/chaos (sur 10)":  round(chaos_pts, 1),
        "H2H (sur 5)":           round(h2h_pts, 1),
        "mémoire équipe (−8 à 0)": round(memory_pts, 1),
        "bonus calibration":     round(calib_bonus, 1),
    }

    return ConfidenceV2(
        score=score,
        label=label,
        risk_level=risk,
        grade=grade,
        data_quality=round(dq, 2),
        source_consistency=round(sc, 2),
        team_stability=round(ts, 2),
        injury_impact=round(inj, 2),
        mc_variance=round(mc_pts, 2),
        chaos_penalty=round(chaos_pts, 2),
        h2h_quality=round(h2h_pts, 2),
        memory_risk=round(memory_pts, 2),
        breakdown=breakdown,
        explanation=explanation,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _form_variance(form_str: str) -> float:
    """
    Calcule la variance de la forme (0 = stable, 1 = erratique).
    Une alternance parfaite W/L donne variance max.
    """
    if not form_str:
        return 0.5
    recent = form_str[-8:]  # 8 derniers matchs max
    values = []
    for ch in recent:
        if ch == "W":
            values.append(1.0)
        elif ch == "D":
            values.append(0.5)
        elif ch == "L":
            values.append(0.0)
    if len(values) < 2:
        return 0.3
    mean = sum(values) / len(values)
    var  = sum((v - mean) ** 2 for v in values) / len(values)
    # Normaliser : variance max pour W/L alterné = 0.25
    return min(1.0, var / 0.25)


def _build_explanation(
    score: int, label: str,
    dq: float, sc: float, ts: float,
    inj: float, mc_pts: float, chaos_pts: float, h2h_pts: float,
) -> str:
    """Génère une explication courte du score de confiance."""
    parts: list[str] = []

    if dq < 8.0:
        parts.append("données insuffisantes")
    if sc < 6.0:
        parts.append("désalignement entre sources")
    if ts < 8.0:
        parts.append("équipes irrégulières")
    if inj < 9.0:
        parts.append("blessures impactantes")
    if mc_pts < 7.0:
        parts.append("simulation instable")
    if chaos_pts < 4.0:
        parts.append("ligue très imprévisible")

    if not parts:
        if score >= 75:
            return "Données solides, modèle convergent, fiabilité élevée."
        else:
            return "Profil standard, confiance modérée."

    return "Limites : " + ", ".join(parts) + "."


# ── Formatage Telegram ─────────────────────────────────────────────────────────

def format_confidence_v2(c: ConfidenceV2, short: bool = False) -> str:
    """Formatte l'indice de confiance V16 pour Telegram."""
    _LABEL_EMOJI = {
        "ELITE":    "🟢🟢",
        "HIGH":     "🟢",
        "MEDIUM":   "🟡",
        "LOW":      "🟠",
        "TRÈS BAS": "🔴",
    }
    emoji = _LABEL_EMOJI.get(c.label, "⚪")

    # Barre de progression
    filled = max(0, min(10, int(c.score / 10)))
    bar_cells = ["🟦"] * filled + ["░"] * (10 - filled)
    bar = "".join(bar_cells)

    if short:
        return f"{emoji} Fiabilité : <b>{c.score}/100</b> [{c.grade}]  — {c.label}"

    lines = [
        f"🎯 <b>Indice de confiance V16</b>",
        f"  {bar}  <b>{c.score}/100</b>  [{c.grade}]",
        f"  {emoji} {c.label}  |  Risque : {c.risk_level}",
        f"  {c.explanation}",
        "",
        "  <b>Détail :</b>",
    ]
    for k, v in c.breakdown.items():
        if v != 0.0:
            sign = "+" if v >= 0 else ""
            if "bonus" in k or "pénalité" in k:
                lines.append(f"  • {k:<24} {sign}{v:.1f}")
            else:
                lines.append(f"  • {k:<24} {v:.1f}")

    return "\n".join(lines)
