"""
engine/tracking.py — V14 prediction tracking & calibration.

Records predictions in a local SQLite database and compares them with
manually entered final scores so the bot can measure its real reliability.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import sqlite3
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)

_DB_FILENAME = "predictions.db"


def _db_path() -> str:
    os.makedirs(config.WEB_CACHE_DIR, exist_ok=True)
    return os.path.join(config.WEB_CACHE_DIR, _DB_FILENAME)


@contextlib.contextmanager
def _connect():
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at        TEXT    NOT NULL,
                home_name         TEXT    NOT NULL,
                away_name         TEXT    NOT NULL,
                league            TEXT,
                kickoff           TEXT,
                home_win_prob     REAL    NOT NULL,
                draw_prob         REAL    NOT NULL,
                away_win_prob     REAL    NOT NULL,
                btts_prob         REAL    NOT NULL,
                over25_prob       REAL    NOT NULL,
                modal_score       TEXT,
                confidence_pct    REAL    NOT NULL,
                confidence_label  TEXT    NOT NULL,
                home_xg            REAL,
                away_xg            REAL,
                result_home       INTEGER,
                result_away       INTEGER,
                settled           INTEGER NOT NULL DEFAULT 0,
                settled_at        TEXT
            )
            """
        )
        existing_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(predictions)").fetchall()
        }
        # These columns were added after the first V15 database format.  Keep
        # the migration local and additive so existing learning data survives.
        if "forebet_url" not in existing_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN forebet_url TEXT")
        if "kickoff_timestamp" not in existing_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN kickoff_timestamp INTEGER")
        # V18 : probabilités 1X2 avant application des multiplicateurs de
        # calibration + version de calibration active au moment de la
        # prédiction. Permet à engine.auto_learning de rejouer l'historique
        # avec une calibration candidate sans jamais appliquer un
        # multiplicateur deux fois. NULL sur les lignes créées avant V18 —
        # ces lignes sont déjà "raw" puisque les multiplicateurs n'étaient
        # alors jamais appliqués (bug corrigé en V18).
        if "home_win_prob_raw" not in existing_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN home_win_prob_raw REAL")
        if "draw_prob_raw" not in existing_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN draw_prob_raw REAL")
        if "away_win_prob_raw" not in existing_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN away_win_prob_raw REAL")
        if "calibration_version" not in existing_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN calibration_version INTEGER")
        # V19.12 — la décision 1X2 réellement affichée à l'utilisateur
        # (calculée par engine.predictor avec draw_detection_factor, donc
        # potentiellement différente d'un simple argmax sur les probabilités
        # brutes) n'était jusqu'ici jamais persistée : calibration_report()
        # et analyse_errors() la recalculaient eux-mêmes via argmax naïf sur
        # (home_win_prob, draw_prob, away_win_prob), ce qui ignorait
        # silencieusement le facteur de détection du nul. Résultat concret
        # observé sur les données réelles : le pronostic affiché pouvait
        # dire "Nul", mais les statistiques de fiabilité ne créditaient (ou
        # débitaient) jamais cette prédiction-là — elles en rejouaient une
        # autre. NULL sur les lignes créées avant V19.12 : le fallback
        # (argmax naïf) reste utilisé pour celles-ci dans le code de
        # lecture, donc l'historique existant continue de fonctionner.
        if "predicted_outcome" not in existing_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN predicted_outcome TEXT")
        # V20 — tier de la ligue (1/2/3, voir engine.leagues) au moment de la
        # prédiction. Nécessaire pour recalibrer la pénalité de confiance PAR
        # TIER à partir de la précision réellement mesurée (engine.league_
        # calibration), au lieu de tiers fixés à la main indéfiniment. NULL
        # sur les lignes créées avant V20 — ignorées par le recalibrage par
        # ligue (pas de régression, juste pas de signal sur ces lignes-là).
        if "league_tier" not in existing_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN league_tier INTEGER")
        # V20.4 — snapshot brut (JSON) au moment de la prédiction. Sans ça,
        # engine.xg_backtest dépend du cache local de snapshots (jointure par
        # forebet_url) pour retrouver les données d'un match — or
        # engine.cache_store.prune_expired_snapshots() purge justement le
        # snapshot d'un match dès que son coup d'envoi est passé, au
        # prochain /scan. Un match n'est réglable qu'après avoir été joué :
        # en pratique son snapshot a donc presque toujours déjà disparu du
        # cache au moment où /resultat est utilisé (confirmé sur données
        # réelles de production : 0/88 matchs réglés retrouvés par
        # xg_backtest.backtest_xg_multiplier() sans ce correctif). Stocker le
        # snapshot ici, une fois pour toutes à la création de la prédiction,
        # rend le backtest indépendant de la rétention du cache. NULL sur les
        # lignes créées avant V20.4 — irrécupérable pour elles, mais chaque
        # nouvelle prédiction en profitera.
        if "snapshot_json" not in existing_columns:
            conn.execute("ALTER TABLE predictions ADD COLUMN snapshot_json TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_predictions_settled "
            "ON predictions(settled)"
        )


