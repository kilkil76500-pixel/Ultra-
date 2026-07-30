"""engine/strength_ablation.py — Audit d'ablation des 8 composantes de
l'indice de force (V20.7).

Pourquoi ce module
-------------------
L'indice de force à 8 composantes (attaque, défense adverse, forme, H2H,
terrain, classement, motivation, forme physique — voir
predictor._compute_strength_index) a des poids fixes jamais mesurés
depuis leur mise en place. Avant de retoucher quoi que ce soit dans le
moteur lui-même, ce module répond à une question précise, par composante :
« si je neutralise cette composante et que je rejoue le match, la
précision réelle change-t-elle, et dans quel sens ? »

Comment
-------
Même architecture que engine.xg_backtest : on retrouve le snapshot
d'origine de chaque prédiction réglée (via xg_backtest._load_settled_with_
snapshots, réutilisée telle quelle — priorité à snapshot_json, repli sur
le cache local par forebet_url), on rejoue engine.scanner.
build_prediction_inputs() + predictor.predict(strength_ablation=<nom>)
pour chaque composante, et on compare la précision 1X2/BTTS/O2.5 à un
scénario de référence (aucune ablation). Split chronologique
calibration/holdout, comme pour xg_backtest et auto_learning — mais ce
module ne propose et n'applique RIEN : contrairement à
xg_global_multiplier ou aux seuils de confiance, une composante de
l'indice de force n'est pas un paramètre qu'on retirerait en production —
c'est un outil de diagnostic, pas de calibrage automatique.

Neutralisation
--------------
Chaque composante ablatée est remplacée par la moitié de son poids
maximum (ex. attaque 25 pts → 12.5), pour les deux équipes symétriquement
— elle ne discrimine alors plus rien entre domicile et extérieur, sans
changer les autres composantes ni la somme totale de façon disproportionnée.

Coût
----
9 scénarios (8 composantes + 1 référence) × (n_calibration + n_holdout)
matchs, à ~0.4s/prédiction — prévoir plusieurs minutes sur un historique
de taille réaliste. Commande séparée, à lancer occasionnellement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from engine import calibration, predictor, scanner, xg_backtest
from engine.predictor import COMPONENT_MAX_POINTS

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 20
_CALIB_FRACTION = 0.70
_COMPONENTS: list[str] = list(COMPONENT_MAX_POINTS.keys())

_COMPONENT_LABELS = {
    "attaque": "Attaque",
    "défense_adverse": "Défense adverse",
    "forme": "Forme récente",
    "h2h": "Historique face-à-face (H2H)",
    "terrain": "Terrain (domicile/extérieur)",
    "classement": "Classement",
    "motivation": "Motivation / mental",
    "forme_physique": "Forme physique / absences",
}


@dataclass
class AblationMetrics:
    component:        str | None = None  # None = référence (aucune ablation)
    n:                int   = 0
    accuracy_1x2:     float = 0.0
    accuracy_btts:    float = 0.0
    accuracy_over25:  float = 0.0

    @property
    def combined_accuracy(self) -> float:
        return round((self.accuracy_1x2 + self.accuracy_btts + self.accuracy_over25) / 3, 4)


def _evaluate_ablation(
    rows: list[dict], component: str | None, btts_th: float, ou25_th: float,
) -> AblationMetrics:
    n = len(rows)
    if n == 0:
        return AblationMetrics(component=component)

    correct_1x2 = correct_btts = correct_over25 = 0
    for row in rows:
        _fixture, _info, kwargs = scanner.build_prediction_inputs(row["snapshot"])
        pred = predictor.predict(strength_ablation=component, **kwargs)
        rh, ra = row["result_home"], row["result_away"]

        actual_1x2 = "home" if rh > ra else ("away" if ra > rh else "draw")
        predicted_1x2 = pred.predicted_outcome or ""
        correct_1x2 += int(predicted_1x2 == actual_1x2)

        actual_btts = rh >= 1 and ra >= 1
        correct_btts += int((pred.btts_prob >= btts_th) == actual_btts)
        actual_over25 = (rh + ra) > 2
        correct_over25 += int((pred.over25_prob >= ou25_th) == actual_over25)

    return AblationMetrics(
        component=component, n=n,
        accuracy_1x2=round(correct_1x2 / n, 4),
        accuracy_btts=round(correct_btts / n, 4),
        accuracy_over25=round(correct_over25 / n, 4),
    )


@dataclass
class AblationReport:
    attempted:                  bool = False
    reason:                     str  = ""
    n_total:                    int  = 0
    n_calibration:               int  = 0
    n_holdout:                   int  = 0
    baseline_calibration:        AblationMetrics | None = None
    baseline_holdout:            AblationMetrics | None = None
    per_component_calibration:   dict[str, AblationMetrics] = field(default_factory=dict)
    per_component_holdout:       dict[str, AblationMetrics] = field(default_factory=dict)

    def holdout_impact(self, component: str) -> float:
        """Perte (positive) ou gain (négatif) de précision combinée sur le
        holdout quand `component` est neutralisée, vs la référence. Une
        valeur POSITIVE veut dire que la composante AIDE (l'ablation fait
        baisser la précision) ; une valeur NÉGATIVE ou proche de 0 veut
        dire qu'elle n'apporte rien, voire dilue le signal des autres."""
        if self.baseline_holdout is None or component not in self.per_component_holdout:
            return 0.0
        return round(self.baseline_holdout.combined_accuracy
                      - self.per_component_holdout[component].combined_accuracy, 4)


def run_ablation_audit(components: list[str] | None = None) -> AblationReport:
    """Audit complet : référence + une ablation par composante, sur le même
    split calibration/holdout que le reste du projet. Ne modifie jamais
    calibration.json ni aucun fichier — purement diagnostique."""
    report = AblationReport()
    rows = xg_backtest._load_settled_with_snapshots()
    report.n_total = len(rows)
    if len(rows) < _MIN_SAMPLES:
        report.reason = (
            f"Pas assez de prédictions réglées avec un snapshot retrouvé "
            f"({len(rows)}, {_MIN_SAMPLES} minimum) pour un audit d'ablation fiable."
        )
        return report

    report.attempted = True
    cut = max(1, int(len(rows) * _CALIB_FRACTION))
    calib_rows, holdout_rows = rows[:cut], rows[cut:]
    report.n_calibration, report.n_holdout = len(calib_rows), len(holdout_rows)

    cfg = calibration.load_calibration()
    btts_th, ou25_th = cfg.btts_threshold, cfg.ou25_threshold

    comp_list = components if components is not None else _COMPONENTS
    logger.info(
        "[strength_ablation] Audit sur %d composantes + référence × (%d calib + %d holdout) — "
        "prévoir ~%.0fs.",
        len(comp_list), len(calib_rows), len(holdout_rows),
        (len(comp_list) + 1) * (len(calib_rows) + len(holdout_rows)) * 0.4,
    )

    report.baseline_calibration = _evaluate_ablation(calib_rows, None, btts_th, ou25_th)
    report.baseline_holdout = _evaluate_ablation(holdout_rows, None, btts_th, ou25_th)

    for comp in comp_list:
        report.per_component_calibration[comp] = _evaluate_ablation(calib_rows, comp, btts_th, ou25_th)
        report.per_component_holdout[comp] = _evaluate_ablation(holdout_rows, comp, btts_th, ou25_th)

    return report


def format_ablation_report(report: AblationReport) -> str:
    """Rendu texte (Telegram-friendly), composantes triées par impact
    holdout décroissant — celle qui aide le plus en premier."""
    if not report.attempted:
        return f"🔬 <b>AUDIT D'ABLATION</b>\n\n{report.reason}"

    lines = [
        "🔬  <b>AUDIT D'ABLATION — INDICE DE FORCE</b>",
        "",
        f"  {report.n_calibration} matchs (calibration) · {report.n_holdout} matchs (holdout)",
        f"  Référence (aucune ablation) — holdout : {report.baseline_holdout.combined_accuracy:.1%}",
        "",
        "  Impact mesuré par composante (holdout, jamais vu pendant le calcul) :",
        "  <i>Positif = la composante aide réellement · proche de 0 ou négatif = bruit</i>",
        "",
    ]
    ordered = sorted(_COMPONENTS, key=lambda c: report.holdout_impact(c), reverse=True)
    for comp in ordered:
        impact = report.holdout_impact(comp)
        label = _COMPONENT_LABELS.get(comp, comp)
        sign = "+" if impact >= 0 else ""
        lines.append(f"  {label:<30s} {sign}{impact:.1%}")
    lines += [
        "",
        "  ⚠️ <i>Mesure indicative — se renforce avec le volume de /resultat "
        "accumulé. Ne change rien automatiquement : purement diagnostique.</i>",
    ]
    return "\n".join(lines)
