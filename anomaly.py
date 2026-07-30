"""
engine/anomaly.py — V20 : Détection d'anomalies pré-match.

Objectif
--------
Avant d'afficher un pronostic, signaler les cas où le contexte du match
ressemble à des situations passées qui ont mal tourné pour le modèle, ou où
la cote du bookmaker s'écarte fortement de la probabilité du modèle. Ce
module ne bloque jamais un pronostic et ne le modifie pas : il ajoute des
avertissements informatifs, à charge pour l'utilisateur d'en tenir compte.

Ce que ce module NE fait PAS
-----------------------------
Il ne prétend pas détecter des anomalies « statistiques » sur des variables
non collectées par ce bot (possession, tirs cadrés...). Il combine trois
signaux réellement disponibles :

  1. Similarité historique — via engine.history_query, le taux réel de
     pronostics corrects sur des matchs passés à confiance/xG comparables.
     Si ce sous-ensemble historique a un taux d'échec nettement supérieur
     à la moyenne globale, c'est signalé.
  2. Écart cote vs modèle — si la probabilité implicite du bookmaker
     s'écarte fortement de la probabilité du modèle sur l'issue favorite,
     c'est signalé (dans un sens ou dans l'autre : ça peut vouloir dire
     que le modèle se trompe, ou que la cote est stale/mal calibrée — ce
     module ne tranche pas lequel).
  3. Instabilité de la simulation — un nombre élevé de scénarios de score
     distincts ou une erreur standard de convergence élevée signale que le
     Monte-Carlo lui-même n'a pas convergé vers un scénario dominant clair.

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from engine import history_query
from engine.leagues import LeagueInfo, _TIER_CONF_PENALTY

logger = logging.getLogger(__name__)

# ── Seuils (volontairement prudents — révisables via retour d'expérience) ────
_HISTORY_FAIL_RATE_ALERT   = 0.55  # taux d'échec 1X2 sur le sous-ensemble similaire
_ODDS_MODEL_GAP_ALERT      = 0.15  # écart absolu de probabilité (15 points)
# V20.1 — `distinct_scorelines` et `mc_convergence_se` ont été testés en
# pipeline complet sur des matchs représentatifs (équilibré, favori net,
# match fermé, match offensif, écrasant) et se sont révélés de mauvais
# signaux : `distinct_scorelines` varie surtout avec le nombre de buts
# attendus (40 à 90+ sur des matchs parfaitement normaux), pas avec
# l'imprévisibilité réelle — il aurait déclenché l'alerte sur presque
# chaque match. `mc_convergence_se` reste quasi constant (~0.0002-0.001)
# quel que soit le match car la simulation tourne toujours à 100 000
# itérations — il ne discrimine rien. On utilise à la place la probabilité
# du scénario de score le plus probable (`top_scores[0][1]`) : une valeur
# normalisée 0-1, directement comparable entre matchs, qui reflète
# vraiment si un scénario domine ou si les probabilités sont étalées.
_LOW_TOP_SCENARIO_PROB      = 0.09  # aucun scénario ne dépasse 9% → très étalé


@dataclass
class AnomalyFlag:
    code:        str   # identifiant stable, ex. "historical_fail_rate"
    severity:    str   # "info" | "warning"
    message:     str   # texte prêt à afficher


@dataclass
class AnomalyReport:
    flags:                list[AnomalyFlag] = field(default_factory=list)
    similar_matches_n:     int   = 0
    similar_matches_fail:  float = 0.0  # taux d'échec 1X2 sur l'historique similaire

    @property
    def has_alerts(self) -> bool:
        return any(f.severity == "warning" for f in self.flags)


def detect_anomalies(
    *,
    league_name: str,
    confidence_pct: float,
    home_xg: float,
    away_xg: float,
    home_win_prob: float,
    draw_prob: float,
    away_win_prob: float,
    top_scenario_prob: float | None = None,
    bookmaker_home_prob: float | None = None,
    bookmaker_draw_prob: float | None = None,
    bookmaker_away_prob: float | None = None,
) -> AnomalyReport:
    """
    Calcule les signaux d'anomalie pour un match donné, à partir des
    éléments déjà produits par le pipeline de prédiction (aucun nouvel
    appel réseau, aucune donnée non collectée par ce bot).
    """
    report = AnomalyReport()

    # 1. Similarité historique ------------------------------------------------
    similar = history_query.similar_past_matches(
        league=league_name,
        confidence_pct=confidence_pct,
        home_xg=home_xg,
        away_xg=away_xg,
    )
    report.similar_matches_n = similar.n_matches
    report.similar_matches_fail = 1.0 - similar.accuracy_1x2
    if similar.data_sufficient and report.similar_matches_fail >= _HISTORY_FAIL_RATE_ALERT:
        report.flags.append(AnomalyFlag(
            code="historical_fail_rate",
            severity="warning",
            message=(
                f"⚠️ Sur {similar.n_matches} matchs passés à confiance/xG "
                f"comparables, le pronostic 1X2 s'est trompé "
                f"{report.similar_matches_fail*100:.0f}% du temps."
            ),
        ))
    elif similar.n_matches > 0 and not similar.data_sufficient:
        report.flags.append(AnomalyFlag(
            code="historical_sample_small",
            severity="info",
            message=(
                f"ℹ️ Seulement {similar.n_matches} matchs comparables dans "
                "l'historique — pas assez pour un signal fiable."
            ),
        ))

    # 2. Écart cote vs modèle --------------------------------------------------
    # V20.10 — trouvé en testant avec de VRAIES cotes injectées (V20.9 vient
    # de rendre ce chemin enfin atteignable) : comparer max(probs modèle) à
    # max(probs marché) peut totalement rater le cas le plus important —
    # celui où modèle et marché ne sont PAS d'accord sur QUI est favori.
    # Exemple réel rencontré : modèle favori = Extérieur à 52% ; marché
    # favori = Domicile à 54% implicite, mais le marché ne crédite
    # l'Extérieur (le favori du modèle) que de 24% — un écart de 28 points
    # sur l'issue qui compte, totalement invisible en comparant seulement
    # les deux maximums (52% vs 54% : quasi identiques, aucune alerte).
    # engine.value calcule déjà correctement cet écart par issue (market_edge)
    # — ce signal doit mesurer la même chose : la probabilité du marché pour
    # l'issue que LE MODÈLE favorise, pas le favori du marché lui-même.
    fav_outcome, fav_model_prob = max(
        ("home", home_win_prob), ("draw", draw_prob), ("away", away_win_prob),
        key=lambda pair: pair[1],
    )
    bookmaker_prob_for_model_fav = {
        "home": bookmaker_home_prob, "draw": bookmaker_draw_prob, "away": bookmaker_away_prob,
    }[fav_outcome]
    if bookmaker_prob_for_model_fav is not None and bookmaker_prob_for_model_fav > 0:
        gap = fav_model_prob - bookmaker_prob_for_model_fav
        if abs(gap) >= _ODDS_MODEL_GAP_ALERT:
            direction = "le modèle est plus confiant que le marché" if gap > 0 \
                else "le marché est plus confiant que le modèle"
            report.flags.append(AnomalyFlag(
                code="odds_model_gap",
                severity="warning",
                message=(
                    f"⚠️ Écart cote/modèle de {abs(gap)*100:.0f} points sur "
                    f"l'issue favorite du modèle ({direction}). Vérifier si la "
                    "cote est à jour avant de s'y fier."
                ),
            ))

    # 3. Dispersion réelle de la simulation (scénario dominant faible) -------
    if top_scenario_prob is not None and top_scenario_prob < _LOW_TOP_SCENARIO_PROB:
        report.flags.append(AnomalyFlag(
            code="scenario_dispersion",
            severity="info",
            message=(
                f"ℹ️ Le scénario de score le plus probable ne pèse que "
                f"{top_scenario_prob*100:.0f}% — les probabilités sont "
                "étalées sur beaucoup de scénarios, aucun ne domine "
                "vraiment."
            ),
        ))

    return report


def league_info_chaos_note(info: LeagueInfo) -> AnomalyFlag | None:
    """Signal informatif sur la fiabilité par tier.

    V20.6 — jusqu'ici cette note se basait sur une hypothèse fixe
    ("Tier 3 = moins fiable"), complètement indépendante de
    engine.league_calibration (V20.5), qui MESURE désormais la vraie
    pénalité de confiance par tier sur l'historique réel une fois assez de
    matchs réglés. `engine.leagues.classify()` peuple déjà `info.
    confidence_penalty` avec cette valeur apprise quand elle existe (repli
    sur la constante par défaut sinon) — ce module se contentait de
    l'ignorer et de retomber sur sa propre hypothèse figée, ce qui pouvait
    afficher "Tier 3, données moins fiables" alors que /recalibrerligues
    avait déjà mesuré autre chose de plus précis pour ce tier précis (par
    exemple une pénalité proche de 1.00 si ce tier s'avère finalement
    fiable, ou au contraire pire que 0.65 par défaut).

    Comportement :
    - Si une valeur APPRISE existe pour ce tier (différente de la
      constante par défaut) : toujours affichée, quel que soit le tier —
      c'est une information mesurée, pas une supposition.
    - Sinon (valeur par défaut encore active) : comportement identique à
      avant, uniquement pour tier >= 2 — pas de nouveau bruit introduit
      tant que rien n'a été mesuré.
    """
    default_penalty = _TIER_CONF_PENALTY.get(info.tier, 1.0)
    active_penalty  = info.confidence_penalty
    is_learned      = abs(active_penalty - default_penalty) > 1e-6

    if is_learned:
        sens = "réduite" if active_penalty < default_penalty else "revue à la hausse"
        return AnomalyFlag(
            code="low_tier_league",
            severity="info",
            message=(
                f"ℹ️ {info.name} — {info.tier_label}, confiance {sens} à "
                f"{active_penalty:.0%} d'après la précision réellement mesurée "
                f"sur ce tier (au lieu de {default_penalty:.0%} par défaut) — "
                f"voir /recalibrerligues."
            ),
        )

    if info.tier >= 2:
        return AnomalyFlag(
            code="low_tier_league",
            severity="info",
            message=(
                f"ℹ️ {info.name} — compétition {info.tier_label}, données "
                f"historiquement moins fiables (valeur par défaut, pas encore "
                f"assez de matchs réglés sur ce tier pour la mesurer — "
                f"voir /recalibrerligues)."
            ),
        )
    return None


def format_anomaly_report(report: AnomalyReport) -> str:
    if not report.flags:
        return "✅ Aucune anomalie détectée pour ce match."
    lines = ["🔍  <b>ANOMALIES DÉTECTÉES</b>", ""]
    for flag in report.flags:
        lines.append(f"  {flag.message}")
    return "\n".join(lines)
