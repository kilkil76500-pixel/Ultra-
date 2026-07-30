"""
engine/auto_learning.py — V18 : Boucle d'auto-amélioration sécurisée.

Objectif : permettre au bot de se recalibrer TOUT SEUL, comme le ferait un
data scientist prudent, sans jamais régresser sur ce qu'il a déjà appris.

Principe : « proposer → backtester → n'appliquer que si c'est prouvé
meilleur, sinon ne rien changer »
------------------------------------------------------------------------
1. On sépare les prédictions réglées en deux lots temporels — exactement
   comme engine.validation : le lot "calibration" (≈70 % les plus
   anciennes) et le lot "holdout" (≈30 % les plus récentes).
2. On calcule une config CANDIDATE uniquement à partir du lot calibration
   (engine.calibration.compute_calibration) — le holdout ne sert jamais à
   proposer quoi que ce soit.
3. On rejoue à la fois la config ACTIVE et la config CANDIDATE sur le lot
   holdout (jamais vu par le candidat) en réappliquant les seuils et
   multiplicateurs de calibration aux probabilités déjà enregistrées.
4. On ne conserve le candidat QUE s'il n'est strictement pas moins bon que
   l'actif sur ce holdout : précision 1X2 au moins égale, Brier pas
   dégradé au-delà du bruit, BTTS/O-U pas dégradés au-delà du bruit. Sinon
   on rejette et on garde l'actif tel quel — la calibration en place ne
   change JAMAIS pour le pire.

Chaque candidat accepté est sauvegardé via engine.calibration.save_calibration,
ce qui crée automatiquement une nouvelle version archivée : /versions permet
donc toujours de revenir en arrière manuellement, même après une
auto-amélioration acceptée.

Limite assumée
---------------
`xg_global_multiplier` agit AVANT la simulation Monte-Carlo (sur le xG des
équipes), donc son effet ne peut pas être rejoué a posteriori à partir des
seules probabilités déjà enregistrées. Cette boucle automatique ne le
modifie donc jamais : il reste sous contrôle humain exclusif via
/recalibrer (immédiat) + /valider.

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import sqlite3

import config
from engine import calibration as calibration_module
from engine import learning as learning_module

logger = logging.getLogger(__name__)

_DB_FILENAME = "predictions.db"

# Mêmes seuils que engine.validation pour une séparation temporelle cohérente.
_HOLDOUT_RATIO  = 0.30
_MIN_HOLDOUT    = 10
_MIN_CALIB_ROWS = 15   # aligné sur calibration._MIN_SAMPLES

# Tolérances de bruit — un candidat n'est jamais accepté s'il est PIRE que
# ces marges sur le holdout. La précision 1X2 (métrique la plus importante,
# et la moins bruitée) n'a AUCUNE tolérance : elle doit être ≥ à l'actif.
_EPS_BRIER      = 0.01
_EPS_SECONDARY  = 0.03   # BTTS / Over-Under 2.5

# V19 — Validation walk-forward : un split unique 70/30 peut, par hasard,
# tomber sur une tranche récente non représentative (une série atypique de
# résultats) et faire accepter — ou rejeter — un candidat à tort. Quand le
# holdout est assez grand, on le découpe en plusieurs fenêtres chronologiques
# contiguës et on exige qu'il n'y ait pas de régression sur la majorité des
# fenêtres, en plus de l'agrégat complet. Avec un holdout trop petit pour
# former des fenêtres fiables, on retombe sur le comportement à split unique
# (fenêtre unique = tout le holdout), strictement identique aux versions
# précédentes.
_N_FOLDS       = 3
_MIN_FOLD_SIZE = 5   # une fenêtre non fiable est pire qu'inutile

# V19 — à partir de ce nombre de rejets consécutifs (régression détectée à
# chaque cycle), on affiche une alerte explicite : ça ne casse rien, mais
# c'est un signal que le modèle plafonne ou que les données dérivent, et ça
# mérite un coup d'œil humain plutôt que de rester noyé dans des rapports
# individuels.
_STREAK_WARNING_THRESHOLD = 3

# V19.14 — nombre minimum d'échantillons dans un palier de confiance (HIGH ou
# LOW) pour que sa précision soit jugée fiable plutôt que du bruit. En
# dessous, discrimination() renvoie None et aucune décision (recherche de
# seuils ou porte anti-régression) ne se base sur ce palier.
_MIN_BUCKET_N = 8

# Champs de CalibrationConfig comparés pour décrire les changements et pour
# détecter un candidat "sans changement réel" (skip pour éviter le bruit de
# versions inutiles).
_TUNABLE_FIELDS = (
    "confidence_high_threshold", "confidence_medium_threshold",
    "prob_multiplier_home", "prob_multiplier_away", "prob_multiplier_draw",
    "btts_threshold", "ou25_threshold", "draw_detection_factor",
)

_FIELD_LABELS = {
    "confidence_high_threshold":   "Seuil confiance HIGH",
    "confidence_medium_threshold": "Seuil confiance MEDIUM",
    "prob_multiplier_home":        "Multiplicateur domicile",
    "prob_multiplier_away":        "Multiplicateur extérieur",
    "prob_multiplier_draw":        "Multiplicateur nul",
    "btts_threshold":              "Seuil BTTS",
    "ou25_threshold":              "Seuil O/U 2.5",
    "draw_detection_factor":       "Facteur détection nul",
}


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


# ── Structures de données ─────────────────────────────────────────────────────

@dataclasses.dataclass
class SimMetrics:
    """Métriques d'une config rejouée sur un lot de prédictions réglées."""
    n:             int   = 0
    accuracy_1x2:  float = 0.0
    brier_1x2:     float = 0.0
    accuracy_btts: float = 0.0
    accuracy_ou25: float = 0.0
    # V19.14 — discrimination du score de confiance : precision par palier,
    # recalculée en appliquant les seuils confidence_high/medium_threshold
    # DE LA CONFIG SIMULÉE (pas le label déjà stocké) au confidence_pct brut
    # de chaque ligne. C'est ce qui permet de vérifier qu'un candidat ne
    # rend pas HIGH/MEDIUM/LOW moins distincts qu'avant.
    n_high:        int   = 0
    acc_high:      float | None = None
    n_medium:      int   = 0
    acc_medium:    float | None = None
    n_low:         int   = 0
    acc_low:       float | None = None

    @property
    def discrimination(self) -> float | None:
        """acc_high - acc_low quand les deux paliers ont assez d'échantillons
        pour être jugés, sinon None (signal non exploitable, pas 0)."""
        if self.acc_high is None or self.acc_low is None:
            return None
        return round(self.acc_high - self.acc_low, 4)


