"""
engine/learning_v18.py — V18 : Apprentissage profond et catalogue de scénarios.

Analyse les 88 prédictions réglées pour :
1. Identifier les 5 types de scénarios d'erreur dominants.
2. Calculer des valeurs de calibration optimales basées sur les données réelles.
3. Construire un catalogue de scénarios pour l'anticipation future.
4. Écrire une nouvelle calibration V18 qui améliore BTTS, O/U et 1X2
   sans jamais dégrader ce qui fonctionne déjà.

RÈGLE ANTI-RÉGRESSION :
- Aucun paramètre n'est changé sans preuve statistique sur les données réelles.
- Le draw_detection_factor global n'est PAS boosted : tests empiriques montrent
  qu'un boost global crée plus de faux draws que de vrais catches.
- La correction draw est CIBLÉE : uniquement draw_prob > 0.28 (zone où les
  vrais nuls ont une sur-représentation mesurée de 34%).

Aucun code Telegram. Aucun appel API.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

import config

logger = logging.getLogger(__name__)

_DB_FILENAME = "predictions.db"
_SCENARIO_CATALOG_FILE = "scenario_catalog_v18.json"
_MIN_SAMPLES = 30  # minimum absolu pour recalibrer


# ── Scénarios d'erreur identifiés sur les 88 matchs ─────────────────────────

SCENARIO_TYPES = {
    "DRAW_EQUILIBRIUM": {
        "description": "Match ultra-serré: les 3 probs sont proches (aucune > 0.48)",
        "detection": lambda h, d, a: max(h, a) < 0.48 and d > 0.20,
        "error_rate_observed": 0.62,  # 62% d'erreur sur ces matchs
        "draw_rate_observed": 0.38,   # 38% de vrais nuls dans ce scénario
        "correction": "draw_boost_conditional",
    },
    "HOME_OVERCONFIDENCE": {
        "description": "Domicile donné à 0.44-0.55 mais sous-perf fréquente",
        "detection": lambda h, d, a: 0.44 <= h <= 0.55 and a < 0.35,
        "error_rate_observed": 0.48,
        "correction": "slight_home_cut",
    },
    "HIGH_XG_LOW_SCORE": {
        "description": "xG total > 3.0 → buts réels souvent <= 2 (surestimation offensive)",
        "detection": lambda h, d, a: False,  # détecté via xg dans le prédicteur
        "error_rate_observed": 0.59,  # 17/88 matches avec xG>3 et buts<=1
        "correction": "xg_multiplier_cut",
    },
    "OUTSIDER_SURGE": {
        "description": "Outsider (max_prob < 0.45) gagne contre toute attente",
        "detection": lambda h, d, a: max(h, d, a) < 0.45,
        "error_rate_observed": 0.70,  # 35 matchs, 70% d'erreur
        "correction": "increase_uncertainty",
    },
    "BTTS_OVERESTIMATE": {
        "description": "BTTS prédit à 0.70+ mais seulement 55% réels à ce niveau",
        "detection": lambda h, d, a: False,  # détecté via btts_prob
        "error_rate_observed": 0.45,
        "correction": "btts_threshold_raise",
    },
}


@dataclass
class ScenarioCatalogEntry:
    """Un pattern d'erreur capturé depuis les données réelles."""
    scenario_type: str
    home_name: str
    away_name: str
    home_prob: float
    draw_prob: float
    away_prob: float
    btts_prob: float
    over25_prob: float
    home_xg: float
    away_xg: float
    predicted: str      # H / D / A
    actual: str         # H / D / A
    score: str          # "2-1"
    confidence_pct: float
    lesson: str         # Ce que ce match enseigne


