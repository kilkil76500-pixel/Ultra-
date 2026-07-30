"""
engine/history_query.py — V20 : Requêtes libres sur l'historique réglé.

Objectif
--------
Répondre à des questions du type « sur les matchs passés qui ressemblaient
à CELUI-CI sur tel ou tel critère, quel a été le taux de réussite réel ? »
en interrogeant directement `predictions.db` (déjà alimenté par
`engine.tracking`), sans dupliquer de données.

Limite assumée et documentée
-----------------------------
`predictions.db` ne stocke PAS de statistiques brutes de match (possession,
tirs cadrés, corners...) : seulement ce que le moteur a lui-même calculé
(probabilités, xG modèle, confiance, ligue, marché, résultat réel). Une
requête du type « 62 % de possession » n'est donc pas possible sur ces
données — ce module ne le prétend pas. Ce qui EST possible et fiable :
filtrer par ligue, par écart de probabilité favori/outsider, par xG modèle,
par niveau de confiance, par marché, et croiser avec le résultat réel. C'est
la mémoire statistique qu'on peut honnêtement construire avec les données
réellement collectées par ce bot.

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from engine import calibration

from engine import tracking

logger = logging.getLogger(__name__)

_MIN_SAMPLES_FOR_STAT = 8  # en dessous, on affiche l'échantillon mais on
                           # avertit que le pourcentage n'est pas fiable


@dataclass
class HistoryFilter:
    """Critères optionnels de filtrage. `None` = pas de filtre sur ce champ."""
    league_contains:      str | None = None   # sous-chaîne insensible à la casse
    min_confidence:       float | None = None
    max_confidence:       float | None = None
    predicted_outcome:    str | None = None   # "home" | "draw" | "away"
    min_prob_gap:         float | None = None  # |favori − 1/3| minimal (dominance du favori)
    max_prob_gap:         float | None = None
    min_xg_total:         float | None = None  # home_xg + away_xg (modèle)
    max_xg_total:         float | None = None
    min_btts_prob:        float | None = None
    min_over25_prob:      float | None = None


@dataclass
class HistoryQueryResult:
    filter_used:        HistoryFilter
    n_matches:           int   = 0
    n_correct_1x2:        int   = 0
    n_correct_btts:       int   = 0
    n_correct_over25:     int   = 0
    accuracy_1x2:         float = 0.0
    accuracy_btts:        float = 0.0
    accuracy_over25:      float = 0.0
    data_sufficient:      bool  = False
    sample_rows:          list[dict[str, Any]] = field(default_factory=list)


def _row_matches(row: dict[str, Any], f: HistoryFilter) -> bool:
    if f.league_contains:
        if f.league_contains.lower() not in (row.get("league") or "").lower():
            return False

    conf = row.get("confidence_pct") or 0.0
    if f.min_confidence is not None and conf < f.min_confidence:
        return False
    if f.max_confidence is not None and conf > f.max_confidence:
        return False

    if f.predicted_outcome is not None:
        predicted = row.get("predicted_outcome") or _naive_outcome(row)
        if predicted != f.predicted_outcome:
            return False

    probs = (row.get("home_win_prob") or 0.0, row.get("draw_prob") or 0.0, row.get("away_win_prob") or 0.0)
    fav_prob = max(probs)
    prob_gap = fav_prob - (1.0 / 3.0)
    if f.min_prob_gap is not None and prob_gap < f.min_prob_gap:
        return False
    if f.max_prob_gap is not None and prob_gap > f.max_prob_gap:
        return False

    xg_total = (row.get("home_xg") or 0.0) + (row.get("away_xg") or 0.0)
    if f.min_xg_total is not None and xg_total < f.min_xg_total:
        return False
    if f.max_xg_total is not None and xg_total > f.max_xg_total:
        return False

    if f.min_btts_prob is not None and (row.get("btts_prob") or 0.0) < f.min_btts_prob:
        return False
    if f.min_over25_prob is not None and (row.get("over25_prob") or 0.0) < f.min_over25_prob:
        return False

    return True


def _naive_outcome(row: dict[str, Any]) -> str:
    probs_map = {
        "home": row.get("home_win_prob") or 0.0,
        "draw": row.get("draw_prob") or 0.0,
        "away": row.get("away_win_prob") or 0.0,
    }
    return max(probs_map, key=probs_map.get)


def _fetch_settled_rows() -> list[dict[str, Any]]:
    """Toutes les prédictions réglées, sous forme de dicts."""
    tracking.init_db()
    with tracking._connect() as conn:  # noqa: SLF001 — module interne, même paquet
        conn.row_factory = None
        cur = conn.execute(
            "SELECT id, league, confidence_pct, predicted_outcome, "
            "home_win_prob, draw_prob, away_win_prob, btts_prob, over25_prob, "
            "home_xg, away_xg, result_home, result_away "
            "FROM predictions WHERE settled = 1"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def run_query(f: HistoryFilter) -> HistoryQueryResult:
    """Applique le filtre aux prédictions réglées et calcule les taux réels."""
    rows = [r for r in _fetch_settled_rows() if _row_matches(r, f)]

    # V20.3 — utilise les seuils BTTS/O2.5 réellement actifs (calibration.json)
    # au lieu de 0.5 en dur, pour que ces taux restent cohérents avec ceux que
    # /fiabilite calcule et avec la décision Oui/Non réellement affichée à
    # l'utilisateur au moment du pronostic.
    _calib = calibration.load_calibration()
    btts_th, ou25_th = _calib.btts_threshold, _calib.ou25_threshold

    n_correct_1x2 = n_correct_btts = n_correct_over25 = 0
    for row in rows:
        rh, ra = row.get("result_home"), row.get("result_away")
        if rh is None or ra is None:
            continue
        actual = "home" if rh > ra else ("away" if ra > rh else "draw")
        predicted = row.get("predicted_outcome") or _naive_outcome(row)
        if predicted == actual:
            n_correct_1x2 += 1

        actual_btts = rh >= 1 and ra >= 1
        pred_btts = (row.get("btts_prob") or 0.0) >= btts_th
        if pred_btts == actual_btts:
            n_correct_btts += 1

        actual_over25 = (rh + ra) > 2
        pred_over25 = (row.get("over25_prob") or 0.0) >= ou25_th
        if pred_over25 == actual_over25:
            n_correct_over25 += 1

    n = len(rows)
    result = HistoryQueryResult(
        filter_used=f,
        n_matches=n,
        n_correct_1x2=n_correct_1x2,
        n_correct_btts=n_correct_btts,
        n_correct_over25=n_correct_over25,
        accuracy_1x2=(n_correct_1x2 / n) if n else 0.0,
        accuracy_btts=(n_correct_btts / n) if n else 0.0,
        accuracy_over25=(n_correct_over25 / n) if n else 0.0,
        data_sufficient=n >= _MIN_SAMPLES_FOR_STAT,
        sample_rows=rows[:20],
    )
    return result


def similar_past_matches(
    *,
    league: str | None,
    confidence_pct: float,
    home_xg: float,
    away_xg: float,
    tolerance_confidence: float = 15.0,
    tolerance_xg: float = 1.0,
) -> HistoryQueryResult:
    """
    Raccourci pratique : construit un HistoryFilter autour d'un match donné
    (même ligue si connue, confiance proche, xG total proche) et retourne le
    bilan réel des matchs passés qui lui ressemblent. Utilisé par
    engine.anomaly pour comparer un match à venir à son historique le plus
    proche.
    """
    f = HistoryFilter(
        league_contains=league or None,
        min_confidence=max(0.0, confidence_pct - tolerance_confidence),
        max_confidence=min(100.0, confidence_pct + tolerance_confidence),
        min_xg_total=max(0.0, (home_xg + away_xg) - tolerance_xg),
        max_xg_total=(home_xg + away_xg) + tolerance_xg,
    )
    result = run_query(f)
    if not result.data_sufficient and league:
        # Repli : mêmes critères mais sans filtrer par ligue, pour élargir
        # l'échantillon plutôt que de renvoyer un résultat vide.
        f_no_league = HistoryFilter(
            min_confidence=f.min_confidence,
            max_confidence=f.max_confidence,
            min_xg_total=f.min_xg_total,
            max_xg_total=f.max_xg_total,
        )
        wider = run_query(f_no_league)
        if wider.n_matches > result.n_matches:
            return wider
    return result


def format_query_result(result: HistoryQueryResult) -> str:
    """Formatage Telegram HTML, dans le même style que les autres rapports."""
    lines = [
        "📊  <b>REQUÊTE HISTORIQUE</b>",
        "",
        f"  Matchs correspondants : <b>{result.n_matches}</b>",
    ]
    if result.n_matches == 0:
        lines.append("  Aucun match réglé ne correspond à ces critères.")
        return "\n".join(lines)

    if not result.data_sufficient:
        lines.append(
            f"  ⚠️ Échantillon faible (&lt;{_MIN_SAMPLES_FOR_STAT}) — "
            "pourcentages indicatifs uniquement."
        )

    lines += [
        "",
        f"  1X2      : {result.accuracy_1x2*100:.1f}% "
        f"({result.n_correct_1x2}/{result.n_matches})",
        f"  BTTS     : {result.accuracy_btts*100:.1f}% "
        f"({result.n_correct_btts}/{result.n_matches})",
        f"  Over 2.5 : {result.accuracy_over25*100:.1f}% "
        f"({result.n_correct_over25}/{result.n_matches})",
    ]
    return "\n".join(lines)
