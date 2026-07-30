"""engine/h2h_audit.py — Audit du canal H2H (V20.8).

Ce qui a été trouvé en auditant le H2H
----------------------------------------
`engine.h2h.compute_h2h_weight()` calcule `home_factor`/`away_factor` —
des multiplicateurs de lambda pondérés par récence et par ratio de buts,
bornés à [0.88, 1.12], que son propre docstring décrit explicitement
comme destinés à ajuster le xG ("produces multiplicative λ adjustment
factors for the prediction engine"). Ces facteurs n'étaient utilisés
NULLE PART ailleurs dans le pipeline — vérifié par recherche exhaustive
dans tout le code (hors h2h.py et ses tests). Le seul canal H2H
réellement actif jusqu'ici était `predictor._h2h_components()`, un
mélange simple 70% moyenne ligue / 30% moyenne de buts H2H brute (sans
pondération par victoire/défaite ni par récence), qui n'alimente que la
composante "h2h" (10 pts) de l'indice de force.

En corrigeant le branchement (voir predictor.predict(h2h_mode=...)), un
deuxième bug a été trouvé : le point d'insertion initial appliquait le
multiplicateur sur un lambda calculé par `_index_to_xg()` — du code mort
depuis la V16 (voir xg_v16.py : "Remplace le calcul _index_to_xg() de V13
par un modèle multi-facteurs"), écrasé 90 lignes plus loin par le vrai
lambda (issu de xg_v16 + xg_global_multiplier) qui alimente réellement la
simulation. Corrigé pour appliquer la pondération au bon endroit.

Ce module compare, sur l'historique réglé, trois modes :
- `None`  (comportement actuel, inchangé) : mélange 70/30 sur la
  composante "h2h" de l'indice de force uniquement.
- `"off"` : H2H complètement neutralisé (aucune influence).
- `"weighted"` : EN PLUS du mélange 70/30, applique home_factor/
  away_factor (le signal calculé mais jamais utilisé jusqu'ici) comme
  multiplicateur direct sur le lambda final.

Même architecture que engine.xg_backtest et engine.strength_ablation :
snapshots retrouvés via snapshot_json (repli cache), split chronologique
calibration/holdout, rien n'est jamais appliqué automatiquement — outil
de mesure, pas de calibrage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from engine import calibration, predictor, scanner, xg_backtest

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 20
_CALIB_FRACTION = 0.70
_MODES: list[str | None] = [None, "off", "weighted"]
_MODE_LABELS = {
    None: "Actuel (mélange 70/30, sans home_factor/away_factor)",
    "off": "H2H désactivé",
    "weighted": "Mélange + home_factor/away_factor (signal jusqu'ici inutilisé)",
}


@dataclass
class H2HMetrics:
    mode:             str | None = "?"
    n:                int   = 0
    n_with_h2h_data:  int   = 0  # sous-ensemble où h2h_weight.has_data est vrai
    accuracy_1x2:     float = 0.0
    accuracy_btts:    float = 0.0
    accuracy_over25:  float = 0.0

    @property
    def combined_accuracy(self) -> float:
        return round((self.accuracy_1x2 + self.accuracy_btts + self.accuracy_over25) / 3, 4)


def _evaluate_mode(rows: list[dict], mode: str | None, btts_th: float, ou25_th: float) -> H2HMetrics:
    n = len(rows)
    if n == 0:
        return H2HMetrics(mode=mode)

    correct_1x2 = correct_btts = correct_over25 = n_with_data = 0
    for row in rows:
        _fixture, _info, kwargs = scanner.build_prediction_inputs(row["snapshot"])
        h2h_w = kwargs.get("h2h_weight")
        if h2h_w is not None and h2h_w.has_data:
            n_with_data += 1
        pred = predictor.predict(h2h_mode=mode, **kwargs)
        rh, ra = row["result_home"], row["result_away"]

        actual_1x2 = "home" if rh > ra else ("away" if ra > rh else "draw")
        correct_1x2 += int((pred.predicted_outcome or "") == actual_1x2)
        actual_btts = rh >= 1 and ra >= 1
        correct_btts += int((pred.btts_prob >= btts_th) == actual_btts)
        actual_over25 = (rh + ra) > 2
        correct_over25 += int((pred.over25_prob >= ou25_th) == actual_over25)

    return H2HMetrics(
        mode=mode, n=n, n_with_h2h_data=n_with_data,
        accuracy_1x2=round(correct_1x2 / n, 4),
        accuracy_btts=round(correct_btts / n, 4),
        accuracy_over25=round(correct_over25 / n, 4),
    )


@dataclass
class H2HAuditReport:
    attempted:               bool = False
    reason:                  str  = ""
    n_total:                 int  = 0
    n_calibration:            int  = 0
    n_holdout:                int  = 0
    per_mode_calibration:      dict = field(default_factory=dict)
    per_mode_holdout:          dict = field(default_factory=dict)


def run_h2h_audit(modes: list[str | None] | None = None) -> H2HAuditReport:
    report = H2HAuditReport()
    rows = xg_backtest._load_settled_with_snapshots()
    report.n_total = len(rows)
    if len(rows) < _MIN_SAMPLES:
        report.reason = (
            f"Pas assez de prédictions réglées avec un snapshot retrouvé "
            f"({len(rows)}, {_MIN_SAMPLES} minimum) pour un audit H2H fiable."
        )
        return report

    report.attempted = True
    cut = max(1, int(len(rows) * _CALIB_FRACTION))
    calib_rows, holdout_rows = rows[:cut], rows[cut:]
    report.n_calibration, report.n_holdout = len(calib_rows), len(holdout_rows)

    cfg = calibration.load_calibration()
    btts_th, ou25_th = cfg.btts_threshold, cfg.ou25_threshold

    mode_list = modes if modes is not None else _MODES
    logger.info(
        "[h2h_audit] Audit sur %d modes × (%d calib + %d holdout) — prévoir ~%.0fs.",
        len(mode_list), len(calib_rows), len(holdout_rows),
        len(mode_list) * (len(calib_rows) + len(holdout_rows)) * 0.4,
    )

    for mode in mode_list:
        report.per_mode_calibration[mode] = _evaluate_mode(calib_rows, mode, btts_th, ou25_th)
        report.per_mode_holdout[mode] = _evaluate_mode(holdout_rows, mode, btts_th, ou25_th)

    return report


def format_h2h_audit_report(report: H2HAuditReport) -> str:
    if not report.attempted:
        return f"🔬 <b>AUDIT H2H</b>\n\n{report.reason}"

    n_data = report.per_mode_holdout[None].n_with_h2h_data if None in report.per_mode_holdout else 0
    lines = [
        "🔬  <b>AUDIT DU CANAL H2H</b>",
        "",
        f"  {report.n_calibration} matchs (calibration) · {report.n_holdout} matchs (holdout)",
        f"  Dont {n_data}/{report.n_holdout} avec historique face-à-face exploitable (holdout)",
        "",
        "  Précision combinée (1X2+BTTS+O2.5), holdout jamais vu :",
        "",
    ]
    for mode in _MODES:
        h = report.per_mode_holdout.get(mode)
        c = report.per_mode_calibration.get(mode)
        if h is None:
            continue
        lines.append(f"  {_MODE_LABELS[mode]}")
        lines.append(f"     calibration : {c.combined_accuracy:.1%}   ·   holdout : {h.combined_accuracy:.1%}")
    lines += [
        "",
        "  ⚠️ <i>Mesure indicative — purement diagnostique, ne change rien "
        "automatiquement.</i>",
    ]
    return "\n".join(lines)
