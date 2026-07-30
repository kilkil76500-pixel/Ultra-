"""Tests pour engine/coherence.py — cohérence interne des pronostics."""

from engine import coherence


def test_no_flags_when_fully_coherent():
    flags = coherence.check_coherence(
        predicted_outcome="home",
        home_win_prob_raw=0.55, draw_prob_raw=0.20, away_win_prob_raw=0.25,
        btts_yes=False, btts_prob=0.35,
        ou25_yes=False, over25_prob=0.40,
        modal_score="2-0",
    )
    assert flags == []


def test_btts_yes_but_modal_score_does_not_satisfy_it():
    flags = coherence.check_coherence(
        predicted_outcome="home",
        home_win_prob_raw=0.55, draw_prob_raw=0.20, away_win_prob_raw=0.25,
        btts_yes=True, btts_prob=0.58,
        ou25_yes=False, over25_prob=0.40,
        modal_score="2-0",
    )
    codes = [f.code for f in flags]
    assert "btts_modal_mismatch" in codes
    msg = next(f.message for f in flags if f.code == "btts_modal_mismatch")
    assert "Oui" in msg and "2-0" in msg and "58%" in msg


def test_btts_no_but_modal_score_satisfies_it_anyway():
    """Cas trouvé en testant sur données réelles : le pronostic 'Non' (sous
    le seuil de calibration) peut coexister avec un score modal qui, lui,
    satisfait BTTS. Le message doit rester non ambigu dans ce sens aussi."""
    flags = coherence.check_coherence(
        predicted_outcome="home",
        home_win_prob_raw=0.40, draw_prob_raw=0.35, away_win_prob_raw=0.25,
        btts_yes=False, btts_prob=0.51,
        ou25_yes=False, over25_prob=0.40,
        modal_score="1-1",
    )
    msg = next(f.message for f in flags if f.code == "btts_modal_mismatch")
    assert "Non" in msg
    assert "51%" in msg  # doit rester la probabilité de "Oui", pas de "Non"
    assert "satisfait BTTS" in msg  # pas "ne satisfait pas" — sens inversé du cas précédent


def test_over25_mismatch_flagged():
    flags = coherence.check_coherence(
        predicted_outcome="home",
        home_win_prob_raw=0.55, draw_prob_raw=0.20, away_win_prob_raw=0.25,
        btts_yes=False, btts_prob=0.30,
        ou25_yes=True, over25_prob=0.57,
        modal_score="1-1",
    )
    codes = [f.code for f in flags]
    assert "over25_modal_mismatch" in codes


def test_draw_boost_flagged_when_not_raw_argmax():
    flags = coherence.check_coherence(
        predicted_outcome="draw",
        home_win_prob_raw=0.25, draw_prob_raw=0.22, away_win_prob_raw=0.30,
        btts_yes=False, btts_prob=0.30,
        ou25_yes=False, over25_prob=0.30,
        modal_score="0-0",  # cohérent avec "draw" niveau catégorie
    )
    codes = [f.code for f in flags]
    assert "draw_is_boosted" in codes
    assert "outcome_modal_mismatch" not in codes  # 0-0 est bien un nul


def test_draw_not_flagged_when_raw_argmax_agrees():
    flags = coherence.check_coherence(
        predicted_outcome="draw",
        home_win_prob_raw=0.25, draw_prob_raw=0.45, away_win_prob_raw=0.30,
        btts_yes=False, btts_prob=0.30,
        ou25_yes=False, over25_prob=0.30,
        modal_score="1-1",
    )
    codes = [f.code for f in flags]
    assert "draw_is_boosted" not in codes


def test_outcome_modal_mismatch_reproduces_reported_case():
    """Cas signalé : pronostic 'Nul' affiché mais les scénarios les plus
    probables sont 2-1 et 1-2 (des victoires, pas des nuls)."""
    flags = coherence.check_coherence(
        predicted_outcome="draw",
        home_win_prob_raw=0.28, draw_prob_raw=0.25, away_win_prob_raw=0.35,
        btts_yes=True, btts_prob=0.55,
        ou25_yes=True, over25_prob=0.55,
        modal_score="2-1",
    )
    codes = [f.code for f in flags]
    assert "outcome_modal_mismatch" in codes
    assert "draw_is_boosted" in codes  # away_raw (0.35) > draw_raw (0.25) ici aussi


def test_malformed_modal_score_does_not_crash():
    flags = coherence.check_coherence(
        predicted_outcome="home",
        home_win_prob_raw=0.5, draw_prob_raw=0.3, away_win_prob_raw=0.2,
        btts_yes=True, btts_prob=0.5,
        ou25_yes=True, over25_prob=0.5,
        modal_score="?",
    )
    assert flags == []  # aucun crash, aucun flag basé sur un score illisible
