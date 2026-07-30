"""engine/xg_backtest.py — Backtest de xg_global_multiplier par
ré-simulation complète (V19.16).

Pourquoi ce module est nécessaire
----------------------------------
Contrairement à draw_detection_factor et aux seuils BTTS/O2.5/confiance
(voir engine/auto_learning.py), xg_global_multiplier agit AVANT la
simulation Monte-Carlo — il multiplie le xG (lambda de Poisson) qui sert
D'ENTRÉE à cette simulation, pas une probabilité déjà calculée. On ne peut
donc pas le rejouer a posteriori en relisant seulement les probabilités
stockées dans predictions.db : il faut littéralement refaire tourner
predictor.predict() en entier avec chaque valeur candidate. C'est
documenté depuis la V19.14 comme "hors de portée" — ce module comble
précisément ce manque.

Comment
-------
1. Chaque prédiction réglée de predictions.db a, depuis V20.4, son propre
   snapshot persisté (colonne snapshot_json) au moment même de sa création —
   avant que le match ne soit joué, donc bien avant que
   engine.cache_store.prune_expired_snapshots() ne purge son snapshot du
   cache local. Pour les prédictions plus anciennes (avant V20.4), on
   retombe sur le mécanisme d'origine : indexer le cache local de snapshots
   par forebet_url pour retrouver l'entrée qui a servi à produire la
   prédiction — mais ce repli reste largement théorique en pratique
   (confirmé sur données réelles de production : 0/88 matchs réglés
   retrouvés par ce seul mécanisme, un match n'étant réglable qu'après son
   coup d'envoi, précisément quand son snapshot vient d'être purgé).
2. Pour chaque valeur candidate, on rejoue engine.scanner.build_prediction_inputs()
   + predictor.predict(xg_multiplier_override=...) — donc le MÊME pipeline
   d'entrée que la production, rien de simplifié.
3. Même philosophie anti-régression que engine.auto_learning : split
   chronologique calibration (70%, plus ancien) / holdout (30%, plus
   récent, jamais utilisé pour choisir le candidat). Un candidat n'est
   proposé comme accepté que s'il ne régresse pas sur le holdout par
   rapport à la valeur actuellement active.
4. N'écrit jamais calibration.json lui-même — retourne un rapport que
   l'appelant (commande Telegram ou script) peut choisir d'appliquer ou
   non, exactement comme pour apply_v18_calibration() (V19.14 : plus
   aucune écriture automatique sans validation humaine ou holdout).

Coût
----
Une prédiction complète (Monte-Carlo par défaut) prend environ 0.4s.
Avec une grille de N candidats sur M matchs réglés, prévoir environ
N × M × 0.4s pour la seule recherche calibration — c'est pourquoi cette
recherche reste une commande séparée de /recalibrer (qui, lui, ne relit
que des probabilités déjà stockées et reste rapide).
"""

from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass, field

import config
from engine import calibration, predictor, scanner

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 20  # en dessous, la recherche est jugée trop bruitée pour être utile
_DEFAULT_GRID = [round(0.82 + 0.03 * i, 2) for i in range(11)]  # 0.82 .. 1.12
_CALIB_FRACTION = 0.70
_REGRESSION_TOLERANCE = 0.01  # 1 point : ne pas rejeter sur du bruit de mesure


def _index_snapshot_cache() -> dict[str, str]:
    """forebet detail_url/source_url -> chemin du fichier snapshot,
    sur l'ensemble du cache local (mêmes fichiers que ceux que
    engine.scanner lit en production)."""
    index: dict[str, str] = {}
    pattern = os.path.join(config.WEB_CACHE_DIR, "*", "football", "*.json")
    for path in glob.glob(pattern):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        forebet = data.get("forebet") or {}
        url = forebet.get("detail_url") or forebet.get("source_url")
        if url:
            index[url] = path
    return index


