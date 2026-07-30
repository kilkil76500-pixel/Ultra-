"""
engine/tactical.py — V16 : Analyse tactique automatique.

Reconnaît le style de jeu d'une équipe et ajuste automatiquement :
  - BTTS (les deux équipes marquent)
  - Over/Under 2.5
  - Score exact modal
  - xG (via xg_v16.py)

Styles détectés :
  offensive       — attaque très haute, beaucoup de buts
  possession      — contrôle du ballon, rythme moyen
  pressing        — récupération haute, buts sur transitions
  counter_attack  — peu de buts mais efficaces, risque en contre
  low_block       — défense solide, peu de buts concédés
  low_intensity   — peu d'engagement, peu de buts des deux côtés
  balanced        — style équilibré (défaut)

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Profil tactique ────────────────────────────────────────────────────────────

@dataclass
class TacticalProfile:
    """
    Profil tactique d'une équipe pour un match donné.

    Tous les champs sont optionnels : les valeurs par défaut correspondent
    à un style équilibré (style == "balanced") sans ajustement.
    """
    style:               str   = "balanced"   # voir liste ci-dessus
    confidence:          float = 0.0          # 0-1 : certitude du profil
    # Métriques brutes utilisées pour la détection
    avg_goals_scored:    float = -1.0         # buts marqués / match
    avg_goals_conceded:  float = -1.0         # buts concédés / match
    avg_shots:           float = -1.0         # tirs / match
    avg_shots_conceded:  float = -1.0         # tirs concédés / match
    possession_pct:      float = -1.0         # possession moyenne (%)
    clean_sheet_pct:     float = -1.0         # % de matches sans encaisser
    failed_score_pct:    float = -1.0         # % de matches sans marquer
    pressing_intensity:  float = -1.0         # proxy : tirs cadrés × récupérations
    # Ajustements calculés
    btts_adj:            float = 0.0          # delta probabilité BTTS
    over25_adj:          float = 0.0          # delta probabilité Over 2.5
    xg_adj_self:         float = 0.0          # delta xG propre
    xg_adj_opp:          float = 0.0          # delta xG adversaire (ce qu'il concède)
    description:         str   = ""
    signals:             list  = field(default_factory=list)


# ── Détection du style ────────────────────────────────────────────────────────

def detect_style(
    avg_goals_scored:   float = -1.0,
    avg_goals_conceded: float = -1.0,
    avg_shots:          float = -1.0,
    avg_shots_conceded: float = -1.0,
    clean_sheet_pct:    float = -1.0,
    failed_score_pct:   float = -1.0,
    possession_pct:     float = -1.0,
    over25_pct:         float = -1.0,   # % de leurs matchs en Over 2.5
    btts_pct:           float = -1.0,   # % de leurs matchs avec BTTS
    team_name:          str   = "",
) -> TacticalProfile:
    """
    Analyse les statistiques disponibles et retourne un TacticalProfile.

    Fonctionne avec données partielles : les signaux manquants sont ignorés.
    La confiance dépend du nombre de signaux disponibles.
    """
    signals: list[str] = []
    style_votes: dict[str, float] = {
        "offensive":     0.0,
        "possession":    0.0,
        "pressing":      0.0,
        "counter_attack": 0.0,
        "low_block":     0.0,
        "low_intensity": 0.0,
        "balanced":      0.5,  # vote de base
    }

    n_signals = 0

    # ── Signal 1 : buts marqués ───────────────────────────────────────────────
    if avg_goals_scored >= 0:
        n_signals += 1
        if avg_goals_scored >= 2.0:
            style_votes["offensive"] += 2.0
            signals.append(f"🔥 Forte attaque ({avg_goals_scored:.1f} buts/match)")
        elif avg_goals_scored >= 1.5:
            style_votes["offensive"] += 0.8
            style_votes["possession"] += 0.5
        elif avg_goals_scored <= 0.8:
            style_votes["low_block"] += 1.5
            style_votes["low_intensity"] += 1.0
            signals.append(f"🔒 Faible attaque ({avg_goals_scored:.1f} buts/match)")
        elif avg_goals_scored <= 1.1:
            style_votes["counter_attack"] += 0.8
            style_votes["low_block"] += 0.5

    # ── Signal 2 : buts concédés ──────────────────────────────────────────────
    if avg_goals_conceded >= 0:
        n_signals += 1
        if avg_goals_conceded <= 0.7:
            style_votes["low_block"] += 2.0
            signals.append(f"🛡️ Défense solide ({avg_goals_conceded:.1f} concédés/match)")
        elif avg_goals_conceded <= 1.0:
            style_votes["low_block"] += 0.8
            style_votes["possession"] += 0.5
        elif avg_goals_conceded >= 2.0:
            style_votes["offensive"] += 0.5  # défense ouverte = match ouvert
            style_votes["low_intensity"] += 0.5
            signals.append(f"⚠️ Défense fragile ({avg_goals_conceded:.1f} concédés/match)")

    # ── Signal 3 : tirs ───────────────────────────────────────────────────────
    if avg_shots >= 0:
        n_signals += 1
        if avg_shots >= 16:
            style_votes["offensive"] += 1.5
            style_votes["possession"] += 1.0
            signals.append(f"🎯 Gros volume de tirs ({avg_shots:.0f}/match)")
        elif avg_shots >= 13:
            style_votes["offensive"] += 0.5
            style_votes["possession"] += 0.8
        elif avg_shots <= 8:
            style_votes["counter_attack"] += 1.0
            style_votes["low_block"] += 0.8
            signals.append(f"🎯 Peu de tirs ({avg_shots:.0f}/match)")

    # ── Signal 4 : tirs concédés ──────────────────────────────────────────────
    if avg_shots_conceded >= 0:
        n_signals += 1
        if avg_shots_conceded <= 8:
            style_votes["low_block"] += 1.5
            style_votes["pressing"] += 0.8   # pressing = pas de tirs adverses
            signals.append(f"🔒 Peu de tirs concédés ({avg_shots_conceded:.0f}/match)")
        elif avg_shots_conceded >= 16:
            style_votes["counter_attack"] += 0.5
            style_votes["low_intensity"] += 0.8

    # ── Signal 5 : clean sheet ────────────────────────────────────────────────
    if clean_sheet_pct >= 0:
        n_signals += 1
        if clean_sheet_pct >= 40:
            style_votes["low_block"] += 1.5
            style_votes["possession"] += 0.5
            signals.append(f"🧱 Clean sheets fréquents ({clean_sheet_pct:.0f}%)")
        elif clean_sheet_pct <= 15:
            style_votes["offensive"] += 0.5
            style_votes["low_intensity"] += 0.5

    # ── Signal 6 : failed to score ────────────────────────────────────────────
    if failed_score_pct >= 0:
        n_signals += 1
        if failed_score_pct >= 40:
            style_votes["low_block"] += 1.0
            style_votes["counter_attack"] += 0.8
            signals.append(f"😶 Souvent muets ({failed_score_pct:.0f}%)")
        elif failed_score_pct <= 15:
            style_votes["offensive"] += 1.0

    # ── Signal 7 : possession ─────────────────────────────────────────────────
    if possession_pct >= 0:
        n_signals += 1
        if possession_pct >= 58:
            style_votes["possession"] += 2.0
            signals.append(f"⚽ Possession dominante ({possession_pct:.0f}%)")
        elif possession_pct >= 52:
            style_votes["possession"] += 0.8
        elif possession_pct <= 42:
            style_votes["counter_attack"] += 1.0
            style_votes["low_block"] += 0.5
            signals.append(f"↩️ Peu de possession ({possession_pct:.0f}%)")

    # ── Signal 8 : Over 2.5 historique ───────────────────────────────────────
    if over25_pct >= 0:
        n_signals += 1
        if over25_pct >= 65:
            style_votes["offensive"] += 1.0
            style_votes["low_intensity"] -= 0.5
            signals.append(f"📈 Souvent Over 2.5 ({over25_pct:.0f}%)")
        elif over25_pct <= 35:
            style_votes["low_block"] += 0.5
            style_votes["low_intensity"] += 0.5
            signals.append(f"📉 Souvent Under 2.5 ({over25_pct:.0f}%)")

    # ── Signal 9 : BTTS historique ────────────────────────────────────────────
    if btts_pct >= 0:
        n_signals += 1
        if btts_pct >= 65:
            style_votes["offensive"] += 0.8
            style_votes["low_intensity"] += 0.3  # matchs ouverts des deux côtés
            signals.append(f"⚡ BTTS fréquent ({btts_pct:.0f}%)")
        elif btts_pct <= 30:
            style_votes["low_block"] += 0.8

    # ── Détermination du style dominant ───────────────────────────────────────
    best_style = max(style_votes, key=style_votes.get)
    best_score = style_votes[best_style]

    # Confiance basée sur le nombre de signaux et l'écart au 2e
    sorted_votes = sorted(style_votes.values(), reverse=True)
    gap = sorted_votes[0] - sorted_votes[1] if len(sorted_votes) > 1 else 0.0
    confidence = min(1.0, (n_signals / 9.0) * (0.5 + 0.5 * min(gap / 2.0, 1.0)))

    # Si trop peu de signaux ou écart faible → balanced
    if n_signals < 2 or (best_style != "balanced" and best_score < 1.0):
        best_style = "balanced"
        confidence = 0.2

    profile = _build_profile(
        style=best_style,
        confidence=confidence,
        avg_goals_scored=avg_goals_scored,
        avg_goals_conceded=avg_goals_conceded,
        avg_shots=avg_shots,
        avg_shots_conceded=avg_shots_conceded,
        possession_pct=possession_pct,
        clean_sheet_pct=clean_sheet_pct,
        failed_score_pct=failed_score_pct,
        signals=signals,
    )
    return profile


def _build_profile(
    style: str,
    confidence: float,
    signals: list[str],
    **kwargs,
) -> TacticalProfile:
    """Construit un TacticalProfile avec les ajustements correspondant au style."""

    # Ajustements par style
    #   btts_adj    : delta sur la probabilité BTTS (ex: +0.08 = +8 pp)
    #   over25_adj  : delta sur Over 2.5
    #   xg_adj_self : delta sur le xG propre
    #   xg_adj_opp  : delta sur le xG que l'on concède (impact défensif)
    _ADJUSTMENTS: dict[str, dict[str, float]] = {
        "offensive":     {"btts_adj": +0.06, "over25_adj": +0.10, "xg_adj_self": +0.08, "xg_adj_opp": +0.04},
        "possession":    {"btts_adj": +0.02, "over25_adj": +0.04, "xg_adj_self": +0.03, "xg_adj_opp": -0.02},
        "pressing":      {"btts_adj": +0.04, "over25_adj": +0.06, "xg_adj_self": +0.05, "xg_adj_opp": -0.03},
        "counter_attack":{"btts_adj": -0.03, "over25_adj": -0.04, "xg_adj_self": -0.04, "xg_adj_opp": -0.02},
        "low_block":     {"btts_adj": -0.10, "over25_adj": -0.12, "xg_adj_self": -0.10, "xg_adj_opp": -0.08},
        "low_intensity": {"btts_adj": -0.05, "over25_adj": -0.08, "xg_adj_self": -0.05, "xg_adj_opp": -0.03},
        "balanced":      {"btts_adj":  0.00, "over25_adj":  0.00, "xg_adj_self":  0.00, "xg_adj_opp":  0.00},
    }

    _DESCRIPTIONS: dict[str, str] = {
        "offensive":      "Équipe très offensive, match ouvert probable",
        "possession":     "Jeu de possession, rythme contrôlé",
        "pressing":       "Pressing haut, récupérations hautes fréquentes",
        "counter_attack": "Jeu en contre, peu de volume mais efficace",
        "low_block":      "Bloc bas, défense solide, peu de buts",
        "low_intensity":  "Faible intensité, peu d'engagement offensif",
        "balanced":       "Style équilibré",
    }

    adj = _ADJUSTMENTS.get(style, _ADJUSTMENTS["balanced"])
    # Pondérer par la confiance : si confiance faible, ajustements atténués
    scale = confidence

    return TacticalProfile(
        style=style,
        confidence=round(confidence, 3),
        btts_adj=round(adj["btts_adj"] * scale, 4),
        over25_adj=round(adj["over25_adj"] * scale, 4),
        xg_adj_self=round(adj["xg_adj_self"] * scale, 4),
        xg_adj_opp=round(adj["xg_adj_opp"] * scale, 4),
        description=_DESCRIPTIONS.get(style, ""),
        signals=signals,
        **{k: v for k, v in kwargs.items() if k in TacticalProfile.__dataclass_fields__},
    )


def apply_tactical_adjustments(
    btts_prob:   float,
    over25_prob: float,
    under25_prob: float,
    home_profile: TacticalProfile | None,
    away_profile: TacticalProfile | None,
) -> dict[str, float]:
    """
    Applique les ajustements tactiques des deux équipes sur les probabilités.

    Retourne un dict avec les probabilités ajustées.
    Les ajustements sont combinés additivement (limités pour rester dans [0,1]).
    """
    btts_delta  = 0.0
    over25_delta = 0.0

    for profile in [home_profile, away_profile]:
        if profile is None:
            continue
        btts_delta   += profile.btts_adj
        over25_delta += profile.over25_adj

    # Atténuation : pas plus de ±15 pp d'ajustement total
    btts_delta   = max(-0.15, min(0.15, btts_delta))
    over25_delta = max(-0.15, min(0.15, over25_delta))

    new_btts    = max(0.02, min(0.98, btts_prob   + btts_delta))
    new_over25  = max(0.02, min(0.98, over25_prob  + over25_delta))
    new_under25 = max(0.02, min(0.98, 1.0 - new_over25))

    return {
        "btts_prob":    round(new_btts,    4),
        "over25_prob":  round(new_over25,  4),
        "under25_prob": round(new_under25, 4),
        "btts_delta":   round(btts_delta,  4),
        "over25_delta": round(over25_delta, 4),
    }


def format_tactical_analysis(
    home_name: str,
    away_name: str,
    home_profile: TacticalProfile | None,
    away_profile: TacticalProfile | None,
) -> str:
    """Formatte l'analyse tactique pour l'affichage Telegram."""
    _STYLE_EMOJI: dict[str, str] = {
        "offensive":      "⚔️",
        "possession":     "🔄",
        "pressing":       "⚡",
        "counter_attack": "↩️",
        "low_block":      "🏰",
        "low_intensity":  "😴",
        "balanced":       "⚖️",
    }

    lines = ["🧠 <b>Analyse tactique V16</b>"]

    for name, profile in [(home_name, home_profile), (away_name, away_profile)]:
        if profile is None:
            lines.append(f"\n  <b>{name}</b> : données insuffisantes")
            continue
        emoji = _STYLE_EMOJI.get(profile.style, "⚖️")
        conf_str = f"({int(profile.confidence * 100)}% confiance)"
        lines.append(f"\n  <b>{name}</b> {emoji} {profile.description} {conf_str}")
        for sig in profile.signals[:3]:   # max 3 signaux pour ne pas surcharger
            lines.append(f"    • {sig}")
        if profile.btts_adj != 0.0:
            sign = "+" if profile.btts_adj >= 0 else ""
            lines.append(f"    → BTTS {sign}{profile.btts_adj * 100:.0f} pp")
        if profile.over25_adj != 0.0:
            sign = "+" if profile.over25_adj >= 0 else ""
            lines.append(f"    → O/U2.5 {sign}{profile.over25_adj * 100:.0f} pp")

    return "\n".join(lines)
