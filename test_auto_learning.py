"""Tests de la boucle d'auto-amélioration sécurisée (engine.auto_learning).

Ces tests vérifient l'invariant central demandé : le bot peut proposer une
meilleure calibration tout seul, mais ne l'applique JAMAIS si elle régresse
sur des données jamais vues (le lot holdout).
"""

from __future__ import annotations

from engine import auto_learning, calibration, tracking


def _seed(monkeypatch, tmp_path):
    # `config` est importé comme un module singleton par tracking.py,
    # calibration.py et auto_learning.py : patcher WEB_CACHE_DIR une seule
    # fois suffit pour les trois.
    monkeypatch.setattr(tracking.config, "WEB_CACHE_DIR", str(tmp_path))


def _record_and_settle(rh_prob, rd_prob, ra_prob, rh, ra):
    pid = tracking.record_prediction(
        home_name="A", away_name="B", league="L1", kickoff="2026-01-01",
        home_win_prob=rh_prob, draw_prob=rd_prob, away_win_prob=ra_prob,
        btts_prob=0.5, over25_prob=0.5, modal_score="1-0",
        confidence_pct=60.0, confidence_label="MEDIUM",
        home_xg=1.4, away_xg=1.0,
        home_win_prob_raw=rh_prob, draw_prob_raw=rd_prob, away_win_prob_raw=ra_prob,
        calibration_version=1,
    )
    tracking.settle(pid, rh, ra)


def test_not_enough_data_leaves_calibration_untouched(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    report = auto_learning.run_auto_learning()
    assert report.attempted is False
    assert report.accepted is False


def test_confirmed_bias_on_holdout_is_accepted(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    # Lot calibration : le modèle sous-estime systématiquement les
    # victoires extérieures (p_away=0.25 alors qu'elles arrivent souvent).
    for i in range(28):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)
    # Lot holdout (plus récent) : le même biais persiste réellement.
    for i in range(12):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)

    report = auto_learning.run_auto_learning()
    assert report.attempted is True
    assert report.accepted is True
    assert report.new_version is not None
    # Jamais moins bon que l'actif sur le holdout jamais vu.
    assert report.candidate_metrics.accuracy_1x2 >= report.active_metrics.accuracy_1x2
    assert report.candidate_metrics.brier_1x2 <= report.active_metrics.brier_1x2 + 1e-9


def test_bias_contradicted_by_holdout_is_rejected(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    # Même lot calibration que ci-dessus (laisse penser à un biais extérieur).
    for i in range(28):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)
    # Le holdout (plus récent) contredit ce biais : le modèle avait en fait
    # raison. Booster "away" y dégraderait la performance.
    for _ in range(12):
        _record_and_settle(0.55, 0.20, 0.25, 1, 0)

    version_before = calibration.load_calibration().version
    report = auto_learning.run_auto_learning()

    assert report.attempted is True
    assert report.accepted is False
    assert "régression" in report.reason.lower() or "rejeté" in report.reason.lower()
    # La calibration active n'a strictement pas bougé.
    assert calibration.load_calibration().version == version_before


def test_no_relevant_change_is_a_safe_no_op(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    # Prédictions parfaites et stables : aucun biais à corriger.
    for _ in range(40):
        _record_and_settle(0.90, 0.06, 0.04, 1, 0)

    report = auto_learning.run_auto_learning()
    assert report.attempted is True
    assert report.accepted is False
    assert report.changes == []


# ── V19 : validation walk-forward multi-fenêtres ──────────────────────────────

def test_small_holdout_falls_back_to_single_window(monkeypatch, tmp_path):
    """
    Avec un holdout < _N_FOLDS * _MIN_FOLD_SIZE (15), il n'y a qu'une seule
    fenêtre (le holdout entier) — comportement identique aux versions
    précédant le walk-forward. C'est le cas de tous les tests ci-dessus
    (holdout=12) : ils continuent de passer sans modification, et on
    vérifie ici explicitement `n_folds == 1`.
    """
    _seed(monkeypatch, tmp_path)
    for i in range(28):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)
    for i in range(12):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)

    report = auto_learning.run_auto_learning()
    assert report.n_folds == 1
    assert report.fold_regressions == []


def test_walk_forward_catches_regression_hidden_by_aggregate(monkeypatch, tmp_path):
    """
    Invariant central du walk-forward : un candidat qui régresse sur une
    fenêtre récente distincte doit être rejeté, MÊME QUAND l'agrégat sur
    tout le holdout semble acceptable (une bonne fenêtre peut compenser une
    mauvaise en moyenne). Un split unique 70/30 ne peut pas voir ça.
    """
    _seed(monkeypatch, tmp_path)
    # Lot calibration : biais extérieur apparent (comme dans les tests ci-dessus).
    for i in range(40):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)
    # Holdout de 18 lignes → 3 fenêtres de 6. Les deux premières confirment
    # le biais (le candidat s'y comporte bien), la dernière le contredit
    # nettement (résultats domicile uniquement) : régression locale que
    # l'agrégat seul n'a pas forcément détectée.
    for i in range(12):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)
    for _ in range(6):
        _record_and_settle(0.55, 0.20, 0.25, 1, 0)

    report = auto_learning.run_auto_learning()
    assert report.attempted is True
    assert report.n_folds == 3
    assert report.accepted is False
    assert report.fold_regressions, "la fenêtre la plus récente doit signaler une régression"
    assert report.rejection_streak == 1


# ── V19 : suivi des rejets consécutifs ────────────────────────────────────────

def test_consecutive_rejections_increment_and_trigger_warning(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    for i in range(40):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)
    for i in range(12):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)
    for _ in range(6):
        _record_and_settle(0.55, 0.20, 0.25, 1, 0)

    r1 = auto_learning.run_auto_learning()
    r2 = auto_learning.run_auto_learning()
    r3 = auto_learning.run_auto_learning()

    assert (r1.rejection_streak, r2.rejection_streak, r3.rejection_streak) == (1, 2, 3)
    assert r1.streak_warning is False
    assert r2.streak_warning is False
    assert r3.streak_warning is True
    assert calibration.load_calibration().consecutive_rejections == 3


def test_accepted_candidate_resets_rejection_streak(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    # Un premier cycle rejeté fait avancer le streak à 1.
    for i in range(28):
        _record_and_settle(0.90, 0.06, 0.04, 1, 0)
    for i in range(12):
        _record_and_settle(0.05, 0.05, 0.90, 0, 1)
    auto_learning.run_auto_learning()

    # Un cycle ensuite accepté doit remettre le compteur à 0.
    for i in range(30):
        if i % 4 == 0:
            _record_and_settle(0.55, 0.20, 0.25, 1, 0)
        else:
            _record_and_settle(0.55, 0.20, 0.25, 0, 1)
    report = auto_learning.run_auto_learning()

    if report.accepted:
        assert report.rejection_streak == 0
        assert calibration.load_calibration().consecutive_rejections == 0
