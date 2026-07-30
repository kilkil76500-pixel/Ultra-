"""
engine/learning_v2.py — V16 : Auto-apprentissage avancé.

Extension de learning.py (V15) avec analyse par :
  - Championnat (league)
  - Équipe (home_name / away_name)
  - Mois (YYYY-MM)

Produit un rapport enrichi LearningReportV2 qui inclut les biais
par segment, avec des recommandations actionnables par contexte.

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from engine.learning import LearningReport, analyse_errors
from engine import tracking

logger = logging.getLogger(__name__)

_MIN_SEGMENT_SAMPLES = 5   # minimum de prédictions réglées pour analyser un segment


# ── Structures de données ─────────────────────────────────────────────────────

@dataclass
class SegmentStats:
    """Stats de performance pour un segment (ligue, équipe ou mois)."""
    segment:         str   = ""
    segment_type:    str   = ""   # "league" | "team" | "month"
    n:               int   = 0    # nombre de prédictions réglées
    accuracy_1x2:    float = 0.0  # % correct sur 1X2
    accuracy_btts:   float = 0.0  # % correct sur BTTS
    accuracy_ou25:   float = 0.0  # % correct sur O/U 2.5
    brier_1x2:       float = 0.0  # Brier score (0=parfait, 1=nul)
    bias_home:       float = 0.0  # sur-estimation domicile ? (positif = oui)
    bias_away:       float = 0.0
    bias_draw:       float = 0.0
    avg_confidence:  float = 0.0  # confiance moyenne donnée par le bot
    recommendation:  str   = ""


@dataclass
class LearningReportV2:
    """Rapport d'apprentissage V16 enrichi par segment."""
    base_report:         LearningReport | None = None   # rapport V15 de base
    leagues:             list[SegmentStats]    = field(default_factory=list)
    teams:               list[SegmentStats]    = field(default_factory=list)
    months:              list[SegmentStats]    = field(default_factory=list)
    best_league:         str                   = ""
    worst_league:        str                   = ""
    best_month:          str                   = ""
    worst_month:         str                   = ""
    most_reliable_team:  str                   = ""
    global_summary:      str                   = ""
    n_total_settled:     int                   = 0


# ── Calcul principal ──────────────────────────────────────────────────────────

def analyse_v2() -> LearningReportV2:
    """
    Analyse complète V16 : rapport de base + segmentation par ligue/équipe/mois.
    """
    report = LearningReportV2()

    # Rapport de base V15
    try:
        report.base_report = analyse_errors()
    except Exception as exc:
        logger.warning("[learning_v2] Base report failed: %s", exc)

    # Charger les prédictions réglées depuis la base SQLite
    rows = _load_settled_predictions()
    report.n_total_settled = len(rows)

    if len(rows) < _MIN_SEGMENT_SAMPLES:
        report.global_summary = (
            f"Seulement {len(rows)} prédiction(s) réglée(s). "
            f"Entre au moins {_MIN_SEGMENT_SAMPLES} résultats pour une analyse segmentée."
        )
        return report

    # ── Segmentation par ligue ────────────────────────────────────────────────
    leagues_data: dict[str, list[dict]] = defaultdict(list)
    teams_data:   dict[str, list[dict]] = defaultdict(list)
    months_data:  dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        league = row.get("league") or "Inconnue"
        leagues_data[league].append(row)

        for key in ("home_name", "away_name"):
            team = row.get(key)
            if team:
                teams_data[team].append(row)

        # Mois (YYYY-MM depuis la colonne kickoff ou created_at)
        month = _extract_month(row)
        if month:
            months_data[month].append(row)

    report.leagues = _compute_segment_stats(leagues_data, "league")
    report.teams   = _compute_segment_stats(teams_data, "team", min_n=8)
    report.months  = _compute_segment_stats(months_data, "month")

    # ── Best / Worst ──────────────────────────────────────────────────────────
    if report.leagues:
        best_l = max(report.leagues, key=lambda s: s.accuracy_1x2, default=None)
        worst_l = min(report.leagues, key=lambda s: s.accuracy_1x2, default=None)
        if best_l and best_l.n >= _MIN_SEGMENT_SAMPLES:
            report.best_league  = best_l.segment
        if worst_l and worst_l.n >= _MIN_SEGMENT_SAMPLES:
            report.worst_league = worst_l.segment

    if report.months:
        best_m  = max(report.months, key=lambda s: s.accuracy_1x2, default=None)
        worst_m = min(report.months, key=lambda s: s.accuracy_1x2, default=None)
        if best_m:
            report.best_month  = best_m.segment
        if worst_m:
            report.worst_month = worst_m.segment

    if report.teams:
        best_t = max(report.teams, key=lambda s: s.accuracy_1x2, default=None)
        if best_t:
            report.most_reliable_team = best_t.segment

    report.global_summary = _build_global_summary(report)
    return report


