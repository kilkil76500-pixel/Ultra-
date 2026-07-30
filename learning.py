"""
engine/learning.py — V15 : Analyse automatique des erreurs par marché.

Interroge les prédictions réglées (settled) dans la base SQLite et produit :
  1. Taux d'erreur par marché : 1X2, BTTS, Over/Under 2.5, score exact.
  2. Identification des causes d'erreurs observables :
       – "surprise totale"  (probabilité prédite ≥ 65 %, résultat inverse)
       – "pari risqué"      (probabilité prédite < 45 %, résultat faux)
       – "biais nul"        (trop de nuls manqués malgré une prob nul élevée)
       – "over-confidence"  (fiabilité HIGH mais taux d'erreur supérieur à MEDIUM)
  3. Biais systématiques par catégorie de confiance (HIGH / MEDIUM / LOW).

Aucune modification des poids du modèle ici — c'est le rôle de calibration.py.
Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)

_DB_FILENAME = "predictions.db"


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


# ── Structures de données ────────────────────────────────────────────────────

@dataclass
class MarketStats:
    """Statistiques d'erreur pour un marché donné."""
    market:       str
    n:            int   = 0
    correct:      int   = 0
    incorrect:    int   = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def error_rate(self) -> float:
        return 1.0 - self.accuracy


@dataclass
class ErrorCause:
    """Une cause d'erreur identifiée avec le nombre d'occurrences."""
    cause:       str
    count:       int
    description: str


@dataclass
class LearningReport:
    """Rapport complet d'analyse des erreurs."""
    n_settled:          int                        = 0
    by_market:          dict[str, MarketStats]     = field(default_factory=dict)
    error_causes:       list[ErrorCause]           = field(default_factory=list)
    overconf_1x2:       bool                       = False  # HIGH moins précis que MEDIUM
    systematic_biases:  list[str]                  = field(default_factory=list)
    recommendation:     str                        = ""

    @property
    def is_empty(self) -> bool:
        return self.n_settled == 0


# ── Analyse principale ───────────────────────────────────────────────────────