@dataclass
class V18CalibrationValues:
    """
    Snapshot figé d'une mesure ponctuelle faite une fois sur 88 matchs (voir
    les commentaires ci-dessous, datés). SEUL `xg_global_multiplier` est
    recalculé à chaque appel de run_v18_analysis() à partir des données
    actuelles (voir plus bas) — tout le reste ici n'est PAS recalculé et ne
    doit jamais être écrit dans calibration.json : /recalibrer (auto_learning.py)
    est le seul chemin qui recalcule et backteste ces valeurs sur les
    données à jour avant de les appliquer. apply_v18_calibration() ci-dessous
    ne les utilise donc plus.
    """
    # Correction xG — mesuré à l'époque: H surestimé de +0.139, A de +0.179
    xg_global_multiplier: float = 0.90

    # V19.14 — figé (snapshot 88 matchs), non recalculé, non appliqué
    btts_threshold: float = 0.65
    ou25_threshold: float = 0.58
    confidence_high_threshold: float = 60.0
    confidence_medium_threshold: float = 44.0
    draw_detection_factor: float = 1.45
    draw_high_zone_factor: float = 1.75
    prob_multiplier_home: float = 0.97
    prob_multiplier_away: float = 1.03
    prob_multiplier_draw: float = 1.08

    # Méta
    n_samples_used: int = 88
    accuracy_at_calibration: float = 0.432
    version_tag: str = "V18"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class V18LearningReport:
    """Rapport complet de l'apprentissage V18."""
    n_settled: int = 0
    accuracy_1x2: float = 0.0
    accuracy_btts: float = 0.0
    accuracy_over25: float = 0.0

    # Distribution réelle mesurée
    real_home_rate: float = 0.0
    real_draw_rate: float = 0.0
    real_away_rate: float = 0.0

    # Biais xG mesuré
    xg_bias_home: float = 0.0
    xg_bias_away: float = 0.0
    xg_correction_factor: float = 0.0

    # Erreurs par type
    errors_predicted_home: int = 0
    errors_predicted_away: int = 0
    errors_predicted_draw: int = 0
    draws_missed: int = 0

    # Catalogue de scénarios
    scenario_catalog: list[ScenarioCatalogEntry] = field(default_factory=list)
    scenario_type_counts: dict[str, int] = field(default_factory=dict)

    # Valeurs de calibration recommandées
    recommended_calibration: V18CalibrationValues = field(
        default_factory=V18CalibrationValues
    )

    # Texte de synthèse
    summary_lines: list[str] = field(default_factory=list)


def _db_path() -> str:
    return os.path.join(config.WEB_CACHE_DIR, _DB_FILENAME)


def _catalog_path() -> str:
    os.makedirs(config.WEB_CACHE_DIR, exist_ok=True)
    return os.path.join(config.WEB_CACHE_DIR, _SCENARIO_CATALOG_FILE)


def _classify_scenario(h: float, d: float, a: float, hxg: float, axg: float,
                        btts: float) -> str:
    """Classe un match dans un des scénarios V18."""
    if max(h, a) < 0.48 and d > 0.20:
        return "DRAW_EQUILIBRIUM"
    if max(h, d, a) < 0.45:
        return "OUTSIDER_SURGE"
    if 0.44 <= h <= 0.55 and a < 0.35:
        return "HOME_OVERCONFIDENCE"
    if (hxg + axg) > 3.0:
        return "HIGH_XG_LOW_SCORE"
    if btts > 0.68:
        return "BTTS_OVERESTIMATE"
    return "STANDARD"


def _lesson_for(scenario: str, predicted: str, actual: str,
                h: float, d: float, a: float) -> str:
    lessons = {
        "DRAW_EQUILIBRIUM": (
            f"Match 3-way: aucune équipe dominante. "
            f"Probs proches ({h:.2f}/{d:.2f}/{a:.2f}) → "
            f"prédit {predicted}, résultat {actual}. "
            "Dans ces configs le nul est structurellement sous-estimé."
        ),
        "OUTSIDER_SURGE": (
            f"Outsider imprévu: max_prob={max(h,d,a):.2f}. "
            f"Prédit {predicted}, réel {actual}. "
            "Ces matchs sont à haute variance — la confiance doit rester LOW."
        ),
        "HOME_OVERCONFIDENCE": (
            f"Domicile à {h:.2f} mais résultat {actual}. "
            "L'avantage domicile est surestimé dans certaines ligues secondaires."
        ),
        "HIGH_XG_LOW_SCORE": (
            f"xG offensif élevé mais score réel bas. "
            "Le moteur xG exagère les chances face à des défenses solides."
        ),
        "BTTS_OVERESTIMATE": (
            "BTTS prédit YES à prob élevée mais les deux équipes n'ont pas marqué."
        ),
        "STANDARD": f"Prédit {predicted}, réel {actual}.",
    }
    return lessons.get(scenario, f"Prédit {predicted}, réel {actual}.")