def _load_settled_with_snapshots() -> list[dict]:
    """Jointure predictions.db (réglées) <-> snapshot du match.

    V20.4 — priorité au snapshot persisté avec la prédiction elle-même
    (colonne snapshot_json, disponible pour toute prédiction créée depuis
    cette version) : c'est la seule source fiable, puisque
    engine.cache_store.prune_expired_snapshots() purge presque toujours le
    snapshot en cache d'un match avant même qu'il ne soit réglé (confirmé sur
    données réelles de production : 0/88 matchs réglés retrouvés par la seule
    jointure forebet_url <-> cache local). Ce mécanisme forebet_url+cache
    reste utilisé en repli pour les lignes plus anciennes, créées avant
    l'ajout de snapshot_json, triée chronologiquement (settled_at croissant)
    pour que le split calibration/holdout reste cohérent avec auto_learning.py.
    """
    from engine import tracking
    tracking.init_db()
    with tracking._connect() as conn:  # noqa: SLF001 — module interne du même paquet
        rows = conn.execute(
            "SELECT id, forebet_url, result_home, result_away, snapshot_json "
            "FROM predictions WHERE settled = 1 ORDER BY settled_at ASC"
        ).fetchall()

    snap_index: dict[str, str] | None = None  # chargé paresseusement, seulement si nécessaire
    matched: list[dict] = []
    skipped_no_snapshot = 0
    for pid, url, rh, ra, snapshot_json in rows:
        snapshot = None
        if snapshot_json:
            try:
                snapshot = json.loads(snapshot_json)
            except (TypeError, ValueError):
                snapshot = None
        if snapshot is None:
            if snap_index is None:
                snap_index = _index_snapshot_cache()
            path = snap_index.get(url) if url else None
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        snapshot = json.load(fh)
                except (OSError, json.JSONDecodeError):
                    snapshot = None
        if snapshot is None:
            skipped_no_snapshot += 1
            continue
        matched.append({"prediction_id": pid, "snapshot": snapshot, "result_home": rh, "result_away": ra})

    if skipped_no_snapshot:
        logger.info(
            "[xg_backtest] %d prédiction(s) réglée(s) sans snapshot retrouvable "
            "(ni snapshot_json, ni cache local — normal si le cache a été purgé "
            "et que la prédiction date d'avant V20.4) — ignorées.",
            skipped_no_snapshot,
        )
    return matched


@dataclass
class XGMetrics:
    multiplier:       float = 1.0
    n:                int   = 0
    xg_mae_home:      float = 0.0
    xg_mae_away:      float = 0.0
    xg_bias_home:     float = 0.0   # positif = surestimation
    xg_bias_away:     float = 0.0
    accuracy_btts:    float = 0.0
    accuracy_over25:  float = 0.0

    @property
    def combined_accuracy(self) -> float:
        return round((self.accuracy_btts + self.accuracy_over25) / 2, 4)


def _evaluate_multiplier(rows: list[dict], multiplier: float | None, btts_th: float, ou25_th: float) -> XGMetrics:
    n = len(rows)
    if n == 0:
        return XGMetrics(multiplier=multiplier if multiplier is not None else 1.0)

    mae_h = mae_a = bias_h = bias_a = 0.0
    correct_btts = correct_over25 = 0

    for row in rows:
        _fixture, _info, kwargs = scanner.build_prediction_inputs(row["snapshot"])
        pred = predictor.predict(xg_multiplier_override=multiplier, **kwargs)
        rh, ra = row["result_home"], row["result_away"]

        mae_h += abs(pred.home_xg - rh)
        mae_a += abs(pred.away_xg - ra)
        bias_h += pred.home_xg - rh
        bias_a += pred.away_xg - ra

        actual_btts = rh >= 1 and ra >= 1
        correct_btts += int((pred.btts_prob >= btts_th) == actual_btts)
        actual_over25 = (rh + ra) > 2
        correct_over25 += int((pred.over25_prob >= ou25_th) == actual_over25)

    return XGMetrics(
        multiplier=multiplier if multiplier is not None else 1.0,
        n=n,
        xg_mae_home=round(mae_h / n, 3), xg_mae_away=round(mae_a / n, 3),
        xg_bias_home=round(bias_h / n, 3), xg_bias_away=round(bias_a / n, 3),
        accuracy_btts=round(correct_btts / n, 4),
        accuracy_over25=round(correct_over25 / n, 4),
    )


@dataclass
class XGBacktestReport:
    attempted:              bool  = False
    accepted:               bool  = False
    reason:                 str   = ""
    n_total:                int   = 0
    n_matched_snapshots:    int   = 0
    n_calibration:          int   = 0
    n_holdout:              int   = 0
    active_multiplier:      float = 1.0
    candidate_multiplier:   float = 1.0
    active_metrics_holdout:    XGMetrics | None = None
    candidate_metrics_holdout: XGMetrics | None = None
    grid_results_calibration:  list[XGMetrics] = field(default_factory=list)


