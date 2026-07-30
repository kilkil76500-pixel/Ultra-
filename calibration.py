"""
engine/calibration.py — V15 : Recalibrage progressif des poids du modèle.

À partir du rapport d'apprentissage (learning.py), génère un ensemble de
multiplicateurs de calibration par marché et les sauvegarde dans un fichier
JSON dans le répertoire de cache. Ces multiplicateurs sont ensuite lus par le
prédicteur pour ajuster ses seuils de confiance.

Fonctionnement
--------------
1. `compute_calibration()` analyse les erreurs et produit un CalibrationConfig.
2. `save_calibration()` écrit la config dans cache/calibration.json,
   en créant une sauvegarde horodatée dans l'historique (versioning.py).
3. `load_calibration()` lit la config active. Retourne les valeurs par défaut
   si aucune calibration n'existe encore.

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass

import config
from engine.learning import LearningReport, analyse_errors

logger = logging.getLogger(__name__)

_CALIBRATION_FILENAME = "calibration.json"
_VERSION_DIR          = "calibration_history"

# ── Limites de sécurité ───────────────────────────────────────────────────────
_MIN_MULTIPLIER = 0.70   # Jamais en dessous de −30 %
_MAX_MULTIPLIER = 1.30   # Jamais au-dessus de +30 %
_MIN_SAMPLES    = 15     # En dessous de ce seuil, pas de recalibrage


def _calib_path() -> str:
    os.makedirs(config.WEB_CACHE_DIR, exist_ok=True)
    return os.path.join(config.WEB_CACHE_DIR, _CALIBRATION_FILENAME)


# ── Structure de données ─────────────────────────────────────────────────────

@dataclass
class CalibrationConfig:
    """
    Multiplicateurs appliqués aux probabilités et seuils du modèle.

    Valeur 1.0 = pas de modification (comportement V14).
    Valeur < 1.0 = le modèle réduira sa confiance sur ce marché.
    Valeur > 1.0 = le modèle augmentera sa confiance sur ce marché.
    """
    # Seuils de confidence_pct pour HIGH / MEDIUM / LOW
    # V17 : seuil HIGH relevé à 72 % — les matchs "HIGH" étaient en réalité
    # moins fiables que les matchs MEDIUM d'après les données réelles (103 matchs).
    confidence_high_threshold:   float = 72.0
    confidence_medium_threshold: float = 50.0

    # Multiplicateurs sur les probabilités avant affichage
    prob_multiplier_home:  float = 1.0  # correction biais domicile
    prob_multiplier_away:  float = 1.0  # correction biais extérieur
    prob_multiplier_draw:  float = 1.0  # correction biais nul

    # Ajustement seuil BTTS (par défaut 0.56)
    # V17 : relevé de 0.50 → 0.56 — 39 faux YES sur 45 erreurs BTTS (87%)
    btts_threshold:  float = 0.56

    # Ajustement seuil Over/Under 2.5 (par défaut 0.54)
    # V17 : relevé de 0.50 → 0.54 — 36 faux OVER sur 42 erreurs O/U (86%)
    ou25_threshold:  float = 0.54

    # Multiplicateur global xG — corrige la sous-estimation systématique des buts
    # V17 : xG prédit 2.78 vs réel 3.12 → +12% nécessaire
    xg_global_multiplier: float = 1.12

    # Facteur de détection nul — amplifie draw_prob pour la décision "résultat prédit"
    # V17 : 0 nul prédit sur 22 réels → draw_prob systématiquement sous-estimé
    draw_detection_factor: float = 1.45

    # Qualité estimée du calibrage : HIGH / MEDIUM / LOW / UNKNOWN
    calibration_quality: str = "UNKNOWN"

    # Nombre de prédictions utilisées pour ce calibrage
    n_samples: int = 0

    # Version (incrémentée à chaque recalibrage)
    version: int = 1

    # Horodatage ISO de la dernière mise à jour
    updated_at: str = ""

    # V19 : nombre de cycles d'auto-apprentissage consécutifs où un candidat
    # a été rejeté (régression détectée sur le holdout). Remis à 0 dès qu'un
    # candidat est accepté. Sert uniquement à alerter l'utilisateur — n'a
    # aucun effet sur les probabilités ou décisions du modèle.
    consecutive_rejections: int = 0

    @classmethod
    def default(cls) -> "CalibrationConfig":
        return cls()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CalibrationConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


# ── Calcul du recalibrage ────────────────────────────────────────────────────

def compute_calibration(report: LearningReport | None = None) -> CalibrationConfig:
    """
    Calcule un CalibrationConfig à partir du rapport d'apprentissage.
    Si `report` est None, appelle analyse_errors() directement.

    Ne modifie PAS le fichier de calibration — utilise save_calibration() pour ça.
    """
    import datetime

    if report is None:
        report = analyse_errors()

    if report.is_empty or report.n_settled < _MIN_SAMPLES:
        logger.info(
            "calibration: not enough samples (%d < %d), returning default",
            report.n_settled, _MIN_SAMPLES
        )
        cfg = CalibrationConfig.default()
        cfg.n_samples = report.n_settled
        return cfg

    m1x2  = report.by_market.get("1X2")
    mbtts = report.by_market.get("BTTS")
    mou25 = report.by_market.get("Over/Under 2.5")

    cfg = CalibrationConfig()
    cfg.n_samples  = report.n_settled

    # ── Seuils de confiance ───────────────────────────────────────────────────
    # Si HIGH est moins précis que MEDIUM, on relève le seuil HIGH
    if report.overconf_1x2:
        cfg.confidence_high_threshold   = min(70.0, cfg.confidence_high_threshold + 5.0)
        cfg.confidence_medium_threshold = min(55.0, cfg.confidence_medium_threshold + 2.5)
        logger.info("calibration: raised HIGH threshold to %.1f", cfg.confidence_high_threshold)

    # ── Biais domicile / extérieur ────────────────────────────────────────────
    # Si les victoires extérieures sont systématiquement sous-estimées :
    underconf_away_cause = next(
        (c for c in report.error_causes if "Sous-estimation extérieur" in c.cause), None
    )
    if underconf_away_cause and m1x2 and m1x2.n >= _MIN_SAMPLES:
        # Léger boost pour l'extérieur, léger frein pour le domicile
        cfg.prob_multiplier_away = min(_MAX_MULTIPLIER, 1.0 + 0.04 * min(underconf_away_cause.count, 5) / 5)
        cfg.prob_multiplier_home = max(_MIN_MULTIPLIER, 1.0 - 0.03 * min(underconf_away_cause.count, 5) / 5)
        logger.info(
            "calibration: away boost %.3f, home cut %.3f",
            cfg.prob_multiplier_away, cfg.prob_multiplier_home
        )

    # Biais nul
    nul_cause = next(
        (c for c in report.error_causes if "Biais nul" in c.cause), None
    )
    if nul_cause and nul_cause.count >= 3:
        cfg.prob_multiplier_draw = min(_MAX_MULTIPLIER, 1.0 + 0.03)

    # ── Seuil BTTS ────────────────────────────────────────────────────────────
    overconf_btts = next(
        (c for c in report.error_causes if "BTTS" in c.cause), None
    )
    if overconf_btts and mbtts and mbtts.error_rate > 0.40:
        cfg.btts_threshold = min(0.65, 0.56 + 0.02 * min(overconf_btts.count, 5))
        logger.info("calibration: BTTS threshold raised to %.2f", cfg.btts_threshold)

    # ── Seuil O/U 2.5 ─────────────────────────────────────────────────────────
    overconf_ou = next(
        (c for c in report.error_causes if "Over" in c.cause or "OU" in c.cause), None
    )
    if overconf_ou and mou25 and mou25.error_rate > 0.40:
        cfg.ou25_threshold = min(0.62, 0.54 + 0.01 * min(overconf_ou.count, 5))
        logger.info("calibration: O/U 2.5 threshold raised to %.2f", cfg.ou25_threshold)

    # ── Seuil confiance HIGH ──────────────────────────────────────────────────
    # Capé à 78 % pour éviter un seuil inatteignable
    if report.overconf_1x2:
        cfg.confidence_high_threshold   = min(78.0, cfg.confidence_high_threshold + 3.0)
        cfg.confidence_medium_threshold = min(57.0, cfg.confidence_medium_threshold + 2.0)

    # ── Qualité du calibrage ──────────────────────────────────────────────────
    if m1x2:
        acc = m1x2.accuracy
        if   acc >= 0.60: cfg.calibration_quality = "HIGH"
        elif acc >= 0.50: cfg.calibration_quality = "MEDIUM"
        else:             cfg.calibration_quality = "LOW"
    else:
        cfg.calibration_quality = "UNKNOWN"

    cfg.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return cfg


def save_calibration(cfg: CalibrationConfig) -> None:
    """
    Sauvegarde la config dans cache/calibration.json.
    Délègue la création d'un snapshot historique à versioning.py.
    """
    from engine import versioning

    # Charger la version précédente pour incrémenter le numéro de version
    prev = load_calibration()
    cfg.version = prev.version + 1

    import datetime
    if not cfg.updated_at:
        cfg.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    path = _calib_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, indent=2, ensure_ascii=False)
    logger.info("calibration: saved v%d to %s", cfg.version, path)

    # Archiver dans l'historique
    versioning.archive_calibration(cfg)


def load_calibration() -> CalibrationConfig:
    """
    Charge la config de calibration active depuis le cache.
    Retourne CalibrationConfig.default() si aucun fichier n'existe.
    """
    path = _calib_path()
    if not os.path.exists(path):
        return CalibrationConfig.default()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return CalibrationConfig.from_dict(data)
    except Exception as exc:
        logger.warning("calibration: failed to load %s — %s", path, exc)
        return CalibrationConfig.default()


def update_rejection_streak(streak: int) -> None:
    """
    Persiste `consecutive_rejections` sur la calibration active, SANS
    incrémenter la version ni créer d'archive — un rejet d'auto-apprentissage
    ne modifie aucun paramètre du modèle, ce n'est qu'un compteur de suivi.

    Écrit le fichier même si aucune calibration n'a encore été acceptée
    (`load_calibration()` retourne alors les valeurs par défaut) : sinon le
    compteur resterait bloqué à 0 tant qu'aucun candidat n'a jamais été
    appliqué, ce qui viderait l'alerte de son sens dans le cas — fréquent au
    tout début de la vie du bot — où les tout premiers candidats sont
    rejetés.
    """
    cfg = load_calibration()
    if cfg.consecutive_rejections == streak:
        return
    cfg.consecutive_rejections = streak
    try:
        path = _calib_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg.to_dict(), fh, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.warning("calibration: failed to persist rejection streak — %s", exc)


def run_recalibration() -> CalibrationConfig:
    """
    Point d'entrée unique : analyse → calcul → sauvegarde.
    Retourne la nouvelle config.
    """
    report = analyse_errors()
    cfg    = compute_calibration(report)
    if report.n_settled >= _MIN_SAMPLES:
        save_calibration(cfg)
        logger.info(
            "calibration: recalibration done — v%d, quality=%s, n=%d",
            cfg.version, cfg.calibration_quality, cfg.n_samples
        )
    else:
        logger.info(
            "calibration: skipped (only %d samples, need %d)",
            report.n_settled, _MIN_SAMPLES
        )
    return cfg
