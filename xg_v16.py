"""
engine/xg_v16.py — Moteur xG V16 (nouvelle génération).

Remplace le calcul _index_to_xg() de V13 par un modèle multi-facteurs
qui tient compte de :

  1. Efficacité offensive réelle (tirs cadrés, grosses occasions)
  2. Efficacité défensive adverse (buts concédés / tirs cadrés concédés)
  3. Facteur terrain domicile/extérieur
  4. Fatigue (matchs joués récemment, rotations)
  5. Importance du match (ligue, phase de saison)
  6. Style tactique (déterminé par tactical.py)
  7. Niveau du championnat (tier)
  8. Blessures (multiplicateur fourni par injuries.py)

Objectif : éviter les scores irréalistes (5-5, 6-4) tout en permettant
un 4-0 lorsque les données le justifient. Plage de sortie : [0.20, 3.20].

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.tactical import TacticalProfile

logger = logging.getLogger(__name__)

# ── Plages de sortie ──────────────────────────────────────────────────────────
XG_MIN_V16 = 0.20
XG_MAX_V16 = 3.20   # légèrement au-dessus de V15 pour permettre les grands écarts

# ── Constantes de calibration ─────────────────────────────────────────────────
# xG de base selon le tier de ligue
_LEAGUE_BASE_XG: dict[int, tuple[float, float]] = {
    # tier: (avg_home_xg, avg_away_xg)
    # Keep a modest home advantage while avoiding a structural gap when both
    # teams have the same strength index.
    1: (1.45, 1.30),   # Top leagues (PL, Liga, BL, Serie A, L1)
    2: (1.42, 1.27),   # Divisions 2
    3: (1.38, 1.23),   # Divisions 3+
}

# Poids des composantes dans le calcul final du xG
_W_BASE        = 0.35   # Indice de force hérité (Strength Index → xG)
_W_SHOTS       = 0.20   # Efficacité tirs cadrés
_W_BIG_CHANCES = 0.15   # Grosses occasions créées/concédées
_W_FORM        = 0.15   # Forme offensive récente
_W_H2H         = 0.10   # Historique des confrontations
_W_IMPORTANCE  = 0.05   # Importance du match


@dataclass
class XGProfile:
    """Profil xG complet pour un match (les deux équipes)."""
    home_xg:         float
    away_xg:         float
    home_xg_raw:     float   # avant application des facteurs correctifs
    away_xg_raw:     float
    home_shots_factor: float  # facteur lié aux tirs
    away_shots_factor: float
    home_big_chance_factor: float
    away_big_chance_factor: float
    importance_factor: float
    fatigue_factor_home: float
    fatigue_factor_away: float
    tactical_adj_home:  float = 0.0   # ajustement tactique
    tactical_adj_away:  float = 0.0
    breakdown:          dict  = field(default_factory=dict)


def compute_xg_v16(
    # Indice de force 0-100 (hérité du Strength Index V13)
    home_index:       float,
    away_index:       float,
    # Stats de base
    home_goals_for:   float = 1.50,
    away_goals_for:   float = 1.15,
    home_goals_against: float = 1.15,
    away_goals_against: float = 1.50,
    # Tirs cadrés (par match, -1 si inconnu)
    home_shots_on_target:  float = -1.0,
    away_shots_on_target:  float = -1.0,
    # Grosses occasions (par match, -1 si inconnu)
    home_big_chances:      float = -1.0,
    away_big_chances:      float = -1.0,
    # Matchs joués (pour la fatigue)
    home_games_played: int = 0,
    away_games_played: int = 0,
    # Blessures
    home_injury_mult:  float = 1.0,
    away_injury_mult:  float = 1.0,
    # Tier de ligue
    tier:              int   = 1,
    # Importance du match (0.0 = bas, 1.0 = très important)
    match_importance:  float = 0.5,
    # Profil tactique (optionnel, depuis tactical.py)
    home_tactical:     "TacticalProfile | None" = None,
    away_tactical:     "TacticalProfile | None" = None,
    # Forme récente (nombre de buts marqués sur 5 derniers matchs)
    home_recent_goals: float = -1.0,
    away_recent_goals: float = -1.0,
    # Moyennes H2H
    h2h_avg_home:      float = -1.0,
    h2h_avg_away:      float = -1.0,
) -> XGProfile:
    """
    Calcule le xG V16 pour les deux équipes.

    Pipeline :
    1. xG de base à partir de l'indice de force (backward-compatible)
    2. Ajustement tirs cadrés (si disponibles)
    3. Ajustement grosses occasions
    4. Ajustement forme offensive récente
    5. Ajustement H2H
    6. Facteur d'importance du match
    7. Facteur de fatigue
    8. Facteur tactique (depuis tactical.py)
    9. Multiplicateur de blessures
    10. Clamp dans [XG_MIN_V16, XG_MAX_V16]
    """
    base_h, base_a = _LEAGUE_BASE_XG.get(tier, _LEAGUE_BASE_XG[3])

    # ── 1. xG de base (Strength Index → xG, même formule qu'en V13) ──────────
    K = 0.8
    raw_h = base_h * math.exp(K * (home_index - 50.0) / 50.0)
    raw_a = base_a * math.exp(K * (away_index - 50.0) / 50.0)

    # ── 2. Facteur tirs cadrés ────────────────────────────────────────────────
    # Référence : ~4.5 tirs cadrés par match en moyenne européenne
    SHOTS_REF = 4.5
    shots_factor_h = _shots_factor(home_shots_on_target, SHOTS_REF)
    shots_factor_a = _shots_factor(away_shots_on_target, SHOTS_REF)

    # ── 3. Grosses occasions ──────────────────────────────────────────────────
    # Référence : ~2.5 grosses occasions par match
    BIG_REF = 2.5
    bc_factor_h = _big_chance_factor(home_big_chances, BIG_REF)
    bc_factor_a = _big_chance_factor(away_big_chances, BIG_REF)

    # ── 4. Forme offensive récente (5 derniers matchs) ────────────────────────
    form_factor_h = _recent_form_factor(home_recent_goals, base_h)
    form_factor_a = _recent_form_factor(away_recent_goals, base_a)

    # ── 5. Ajustement H2H ─────────────────────────────────────────────────────
    h2h_factor_h = _h2h_factor(h2h_avg_home, base_h)
    h2h_factor_a = _h2h_factor(h2h_avg_away, base_a)

    # ── 6. Importance du match ────────────────────────────────────────────────
    # Match très important → équipes plus prudentes → légère réduction des buts
    importance_factor = 1.0 - 0.06 * match_importance  # max −6 % pour match décisif

    # ── 7. Fatigue ────────────────────────────────────────────────────────────
    fatigue_h = _fatigue_factor(home_games_played)
    fatigue_a = _fatigue_factor(away_games_played)

    # ── 8. Ajustement tactique ────────────────────────────────────────────────
    tact_adj_h = _tactical_xg_adjustment(home_tactical, away_tactical, side="home")
    tact_adj_a = _tactical_xg_adjustment(away_tactical, home_tactical, side="away")

    # ── Assemblage pondéré ────────────────────────────────────────────────────
    # Chaque facteur est une déviation par rapport à 1.0 :
    # xG_final = raw_xg × Π(facteurs) × injury_mult
    #
    # On utilise des poids pour limiter l'impact d'un seul facteur aberrant.
    def _weighted_product(raw: float, factors: list[tuple[float, float]]) -> float:
        """Combine des facteurs par moyenne pondérée de leurs déviations."""
        total_w = sum(w for _, w in factors)
        if total_w <= 0:
            return raw
        combined = 1.0
        for factor, w in factors:
            # Chaque facteur contribue proportionnellement à son poids
            combined *= factor ** (w / total_w)
        return raw * combined

    xg_h = _weighted_product(raw_h, [
        (shots_factor_h,  _W_SHOTS),
        (bc_factor_h,     _W_BIG_CHANCES),
        (form_factor_h,   _W_FORM),
        (h2h_factor_h,    _W_H2H),
        (importance_factor, _W_IMPORTANCE),
    ])
    xg_a = _weighted_product(raw_a, [
        (shots_factor_a,  _W_SHOTS),
        (bc_factor_a,     _W_BIG_CHANCES),
        (form_factor_a,   _W_FORM),
        (h2h_factor_a,    _W_H2H),
        (importance_factor, _W_IMPORTANCE),
    ])

    # Appliquer fatigue, tactical et blessures
    xg_h = xg_h * fatigue_h * (1.0 + tact_adj_h) * home_injury_mult
    xg_a = xg_a * fatigue_a * (1.0 + tact_adj_a) * away_injury_mult

    # Clamp final
    xg_h = max(XG_MIN_V16, min(XG_MAX_V16, xg_h))
    xg_a = max(XG_MIN_V16, min(XG_MAX_V16, xg_a))

    breakdown = {
        "home": {
            "base_xg":       round(raw_h, 3),
            "shots_factor":  round(shots_factor_h, 3),
            "big_chance":    round(bc_factor_h, 3),
            "form_factor":   round(form_factor_h, 3),
            "h2h_factor":    round(h2h_factor_h, 3),
            "importance":    round(importance_factor, 3),
            "fatigue":       round(fatigue_h, 3),
            "tactical_adj":  round(tact_adj_h, 3),
            "injury_mult":   round(home_injury_mult, 3),
            "final_xg":      round(xg_h, 3),
        },
        "away": {
            "base_xg":       round(raw_a, 3),
            "shots_factor":  round(shots_factor_a, 3),
            "big_chance":    round(bc_factor_a, 3),
            "form_factor":   round(form_factor_a, 3),
            "h2h_factor":    round(h2h_factor_a, 3),
            "importance":    round(importance_factor, 3),
            "fatigue":       round(fatigue_a, 3),
            "tactical_adj":  round(tact_adj_a, 3),
            "injury_mult":   round(away_injury_mult, 3),
            "final_xg":      round(xg_a, 3),
        },
    }

    return XGProfile(
        home_xg=round(xg_h, 3),
        away_xg=round(xg_a, 3),
        home_xg_raw=round(raw_h, 3),
        away_xg_raw=round(raw_a, 3),
        home_shots_factor=round(shots_factor_h, 3),
        away_shots_factor=round(shots_factor_a, 3),
        home_big_chance_factor=round(bc_factor_h, 3),
        away_big_chance_factor=round(bc_factor_a, 3),
        importance_factor=round(importance_factor, 3),
        fatigue_factor_home=round(fatigue_h, 3),
        fatigue_factor_away=round(fatigue_a, 3),
        tactical_adj_home=round(tact_adj_h, 3),
        tactical_adj_away=round(tact_adj_a, 3),
        breakdown=breakdown,
    )


# ── Fonctions de facteur ──────────────────────────────────────────────────────

def _shots_factor(shots_on_target: float, reference: float) -> float:
    """
    Retourne un facteur multiplicatif basé sur les tirs cadrés.
    Limité à [0.75, 1.30] pour éviter les amplifications extrêmes.
    Si shots_on_target < 0, retourne 1.0 (neutre).
    """
    if shots_on_target < 0:
        return 1.0
    # Rapport à la référence, légèrement atténué (racine carrée)
    ratio = shots_on_target / max(reference, 0.1)
    factor = math.sqrt(ratio)
    return max(0.75, min(1.30, factor))


def _big_chance_factor(big_chances: float, reference: float) -> float:
    """
    Facteur basé sur les grosses occasions.
    Les grosses occasions ont un impact fort sur le xG réel.
    Limité à [0.80, 1.25].
    """
    if big_chances < 0:
        return 1.0
    ratio = big_chances / max(reference, 0.1)
    # Impact direct, pas de racine carrée (grosses occasions = fiables)
    factor = 0.80 + 0.45 * min(ratio, 1.0)   # sature à 1.25 pour ratio=1
    return max(0.80, min(1.25, factor))


def _recent_form_factor(recent_goals: float, league_avg: float) -> float:
    """
    Facteur basé sur les buts marqués sur les 5 derniers matchs.
    Si recent_goals < 0, retourne 1.0.
    """
    if recent_goals < 0:
        return 1.0
    if league_avg <= 0:
        return 1.0
    # Normaliser par rapport à la moyenne de ligue × 5 matchs
    expected = league_avg * 5.0
    ratio = recent_goals / expected
    # Atténuer avec racine cubique pour lisser les extrêmes
    factor = ratio ** (1.0 / 3.0)
    return max(0.80, min(1.20, factor))


def _h2h_factor(h2h_avg: float, league_avg: float) -> float:
    """
    Facteur basé sur la moyenne H2H.
    Retourne 1.0 si h2h_avg < 0 (inconnu).
    """
    if h2h_avg < 0 or league_avg <= 0:
        return 1.0
    ratio = h2h_avg / league_avg
    factor = 0.90 + 0.20 * min(ratio, 1.0)   # plage [0.90, 1.10]
    return max(0.90, min(1.10, factor))


def _fatigue_factor(games_played: int) -> float:
    """
    Légère pénalité de fatigue pour les équipes avec beaucoup de matchs.
    Au-delà de 30 matchs, légère réduction du xG.
    En dessous de 5, données insuffisantes → légère incertitude (0.95).
    """
    if games_played <= 0:
        return 0.95   # données manquantes → légère incertitude
    if games_played < 5:
        return 0.96
    if games_played < 25:
        return 1.00   # normal
    if games_played < 30:
        return 0.99   # légère fatigue
    if games_played < 38:
        return 0.97   # fin de saison
    return 0.96       # saison très longue


def _tactical_xg_adjustment(
    own_profile:  "TacticalProfile | None",
    opp_profile:  "TacticalProfile | None",
    side:         str,
) -> float:
    """
    Retourne un ajustement xG basé sur les profils tactiques.
    Valeur en delta par rapport à 1.0 (ex: +0.08 = +8 %).

    Règles :
    - Équipe très offensive vs bloc bas → xG réduit (difficile de percer)
    - Contre-attaque vs possession → xG légèrement augmenté (espaces)
    - Pressing haut vs faible intensité → xG augmenté (récupérations hautes)
    """
    if own_profile is None:
        return 0.0

    adj = 0.0

    # Équipe offensive : naturellement plus de xG
    if own_profile.style == "offensive":
        adj += 0.06
    elif own_profile.style == "counter_attack":
        # Efficace mais moins de volume → légèrement réduit
        adj -= 0.03
    elif own_profile.style == "possession":
        # Possession sans efficacité → neutre
        adj += 0.02
    elif own_profile.style == "low_block":
        # Bloc bas → faible xG offensif
        adj -= 0.10
    elif own_profile.style == "low_intensity":
        adj -= 0.05

    # Impact de l'adversaire
    if opp_profile is not None:
        if opp_profile.style == "low_block":
            # Bloc bas défensif → réduit xG de l'attaquant
            adj -= 0.05
        elif opp_profile.style == "pressing":
            # Pressing haut adversaire → légère réduction du jeu de construction
            adj -= 0.03
        elif opp_profile.style == "low_intensity":
            # Adversaire passif → légère augmentation
            adj += 0.04

    return max(-0.20, min(0.15, adj))


def format_xg_breakdown(profile: XGProfile, home_name: str, away_name: str) -> str:
    """Formatte le breakdown xG pour l'affichage Telegram."""
    lines = ["🔬 <b>Détail xG V16</b>"]
    for side, name in [("home", home_name), ("away", away_name)]:
        b = profile.breakdown.get(side, {})
        lines.append(f"\n  <b>{name}</b>")
        lines.append(f"  Base xG     : {b.get('base_xg', 0):.2f}")
        if b.get("shots_factor", 1.0) != 1.0:
            lines.append(f"  Tirs cadrés : ×{b.get('shots_factor', 1):.2f}")
        if b.get("big_chance", 1.0) != 1.0:
            lines.append(f"  Gdes occas  : ×{b.get('big_chance', 1):.2f}")
        if b.get("form_factor", 1.0) != 1.0:
            lines.append(f"  Forme off.  : ×{b.get('form_factor', 1):.2f}")
        if b.get("tactical_adj", 0.0) != 0.0:
            lines.append(f"  Tactique    : {b.get('tactical_adj', 0):+.2f}")
        if b.get("fatigue", 1.0) < 0.99:
            lines.append(f"  Fatigue     : ×{b.get('fatigue', 1):.2f}")
        lines.append(f"  ➡ xG final  : <b>{b.get('final_xg', 0):.2f}</b>")
    return "\n".join(lines)