def backtest_xg_multiplier(candidates: list[float] | None = None) -> XGBacktestReport:
    """Recherche par grille de xg_global_multiplier, validée par holdout
    chronologique jamais vu. N'écrit jamais calibration.json — retourne un
    rapport ; l'appelant décide d'appliquer candidate_multiplier ou non.
    """
    report = XGBacktestReport()
    active_cfg = calibration.load_calibration()
    report.active_multiplier = active_cfg.xg_global_multiplier
    report.candidate_multiplier = active_cfg.xg_global_multiplier  # défaut = pas de changement

    rows = _load_settled_with_snapshots()
    report.n_total = report.n_matched_snapshots = len(rows)
    if len(rows) < _MIN_SAMPLES:
        report.reason = (
            f"Pas assez de prédictions réglées avec un snapshot retrouvé dans le "
            f"cache local ({len(rows)}, {_MIN_SAMPLES} minimum) pour backtester "
            f"xg_global_multiplier de façon fiable."
        )
        return report

    report.attempted = True
    cut = max(1, int(len(rows) * _CALIB_FRACTION))
    calib_rows, holdout_rows = rows[:cut], rows[cut:]
    report.n_calibration, report.n_holdout = len(calib_rows), len(holdout_rows)

    if len(holdout_rows) < _MIN_SAMPLES // 2:
        report.reason = (
            f"Lot holdout trop petit ({len(holdout_rows)}) une fois le split "
            f"calibration/holdout appliqué — pas assez pour valider un candidat "
            f"sans risquer de sur-ajuster."
        )
        return report

    btts_th, ou25_th = active_cfg.btts_threshold, active_cfg.ou25_threshold
    grid = candidates if candidates is not None else _DEFAULT_GRID
    logger.info("[xg_backtest] Recherche sur %d candidats × %d matchs (calibration) — "
                "prévoir ~%.0fs.", len(grid), len(calib_rows), len(grid) * len(calib_rows) * 0.4)

    grid_results = [_evaluate_multiplier(calib_rows, m, btts_th, ou25_th) for m in grid]
    report.grid_results_calibration = grid_results
    best = max(grid_results, key=lambda m: m.combined_accuracy)
    report.candidate_multiplier = best.multiplier

    if best.multiplier == active_cfg.xg_global_multiplier:
        report.reason = "La valeur actuelle est déjà celle qui maximise la précision BTTS/O2.5 sur le lot calibration — aucun changement proposé."
        report.accepted = False
        return report

    # ── Validation sur le holdout JAMAIS vu pendant la recherche ───────────
    active_h = _evaluate_multiplier(holdout_rows, active_cfg.xg_global_multiplier, btts_th, ou25_th)
    cand_h = _evaluate_multiplier(holdout_rows, best.multiplier, btts_th, ou25_th)
    report.active_metrics_holdout = active_h
    report.candidate_metrics_holdout = cand_h

    if cand_h.combined_accuracy < active_h.combined_accuracy - _REGRESSION_TOLERANCE:
        report.accepted = False
        report.reason = (
            f"Candidat {best.multiplier} rejeté — régresse sur le holdout jamais vu "
            f"({cand_h.combined_accuracy:.1%} contre {active_h.combined_accuracy:.1%} "
            f"actuellement, sur {len(holdout_rows)} matchs)."
        )
    else:
        report.accepted = True
        report.reason = (
            f"Candidat {best.multiplier} accepté — holdout {cand_h.combined_accuracy:.1%} "
            f"contre {active_h.combined_accuracy:.1%} actuellement (sur {len(holdout_rows)} matchs), "
            f"pas de régression détectée."
        )
    return report


def apply_candidate(report: XGBacktestReport) -> bool:
    """Écrit candidate_multiplier dans calibration.json — UNIQUEMENT si le
    rapport est accepted=True. Retourne False sinon, sans rien écrire."""
    if not report.accepted:
        logger.info("[xg_backtest] apply_candidate() appelé sur un rapport non accepté — aucune écriture.")
        return False
    cfg = calibration.load_calibration()
    cfg.xg_global_multiplier = report.candidate_multiplier
    calibration.save_calibration(cfg)
    logger.info("[xg_backtest] xg_global_multiplier appliqué : %.3f -> %.3f",
                report.active_multiplier, report.candidate_multiplier)
    return True
