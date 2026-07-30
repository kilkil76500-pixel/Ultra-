"""
engine/league_calibration.py — V20 : Pondération des ligues APPRISE.

Objectif
--------
`engine.leagues` fixe la pénalité de confiance par tier (1.00 / 0.85 / 0.65)
à la main, une fois pour toutes. Ce module la recalibre à partir de la
précision 1X2 RÉELLEMENT mesurée par tier sur l'historique réglé —
même philosophie prudente que `engine.auto_learning` pour les marchés :
« proposer à partir d'une partie des données → vérifier sur une partie
jamais vue → n'appliquer que si l'ordre de fiabilité tient toujours ».

Portée volontairement plus modeste que engine.auto_learning
--------------------------------------------------------------
`auto_learning.py` rejoue des probabilités et fait du walk-forward à
plusieurs fenêtres parce qu'il dispose d'assez de lignes pour ça. Ici, on
recalibre seulement 3 compartiments (tiers), et chaque tier a besoin de
`_MIN_SAMPLES_PER_TIER` échantillons RÉGLÉS pour être pris en compte — avec
88 matchs au total répartis sur 3 tiers (et le monde entier des ligues), un
découpage plus fin serait de l'ajustement sur du bruit, pas un apprentissage
réel. Un tier sans échantillon suffisant garde sa valeur par défaut
(`engine.leagues._TIER_CONF_PENALTY`) inchangée.

Validation
----------
1. Split chronologique 70/30 (calibration / holdout), comme auto_learning.
2. Le candidat est calculé UNIQUEMENT à partir du lot calibration : pour
   chaque tier suffisamment représenté, la pénalité proposée est la
   précision 1X2 de ce tier normalisée par rapport au tier le plus fiable
   observé (le meilleur tier garde 1.00, les autres sont réduits en
   proportion), plafonnée à [0.50, 1.00].
3. Sur le lot holdout (jamais vu par le candidat), on vérifie que l'ORDRE
   de fiabilité entre tiers proposé par le candidat tient toujours — pas
   besoin que les pourcentages soient identiques, juste que le tier jugé
   plus fiable par le candidat ne soit pas mesurablement MOINS fiable que
   l'autre sur le holdout. Si l'ordre s'inverse pour au moins une paire de
   tiers comparables, on rejette le candidat EN ENTIER et on garde les
   valeurs par défaut — jamais d'application partielle.

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os

import config
from engine import tracking
from engine.leagues import _TIER_CONF_PENALTY

logger = logging.getLogger(__name__)

_FILENAME             = "league_calibration.json"
_MIN_SAMPLES_PER_TIER = 15   # aligné sur calibration._MIN_SAMPLES
_MIN_HOLDOUT_PER_TIER = 5    # en dessous, la comparaison holdout n'a pas de sens
_HOLDOUT_RATIO        = 0.30
_MIN_PENALTY          = 0.50
_MAX_PENALTY          = 1.00
_ORDER_TOLERANCE      = 0.05  # 5 points : marge de bruit avant de parler d'inversion


def _path() -> str:
    return os.path.join(config.WEB_CACHE_DIR, _FILENAME)


# ── Structures de données ─────────────────────────────────────────────────────

@dataclasses.dataclass
class LeagueCalibrationConfig:
    tier_confidence_penalty: dict[int, float] = dataclasses.field(
        default_factory=lambda: dict(_TIER_CONF_PENALTY)
    )
    version:    int = 0
    updated_at: str = ""
    n_samples:  int = 0

    @staticmethod
    def default() -> "LeagueCalibrationConfig":
        return LeagueCalibrationConfig(tier_confidence_penalty=dict(_TIER_CONF_PENALTY))

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["tier_confidence_penalty"] = {str(k): v for k, v in self.tier_confidence_penalty.items()}
        return d

    @staticmethod
    def from_dict(data: dict) -> "LeagueCalibrationConfig":
        raw_penalty = data.get("tier_confidence_penalty") or {}
        penalty = dict(_TIER_CONF_PENALTY)
        for k, v in raw_penalty.items():
            try:
                penalty[int(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return LeagueCalibrationConfig(
            tier_confidence_penalty=penalty,
            version=int(data.get("version", 0)),
            updated_at=str(data.get("updated_at", "")),
            n_samples=int(data.get("n_samples", 0)),
        )


@dataclasses.dataclass
class TierAccuracy:
    tier: int
    n: int = 0
    accuracy_1x2: float = 0.0


@dataclasses.dataclass
class LeagueCalibrationReport:
    attempted:         bool = False
    accepted:          bool = False
    reason:            str  = ""
    calib_accuracies:  list[TierAccuracy] = dataclasses.field(default_factory=list)
    holdout_accuracies: list[TierAccuracy] = dataclasses.field(default_factory=list)
    candidate:         LeagueCalibrationConfig | None = None
    active:            LeagueCalibrationConfig | None = None


# ── Persistance (auto-suffisante, corrompue → défauts, jamais d'exception) ────

def load_league_calibration() -> LeagueCalibrationConfig:
    path = _path()
    if not os.path.exists(path):
        return LeagueCalibrationConfig.default()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return LeagueCalibrationConfig.from_dict(data)
    except Exception as exc:
        logger.warning("[league_calibration] fichier corrompu, retour aux défauts : %s", exc)
        return LeagueCalibrationConfig.default()


def save_league_calibration(cfg: LeagueCalibrationConfig) -> None:
    import datetime
    cfg.version = load_league_calibration().version + 1
    cfg.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    os.makedirs(config.WEB_CACHE_DIR, exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, indent=2, ensure_ascii=False)


# ── Chargement des lignes réglées, avec tier connu ────────────────────────────

def _load_rows() -> list[tuple]:
    """(tier, predicted_outcome_or_None, home_win_prob, draw_prob, away_win_prob,
    result_home, result_away), triées chronologiquement (settled_at)."""
    tracking.init_db()
    with tracking._connect() as conn:  # noqa: SLF001 — même paquet
        rows = conn.execute(
            "SELECT league_tier, predicted_outcome, home_win_prob, draw_prob, "
            "away_win_prob, result_home, result_away "
            "FROM predictions WHERE settled = 1 AND league_tier IS NOT NULL "
            "ORDER BY settled_at ASC"
        ).fetchall()
    return rows


def _actual_and_predicted(row: tuple) -> tuple[str, str]:
    tier, predicted, ph, pd, pa, rh, ra = row
    actual = "home" if rh > ra else ("away" if ra > rh else "draw")
    if predicted:
        return predicted, actual
    probs = {"home": ph or 0.0, "draw": pd or 0.0, "away": pa or 0.0}
    return max(probs, key=probs.get), actual


def _tier_accuracies(rows: list[tuple]) -> dict[int, TierAccuracy]:
    buckets: dict[int, list[int]] = {1: [], 2: [], 3: []}
    for row in rows:
        tier = row[0]
        if tier not in buckets:
            continue
        predicted, actual = _actual_and_predicted(row)
        buckets[tier].append(int(predicted == actual))
    return {
        t: TierAccuracy(tier=t, n=len(vals), accuracy_1x2=(sum(vals) / len(vals)) if vals else 0.0)
        for t, vals in buckets.items()
    }


def _temporal_split(rows: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    n = len(rows)
    split = int(n * (1.0 - _HOLDOUT_RATIO))
    return rows[:split], rows[split:]


def _propose_candidate(calib_accuracies: dict[int, TierAccuracy]) -> LeagueCalibrationConfig:
    """Ne touche que les tiers avec assez d'échantillons ; les autres gardent
    leur valeur par défaut."""
    eligible = {t: a for t, a in calib_accuracies.items() if a.n >= _MIN_SAMPLES_PER_TIER}
    penalty = dict(_TIER_CONF_PENALTY)
    if eligible:
        best_acc = max(a.accuracy_1x2 for a in eligible.values()) or 1.0
        for t, a in eligible.items():
            raw = a.accuracy_1x2 / best_acc if best_acc > 0 else 1.0
            penalty[t] = round(min(_MAX_PENALTY, max(_MIN_PENALTY, raw)), 3)
    return LeagueCalibrationConfig(tier_confidence_penalty=penalty)


def _order_holds_on_holdout(
    candidate: LeagueCalibrationConfig,
    holdout_accuracies: dict[int, TierAccuracy],
) -> tuple[bool, str]:
    """Vérifie que l'ordre de fiabilité proposé par le candidat n'est pas
    contredit par le holdout, pour toute paire de tiers comparable (assez
    d'échantillons des deux côtés)."""
    tiers = sorted(candidate.tier_confidence_penalty)
    for i in range(len(tiers)):
        for j in range(i + 1, len(tiers)):
            t_hi, t_lo = tiers[i], tiers[j]
            p_hi = candidate.tier_confidence_penalty[t_hi]
            p_lo = candidate.tier_confidence_penalty[t_lo]
            if abs(p_hi - p_lo) < 1e-9:
                continue  # candidat ne distingue pas ces deux tiers, rien à vérifier
            # t_hi doit être le tier jugé PLUS fiable par le candidat
            hi, lo = (t_hi, t_lo) if p_hi > p_lo else (t_lo, t_hi)
            a_hi = holdout_accuracies.get(hi)
            a_lo = holdout_accuracies.get(lo)
            if not a_hi or not a_lo:
                continue
            if a_hi.n < _MIN_HOLDOUT_PER_TIER or a_lo.n < _MIN_HOLDOUT_PER_TIER:
                continue
            if a_hi.accuracy_1x2 < a_lo.accuracy_1x2 - _ORDER_TOLERANCE:
                return False, (
                    f"tier {hi} jugé plus fiable par le candidat mais mesuré "
                    f"moins fiable sur le holdout ({a_hi.accuracy_1x2:.0%} vs "
                    f"{a_lo.accuracy_1x2:.0%} pour le tier {lo})"
                )
    return True, ""


def run_league_calibration() -> LeagueCalibrationReport:
    """Cycle complet : propose → vérifie sur holdout → applique ou rejette."""
    active = load_league_calibration()
    rows = _load_rows()
    report = LeagueCalibrationReport(active=active)

    if len(rows) < 2 * _MIN_SAMPLES_PER_TIER:
        report.reason = (
            f"Pas assez de matchs réglés avec tier connu ({len(rows)}) — "
            f"minimum {2 * _MIN_SAMPLES_PER_TIER} recommandé. Valeurs par "
            "défaut conservées."
        )
        return report

    report.attempted = True
    calib_rows, holdout_rows = _temporal_split(rows)
    calib_acc = _tier_accuracies(calib_rows)
    holdout_acc = _tier_accuracies(holdout_rows)
    report.calib_accuracies = list(calib_acc.values())
    report.holdout_accuracies = list(holdout_acc.values())

    candidate = _propose_candidate(calib_acc)
    eligible_tiers = [t for t, a in calib_acc.items() if a.n >= _MIN_SAMPLES_PER_TIER]
    if not eligible_tiers:
        report.reason = "Aucun tier n'a assez d'échantillons pour proposer un changement."
        return report
    if candidate.tier_confidence_penalty == active.tier_confidence_penalty:
        report.reason = (
            "Précision mesurée cohérente avec les valeurs actives — "
            "aucun changement à proposer."
        )
        return report

    ok, reason = _order_holds_on_holdout(candidate, holdout_acc)
    if not ok:
        report.reason = f"Candidat rejeté (régression sur holdout) : {reason}"
        report.candidate = candidate
        return report

    save_league_calibration(candidate)
    report.accepted = True
    report.candidate = candidate
    report.reason = "Candidat accepté — l'ordre de fiabilité tient sur le holdout."
    return report


def format_report(report: LeagueCalibrationReport) -> str:
    lines = ["🌍  <b>RECALIBRAGE DES LIGUES</b>", ""]
    if not report.attempted:
        lines.append(f"  ⚠️ {report.reason}")
        return "\n".join(lines)

    lines.append("  <b>Précision 1X2 mesurée (lot calibration) :</b>")
    for acc in sorted(report.calib_accuracies, key=lambda a: a.tier):
        lines.append(f"    Tier {acc.tier} — n={acc.n:<3} — {acc.accuracy_1x2*100:.1f}%")
    lines.append("")
    if report.accepted and report.candidate:
        lines.append("  ✅ <b>Candidat appliqué</b>")
        for t, p in sorted(report.candidate.tier_confidence_penalty.items()):
            lines.append(f"    Tier {t} → pénalité de confiance {p:.2f}")
    else:
        lines.append(f"  ❌ <b>Candidat rejeté</b> — {report.reason}")
    return "\n".join(lines)