def run_v18_analysis() -> V18LearningReport:
    """
    Analyse complète V18 sur toutes les prédictions réglées.
    Retourne un V18LearningReport avec catalogue de scénarios et calibration.
    """
    report = V18LearningReport()

    db = _db_path()
    if not os.path.exists(db):
        logger.warning("learning_v18: no DB at %s", db)
        report.summary_lines.append("❌ Base de données introuvable.")
        return report

    conn = sqlite3.connect(db)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT home_name, away_name, league,
                   home_win_prob, draw_prob, away_win_prob,
                   btts_prob, over25_prob,
                   home_xg, away_xg,
                   result_home, result_away,
                   confidence_pct, confidence_label
            FROM predictions
            WHERE settled = 1
              AND result_home IS NOT NULL
              AND result_away IS NOT NULL
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    if len(rows) < _MIN_SAMPLES:
        report.summary_lines.append(
            f"⚠️ Seulement {len(rows)} prédictions réglées "
            f"(minimum {_MIN_SAMPLES} requis)."
        )
        return report

    report.n_settled = len(rows)

    # V19.14 — jusqu'ici cette analyse recalculait "ce qu'aurait prédit le
    # modèle" avec des constantes figées à l'époque de la V18 originale
    # (facteur nul 1.45, seuils BTTS/O2.5 0.56/0.54), sans jamais regarder ce
    # que /recalibrer a pu valider depuis. Un rapport qui ignore la
    # calibration RÉELLEMENT active mesure la fiabilité d'un bot qui n'existe
    # plus, puis en tire des "corrections" par rapport à cette version
    # fictive. On charge donc la calibration active pour ce calcul.
    try:
        from engine.calibration import load_calibration
        _calib = load_calibration()
        draw_factor, btts_th, ou25_th = (
            _calib.draw_detection_factor, _calib.btts_threshold, _calib.ou25_threshold,
        )
    except Exception:
        draw_factor, btts_th, ou25_th = 1.45, 0.56, 0.54

    correct_1x2 = correct_btts = correct_over = 0
    home_wins = draws = away_wins = 0
    xg_bias_h_total = xg_bias_a_total = 0.0
    catalog: list[ScenarioCatalogEntry] = []
    scenario_counts: dict[str, int] = {}
    scenario_errors: dict[str, int] = {}

    for row in rows:
        (home_name, away_name, league,
         ph, pd, pa, pbtts, pover,
         hxg, axg,
         rh, ra,
         conf_pct, conf_label) = row

        rh, ra = int(rh), int(ra)

        # Résultat réel
        if rh > ra:
            actual = "H"; home_wins += 1
        elif rh == ra:
            actual = "D"; draws += 1
        else:
            actual = "A"; away_wins += 1

        # Pronostic bot actuel (calibration réellement active, pas figée V18)
        probs = {"H": ph, "D": pd * draw_factor, "A": pa}
        predicted = max(probs, key=probs.get)
        # Pour le catalogue, utiliser la décision brute (sans factor draw)
        raw_pred = max({"H": ph, "D": pd, "A": pa}, key={"H": ph, "D": pd, "A": pa}.get)

        correct = predicted == actual
        if correct:
            correct_1x2 += 1

        # BTTS
        btts_actual = rh > 0 and ra > 0
        btts_pred = pbtts >= btts_th
        if btts_actual == btts_pred:
            correct_btts += 1

        # Over2.5
        over_actual = (rh + ra) > 2
        over_pred = pover >= ou25_th
        if over_actual == over_pred:
            correct_over += 1

        # xG bias
        xg_bias_h_total += hxg - rh
        xg_bias_a_total += axg - ra

        # Classification scénario
        scenario = _classify_scenario(ph, pd, pa, hxg, axg, pbtts)
        scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1
        if not correct:
            scenario_errors[scenario] = scenario_errors.get(scenario, 0) + 1

        # Catalogue — on enregistre TOUS les scénarios (erreurs + corrects pour patterns)
        score_str = f"{rh}-{ra}"
        lesson = _lesson_for(scenario, predicted if not correct else "✓", actual, ph, pd, pa)
        entry = ScenarioCatalogEntry(
            scenario_type=scenario,
            home_name=home_name,
            away_name=away_name,
            home_prob=ph,
            draw_prob=pd,
            away_prob=pa,
            btts_prob=pbtts,
            over25_prob=pover,
            home_xg=hxg,
            away_xg=axg,
            predicted=predicted,
            actual=actual,
            score=score_str,
            confidence_pct=conf_pct or 0.0,
            lesson=lesson,
        )
        catalog.append(entry)

    n = report.n_settled
    report.accuracy_1x2 = correct_1x2 / n
    report.accuracy_btts = correct_btts / n
    report.accuracy_over25 = correct_over / n
    report.real_home_rate = home_wins / n
    report.real_draw_rate = draws / n
    report.real_away_rate = away_wins / n
    report.xg_bias_home = xg_bias_h_total / n
    report.xg_bias_away = xg_bias_a_total / n

    # Correction multiplicateur xG
    # Biais moyen global = (bias_h + bias_a) / 2
    # On réduit le multiplier proportionnellement
    avg_pred_goals = sum(
        (r[8] + r[9]) for r in rows
    ) / (2 * n)
    avg_real_goals = sum(
        (int(r[10]) + int(r[11])) for r in rows
    ) / (2 * n)
    if avg_pred_goals > 0:
        report.xg_correction_factor = avg_real_goals / avg_pred_goals
    else:
        report.xg_correction_factor = 1.0

    report.scenario_catalog = catalog
    report.scenario_type_counts = scenario_counts

    # Erreurs prédites
    wrong = [e for e in catalog if e.predicted != e.actual]
    report.errors_predicted_home = sum(1 for e in wrong if e.predicted == "H")
    report.errors_predicted_away = sum(1 for e in wrong if e.predicted == "A")
    report.errors_predicted_draw = sum(1 for e in wrong if e.predicted == "D")
    report.draws_missed = sum(1 for e in wrong if e.actual == "D")

    # Calibration recommandée — SEUL xg_global_multiplier est recalculé ici
    # (voir docstring de V18CalibrationValues) ; le reste n'est ni recalculé
    # ni appliqué par cette commande.
    calib = V18CalibrationValues()
    # xG: corrige vers la valeur mesurée mais ne dépasse pas les bornes [0.80, 1.00]
    calib.xg_global_multiplier = round(
        max(0.80, min(1.00, report.xg_correction_factor)), 4
    )
    report.recommended_calibration = calib

    # Résumé textuel
    n_errors = n - correct_1x2
    report.summary_lines = [
        f"📊 **Analyse V18 — {n} matchs réglés**",
        "",
        f"🎯 Précision 1X2 actuelle : **{correct_1x2}/{n} = {report.accuracy_1x2*100:.1f}%**",
        f"⚽ BTTS (seuil actif {btts_th:.2f}) : {correct_btts}/{n} = {report.accuracy_btts*100:.1f}%",
        f"📈 Over2.5 (seuil actif {ou25_th:.2f}) : {correct_over}/{n} = {report.accuracy_over25*100:.1f}%",
        "",
        f"📐 Distribution réelle : {home_wins}H ({report.real_home_rate*100:.1f}%) "
        f"| {draws}D ({report.real_draw_rate*100:.1f}%) "
        f"| {away_wins}A ({report.real_away_rate*100:.1f}%)",
        "",
        f"🔬 **Biais xG mesuré (sur ces {n} matchs)** :",
        f"  • Domicile : +{report.xg_bias_home:.3f} buts/match (surestimation)",
        f"  • Extérieur : +{report.xg_bias_away:.3f} buts/match (surestimation)",
        f"  • Suggestion xg_multiplier : **{calib.xg_global_multiplier:.2f}** "
        f"— <i>informatif seulement, voir note ci-dessous</i>",
        "",
        f"❌ **{n_errors} erreurs 1X2 — répartition** :",
        f"  • Prédit H mais faux : {report.errors_predicted_home}",
        f"  • Prédit A mais faux : {report.errors_predicted_away}",
        f"  • Nuls manqués : {report.draws_missed}/20 (22.7% réels)",
        "",
        "🗂️ **Scénarios d'erreur dominants** :",
    ]

    for stype, count in sorted(
        scenario_counts.items(), key=lambda x: -scenario_errors.get(x[0], 0)
    ):
        err = scenario_errors.get(stype, 0)
        rate = err / count * 100 if count else 0
        desc = SCENARIO_TYPES.get(stype, {}).get("description", stype)
        report.summary_lines.append(
            f"  • {stype}: {count} matchs, {err} erreurs ({rate:.0f}%) — {desc}"
        )

    # V19.14 — cette commande n'écrit plus RIEN dans calibration.json.
    # xg_global_multiplier ne peut pas être backtesté sur des probabilités
    # déjà enregistrées (il agit avant la simulation Monte-Carlo — voir
    # auto_learning.py) donc il reste sous contrôle humain exclusif ; tous
    # les autres champs ci-dessus (BTTS, O/U, seuils de confiance,
    # multiplicateurs) sont un instantané figé d'une mesure ponctuelle
    # passée et NE SONT PAS recalculés par cette analyse — /recalibrer est
    # le seul chemin qui les recalcule et les backteste avant application.
    report.summary_lines += [
        "",
        "ℹ️ **Cette analyse est informative — rien n'est appliqué automatiquement.**",
        f"  • xg_global_multiplier suggéré : **{calib.xg_global_multiplier:.2f}** "
        "(non backtestable a posteriori — ajustement manuel uniquement)",
        "  • Pour BTTS / O-U / seuils de confiance / multiplicateurs : "
        "utilise /recalibrer, qui les recalcule et les valide sur des "
        "données jamais vues avant de les appliquer.",
        "",
        f"📁 Catalogue de {n} scénarios sauvegardé dans {_SCENARIO_CATALOG_FILE}",
    ]

    return report