# ── Chargement des données ────────────────────────────────────────────────────

def _load_settled_predictions() -> list[dict]:
    """Charge toutes les prédictions réglées depuis la base de suivi."""
    db_path = tracking.DB_PATH
    if not db_path or not __import__("os").path.exists(db_path):
        return []
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.execute(
            """
            SELECT p.*, s.actual_home, s.actual_away, s.correct_1x2,
                   s.correct_btts, s.correct_ou25, s.settled_at
            FROM predictions p
            JOIN settlements s ON s.prediction_id = p.id
            ORDER BY p.created_at
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return rows
    except Exception as exc:
        logger.error("[learning_v2] DB load failed: %s", exc)
        return []


def _extract_month(row: dict) -> str:
    """Extrait YYYY-MM depuis kickoff ou created_at."""
    for key in ("kickoff", "created_at", "settled_at"):
        val = row.get(key)
        if val and isinstance(val, str) and len(val) >= 7:
            return val[:7]
    return ""


# ── Calcul des stats par segment ──────────────────────────────────────────────

def _compute_segment_stats(
    data:    dict[str, list[dict]],
    seg_type: str,
    min_n:   int = _MIN_SEGMENT_SAMPLES,
) -> list[SegmentStats]:
    """Calcule les SegmentStats pour chaque clé dans data."""
    result: list[SegmentStats] = []

    for segment, rows in data.items():
        if len(rows) < min_n:
            continue

        n = len(rows)
        correct_1x2 = sum(1 for r in rows if r.get("correct_1x2"))
        correct_btts = sum(1 for r in rows if r.get("correct_btts"))
        correct_ou25 = sum(1 for r in rows if r.get("correct_ou25"))

        acc_1x2 = correct_1x2 / n
        acc_btts = correct_btts / n if any(r.get("correct_btts") is not None for r in rows) else -1
        acc_ou25 = correct_ou25 / n if any(r.get("correct_ou25") is not None for r in rows) else -1

        # Biais 1X2 : moyenne de (prob_home - indicatrice_home_win)
        bias_h = bias_a = bias_d = 0.0
        brier  = 0.0
        brier_n = 0
        conf_total = 0.0

        for r in rows:
            ph = r.get("home_win_prob") or 0.0
            pd = r.get("draw_prob") or 0.0
            pa = r.get("away_win_prob") or 0.0
            ah = r.get("actual_home", 0) or 0
            aa = r.get("actual_away", 0) or 0

            ih = 1.0 if ah > aa else 0.0
            id_ = 1.0 if ah == aa else 0.0
            ia = 1.0 if aa > ah else 0.0

            bias_h += ph - ih
            bias_a += pa - ia
            bias_d += pd - id_

            brier  += (ph - ih) ** 2 + (pd - id_) ** 2 + (pa - ia) ** 2
            brier_n += 1

            conf_total += r.get("confidence_pct") or 50.0

        bias_h /= n
        bias_a /= n
        bias_d /= n
        brier   = brier / brier_n if brier_n else 0.0
        avg_conf = conf_total / n

        recommendation = _segment_recommendation(acc_1x2, bias_h, bias_a, n)

        result.append(SegmentStats(
            segment=segment,
            segment_type=seg_type,
            n=n,
            accuracy_1x2=round(acc_1x2, 3),
            accuracy_btts=round(acc_btts, 3) if acc_btts >= 0 else -1,
            accuracy_ou25=round(acc_ou25, 3) if acc_ou25 >= 0 else -1,
            brier_1x2=round(brier / 3, 4) if brier_n else 0.0,
            bias_home=round(bias_h, 3),
            bias_away=round(bias_a, 3),
            bias_draw=round(bias_d, 3),
            avg_confidence=round(avg_conf, 1),
            recommendation=recommendation,
        ))

    return sorted(result, key=lambda s: s.n, reverse=True)


def _segment_recommendation(acc: float, bias_h: float, bias_a: float, n: int) -> str:
    """Génère une recommandation courte."""
    parts: list[str] = []
    if acc >= 0.55:
        parts.append(f"bon taux 1X2 ({acc:.0%})")
    elif acc < 0.40:
        parts.append(f"⚠️ taux 1X2 faible ({acc:.0%})")
    if abs(bias_h) > 0.06:
        direction = "surestime domicile" if bias_h > 0 else "sous-estime domicile"
        parts.append(direction)
    if abs(bias_a) > 0.06:
        direction = "surestime extérieur" if bias_a > 0 else "sous-estime extérieur"
        parts.append(direction)
    if not parts:
        return "profil stable"
    return ", ".join(parts)


def _build_global_summary(report: LearningReportV2) -> str:
    """Résumé global du rapport V2."""
    n = report.n_total_settled
    lines: list[str] = [f"Analyse de {n} prédictions réglées."]

    if report.best_league and report.worst_league:
        lines.append(
            f"Meilleure ligue : {report.best_league}. "
            f"Ligue difficile : {report.worst_league}."
        )
    if report.most_reliable_team:
        lines.append(f"Équipe la mieux prédite : {report.most_reliable_team}.")
    if report.best_month and report.worst_month:
        lines.append(
            f"Meilleur mois : {report.best_month}. "
            f"Mois difficile : {report.worst_month}."
        )

    return " ".join(lines)


# ── Formatage Telegram ────────────────────────────────────────────────────────

def format_learning_report_v2(report: LearningReportV2) -> str:
    """Formatte le rapport V2 pour l'affichage Telegram (multi-messages si nécessaire)."""
    lines: list[str] = []

    lines.append("🧠 <b>Auto-apprentissage V2</b>")
    lines.append(f"  {report.global_summary}")

    # ── Par ligue ─────────────────────────────────────────────────────────────
    if report.leagues:
        lines.append("\n📋 <b>Par championnat</b>")
        for s in report.leagues[:8]:
            acc_str = f"{s.accuracy_1x2:.0%}" if s.accuracy_1x2 >= 0 else "?"
            lines.append(
                f"  • <b>{s.segment}</b> ({s.n} matchs) — 1X2 {acc_str}"
                + (f" | {s.recommendation}" if s.recommendation else "")
            )
    else:
        lines.append("\n  Pas assez de données par championnat.")

    # ── Par équipe ────────────────────────────────────────────────────────────
    if report.teams:
        lines.append("\n👥 <b>Par équipe (top 6)</b>")
        for s in report.teams[:6]:
            acc_str = f"{s.accuracy_1x2:.0%}" if s.accuracy_1x2 >= 0 else "?"
            lines.append(
                f"  • <b>{s.segment}</b> ({s.n} matchs) — 1X2 {acc_str}"
            )
    else:
        lines.append("\n  Pas assez de données par équipe (min. 8 matchs).")

    # ── Par mois ──────────────────────────────────────────────────────────────
    if report.months:
        lines.append("\n📅 <b>Par mois</b>")
        for s in report.months[:6]:
            acc_str = f"{s.accuracy_1x2:.0%}" if s.accuracy_1x2 >= 0 else "?"
            lines.append(
                f"  • {s.segment} ({s.n} matchs) — 1X2 {acc_str}"
            )

    return "\n".join(lines)