def record_prediction(
    *,
    home_name: str,
    away_name: str,
    league: str,
    kickoff: str,
    home_win_prob: float,
    draw_prob: float,
    away_win_prob: float,
    btts_prob: float,
    over25_prob: float,
    modal_score: str,
    confidence_pct: float,
    confidence_label: str,
    home_xg: float,
    away_xg: float,
    forebet_url: str = "",
    kickoff_timestamp: int | None = None,
    home_win_prob_raw: float | None = None,
    draw_prob_raw: float | None = None,
    away_win_prob_raw: float | None = None,
    calibration_version: int | None = None,
    predicted_outcome: str | None = None,
    league_tier: int | None = None,
    snapshot_json: str | None = None,
) -> int:
    """Store one prediction and return its id."""
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO predictions (
                created_at, home_name, away_name, league, kickoff,
                home_win_prob, draw_prob, away_win_prob, btts_prob, over25_prob,
                modal_score, confidence_pct, confidence_label, home_xg, away_xg,
                forebet_url, kickoff_timestamp,
                home_win_prob_raw, draw_prob_raw, away_win_prob_raw, calibration_version,
                predicted_outcome, league_tier, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                home_name,
                away_name,
                league,
                kickoff,
                home_win_prob,
                draw_prob,
                away_win_prob,
                btts_prob,
                over25_prob,
                modal_score,
                confidence_pct,
                confidence_label,
                home_xg,
                away_xg,
                forebet_url,
                kickoff_timestamp,
                home_win_prob_raw,
                draw_prob_raw,
                away_win_prob_raw,
                calibration_version,
                predicted_outcome,
                league_tier,
                snapshot_json,
            ),
        )
        return int(cur.lastrowid)


@dataclass
class SettlementOutcome:
    prediction_id: int
    home_name: str
    away_name: str
    result_home: int
    result_away: int
    predicted_1x2: str
    actual_1x2: str
    correct_1x2: bool
    predicted_btts_yes: bool
    actual_btts: bool
    correct_btts: bool
    predicted_over25: bool
    actual_over25: bool
    correct_over25: bool
    brier_1x2: float


def _brier_1x2(p_home: float, p_draw: float, p_away: float, actual: str) -> float:
    o_home = 1.0 if actual == "home" else 0.0
    o_draw = 1.0 if actual == "draw" else 0.0
    o_away = 1.0 if actual == "away" else 0.0
    return (p_home - o_home) ** 2 + (p_draw - o_draw) ** 2 + (p_away - o_away) ** 2