def save_scenario_catalog(report: V18LearningReport) -> str:
    """Sauvegarde le catalogue de scénarios en JSON."""
    path = _catalog_path()
    data = {
        "version": "V18",
        "n_total": report.n_settled,
        "accuracy_1x2_at_save": round(report.accuracy_1x2, 4),
        "xg_bias_home": round(report.xg_bias_home, 4),
        "xg_bias_away": round(report.xg_bias_away, 4),
        "scenario_counts": report.scenario_type_counts,
        "entries": [asdict(e) for e in report.scenario_catalog],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("learning_v18: scenario catalog saved to %s (%d entries)", path, len(report.scenario_catalog))
    return path


def load_scenario_catalog() -> dict[str, Any]:
    """Charge le catalogue de scénarios V18 si disponible."""
    path = _catalog_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def apply_v18_calibration(report: V18LearningReport) -> bool:
    """
    V19.14 — DÉSACTIVÉ. Cette fonction écrivait auparavant DIRECTEMENT dans
    calibration.json, sans le moindre backtest sur données jamais vues,
    avec la plupart des champs (btts_threshold, ou25_threshold, seuils de
    confiance, multiplicateurs) copiés depuis un instantané figé d'une
    mesure ponctuelle passée sur 88 matchs — pas depuis un calcul sur les
    données actuelles. Elle forçait aussi systématiquement
    draw_detection_factor à 1.45, écrasant silencieusement toute valeur que
    /recalibrer aurait validée par ailleurs (holdout + walk-forward, voir
    auto_learning.py). C'était donc un chemin d'écriture non gardé qui
    pouvait annuler les progrès du chemin gardé — l'inverse de ce qu'un
    système qui ne doit jamais régresser doit faire.

    Ne fait plus rien d'autre que le journaliser. Toute recalibration passe
    désormais exclusivement par /recalibrer (engine.auto_learning), qui
    recalcule chaque champ à partir des données actuelles et ne l'applique
    que s'il est prouvé au moins aussi bon sur un lot jamais vu.
    """
    logger.info(
        "learning_v18: apply_v18_calibration() appelée mais désactivée "
        "(V19.14) — aucune écriture. Utilisez /recalibrer."
    )
    return False


def format_v18_report_telegram(report: V18LearningReport) -> str:
    """Formate le rapport V18 pour un message Telegram (Markdown)."""
    return "\n".join(report.summary_lines)
