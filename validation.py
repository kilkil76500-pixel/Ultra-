"""
engine/validation.py — V15 : Validation sur jeu holdout temporel.

Principe : on utilise les prédictions réglées les plus récentes (30 % du total,
au minimum 10) comme jeu de validation — jamais utilisées pour la calibration.
Le reste (70 % les plus anciennes) sert à la calibration.

Cette séparation temporelle est la seule vraie garantie que les chiffres de
validation mesurent la performance sur des données "inconnues" et non la
simple adaptation aux données passées.

Retourne un ValidationReport qui compare la performance modèle (accuracy,
Brier Score) sur le jeu de validation vs. le jeu de calibration, ainsi
qu'un signal "go / no-go" pour l'application d'une nouvelle calibration.

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import sqlite3
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)

_DB_FILENAME     = "predictions.db"
_HOLDOUT_RATIO   = 0.30   # fraction des prédictions les plus récentes = holdout
_MIN_HOLDOUT     = 10     # minimum absolu pour valider
_MIN_CALIB_ROWS  = 10     # minimum absolu pour la partie calibration


def _db_path() -> str:
    return os.path.join(config.WEB_CACHE_DIR, _DB_FILENAME)


@contextlib.contextmanager
def _connect():
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        yield conn
    finally:
        conn.close()


# ── Structures ────────────────────────────────────────────────────────────────

@dataclass
class SplitStats:
    """Métriques sur un sous-ensemble de prédictions."""
    label:       str
    n:           int   = 0
    accuracy_1x2: float = 0.0
    brier_1x2:    float = 0.0
    accuracy_btts: float = 0.0
    accuracy_ou25: float = 0.0


@dataclass
class ValidationReport:
    """Résultat de la validation holdout."""
    n_total:         int        = 0
    n_calibration:   int        = 0
    n_holdout:       int        = 0
    calibration_set: SplitStats = field(default_factory=lambda: SplitStats("calibration"))
    holdout_set:     SplitStats = field(default_factory=lambda: SplitStats("holdout"))
    is_valid:        bool       = False   # True si le holdout ≥ seuil minimum
    recommendation:  str        = ""

    @property
    def regression_detected(self) -> bool:
        """True si la précision sur le holdout est nettement inférieure à la calibration."""
        if not self.is_valid:
            return False
        return self.holdout_set.accuracy_1x2 < self.calibration_set.accuracy_1x2 - 0.08

    @property
    def improvement_detected(self) -> bool:
        if not self.is_valid:
            return False
        return self.holdout_set.accuracy_1x2 > self.calibration_set.accuracy_1x2 + 0.03


# ── Helpers de métriques ──────────────────────────────────────────────────────

def _brier_1x2(p_home: float, p_draw: float, p_away: float, actual: str) -> float:
    targets = {"home": (1, 0, 0), "draw": (0, 1, 0), "away": (0, 0, 1)}
    t_h, t_d, t_a = targets.get(actual, (0, 0, 0))
    return (p_home - t_h)**2 + (p_draw - t_d)**2 + (p_away - t_a)**2


def _compute_stats(rows: list, label: str) -> SplitStats:
    if not rows:
        return SplitStats(label)
    n = len(rows)
    correct_1x2 = correct_btts = correct_ou25 = 0
    brier_total = 0.0
    for p_home, p_draw, p_away, p_btts, p_ou25, rh, ra in rows:
        actual = "home" if rh > ra else ("away" if ra > rh else "draw")
        pred   = max(("home", p_home), ("draw", p_draw), ("away", p_away),
                     key=lambda pair: pair[1])[0]
        correct_1x2  += int(pred == actual)
        brier_total  += _brier_1x2(p_home, p_draw, p_away, actual)
        correct_btts += int((p_btts >= 0.5) == (rh >= 1 and ra >= 1))
        correct_ou25 += int((p_ou25 >= 0.5) == ((rh + ra) > 2))
    return SplitStats(
        label         = label,
        n             = n,
        accuracy_1x2  = round(correct_1x2 / n, 4),
        brier_1x2     = round(brier_total / n, 4),
        accuracy_btts = round(correct_btts / n, 4),
        accuracy_ou25 = round(correct_ou25 / n, 4),
    )


# ── Point d'entrée public ─────────────────────────────────────────────────────

def run_validation() -> ValidationReport:
    """
    Effectue la validation holdout temporelle sur les prédictions réglées.
    Retourne un ValidationReport complet.
    """
    if not os.path.exists(_db_path()):
        report = ValidationReport()
        report.recommendation = "Aucune base de données trouvée. Lance /scan puis /resultat."
        return report

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT home_win_prob, draw_prob, away_win_prob,
                   btts_prob, over25_prob, result_home, result_away
            FROM predictions
            WHERE settled = 1
            ORDER BY settled_at ASC
            """
        ).fetchall()

    n_total = len(rows)
    report  = ValidationReport(n_total=n_total)

    if n_total < (_MIN_HOLDOUT + _MIN_CALIB_ROWS):
        report.recommendation = (
            f"Pas assez de prédictions réglées ({n_total}) pour une validation "
            f"fiable (besoin de {_MIN_HOLDOUT + _MIN_CALIB_ROWS}). "
            "Continue à saisir des résultats via /resultat."
        )
        return report

    split      = max(_MIN_CALIB_ROWS, int(n_total * (1.0 - _HOLDOUT_RATIO)))
    calib_rows = rows[:split]
    hold_rows  = rows[split:]

    if len(hold_rows) < _MIN_HOLDOUT:
        report.recommendation = (
            f"Le jeu holdout est trop petit ({len(hold_rows)} prédictions). "
            f"Il faut au minimum {_MIN_HOLDOUT} pour valider. Continue à saisir des résultats."
        )
        return report

    report.n_calibration   = len(calib_rows)
    report.n_holdout       = len(hold_rows)
    report.calibration_set = _compute_stats(calib_rows, "calibration")
    report.holdout_set     = _compute_stats(hold_rows,  "holdout")
    report.is_valid        = True

    # ── Recommandation ────────────────────────────────────────────────────────
    if report.regression_detected:
        report.recommendation = (
            f"⚠️ Régression détectée : précision holdout {report.holdout_set.accuracy_1x2:.0%} "
            f"vs calibration {report.calibration_set.accuracy_1x2:.0%}. "
            "Envisage un retour à la version précédente via /versions."
        )
    elif report.improvement_detected:
        report.recommendation = (
            f"✅ Amélioration confirmée : précision holdout {report.holdout_set.accuracy_1x2:.0%} "
            f"vs calibration {report.calibration_set.accuracy_1x2:.0%}. "
            "La calibration actuelle est meilleure."
        )
    else:
        report.recommendation = (
            f"✅ Pas de régression notable : holdout {report.holdout_set.accuracy_1x2:.0%} "
            f"vs calibration {report.calibration_set.accuracy_1x2:.0%}. "
            "Le modèle est stable."
        )

    return report
