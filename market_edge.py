"""engine/market_edge.py — Recommande, PAR MATCH, le marché (1X2 / BTTS /
Plus-moins 2,5) sur lequel le bot a historiquement été le plus fiable, à un
niveau de confiance comparable.

V19.13 — Pourquoi ce module
----------------------------
Sur les 88 premiers pronostics réglés, le bot est nettement plus fiable sur
BTTS et Plus/moins 2,5 (~59-60% de précision) que sur le 1X2 (~41-43% une
fois `predicted_outcome` correctement pris en compte, voir tracking.py). Un
utilisateur qui suit systématiquement le pronostic 1X2 affiché en premier
laisse donc de la fiabilité sur la table par rapport à BTTS/O2.5 — sans que
rien dans le rapport ne le signale explicitement.

Ce module ne change AUCUNE probabilité ni aucun pronostic existant : il lit
l'historique déjà réglé en base et calcule, pour CE match précis, quel
marché a — empiriquement, à niveau de confiance comparable — la meilleure
fiabilité passée. C'est une couche additive au-dessus de PredictionResult
(nouveaux champs `recommended_market` / `recommended_market_reason`,
défauts vides) : rien d'existant n'est modifié si ce module échoue ou si
l'historique est encore trop court.

Prudence statistique délibérée
-------------------------------
88 matchs, c'est peu. Ce module :
- n'affiche une recommandation que si au moins `_MIN_SAMPLES_PER_BUCKET`
  pronostics réglés existent pour la comparaison utilisée (sinon
  `data_sufficient=False` et un message neutre plutôt qu'un chiffre
  hasardeux) ;
- ne découpe qu'en DEUX paniers de confiance par marché (médiane), pas plus
  finement — un découpage plus fin diviserait encore l'échantillon et
  produirait des pourcentages qui ont l'air précis mais qui ne le sont pas ;
- retombe sur la fiabilité globale du marché (tous niveaux de confiance
  confondus) si le panier spécifique à ce niveau de confiance est encore
  trop petit.
Ces chiffres sont donc INDICATIFS et évolueront (potentiellement beaucoup)
à mesure que l'historique s'étoffe — pas une garantie statistique figée.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from engine import tracking

logger = logging.getLogger(__name__)

_MIN_SAMPLES_PER_BUCKET = 15  # même seuil de prudence que engine.calibration

_MARKETS = ("1x2", "btts", "over25")

_MARKET_LABELS = {
    "1x2":    "1X2",
    "btts":   "BTTS (les deux équipes marquent)",
    "over25": "Plus/moins de 2,5 buts",
}


@dataclass
class _MarketBucketStat:
    n: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0


@dataclass
class MarketRecommendation:
    market:            str   = ""     # "1x2" | "btts" | "over25" | ""
    market_label:       str   = ""
    reason:             str   = ""
    expected_accuracy:  float = 0.0
    sample_size:        int   = 0
    data_sufficient:    bool  = False
    # Détail des trois marchés, pour un affichage étendu si souhaité.
    all_markets:        dict  = field(default_factory=dict)


def _margin_1x2(p_home: float, p_draw: float, p_away: float, predicted_outcome: str) -> float:
    """Écart entre la probabilité du choix retenu et celle du deuxième
    résultat le plus probable — une meilleure mesure de "à quel point le
    pronostic est net" que la probabilité brute seule (surtout pour un nul
    détecté via le facteur de boost, dont la probabilité affichée n'est pas
    toujours le max brut des trois)."""
    probs = {"home": p_home, "draw": p_draw, "away": p_away}
    if predicted_outcome not in probs:
        predicted_outcome = max(probs, key=probs.get)
    picked = probs[predicted_outcome]
    others = sorted((v for k, v in probs.items() if k != predicted_outcome), reverse=True)
    second = others[0] if others else 0.0
    return max(0.0, picked - second)


def _margin_binary(prob: float) -> float:
    """0 = pile ou face (aucune information), 1 = certitude totale."""
    return abs((prob or 0.0) - 0.5) * 2.0


def _load_settled_rows() -> list[tuple]:
    tracking.init_db()
    with tracking._connect() as conn:  # noqa: SLF001 — module interne du même paquet
        return conn.execute(
            "SELECT home_win_prob, draw_prob, away_win_prob, predicted_outcome, "
            "btts_prob, over25_prob, result_home, result_away "
            "FROM predictions WHERE settled = 1"
        ).fetchall()


def _build_market_stats() -> tuple[dict[str, dict[str, _MarketBucketStat]], dict[str, float]]:
    """Retourne (stats, médianes). stats[marché]["global"|"confiant"|"prudent"]."""
    rows = _load_settled_rows()
    stats: dict[str, dict[str, _MarketBucketStat]] = {
        m: {"global": _MarketBucketStat(), "confiant": _MarketBucketStat(), "prudent": _MarketBucketStat()}
        for m in _MARKETS
    }
    medians = {m: 0.0 for m in _MARKETS}
    if not rows:
        return stats, medians

    parsed: list[dict[str, tuple[float, bool]]] = []
    margins: dict[str, list[float]] = {m: [] for m in _MARKETS}

    for p_home, p_draw, p_away, pred_out, p_btts, p_o25, rh, ra in rows:
        p_home, p_draw, p_away = p_home or 0.0, p_draw or 0.0, p_away or 0.0
        actual_1x2 = "home" if rh > ra else ("away" if ra > rh else "draw")
        probs = {"home": p_home, "draw": p_draw, "away": p_away}
        pred_1x2 = pred_out if pred_out in probs else max(probs, key=probs.get)
        m_1x2 = _margin_1x2(p_home, p_draw, p_away, pred_1x2)
        correct_1x2 = pred_1x2 == actual_1x2

        p_btts = p_btts or 0.0
        actual_btts = rh >= 1 and ra >= 1
        m_btts = _margin_binary(p_btts)
        correct_btts = (p_btts >= 0.5) == actual_btts

        p_o25 = p_o25 or 0.0
        actual_o25 = (rh + ra) > 2
        m_o25 = _margin_binary(p_o25)
        correct_o25 = (p_o25 >= 0.5) == actual_o25

        row_result = {"1x2": (m_1x2, correct_1x2), "btts": (m_btts, correct_btts), "over25": (m_o25, correct_o25)}
        parsed.append(row_result)
        for m in _MARKETS:
            margins[m].append(row_result[m][0])

    for m in _MARKETS:
        vals = sorted(margins[m])
        medians[m] = vals[len(vals) // 2] if vals else 0.0

    for row_result in parsed:
        for m in _MARKETS:
            margin, correct = row_result[m]
            bucket = "confiant" if margin >= medians[m] else "prudent"
            for key in ("global", bucket):
                stats[m][key].n += 1
                stats[m][key].correct += int(correct)

    return stats, medians


def recommend_market(prediction) -> MarketRecommendation:
    """A appeler avec un engine.predictor.PredictionResult déjà construit.

    Ne lève jamais d'exception : en cas de souci (base absente, historique
    vide…), retourne une recommandation vide avec data_sufficient=False —
    c'est une couche d'aide, pas un chemin critique du scan."""
    try:
        stats, medians = _build_market_stats()
    except Exception as exc:  # ne doit jamais casser un scan
        logger.warning("[market_edge] Impossible de charger l'historique : %s", exc)
        return MarketRecommendation()

    total_n = stats["1x2"]["global"].n
    if total_n < _MIN_SAMPLES_PER_BUCKET:
        return MarketRecommendation(
            reason=(
                f"Historique encore trop court ({total_n} pronostic(s) réglé(s), "
                f"{_MIN_SAMPLES_PER_BUCKET} minimum) pour recommander un marché "
                f"de façon fiable."
            ),
            data_sufficient=False,
        )

    predicted_outcome = getattr(prediction, "predicted_outcome", "") or ""
    m_1x2 = _margin_1x2(
        prediction.home_win_prob, prediction.draw_prob, prediction.away_win_prob, predicted_outcome,
    )
    m_btts = _margin_binary(prediction.btts_prob)
    m_o25 = _margin_binary(prediction.over25_prob)
    this_match_margins = {"1x2": m_1x2, "btts": m_btts, "over25": m_o25}

    evaluated: dict[str, dict] = {}
    for m in _MARKETS:
        bucket = "confiant" if this_match_margins[m] >= medians[m] else "prudent"
        bucket_stat = stats[m][bucket]
        used_bucket = bucket
        if bucket_stat.n < _MIN_SAMPLES_PER_BUCKET:
            # Panier spécifique trop petit : on retombe sur la fiabilité
            # globale du marché (tous niveaux de confiance confondus).
            bucket_stat = stats[m]["global"]
            used_bucket = "global"
        evaluated[m] = {
            "accuracy": bucket_stat.accuracy,
            "n": bucket_stat.n,
            "bucket": used_bucket,
            "margin": this_match_margins[m],
        }

    best_market = max(_MARKETS, key=lambda m: (evaluated[m]["accuracy"], evaluated[m]["n"]))
    best = evaluated[best_market]

    others_desc = " ; ".join(
        f"{_MARKET_LABELS[m]} {evaluated[m]['accuracy']:.0%} (n={evaluated[m]['n']})"
        for m in _MARKETS if m != best_market
    )
    bucket_note = (
        "sur des pronostics de confiance comparable" if best["bucket"] != "global"
        else "sur l'ensemble de l'historique de ce marché (pas assez de données au même niveau de confiance)"
    )
    reason = (
        f"{_MARKET_LABELS[best_market]} a été juste {best['accuracy']:.0%} du temps "
        f"({best['n']} cas réglés, {bucket_note}), contre {others_desc}."
    )

    return MarketRecommendation(
        market=best_market,
        market_label=_MARKET_LABELS[best_market],
        reason=reason,
        expected_accuracy=best["accuracy"],
        sample_size=best["n"],
        data_sufficient=True,
        all_markets=evaluated,
    )
