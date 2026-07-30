"""
engine/scenarios.py — V15 : Scénarios Monte-Carlo plausibles.

Remplace le tirage aléatoire illustratif de V14 par trois scénarios
construits et narrativisés :

  1. Scénario principal    — le score le plus probable issu de la simulation.
  2. Scénario favorable    — meilleur cas pour le vainqueur prédit :
                             λ du favori boosté (+15 %), λ de l'adversaire
                             réduit (−12 %).
  3. Scénario défavorable plausible — retournement crédible :
                             λ du favori réduit (−25 %), λ de l'adversaire
                             boosté (+15 %). Le narratif précise pourquoi
                             ce scénario a déjà été observé
                             (H2H, chaos de ligue, faible écart de force).

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Sequence

from engine import montecarlo

logger = logging.getLogger(__name__)

# ── Constantes de modulation λ ───────────────────────────────────────────────
_FAV_BOOST    = 1.15   # favori dans le scénario favorable
_OPP_CUT      = 0.88   # adversaire dans le scénario favorable
_FAV_CUT      = 0.75   # favori dans le scénario défavorable
_OPP_BOOST    = 1.15   # adversaire dans le scénario défavorable

# Nombre de tirages pour les mini-simulations de scénarios
_SCENARIO_ITERS = 20_000


# ── Structures de données ────────────────────────────────────────────────────

@dataclass
class MatchScenario:
    label:        str              # "principal" | "favorable" | "défavorable plausible"
    score:        tuple[int, int]  # (home_goals, away_goals)
    probability:  float            # probabilité estimée de ce scoreline exact
    outcome:      str              # "home" | "draw" | "away"
    narrative:    str              # explication courte (1-2 phrases)


@dataclass
class ScenarioBundle:
    """Les trois scénarios d'un même match, avec le vainqueur prédit."""
    principal:       MatchScenario
    favorable:       MatchScenario
    adverse:         MatchScenario
    predicted_winner: str  # "home" | "draw" | "away"
    home_name:       str   = "Domicile"
    away_name:       str   = "Extérieur"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _outcome(hg: int, ag: int) -> str:
    if hg > ag:   return "home"
    if ag > hg:   return "away"
    return "draw"


def _top_score(
    lam_h: float,
    lam_a: float,
    chaos_level: float = 0.0,
    n: int = _SCENARIO_ITERS,
) -> tuple[tuple[int, int], float]:
    """Lance une mini-simulation et retourne le score modal + sa probabilité."""
    mc = montecarlo.simulate(
        lam_h, lam_a,
        n=n,
        chaos_level=chaos_level,
        max_iterations=n,
    )
    if mc.top_scores:
        score_str, prob = mc.top_scores[0]
        try:
            home_s, away_s = (int(x) for x in str(score_str).split("-", 1))
            return (home_s, away_s), prob
        except Exception:
            pass
    return mc.modal_score, 0.0


def _build_narrative_adverse(
    predicted_winner: str,
    home_name: str,
    away_name: str,
    home_index: float,
    away_index: float,
    chaos_level: float,
    h2h_scores: Sequence[tuple[int, int]],
    h2h_upsets: int,
) -> str:
    """Construit le narratif contextuel du scénario défavorable."""
    favori = home_name if predicted_winner == "home" else away_name
    adversaire = away_name if predicted_winner == "home" else home_name

    reasons: list[str] = []

    # Écart de force faible → surprise crédible
    index_gap = abs(home_index - away_index)
    if index_gap < 8:
        reasons.append("faible écart de force entre les deux équipes")
    elif index_gap < 15:
        reasons.append("écart de force limité")

    # Chaos de ligue
    if chaos_level >= 0.5:
        reasons.append("ligue à forte variance (beaucoup de surprises historiques)")
    elif chaos_level >= 0.3:
        reasons.append("ligue avec une variance notable")

    # H2H upsets
    if h2h_upsets >= 2:
        reasons.append(
            f"{h2h_upsets} retournements déjà observés entre ces deux équipes en confrontation directe"
        )
    elif h2h_upsets == 1:
        reasons.append("un retournement déjà observé en confrontation directe")

    # Carton rouge / événement perturbateur
    if chaos_level >= 0.25:
        reasons.append("risque non nul d'événement perturbateur (carton rouge, blessure précoce)")

    # Pression adverse
    if predicted_winner == "home" and away_index >= home_index * 0.85:
        reasons.append(f"forte pression offensive potentielle de {adversaire}")

    if not reasons:
        reasons.append("historique de matchs similaires où le favori statistique a été pris en défaut")

    narrative_reasons = " ; ".join(reasons)
    return (
        f"Scénario construit à partir de contextes similaires où {favori} "
        f"a sous-performé offensivement : {narrative_reasons}."
    )