def settle(prediction_id: int, result_home: int, result_away: int) -> SettlementOutcome | None:
    """Attach a real final score to a stored prediction and score it."""
    if result_home < 0 or result_away < 0:
        return None
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT home_name, away_name, home_win_prob, draw_prob, away_win_prob, "
            "btts_prob, over25_prob, settled, predicted_outcome "
            "FROM predictions WHERE id = ?",
            (prediction_id,),
        ).fetchone()
        if row is None or row[7]:
            return None
        (home_name, away_name, p_home, p_draw, p_away, p_btts, p_over25,
         _settled, stored_pred) = row

        actual = "home" if result_home > result_away else (
            "away" if result_away > result_home else "draw"
        )
        # V19.14 — utilise la décision RÉELLEMENT affichée à l'utilisateur
        # (calculée par engine.predictor avec draw_detection_factor) quand
        # elle est disponible, au lieu d'un argmax naïf sur les probabilités
        # brutes qui pouvait contredire ce que l'utilisateur avait sous les
        # yeux pour ce même pronostic. Retombe sur l'argmax naïf pour les
        # lignes antérieures à V19.12 (predicted_outcome NULL) — même
        # convention que tracking.calibration_report().
        probs_map = {"home": p_home, "draw": p_draw, "away": p_away}
        if stored_pred and stored_pred in probs_map:
            predicted = stored_pred
        else:
            predicted = max(probs_map, key=probs_map.get)

        # V19.14 — seuils BTTS/O2.5 RÉELLEMENT actifs, au lieu d'un 0.5 fixe
        # qui ne correspond à aucune décision jamais affichée à l'utilisateur
        # (le seuil réel est calibré empiriquement, ex. 0.65/0.58). Utilise
        # la calibration ACTUELLE : en pratique elle ne bouge que par petits
        # pas validés par holdout (voir auto_learning.py), donc l'écart avec
        # le seuil réellement actif au moment de la prédiction reste faible.
        try:
            from engine.calibration import load_calibration
            _calib = load_calibration()
            btts_th, ou25_th = _calib.btts_threshold, _calib.ou25_threshold
        except Exception:
            btts_th, ou25_th = 0.5, 0.5

        actual_btts = result_home >= 1 and result_away >= 1
        actual_over25 = result_home + result_away > 2
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

        conn.execute(
            "UPDATE predictions SET result_home = ?, result_away = ?, "
            "settled = 1, settled_at = ? WHERE id = ?",
            (result_home, result_away, now, prediction_id),
        )

        outcome = SettlementOutcome(
            prediction_id=prediction_id,
            home_name=home_name,
            away_name=away_name,
            result_home=result_home,
            result_away=result_away,
            predicted_1x2=predicted,
            actual_1x2=actual,
            correct_1x2=predicted == actual,
            predicted_btts_yes=p_btts >= btts_th,
            actual_btts=actual_btts,
            correct_btts=(p_btts >= btts_th) == actual_btts,
            predicted_over25=p_over25 >= ou25_th,
            actual_over25=actual_over25,
            correct_over25=(p_over25 >= ou25_th) == actual_over25,
            brier_1x2=round(_brier_1x2(p_home, p_draw, p_away, actual), 4),
        )

    # V19.14 — alimente la mémoire d'équipe (engine/team_memory.py) : jusqu'ici
    # update_from_result() n'était appelé nulle part et /memoire était
    # toujours vide. On le fait ici, dans settle(), pour que ÇA MARCHE quel
    # que soit le point d'entrée (/resultat ou /autoresultat) sans dépendre
    # de chaque appelant pour s'en souvenir. N'affecte jamais le résultat du
    # règlement lui-même si la mémoire échoue à s'écrire.
    try:
        from engine import team_memory
        mgr = team_memory.get_manager()
        mgr.record_model_outcome(home_name, outcome.correct_1x2)
        mgr.record_model_outcome(away_name, outcome.correct_1x2)
        mgr.save()
    except Exception as exc:
        logger.warning("[tracking] team_memory update failed: %s", exc)

    return outcome


def find_unsettled(limit: int = 15) -> list[tuple]:
    """Return recent predictions without a result, including Forebet linkage."""
    init_db()
    with _connect() as conn:
        return conn.execute(
            "SELECT id, home_name, away_name, kickoff, created_at, forebet_url, "
            "kickoff_timestamp "
            "FROM predictions WHERE settled = 0 ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def count_predictions() -> tuple[int, int]:
    """Return (total, settled) prediction counts — used to show what a
    cache reset preserves (predictions.db lives outside the day-snapshot
    directories that /delete clears)."""
    init_db()
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        settled = conn.execute("SELECT COUNT(*) FROM predictions WHERE settled = 1").fetchone()[0]
    return int(total), int(settled)


def get_prediction_probs(prediction_id: int) -> dict | None:
    """Retourne les probabilités et la confiance d'un pronostic par son id."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT home_win_prob, draw_prob, away_win_prob, btts_prob, over25_prob, "
            "confidence_label, confidence_pct FROM predictions WHERE id=?",
            (prediction_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "home_win_prob":   float(row[0]),
        "draw_prob":       float(row[1]),
        "away_win_prob":   float(row[2]),
        "btts_prob":       float(row[3]),
        "over25_prob":     float(row[4]),
        "confidence_label": str(row[5] or "MEDIUM"),
        "confidence_pct":  float(row[6]),
    }


def find_unsettled_for_auto_result(limit: int = 100) -> list[dict]:
    """Return pending predictions in a shape suitable for the Forebet matcher."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, home_name, away_name, kickoff, created_at, forebet_url, "
            "kickoff_timestamp FROM predictions WHERE settled = 0 "
            "ORDER BY COALESCE(kickoff_timestamp, 0), id"
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "home": str(row[1]),
            "away": str(row[2]),
            "kickoff": str(row[3] or ""),
            "created_at": str(row[4] or ""),
            "forebet_url": str(row[5] or ""),
            "kickoff_timestamp": int(row[6]) if row[6] is not None else None,
        }
        for row in rows[:limit]
    ]


