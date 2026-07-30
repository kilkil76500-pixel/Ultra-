"""engine/coherence.py — V20.3 : Cohérence interne des pronostics affichés.

Le problème signalé par l'utilisateur
--------------------------------------
Le pronostic principal (1X2, BTTS, Plus/moins 2,5) et le classement des
scénarios de score les plus probables (top_scores) viennent tous de la
MÊME simulation Monte-Carlo (voir montecarlo_v5.py, une seule boucle de
tirages alimente tout : home_win_prob, draw_prob, away_win_prob, btts_prob,
over25_prob, modal_score et top_scores). Ce ne sont donc jamais deux
calculs séparés qui se contrediraient par bug — mais ils répondent à des
questions différentes, et le résultat peut légitimement sembler
contradictoire :

- BTTS/O2.5/1X2 sont des probabilités MARGINALES, cumulées sur TOUS les
  scénarios de score qui satisfont la condition (BTTS "oui" additionne
  1-1, 2-1, 1-2, 2-2, 3-1, 3-2… tous ensemble).
- Le "score le plus probable" (modal_score) est UN SEUL scénario pris
  isolément, qui peut ne pas satisfaire BTTS/O2.5 même si la SOMME de
  tous les scénarios qui les satisfont dépasse 50%.

Exemple réel possible : score le plus probable 2-0 (24% des tirages) mais
BTTS "Oui" affiché à 55% — parce que 1-1, 2-1, 1-2, 2-2… cumulés dépassent
les 50%, même si aucun d'eux individuellement ne dépasse 2-0. Les deux
chiffres sont corrects ; ils ne répondent juste pas à la même question.
Sans explication, ça ressemble à une contradiction interne — ce module la
détecte et l'explique en une phrase au lieu de laisser deviner.

Le cas du nul est traité à part : predicted_outcome peut afficher "Nul"
non pas parce que draw_prob (brute) était la plus haute des trois, mais
parce que draw_detection_factor (engine/calibration.py) l'a délibérément
boostée pour compenser un biais connu du modèle (V17 : le bot ne prédisait
jamais nul, 0/22 nuls détectés sans ce correctif). C'est une décision
assumée du produit — mais elle doit être dite explicitement, pas présentée
comme si "Nul" était simplement le résultat brut le plus probable.

Ce module ne recalcule rien et ne change aucun pronostic : il compare des
champs déjà produits par predictor.predict() et montecarlo_v5, et retourne
des AnomalyFlag (même type que engine.anomaly, réutilisé pour partager le
même bloc d'affichage côté Telegram — voir engine/scanner.py).
"""

from __future__ import annotations

from engine.anomaly import AnomalyFlag

_OUTCOME_LABEL = {"home": "Domicile", "draw": "Nul", "away": "Extérieur"}


def _parse_score(score_str: str) -> tuple[int, int] | None:
    if not score_str or "-" not in score_str:
        return None
    try:
        h, a = score_str.split("-", 1)
        return int(h), int(a)
    except (ValueError, TypeError):
        return None


def _score_outcome(score_str: str) -> str | None:
    parsed = _parse_score(score_str)
    if parsed is None:
        return None
    h, a = parsed
    return "home" if h > a else ("away" if a > h else "draw")


def _score_satisfies_btts(score_str: str) -> bool | None:
    parsed = _parse_score(score_str)
    if parsed is None:
        return None
    h, a = parsed
    return h >= 1 and a >= 1


def _score_satisfies_over25(score_str: str) -> bool | None:
    parsed = _parse_score(score_str)
    if parsed is None:
        return None
    h, a = parsed
    return (h + a) > 2


def check_coherence(
    *,
    predicted_outcome: str,
    home_win_prob_raw: float,
    draw_prob_raw: float,
    away_win_prob_raw: float,
    btts_yes: bool,
    btts_prob: float,
    ou25_yes: bool,
    over25_prob: float,
    modal_score: str,
) -> list[AnomalyFlag]:
    """Compare le pronostic affiché au score le plus probable pris isolément
    et signale (en 'info', jamais 'warning' — ce n'est pas une alerte de
    fiabilité) les cas où l'un ne découle pas visiblement de l'autre."""
    flags: list[AnomalyFlag] = []

    # 1. Le nul affiché est-il un boost, pas la probabilité brute la plus haute ?
    raw_probs = {"home": home_win_prob_raw, "draw": draw_prob_raw, "away": away_win_prob_raw}
    if any(raw_probs.values()):
        raw_argmax = max(raw_probs, key=raw_probs.get)
        if predicted_outcome == "draw" and raw_argmax != "draw":
            flags.append(AnomalyFlag(
                code="draw_is_boosted",
                severity="info",
                message=(
                    f"ℹ️ Le nul affiché est un ajustement statistique délibéré "
                    f"(le modèle sous-estime les nuls sans ce correctif), pas la "
                    f"probabilité brute la plus haute — brute : {raw_probs['home']*100:.0f}% "
                    f"domicile / {raw_probs['draw']*100:.0f}% nul / {raw_probs['away']*100:.0f}% extérieur."
                ),
            ))

    modal_outcome = _score_outcome(modal_score)
    modal_btts    = _score_satisfies_btts(modal_score)
    modal_over25  = _score_satisfies_over25(modal_score)

    # 2. Le 1X2 affiché correspond-il à la catégorie du score isolé le plus probable ?
    if modal_outcome is not None and predicted_outcome in _OUTCOME_LABEL and modal_outcome != predicted_outcome:
        flags.append(AnomalyFlag(
            code="outcome_modal_mismatch",
            severity="info",
            message=(
                f"ℹ️ Le pronostic 1X2 ({_OUTCOME_LABEL[predicted_outcome]}) cumule la probabilité "
                f"sur tous les scénarios ; pris isolément, le score le plus probable "
                f"({modal_score}) correspond plutôt à {_OUTCOME_LABEL[modal_outcome]}. "
                f"Les deux chiffres sont corrects — ils ne répondent pas à la même question."
            ),
        ))

    # 3. BTTS affiché vs score isolé le plus probable
    if modal_btts is not None and btts_yes != modal_btts:
        sens = "Oui" if btts_yes else "Non"
        modal_word = "satisfait" if modal_btts else "ne satisfait pas"
        flags.append(AnomalyFlag(
            code="btts_modal_mismatch",
            severity="info",
            message=(
                f"ℹ️ BTTS : {sens} — probabilité de BTTS \"Oui\" à {btts_prob*100:.0f}% "
                f"(cumulée sur plusieurs scénarios), mais le score isolé le plus probable "
                f"({modal_score}) {modal_word} BTTS."
            ),
        ))

    # 4. Plus/moins 2,5 affiché vs score isolé le plus probable
    if modal_over25 is not None and ou25_yes != modal_over25:
        sens = "Plus" if ou25_yes else "Moins"
        modal_word = "dépasse" if modal_over25 else "ne dépasse pas"
        flags.append(AnomalyFlag(
            code="over25_modal_mismatch",
            severity="info",
            message=(
                f"ℹ️ 2,5 buts : {sens} — probabilité de \"Plus de 2,5\" à {over25_prob*100:.0f}% "
                f"(cumulée sur plusieurs scénarios), mais le score isolé le plus probable "
                f"({modal_score}) {modal_word} 2,5 buts."
            ),
        ))

    return flags
