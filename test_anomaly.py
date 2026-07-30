"""Tests de engine/anomaly.py."""

from __future__ import annotations

from engine import anomaly, tracking
from engine.leagues import classify


def _seed(monkeypatch, tmp_path):
    monkeypatch.setattr(tracking.config, "WEB_CACHE_DIR", str(tmp_path))


def _record_and_settle(*, league, conf, home_xg, away_xg, home_prob, draw_prob,
                        away_prob, rh, ra):
    pid = tracking.record_prediction(
        home_name="A", away_name="B", league=league, kickoff="2026-01-01",
        home_win_prob=home_prob, draw_prob=draw_prob, away_win_prob=away_prob,
        btts_prob=0.5, over25_prob=0.5, modal_score="1-0",
        confidence_pct=conf, confidence_label="HIGH",
        home_xg=home_xg, away_xg=away_xg,
        predicted_outcome=max(
            {"home": home_prob, "draw": draw_prob, "away": away_prob},
            key={"home": home_prob, "draw": draw_prob, "away": away_prob}.get,
        ),
    )
    tracking.settle(pid, rh, ra)


def test_no_flags_when_no_history_and_no_odds(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    report = anomaly.detect_anomalies(
        league_name="Premier League", confidence_pct=75,
        home_xg=1.6, away_xg=1.0,
        home_win_prob=0.6, draw_prob=0.25, away_win_prob=0.15,
    )
    assert report.has_alerts is False


def test_historical_fail_rate_flagged(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    # 10 similar matches, favourite (home) wrong 7 times -> 70% fail rate
    for i in range(10):
        rh, ra = (0, 1) if i < 7 else (2, 0)
        _record_and_settle(league="Ligue X", conf=75, home_xg=1.6, away_xg=1.0,
                            home_prob=0.6, draw_prob=0.25, away_prob=0.15, rh=rh, ra=ra)

    report = anomaly.detect_anomalies(
        league_name="Ligue X", confidence_pct=75, home_xg=1.6, away_xg=1.0,
        home_win_prob=0.6, draw_prob=0.25, away_win_prob=0.15,
    )
    assert report.has_alerts is True
    assert any(f.code == "historical_fail_rate" for f in report.flags)


def test_odds_model_gap_flagged(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    report = anomaly.detect_anomalies(
        league_name="Premier League", confidence_pct=75,
        home_xg=1.6, away_xg=1.0,
        home_win_prob=0.75, draw_prob=0.15, away_win_prob=0.10,
        bookmaker_home_prob=0.50,  # 25-point gap, même issue (domicile) des deux côtés
    )
    assert any(f.code == "odds_model_gap" for f in report.flags)


def test_odds_model_gap_uses_same_outcome_not_max_vs_max(monkeypatch, tmp_path):
    """V20.10 — bug trouvé en injectant de vraies cotes (V20.9 les rend
    enfin captables) : le modèle favorise Extérieur (52%) mais le marché
    favorise Domicile (54% implicite) ; comparer seulement les deux
    maximums masquait l'écart réel (52% vs 54% ~ rien), alors que le
    marché ne crédite l'Extérieur — le favori du MODÈLE — que de 24%,
    un vrai écart de 28 points sur l'issue qui compte."""
    report = anomaly.detect_anomalies(
        league_name="Premier League", confidence_pct=75,
        home_xg=1.3, away_xg=1.8,
        home_win_prob=0.283, draw_prob=0.196, away_win_prob=0.521,
        bookmaker_home_prob=0.5405,  # favori du marché : Domicile
        bookmaker_draw_prob=0.2778,
        bookmaker_away_prob=0.2381,  # ce que le marché crédite au favori du MODÈLE
    )
    flags = [f for f in report.flags if f.code == "odds_model_gap"]
    assert len(flags) == 1
    assert "28 points" in flags[0].message or "29 points" in flags[0].message


def test_odds_model_gap_not_flagged_when_same_outcome_agrees(monkeypatch, tmp_path):
    """Repli : quand modèle et marché sont d'accord sur l'issue favorite et
    que l'écart réel est petit, pas de fausse alerte."""
    report = anomaly.detect_anomalies(
        league_name="Premier League", confidence_pct=75,
        home_xg=1.6, away_xg=1.0,
        home_win_prob=0.55, draw_prob=0.25, away_win_prob=0.20,
        bookmaker_home_prob=0.58, bookmaker_draw_prob=0.24, bookmaker_away_prob=0.18,
    )
    assert not any(f.code == "odds_model_gap" for f in report.flags)


def test_scenario_dispersion_flagged(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    report = anomaly.detect_anomalies(
        league_name="Premier League", confidence_pct=75,
        home_xg=1.6, away_xg=1.0,
        home_win_prob=0.6, draw_prob=0.25, away_win_prob=0.15,
        top_scenario_prob=0.06,
    )
    assert any(f.code == "scenario_dispersion" for f in report.flags)


def test_scenario_dispersion_not_flagged_when_scenario_dominates(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    report = anomaly.detect_anomalies(
        league_name="Premier League", confidence_pct=75,
        home_xg=1.6, away_xg=1.0,
        home_win_prob=0.6, draw_prob=0.25, away_win_prob=0.15,
        top_scenario_prob=0.22,
    )
    assert not any(f.code == "scenario_dispersion" for f in report.flags)


def test_league_info_chaos_note_tier3():
    info = classify(league_id=999999, name="D3 amateur", country="Nowhere")
    flag = anomaly.league_info_chaos_note(info)
    assert flag is not None
    assert flag.code == "low_tier_league"


def test_league_info_chaos_note_tier1():
    info = classify(league_id=39, name="Premier League", country="England")
    assert anomaly.league_info_chaos_note(info) is None


def test_league_info_chaos_note_uses_learned_penalty_when_reduced():
    """V20.6 : une pénalité APPRISE différente de la constante par défaut
    doit toujours être signalée, quel que soit le tier — y compris tier 1,
    qui ne déclenchait jamais rien avant si non appris."""
    from engine.leagues import LeagueInfo
    info = LeagueInfo(
        id=39, name="Premier League", country="England", tier=1,
        avg_home_goals=1.5, avg_away_goals=1.2,
        confidence_penalty=0.80,  # mesuré, différent du défaut (1.00) pour tier 1
        tier_label="TIER 1", tier_emoji="🥇",
    )
    flag = anomaly.league_info_chaos_note(info)
    assert flag is not None
    assert "réduite" in flag.message
    assert "80%" in flag.message


def test_league_info_chaos_note_learned_penalty_upward():
    """Une pénalité apprise MEILLEURE que le défaut doit être signalée
    positivement ('revue à la hausse'), pas comme un avertissement."""
    from engine.leagues import LeagueInfo
    info = LeagueInfo(
        id=999999, name="D3 amateur", country="Nowhere", tier=3,
        avg_home_goals=1.4, avg_away_goals=1.1,
        confidence_penalty=0.90,  # mesuré, meilleur que le défaut (0.65) pour tier 3
        tier_label="TIER 3", tier_emoji="🥉",
    )
    flag = anomaly.league_info_chaos_note(info)
    assert flag is not None
    assert "hausse" in flag.message
    assert "90%" in flag.message