def analyse_errors(rows: list[tuple] | None = None) -> LearningReport:
    """
    Lit les prédictions réglées et retourne un LearningReport complet.
    Retourne un rapport vide si la base n'existe pas ou est vide.

    `rows`, si fourni, doit être une liste de tuples dans le même ordre que
    la requête SQL ci-dessous (home_win_prob, draw_prob, away_win_prob,
    btts_prob, over25_prob, modal_score, confidence_label, confidence_pct,
    result_home, result_away). Permet à engine.auto_learning de calculer un
    rapport limité à un sous-ensemble temporel (le lot "calibration" d'un
    découpage holdout) sans dupliquer la logique d'analyse.
    """
    if rows is None:
        if not os.path.exists(_db_path()):
            return LearningReport()

        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT home_win_prob, draw_prob, away_win_prob,
                       btts_prob, over25_prob, modal_score,
                       confidence_label, confidence_pct,
                       result_home, result_away, predicted_outcome
                FROM predictions
                WHERE settled = 1
                """
            ).fetchall()

    if not rows:
        return LearningReport()

    report = LearningReport(n_settled=len(rows))

    # ── Marchés ───────────────────────────────────────────────────────────────
    m1x2   = MarketStats("1X2")
    mbtts  = MarketStats("BTTS")
    mou25  = MarketStats("Over/Under 2.5")
    mscore = MarketStats("Score exact (modal)")

    # ── Causes d'erreurs ─────────────────────────────────────────────────────
    cause_counts: dict[str, int] = {
        "surprise_totale":    0,  # prob ≥ 65 % mais résultat faux
        "pari_risque":        0,  # prob < 45 % mais résultat faux
        "biais_nul":          0,  # nul raté malgré prob nul ≥ 35 %
        "overconf_btts":      0,  # BTTS yes prédit à > 65 % mais raté
        "underconf_away":     0,  # victoire extérieure souvent manquée
    }

    # Confiance par bucket
    bucket_correct:   dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    bucket_total:     dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}

    for (p_home, p_draw, p_away, p_btts, p_over25,
         modal_score, conf_label, conf_pct,
         rh, ra, stored_pred) in rows:

        actual_1x2 = "home" if rh > ra else ("away" if ra > rh else "draw")
        probs_map = {"home": p_home, "draw": p_draw, "away": p_away}
        # V19.12 — utilise la décision réellement affichée si disponible
        if stored_pred and stored_pred in probs_map:
            pred_1x2 = stored_pred
        else:
            pred_1x2 = max(probs_map, key=probs_map.get)
        pred_prob = probs_map[pred_1x2]

        # ── 1X2 ──────────────────────────────────────────────────────────────
        m1x2.n += 1
        ok_1x2 = (pred_1x2 == actual_1x2)
        m1x2.correct  += int(ok_1x2)
        m1x2.incorrect += int(not ok_1x2)

        # Causes
        if not ok_1x2:
            if pred_prob >= 0.65:
                cause_counts["surprise_totale"] += 1
            elif pred_prob < 0.45:
                cause_counts["pari_risque"] += 1
            if actual_1x2 == "draw" and p_draw >= 0.35:
                cause_counts["biais_nul"] += 1
            if actual_1x2 == "away" and p_away < 0.35:
                cause_counts["underconf_away"] += 1

        # Bucket confiance
        bucket = conf_label if conf_label in bucket_total else "LOW"
        bucket_total[bucket]   += 1
        bucket_correct[bucket] += int(ok_1x2)

        # ── BTTS ─────────────────────────────────────────────────────────────
        mbtts.n += 1
        actual_btts = rh >= 1 and ra >= 1
        pred_btts   = p_btts >= 0.5
        ok_btts = (pred_btts == actual_btts)
        mbtts.correct  += int(ok_btts)
        mbtts.incorrect += int(not ok_btts)
        if not ok_btts and p_btts >= 0.65:
            cause_counts["overconf_btts"] += 1

        # ── Over/Under 2.5 ────────────────────────────────────────────────────
        mou25.n += 1
        actual_over25 = (rh + ra) > 2
        pred_over25   = p_over25 >= 0.5
        ok_ou = (pred_over25 == actual_over25)
        mou25.correct  += int(ok_ou)
        mou25.incorrect += int(not ok_ou)

        # ── Score exact ───────────────────────────────────────────────────────
        if modal_score and "-" in modal_score:
            try:
                mh_s, ma_s = (int(x) for x in modal_score.split("-", 1))
                mscore.n += 1
                if mh_s == rh and ma_s == ra:
                    mscore.correct += 1
                else:
                    mscore.incorrect += 1
            except ValueError:
                pass

    report.by_market = {
        "1X2":              m1x2,
        "BTTS":             mbtts,
        "Over/Under 2.5":   mou25,
        "Score exact":      mscore,
    }

    # ── Causes d'erreurs ─────────────────────────────────────────────────────
    cause_descriptions = {
        "surprise_totale": (
            "Surprise totale",
            "Résultat inverse malgré une probabilité ≥ 65 % — "
            "événement rare mais systématique (carton rouge, blessure précoce)."
        ),
        "pari_risque": (
            "Pari risqué",
            "Résultat faux sur un pronostic à moins de 45 % de confiance — "
            "l'incertitude était élevée dès le départ."
        ),
        "biais_nul": (
            "Biais nul",
            "Nul réel manqué alors que la prob. nul était ≥ 35 % — "
            "le modèle sous-pondère peut-être les matchs équilibrés."
        ),
        "overconf_btts": (
            "Surestimation BTTS",
            "BTTS Yes prédit à > 65 % mais non réalisé — "
            "la défense adverse a mieux tenu que prévu."
        ),
        "underconf_away": (
            "Sous-estimation extérieur",
            "Victoire extérieure fréquemment ratée — "
            "le modèle pourrait légèrement sur-pondérer l'avantage domicile."
        ),
    }

    report.error_causes = [
        ErrorCause(
            cause       = cause_descriptions[k][0],
            count       = v,
            description = cause_descriptions[k][1],
        )
        for k, v in sorted(cause_counts.items(), key=lambda item: item[1], reverse=True)
        if v > 0
    ]

    # ── Biais systématiques ───────────────────────────────────────────────────
    biases: list[str] = []

    acc_high   = bucket_correct["HIGH"]   / bucket_total["HIGH"]   if bucket_total["HIGH"]   else None
    acc_medium = bucket_correct["MEDIUM"] / bucket_total["MEDIUM"] if bucket_total["MEDIUM"] else None
    acc_low    = bucket_correct["LOW"]    / bucket_total["LOW"]    if bucket_total["LOW"]    else None

    if acc_high is not None and acc_medium is not None:
        if acc_high < acc_medium - 0.05:
            report.overconf_1x2 = True
            biases.append(
                f"Surestimation de la fiabilité HIGH ({acc_high:.0%}) "
                f"vs MEDIUM ({acc_medium:.0%}) — réévaluation du seuil de confiance recommandée."
            )

    if m1x2.error_rate > 0.50:
        biases.append(
            f"Taux d'erreur 1X2 global élevé ({m1x2.error_rate:.0%}) — "
            "données insuffisantes ou ligue très imprévisible."
        )

    if mbtts.error_rate > 0.40:
        biases.append(
            f"Taux d'erreur BTTS ({mbtts.error_rate:.0%}) — "
            "le seuil de 50 % pourrait être ajusté à la hausse."
        )

    if cause_counts["underconf_away"] >= 3:
        biases.append(
            "Victoires extérieures systématiquement sous-estimées — "
            "réduire le biais domicile dans le calcul de l'indice de force."
        )

    report.systematic_biases = biases

    # ── Recommandation globale ────────────────────────────────────────────────
    if m1x2.n >= 20:
        if m1x2.accuracy >= 0.60:
            report.recommendation = (
                f"Précision 1X2 satisfaisante ({m1x2.accuracy:.0%}). "
                "Continuer à collecter des données pour affiner."
            )
        elif m1x2.accuracy >= 0.50:
            report.recommendation = (
                f"Précision 1X2 correcte ({m1x2.accuracy:.0%}). "
                "Recalibrage léger recommandé via /recalibrer."
            )
        else:
            report.recommendation = (
                f"Précision 1X2 faible ({m1x2.accuracy:.0%}). "
                "Recalibrage urgent recommandé via /recalibrer."
            )
    elif m1x2.n > 0:
        report.recommendation = (
            f"Seulement {m1x2.n} pronostic(s) réglé(s). "
            "Entre au moins 20 résultats via /resultat pour une analyse fiable."
        )
    else:
        report.recommendation = "Aucune donnée. Utilise /resultat pour enregistrer des résultats réels."

    return report