def _pick_principal(
    top_scores: list,
    predicted_winner: str,
    ou25_yes: bool | None,
) -> tuple[tuple[int, int], float, bool]:
    """
    Choisit le score à mettre en avant comme scénario "principal" parmi les
    scorelines déjà classées par probabilité (`top_scores`, issues de la
    simulation réelle — aucune probabilité n'est recalculée ici).

    V19.2 — avant, on prenait toujours `top_scores[0]` (le scoreline isolé
    le plus fréquent), sans lien avec le pronostic 1X2 affiché juste
    au-dessus dans le rapport. Statistiquement les deux peuvent diverger
    (ex : "Nul" cumule plusieurs scorelines à faible probabilité chacun,
    quand "Victoire domicile" est portée par un seul scoreline très
    fréquent comme 2-1) — mais affiché tel quel, ça ressemble à une
    contradiction du bot plutôt qu'à un fait statistique normal.

    On préfère donc, dans l'ordre :
      1. le scoreline le plus probable qui correspond à LA FOIS au
         pronostic 1X2 ET à la recommandation Over/Under 2.5 affichés ;
      2. à défaut, le plus probable qui correspond au moins au 1X2 ;
      3. à défaut (ex : le pronostic 1X2 prédit un résultat qu'aucun des
         10 scorelines les plus fréquents n'illustre), le scoreline
         global le plus probable, comme avant.

    Retourne (score, probabilité, coherent) où `coherent` indique si le
    choix retenu correspond bien au pronostic 1X2 (cas 1 ou 2) — utilisé
    pour adapter le texte du narratif.
    """
    parsed: list[tuple[tuple[int, int], float]] = []
    for score_str, prob in top_scores:
        try:
            h, a = (int(x) for x in str(score_str).split("-", 1))
            parsed.append(((h, a), prob))
        except Exception:
            continue

    if not parsed:
        return (0, 0), 0.0, False

    def _matches_outcome(score: tuple[int, int]) -> bool:
        return not predicted_winner or _outcome(*score) == predicted_winner

    def _matches_ou25(score: tuple[int, int]) -> bool:
        if ou25_yes is None:
            return True
        total = score[0] + score[1]
        return (total >= 3) == ou25_yes

    for score, prob in parsed:
        if _matches_outcome(score) and _matches_ou25(score):
            return score, prob, True

    for score, prob in parsed:
        if _matches_outcome(score):
            return score, prob, True

    return parsed[0][0], parsed[0][1], False


def _count_h2h_upsets(
    h2h_scores: Sequence[tuple[int, int]],
    predicted_winner: str,
) -> int:
    """
    Compte les matchs H2H où le vainqueur prédit a perdu.
    Les scores H2H sont orientés du point de vue de l'équipe à domicile
    du match actuel (convention engine/h2h.py).
    """
    upsets = 0
    for hg, ag in h2h_scores:
        actual = _outcome(hg, ag)
        if actual != predicted_winner and actual != "draw":
            upsets += 1
    return upsets


# ── API publique ─────────────────────────────────────────────────────────────