@dataclasses.dataclass
class AutoLearningReport:
    """Résultat complet d'un cycle d'auto-apprentissage sécurisé."""
    attempted:          bool   = False   # False si pas assez de données
    accepted:           bool   = False   # True si un candidat a été appliqué
    reason:             str    = ""
    n_total:            int    = 0
    n_calibration:      int    = 0
    n_holdout:          int    = 0
    active_version:     int    = 0
    new_version:        int | None = None
    active_metrics:     SimMetrics | None = None
    candidate_metrics:  SimMetrics | None = None
    changes:            list[str] = dataclasses.field(default_factory=list)
    # V19 — validation walk-forward multi-fenêtres
    n_folds:            int = 0     # 0 ou 1 = comportement single-holdout (legacy)
    fold_regressions:   list[str] = dataclasses.field(default_factory=list)
    # V19 — suivi des rejets consécutifs (alerte de dérive)
    rejection_streak:   int = 0
    streak_warning:     bool = False


# ── Chargement & découpage temporel ───────────────────────────────────────────

def _load_settled_rows() -> list[tuple]:
    """
    Charge toutes les prédictions réglées, triées chronologiquement par
    date de règlement (le même ordre que engine.validation).

    Colonnes : home_win_prob(raw), draw_prob(raw), away_win_prob(raw),
    btts_prob, over25_prob, modal_score, confidence_label, confidence_pct,
    result_home, result_away, predicted_outcome — c'est-à-dire exactement
    l'ordre attendu par engine.learning.analyse_errors(rows=...), mais avec
    les probabilités 1X2 *raw* (pré-multiplicateur) quand elles existent.

    predicted_outcome est toujours forcé à NULL ici (volontairement, pas un
    oubli) : ce chargeur sert à rejouer un facteur de détection du nul
    CANDIDAT sur les probabilités brutes (voir _simulate()), donc la
    décision réellement affichée à l'époque — calculée avec le facteur
    ALORS actif — n'est pas pertinente pour ce remplacement hypothétique.
    analyse_errors() retombe alors sur son argmax naïf historique, ce qui
    correspond exactement à son comportement d'avant l'ajout de la colonne.
    """
    if not os.path.exists(_db_path()):
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(home_win_prob_raw, home_win_prob),
                       COALESCE(draw_prob_raw, draw_prob),
                       COALESCE(away_win_prob_raw, away_win_prob),
                       btts_prob, over25_prob, modal_score,
                       confidence_label, confidence_pct,
                       result_home, result_away, NULL
                FROM predictions
                WHERE settled = 1
                ORDER BY settled_at ASC
                """
            ).fetchall()
        return rows
    except Exception as exc:
        logger.error("[auto_learning] DB load failed: %s", exc)
        return []


def _temporal_split(rows: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    """Découpe 70% (calibration, plus anciennes) / 30% (holdout, plus récentes)."""
    n = len(rows)
    split = max(_MIN_CALIB_ROWS, int(n * (1.0 - _HOLDOUT_RATIO)))
    return rows[:split], rows[split:]


def _fold_split(holdout_rows: list[tuple]) -> list[list[tuple]]:
    """
    Découpe le holdout en `_N_FOLDS` fenêtres chronologiques contiguës
    (walk-forward), chacune couvrant une période distincte et non
    chevauchante des prédictions les plus récentes.

    Retourne une liste à un seul élément (le holdout entier) quand il n'y a
    pas assez de lignes pour former `_N_FOLDS` fenêtres fiables — c'est le
    comportement historique (single-holdout), inchangé dans ce cas.
    """
    n = len(holdout_rows)
    if n < _N_FOLDS * _MIN_FOLD_SIZE:
        return [holdout_rows]

    base = n // _N_FOLDS
    folds: list[list[tuple]] = []
    start = 0
    for i in range(_N_FOLDS):
        end = n if i == _N_FOLDS - 1 else start + base
        folds.append(holdout_rows[start:end])
        start = end
    return folds


# ── Simulation d'une config sur un lot de lignes ──────────────────────────────

def _simulate(rows: list[tuple], cfg) -> SimMetrics:
    """
    Rejoue `cfg` (CalibrationConfig) sur `rows` (probabilités RAW déjà
    enregistrées) et calcule les métriques qui en résulteraient — exactement
    la même logique que le pipeline réel dans engine.predictor.
    """
    n = len(rows)
    if n == 0:
        return SimMetrics()

    correct_1x2 = correct_btts = correct_ou25 = 0
    brier_total = 0.0
    # V19.14 — buckets recalculés sous les seuils de `cfg` (pas le label déjà
    # stocké en base, qui reflète les seuils actifs AU MOMENT de la prédiction)
    bucket_n  = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    bucket_ok = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}

    # V19.14 — _load_settled_rows() SELECT 11 colonnes (la 11e, NULL, pour
    # predicted_outcome — voir sa docstring). _simulate() n'en déballait que
    # 10 : ÇA FAISAIT PLANTER /recalibrer sur un ValueError à chaque exécution
    # dès qu'il y avait assez de lignes réglées pour tenter un backtest —
    # confirmé en le reproduisant sur les données réelles avant ce correctif.
    for (ph, pd, pa, p_btts, p_ou25, _modal, _conf_label, conf_pct,
         rh, ra, _predicted_outcome) in rows:
        ph = ph or 0.0
        pd = pd or 0.0
        pa = pa or 0.0

        mh = max(0.0, ph * cfg.prob_multiplier_home)
        md = max(0.0, pd * cfg.prob_multiplier_draw)
        ma = max(0.0, pa * cfg.prob_multiplier_away)
        total = mh + md + ma
        if total <= 0:
            mh, md, ma = ph, pd, pa
        else:
            mh, md, ma = mh / total, md / total, ma / total

        draw_eff = md * cfg.draw_detection_factor
        outcome_map = {"home": mh, "draw": draw_eff, "away": ma}
        pred_1x2 = max(outcome_map, key=outcome_map.get)
        actual_1x2 = "home" if rh > ra else ("away" if ra > rh else "draw")
        is_correct_1x2 = pred_1x2 == actual_1x2
        correct_1x2 += int(is_correct_1x2)

        ih = 1.0 if rh > ra else 0.0
        id_ = 1.0 if rh == ra else 0.0
        ia = 1.0 if ra > rh else 0.0
        brier_total += (mh - ih) ** 2 + (md - id_) ** 2 + (ma - ia) ** 2

        pred_btts = (p_btts or 0.0) >= cfg.btts_threshold
        actual_btts = rh >= 1 and ra >= 1
        correct_btts += int(pred_btts == actual_btts)

        pred_ou25 = (p_ou25 or 0.0) >= cfg.ou25_threshold
        actual_ou25 = (rh + ra) > 2
        correct_ou25 += int(pred_ou25 == actual_ou25)

        conf_pct = conf_pct or 0.0
        if conf_pct >= cfg.confidence_high_threshold:
            b = "HIGH"
        elif conf_pct >= cfg.confidence_medium_threshold:
            b = "MEDIUM"
        else:
            b = "LOW"
        bucket_n[b] += 1
        bucket_ok[b] += int(is_correct_1x2)

    def _bucket_acc(name: str) -> float | None:
        return round(bucket_ok[name] / bucket_n[name], 4) if bucket_n[name] >= _MIN_BUCKET_N else None

    return SimMetrics(
        n=n,
        accuracy_1x2=round(correct_1x2 / n, 4),
        brier_1x2=round(brier_total / n, 4),
        accuracy_btts=round(correct_btts / n, 4),
        accuracy_ou25=round(correct_ou25 / n, 4),
        n_high=bucket_n["HIGH"], acc_high=_bucket_acc("HIGH"),
        n_medium=bucket_n["MEDIUM"], acc_medium=_bucket_acc("MEDIUM"),
        n_low=bucket_n["LOW"], acc_low=_bucket_acc("LOW"),
    )


def _check_regressions(active_m: SimMetrics, candidate_m: SimMetrics, label: str) -> list[str]:
    """
    Compare deux SimMetrics (actif vs candidat) sur un même lot et retourne
    la liste des régressions détectées, préfixées par `label` (ex: "holdout",
    "fenêtre 2/3"). Mêmes tolérances de bruit que la porte de décision
    principale — cette fonction est la seule source de vérité pour "est-ce
    une régression", qu'on l'applique à l'agrégat ou à une fenêtre walk-forward.
    """
    regressions: list[str] = []
    if candidate_m.accuracy_1x2 < active_m.accuracy_1x2:
        regressions.append(
            f"précision 1X2 {label} {candidate_m.accuracy_1x2:.0%} "
            f"< {active_m.accuracy_1x2:.0%} actuelle"
        )
    if candidate_m.brier_1x2 > active_m.brier_1x2 + _EPS_BRIER:
        regressions.append(
            f"Brier {label} {candidate_m.brier_1x2:.3f} "
            f"> {active_m.brier_1x2:.3f} actuel"
        )
    if candidate_m.accuracy_btts < active_m.accuracy_btts - _EPS_SECONDARY:
        regressions.append(
            f"précision BTTS {label} {candidate_m.accuracy_btts:.0%} "
            f"< {active_m.accuracy_btts:.0%} actuelle"
        )
    if candidate_m.accuracy_ou25 < active_m.accuracy_ou25 - _EPS_SECONDARY:
        regressions.append(
            f"précision O/U 2.5 {label} {candidate_m.accuracy_ou25:.0%} "
            f"< {active_m.accuracy_ou25:.0%} actuelle"
        )
    # V19.14 — signal de régression dédié à la confiance : le but n'est pas
    # que HIGH/MEDIUM/LOW soient beaux, c'est qu'ils RESTENT au moins aussi
    # discriminants qu'avant (HIGH plus fiable que LOW). Comparé seulement
    # quand les deux configs ont assez d'échantillons dans les deux paliers
    # pour que la comparaison ait un sens — sinon on ne peut ni valider ni
    # invalider, donc on ne bloque pas sur du bruit.
    if active_m.discrimination is not None and candidate_m.discrimination is not None:
        if candidate_m.discrimination < active_m.discrimination - _EPS_SECONDARY:
            regressions.append(
                f"discrimination de confiance {label} "
                f"(HIGH−LOW={candidate_m.discrimination:+.0%}) moins nette "
                f"qu'actuellement ({active_m.discrimination:+.0%})"
            )
    return regressions


def _describe_changes(active, candidate) -> list[str]:
    """Liste lisible des paramètres qui changeraient si le candidat était appliqué."""
    changes: list[str] = []
    for field_name in _TUNABLE_FIELDS:
        old_v = getattr(active, field_name)
        new_v = getattr(candidate, field_name)
        if abs(old_v - new_v) > 1e-9:
            label = _FIELD_LABELS.get(field_name, field_name)
            changes.append(f"{label} : {old_v:.3f} → {new_v:.3f}")
    return changes


# ── Recherche des seuils de confiance (V19.14) ────────────────────────────────

# Grille de recherche : pas de 2 points, plage large pour ne pas présupposer
# où se trouve la bonne coupure — c'est justement ce qui manquait (l'ancienne
# heuristique ne faisait que monter un seuil déjà fixé, jamais le descendre,
# donc un seuil mal placé au départ restait mal placé pour toujours).
_CONF_GRID = list(range(30, 86, 2))


def _propose_confidence_thresholds(
    calib_rows: list[tuple], base_cfg,
) -> tuple[float, float]:
    """
    Cherche, sur le lot CALIBRATION uniquement (jamais le holdout), la paire
    (seuil HIGH, seuil MEDIUM) qui maximise la discrimination HIGH-LOW tout
    en gardant chaque palier peuplé d'au moins `_MIN_BUCKET_N` échantillons.

    Si aucune paire ne fait mieux que la config active (ou si les données
    sont trop rares pour juger), renvoie les seuils actuels — le silence
    est le comportement sûr, pas une paire choisie au hasard.
    """
    n = len(calib_rows)
    if n < _MIN_BUCKET_N * 3:
        return base_cfg.confidence_high_threshold, base_cfg.confidence_medium_threshold

    # confidence_pct est à l'index 7 de chaque ligne (voir _load_settled_rows)
    conf_pcts = [(row[7] or 0.0) for row in calib_rows]
    corrects  = []
    for (ph, pd, pa, _btts, _ou25, _modal, _lbl, _pct, rh, ra, _pred) in calib_rows:
        actual = "home" if rh > ra else ("away" if ra > rh else "draw")
        probs = {"home": ph or 0.0, "draw": pd or 0.0, "away": pa or 0.0}
        predicted = max(probs, key=probs.get)
        corrects.append(int(predicted == actual))

    best = (base_cfg.confidence_high_threshold, base_cfg.confidence_medium_threshold)
    best_score = -1.0  # toute paire valide (>=0 discrimination mesurable) la bat

    for high_th in _CONF_GRID:
        for med_th in _CONF_GRID:
            if med_th >= high_th:
                continue
            n_high = n_med = n_low = 0
            ok_high = ok_med = ok_low = 0
            for pct, ok in zip(conf_pcts, corrects):
                if pct >= high_th:
                    n_high += 1; ok_high += ok
                elif pct >= med_th:
                    n_med += 1; ok_med += ok
                else:
                    n_low += 1; ok_low += ok
            if n_high < _MIN_BUCKET_N or n_low < _MIN_BUCKET_N:
                continue
            acc_high = ok_high / n_high
            acc_low  = ok_low / n_low
            score = acc_high - acc_low
            if score > best_score:
                best_score = score
                best = (float(high_th), float(med_th))

    return best


# ── Point d'entrée public ─────────────────────────────────────────────────────

def run_auto_learning() -> AutoLearningReport:
    """
    Cycle complet d'auto-apprentissage sécurisé : analyse → propose →
    backteste sur du holdout jamais vu → n'applique QUE si prouvé
    non-régressif. Retourne un AutoLearningReport détaillé.

    Ne lève jamais d'exception métier : toute donnée insuffisante ou erreur
    de calcul se traduit par `attempted=False` et un message explicite, en
    laissant la calibration active totalement inchangée.
    """
    report = AutoLearningReport()

    active_cfg = calibration_module.load_calibration()
    report.active_version = active_cfg.version

    rows = _load_settled_rows()
    report.n_total = len(rows)

    if report.n_total < (_MIN_HOLDOUT + _MIN_CALIB_ROWS):
        report.reason = (
            f"Pas assez de prédictions réglées ({report.n_total}) pour "
            f"proposer une auto-amélioration en toute sécurité (besoin de "
            f"{_MIN_HOLDOUT + _MIN_CALIB_ROWS}). Continue à saisir des "
            "résultats via /resultat."
        )
        return report

    calib_rows, holdout_rows = _temporal_split(rows)
    if len(holdout_rows) < _MIN_HOLDOUT:
        report.reason = (
            f"Le lot holdout est trop petit ({len(holdout_rows)}) pour "
            f"valider un candidat sans risque. Il en faut au moins "
            f"{_MIN_HOLDOUT}."
        )
        return report

    report.n_calibration = len(calib_rows)
    report.n_holdout      = len(holdout_rows)
    report.attempted      = True

    # ── 1. Proposer un candidat à partir du lot calibration UNIQUEMENT ──────
    calib_report = learning_module.analyse_errors(rows=calib_rows)
    candidate_cfg = calibration_module.compute_calibration(calib_report)
    # xg_global_multiplier ne peut pas être rejoué a posteriori (il agit sur
    # le xG, avant la simulation Monte-Carlo) : on le fige à sa valeur
    # active pour ne jamais l'ajuster sans validation humaine explicite.
    candidate_cfg.xg_global_multiplier = active_cfg.xg_global_multiplier

    # V19.14 — remplace le réglage un-directionnel des seuils de confiance
    # (compute_calibration ne faisait que les monter, jamais les descendre)
    # par une recherche par grille sur le lot calibration, qui vise
    # explicitement à maximiser l'écart HIGH-LOW. Le résultat passe ensuite
    # par le MÊME backtest holdout que tout le reste : si la paire trouvée
    # ne tient pas sur des données jamais vues, elle est rejetée comme
    # n'importe quel autre changement.
    candidate_cfg.confidence_high_threshold, candidate_cfg.confidence_medium_threshold = (
        _propose_confidence_thresholds(calib_rows, active_cfg)
    )

    changes = _describe_changes(active_cfg, candidate_cfg)
    report.changes = changes

    if not changes:
        report.reason = (
            "Aucun ajustement pertinent détecté par rapport à la "
            "calibration active — rien à améliorer pour l'instant."
        )
        return report

    # ── 2. Rejouer ACTIF et CANDIDAT sur le MÊME holdout jamais vu ──────────
    active_metrics    = _simulate(holdout_rows, active_cfg)
    candidate_metrics = _simulate(holdout_rows, candidate_cfg)
    report.active_metrics    = active_metrics
    report.candidate_metrics = candidate_metrics

    # ── 3. Porte de non-régression stricte sur l'agrégat holdout complet ───
    regressions = _check_regressions(active_metrics, candidate_metrics, "holdout")

    # ── 3bis. Walk-forward (V19) ─────────────────────────────────────────────
    # Un split unique peut, par hasard, tomber sur une tranche récente non
    # représentative. Quand le holdout est assez grand, on le redécoupe en
    # plusieurs fenêtres chronologiques distinctes et on exige l'absence de
    # régression sur CHACUNE d'elles, pas seulement en moyenne — un candidat
    # qui compense une mauvaise fenêtre par une bonne n'est pas assez stable
    # pour être appliqué automatiquement. Avec un holdout trop petit pour
    # des fenêtres fiables, `folds` ne contient que le holdout entier (déjà
    # couvert ci-dessus) et cette étape n'ajoute rien de plus.
    folds = _fold_split(holdout_rows)
    report.n_folds = len(folds)
    if len(folds) > 1:
        for i, fold_rows in enumerate(folds, start=1):
            fold_active_m    = _simulate(fold_rows, active_cfg)
            fold_candidate_m = _simulate(fold_rows, candidate_cfg)
            report.fold_regressions.extend(
                _check_regressions(
                    fold_active_m, fold_candidate_m, f"fenêtre {i}/{len(folds)}"
                )
            )

    all_regressions = regressions + report.fold_regressions

    if all_regressions:
        report.accepted = False
        streak = active_cfg.consecutive_rejections + 1
        report.rejection_streak = streak
        report.streak_warning = streak >= _STREAK_WARNING_THRESHOLD
        calibration_module.update_rejection_streak(streak)
        report.reason = (
            "Candidat rejeté — régression détectée : "
            + "; ".join(all_regressions)
            + ". La calibration active est conservée à l'identique."
        )
        logger.info("[auto_learning] candidate rejected: %s", report.reason)
        return report

    # ── 4. Candidat prouvé non-régressif → application + versionnage ────────
    candidate_cfg.n_samples = calib_report.n_settled
    candidate_cfg.consecutive_rejections = 0
    calibration_module.save_calibration(candidate_cfg)
    report.accepted    = True
    report.rejection_streak = 0
    report.new_version = candidate_cfg.version
    fold_note = (
        f" sur {report.n_folds} fenêtres walk-forward" if report.n_folds > 1 else ""
    )
    report.reason = (
        "Candidat accepté — pas de régression sur le holdout jamais vu "
        f"({len(holdout_rows)} prédictions{fold_note}), amélioration ou "
        "stabilité confirmée."
    )
    logger.info(
        "[auto_learning] candidate accepted -> v%d (%s)",
        candidate_cfg.version, changes,
    )
    return report


# ── Formatage Telegram ────────────────────────────────────────────────────────

def format_auto_learning_report(report: AutoLearningReport) -> str:
    """Formatte un AutoLearningReport pour l'affichage Telegram (HTML)."""
    lines: list[str] = ["🔒 <b>Auto-amélioration sécurisée</b>"]

    if not report.attempted:
        lines.append(f"  ⏳ {report.reason}")
        return "\n".join(lines)

    lines.append(
        f"  📊 {report.n_total} réglées — "
        f"{report.n_calibration} calibration / {report.n_holdout} holdout <i>(jamais vu)</i>"
    )

    if report.accepted:
        lines.append(f"  ✅ <b>Amélioration appliquée</b> → v{report.new_version}")
    else:
        lines.append("  🛡️ <b>Aucun changement appliqué</b> (candidat non-concluant ou rejeté)")

    if report.changes:
        lines.append("\n  🔧 <b>Changement(s) proposé(s)</b>")
        for c in report.changes:
            lines.append(f"    • {c}")

    am, cm = report.active_metrics, report.candidate_metrics
    if am and cm:
        holdout_label = (
            f"Sur le holdout jamais vu ({report.n_folds} fenêtres walk-forward)"
            if report.n_folds > 1 else "Sur le holdout jamais vu"
        )
        lines.append(f"\n  🔬 <b>{holdout_label}</b>")
        lines.append(
            f"    Actif    — 1X2 {am.accuracy_1x2:.0%}  Brier {am.brier_1x2:.3f}"
            f"  BTTS {am.accuracy_btts:.0%}  O/U {am.accuracy_ou25:.0%}"
        )
        lines.append(
            f"    Candidat — 1X2 {cm.accuracy_1x2:.0%}  Brier {cm.brier_1x2:.3f}"
            f"  BTTS {cm.accuracy_btts:.0%}  O/U {cm.accuracy_ou25:.0%}"
        )

    if report.fold_regressions:
        lines.append("\n  📐 <b>Instabilité détectée sur au moins une fenêtre</b>")
        for r in report.fold_regressions:
            lines.append(f"    • {r}")

    if report.streak_warning:
        lines.append(
            f"\n  ⚠️ <b>{report.rejection_streak} rejets consécutifs</b> — "
            "signal possible de plafonnement du modèle ou de dérive des "
            "données ; une revue manuelle peut être utile."
        )

    lines.append(f"\n  💡 <i>{report.reason}</i>")
    return "\n".join(lines)