@dataclass
class BucketStats:
    label: str
    n: int = 0
    correct_1x2: int = 0
    brier_sum: float = 0.0

    @property
    def accuracy_1x2(self) -> float:
        return self.correct_1x2 / self.n if self.n else 0.0

    @property
    def brier_avg(self) -> float:
        return self.brier_sum / self.n if self.n else 0.0


@dataclass
class CalibrationReport:
    n_settled: int = 0
    accuracy_1x2: float = 0.0
    brier_1x2: float = 0.0
    accuracy_btts: float = 0.0
    accuracy_over25: float = 0.0
    by_confidence: dict[str, BucketStats] = field(default_factory=dict)


def calibration_report() -> CalibrationReport:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT home_win_prob, draw_prob, away_win_prob, btts_prob, over25_prob, "
            "confidence_label, result_home, result_away, predicted_outcome "
            "FROM predictions WHERE settled = 1"
        ).fetchall()

    report = CalibrationReport()
    if not rows:
        return report

    # V19.14 — mêmes seuils BTTS/O2.5 que settle() (calibration réellement
    # active), au lieu d'un 0.5 fixe qui ne correspond à aucune décision
    # jamais affichée à l'utilisateur.
    try:
        from engine.calibration import load_calibration
        _calib = load_calibration()
        btts_th, ou25_th = _calib.btts_threshold, _calib.ou25_threshold
    except Exception:
        btts_th, ou25_th = 0.5, 0.5

    report.n_settled = len(rows)
    correct_1x2 = correct_btts = correct_over25 = 0
    brier_total = 0.0
    buckets: dict[str, BucketStats] = {
        "HIGH": BucketStats("HIGH"),
        "MEDIUM": BucketStats("MEDIUM"),
        "LOW": BucketStats("LOW"),
    }

    for p_home, p_draw, p_away, p_btts, p_over25, conf_label, rh, ra, stored_pred in rows:
        actual = "home" if rh > ra else ("away" if ra > rh else "draw")
        # V19.12 — utilise la décision réellement affichée si disponible ;
        # retombe sur argmax naïf pour les lignes antérieures à V19.12 (NULL).
        probs_map = {"home": p_home, "draw": p_draw, "away": p_away}
        if stored_pred and stored_pred in probs_map:
            predicted = stored_pred
        else:
            predicted = max(probs_map, key=probs_map.get)
        is_correct = predicted == actual
        brier = _brier_1x2(p_home, p_draw, p_away, actual)
        correct_1x2 += int(is_correct)
        brier_total += brier

        actual_btts = rh >= 1 and ra >= 1
        correct_btts += int((p_btts >= btts_th) == actual_btts)
        actual_over25 = rh + ra > 2
        correct_over25 += int((p_over25 >= ou25_th) == actual_over25)

        bucket = buckets.get(conf_label or "LOW", buckets["LOW"])
        bucket.n += 1
        bucket.brier_sum += brier
        bucket.correct_1x2 += int(is_correct)

    n = report.n_settled
    report.accuracy_1x2 = round(correct_1x2 / n, 4)
    report.brier_1x2 = round(brier_total / n, 4)
    report.accuracy_btts = round(correct_btts / n, 4)
    report.accuracy_over25 = round(correct_over25 / n, 4)
    report.by_confidence = buckets
    return report