def build_scenarios(
    *,
    home_name:       str,
    away_name:       str,
    home_xg:         float,
    away_xg:         float,
    home_win_prob:   float,
    draw_prob:       float,
    away_win_prob:   float,
    home_index:      float = 50.0,
    away_index:      float = 50.0,
    chaos_level:     float = 0.0,
    h2h_scores:      Sequence[tuple[int, int]] = (),
    top_scores:      list | None = None,      # déjà calculé par le prédicteur
    modal_score:     str = "",
    predicted_outcome: str = "",              # V19.2 — pronostic 1X2 déjà décidé
    ou25_yes:        bool | None = None,      # V19.2 — recommandation O/U 2.5 déjà décidée
) -> ScenarioBundle:
    """
    Construit les trois scénarios pour un match.

    Parameters
    ----------
    top_scores  : liste de (score_str, probability) issue du prédicteur V14.
                  Si fournie, le scénario principal en est extrait directement
                  sans nouvelle simulation.
    predicted_outcome : le pronostic 1X2 déjà calculé par le prédicteur
                  (`PredictionResult.predicted_outcome`), qui peut différer
                  d'un simple argmax des probabilités brutes (ex : détection
                  du nul calibrée). Si fourni, les scénarios s'alignent sur
                  CE pronostic plutôt que d'en recalculer un autre — pour
                  que "favorable"/"défavorable" et le score mis en avant
                  ne contredisent jamais le pronostic affiché au-dessus.
    ou25_yes    : la recommandation Over/Under 2.5 déjà décidée, utilisée
                  uniquement pour départager entre scorelines à égalité de
                  pertinence pour le pronostic 1X2 (aucun effet si absent).
    """

    # ── Vainqueur prédit ──────────────────────────────────────────────────────
    # V19.2 : on réutilise le pronostic déjà décidé par le prédicteur quand il
    # est fourni, plutôt que de recalculer un argmax indépendant sur les
    # probabilités brutes — les deux peuvent diverger (détection du nul
    # calibrée) et un scénario construit sur un pronostic différent de celui
    # affiché dans le rapport est ce qui rend la section illogique.
    if predicted_outcome in ("home", "draw", "away"):
        predicted_winner = predicted_outcome
    else:
        predicted_winner = max(
            ("home", home_win_prob),
            ("draw", draw_prob),
            ("away", away_win_prob),
            key=lambda pair: pair[1],
        )[0]

    # ── Scénario principal ────────────────────────────────────────────────────
    # V19.2 : parmi les scorelines déjà classés par probabilité, on met en
    # avant le plus probable qui NE CONTREDIT PAS le pronostic 1X2 (et,
    # dans l'idéal, pas non plus la recommandation O/U) plutôt que le
    # scoreline isolé le plus fréquent sans lien avec ce pronostic. Aucune
    # probabilité n'est recalculée : on choisit seulement lequel des
    # scorelines déjà simulés on affiche en premier.
    if top_scores:
        main_score, main_prob, coherent = _pick_principal(
            top_scores, predicted_winner, ou25_yes
        )
    else:
        main_score, main_prob = _top_score(home_xg, away_xg, chaos_level)
        coherent = _outcome(*main_score) == predicted_winner

    main_narrative = "Score le plus fréquent dans la simulation de 100 000 tirages."
    if not coherent:
        main_narrative += (
            " Aucun scoreline fréquent ne coïncide exactement avec le "
            "pronostic 1X2 — celui-ci reste porté par la somme de "
            "plusieurs scorelines proches, pas par un seul score dominant."
        )

    principal = MatchScenario(
        label       = "principal",
        score       = main_score,
        probability = main_prob,
        outcome     = _outcome(*main_score),
        narrative   = main_narrative,
    )

    # ── Scénario favorable ────────────────────────────────────────────────────
    if predicted_winner == "home":
        lam_h_fav = home_xg * _FAV_BOOST
        lam_a_fav = away_xg * _OPP_CUT
        fav_narrative = (
            f"{home_name} en grande forme offensive, {away_name} en difficulté défensive. "
            f"Le favori concrétise mieux que son xG moyen."
        )
    elif predicted_winner == "away":
        lam_h_fav = home_xg * _OPP_CUT
        lam_a_fav = away_xg * _FAV_BOOST
        fav_narrative = (
            f"{away_name} exploite ses occasions mieux que son xG moyen. "
            f"{home_name} moins efficace défensivement que d'habitude."
        )
    else:  # draw prédit
        lam_h_fav = home_xg * 0.92
        lam_a_fav = away_xg * 0.92
        fav_narrative = (
            "Match équilibré avec peu de buts — les deux défenses tiennent le coup. "
            "Le nul est confirmé dans les deux cas."
        )

    fav_score, fav_prob = _top_score(lam_h_fav, lam_a_fav, chaos_level)

    favorable = MatchScenario(
        label       = "favorable",
        score       = fav_score,
        probability = fav_prob,
        outcome     = _outcome(*fav_score),
        narrative   = fav_narrative,
    )

    # ── Scénario défavorable plausible ────────────────────────────────────────
    if predicted_winner == "home":
        lam_h_adv = home_xg * _FAV_CUT
        lam_a_adv = away_xg * _OPP_BOOST
    elif predicted_winner == "away":
        lam_h_adv = home_xg * _OPP_BOOST
        lam_a_adv = away_xg * _FAV_CUT
    else:
        # Nul prédit : scénario défavorable = l'un ou l'autre prend l'avantage
        lam_h_adv = home_xg * _OPP_BOOST
        lam_a_adv = away_xg * _OPP_BOOST

    adv_score, adv_prob = _top_score(lam_h_adv, lam_a_adv, chaos_level)

    h2h_upsets = _count_h2h_upsets(h2h_scores, predicted_winner)
    adv_narrative = _build_narrative_adverse(
        predicted_winner = predicted_winner,
        home_name        = home_name,
        away_name        = away_name,
        home_index       = home_index,
        away_index       = away_index,
        chaos_level      = chaos_level,
        h2h_scores       = h2h_scores,
        h2h_upsets       = h2h_upsets,
    )

    adverse = MatchScenario(
        label       = "défavorable plausible",
        score       = adv_score,
        probability = adv_prob,
        outcome     = _outcome(*adv_score),
        narrative   = adv_narrative,
    )

    return ScenarioBundle(
        principal        = principal,
        favorable        = favorable,
        adverse          = adverse,
        predicted_winner = predicted_winner,
        home_name        = home_name,
        away_name        = away_name,
    )
