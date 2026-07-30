"""Cache-only match analysis — V13 extended stats integration.

Internet access belongs exclusively to ``web_collector``.  Every function here
reads the JSON snapshots created by /scan and runs the V13 strength-index engine.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

import config
from engine import cache_store, h2h as h2h_module, injuries as injuries_module
from engine import anomaly, coherence, leagues, market_edge, montecarlo, predictor, tracking, value
from engine.h2h import H2HWeight
from engine.injuries import InjuryImpact
from engine.odds import MatchOdds
from engine.providers.base import NormalizedFixture, NormalizedTeamStats, TeamExtendedStats
from engine.utils import kickoff_label

logger = logging.getLogger(__name__)
Progress = Callable[[int, int, str], None]


@dataclass
class ScanResult:
    fixture:     NormalizedFixture
    prediction:  predictor.PredictionResult
    match_odds:  MatchOdds
    report:      value.ValueReport
    score:       float
    league_info: leagues.LeagueInfo
    kickoff:     str
    snapshot:    dict | None = None

    @property
    def league_name(self) -> str:
        return self.league_info.name

    @property
    def tier(self) -> int:
        return self.league_info.tier


def cached_snapshots() -> list[dict]:
    return cache_store.load_snapshots()


def list_cached(tier_filter: int | None = None) -> list[tuple[dict, leagues.LeagueInfo]]:
    rows: list[tuple[dict, leagues.LeagueInfo]] = []
    for snapshot in cached_snapshots():
        info = leagues.classify(
            int(snapshot.get("league_id") or 0),
            str(snapshot.get("competition") or ""),
            str(snapshot.get("country") or ""),
        )
        if tier_filter is None or info.tier == tier_filter:
            rows.append((snapshot, info))
    return rows


def _stats(snapshot: dict, side: str) -> NormalizedTeamStats:
    raw = (snapshot.get("stats") or {}).get(side) or {}
    played = int(raw.get("played") or 0)
    if played <= 0:
        goals_for    = 0.0
        goals_against = 0.0
    else:
        goals_for    = float(raw.get("goals_for") or 0)
        goals_against = float(raw.get("goals_against") or 0)
    return NormalizedTeamStats(
        played=played,
        goals_for=goals_for,
        goals_against=goals_against,
        form=str(raw.get("form") or ""),
    )


def _extended_stats(snapshot: dict, side: str) -> TeamExtendedStats | None:
    """
    Build a TeamExtendedStats from the V2 extended fields stored in the snapshot.
    Returns None when no extended data is present (graceful degradation).
    """
    uo       = snapshot.get("uo_stats") or {}
    cs       = snapshot.get("cs_stats") or {}
    positions = snapshot.get("positions") or {}
    streaks  = snapshot.get("streaks") or {}
    ext_forms = snapshot.get("extended_forms") or {}

    team_key = "home_team" if side == "home" else "away_team"
    team_forms = ext_forms.get(team_key) or {}

    # Check if we have anything useful
    has_data = any([uo, cs, positions, streaks, team_forms.get("home"), team_forms.get("away")])
    if not has_data:
        return None

    # Over/Under stats come from the match page — same for both teams
    over25_pct  = float(uo.get("over25_pct", -1))
    over15_pct  = float(uo.get("over15_pct", -1))
    btts_pct    = float(uo.get("btts_pct", -1))

    # Clean sheet / failed-to-score are team-specific
    prefix = side  # "home" or "away"
    clean_sheet_pct  = float(cs.get(f"{prefix}_clean_sheet_pct", -1))
    failed_score_pct = float(cs.get(f"{prefix}_failed_score_pct", -1))

    # Split form
    home_form = str(team_forms.get("home") or "")
    away_form = str(team_forms.get("away") or "")

    # League position
    pos_key  = f"{prefix}_position"
    position = int(positions.get(pos_key) or 0)
    league_size = int(positions.get("league_size") or 20)

    # Current streak
    streak = int((streaks.get(side) or 0))

    return TeamExtendedStats(
        btts_pct=btts_pct,
        over25_pct=over25_pct,
        over15_pct=over15_pct,
        clean_sheet_pct=clean_sheet_pct,
        failed_score_pct=failed_score_pct,
        home_form=home_form,
        away_form=away_form,
        league_position=position,
        league_size=league_size,
        current_streak=streak,
    )


def _h2h(snapshot: dict, fixture: NormalizedFixture) -> H2HWeight | None:
    values = []
    for item in (snapshot.get("h2h") or [])[:10]:
        from engine.providers.base import NormalizedH2HFixture
        values.append(NormalizedH2HFixture(
            home_id=str(item.get("home_id") or fixture.home_id),
            away_id=str(item.get("away_id") or fixture.away_id),
            goals_home=int(item.get("goals_home") or 0),
            goals_away=int(item.get("goals_away") or 0),
        ))
    return h2h_module.compute_h2h_weight(values, fixture.home_id, fixture.away_id) if values else None


def _injury(snapshot: dict, fixture: NormalizedFixture) -> InjuryImpact | None:
    rows = snapshot.get("injuries") or []
    if not rows:
        return None
    return injuries_module.compute_injury_impact(rows, fixture.home_id, fixture.away_id)


def _forebet_hint(snapshot: dict) -> dict | None:
    block = snapshot.get("forebet") or {}
    score = block.get("score")
    detail = snapshot.get("forebet_detail") or {}
    if not score or len(score) != 2:
        if not detail:
            return None
        return {"detail": detail}
    return {"score": (score[0], score[1]), "detail": detail}


def build_prediction_inputs(snapshot: dict) -> tuple[NormalizedFixture, "leagues.LeagueInfo", dict]:
    """Construit (fixture, info_ligue, kwargs) pour predictor.predict() à
    partir d'un snapshot brut — factorisé hors de analyse_snapshot() (V19.16)
    pour que engine.xg_backtest puisse rejouer EXACTEMENT le même pipeline
    d'entrée que la production lors du backtest de xg_global_multiplier,
    sans dupliquer cette logique."""
    fixture = cache_store.fixture_from_snapshot(snapshot)
    info = leagues.classify(
        fixture.league_id, fixture.league_name, fixture.league_country,
    )
    home_extended = _extended_stats(snapshot, "home")
    away_extended = _extended_stats(snapshot, "away")
    kwargs = dict(
        home_stats    = _stats(snapshot, "home"),
        away_stats    = _stats(snapshot, "away"),
        home_name     = fixture.home_name,
        away_name     = fixture.away_name,
        league_info   = info,
        h2h_weight    = _h2h(snapshot, fixture),
        injury_impact = _injury(snapshot, fixture),
        forebet_hint  = _forebet_hint(snapshot),
        home_extended = home_extended,
        away_extended = away_extended,
    )
    return fixture, info, kwargs


def analyse_snapshot(snapshot: dict, progress: Progress | None = None) -> ScanResult:
    fixture, info, pred_kwargs = build_prediction_inputs(snapshot)
    prediction = predictor.predict(progress=progress, **pred_kwargs)
    raw_odds = snapshot.get("odds") or {}
    odds = MatchOdds(**{
        key: float(raw_odds.get(key) or 0)
        for key in (
            "home_win", "draw", "away_win", "over25", "under25",
            "over35", "under35", "btts_yes", "btts_no",
        )
    })
    report = value.analyse_value(prediction, odds, info)
    # V19.13 — marché conseillé (1X2/BTTS/O2.5) d'après la fiabilité réelle
    # déjà observée, à niveau de confiance comparable. Lecture seule sur la
    # DB de suivi ; ne modifie aucune probabilité ni pronostic. N'affecte
    # jamais le scan lui-même en cas d'échec (voir market_edge.recommend_market).
    try:
        edge = market_edge.recommend_market(prediction)
        prediction.recommended_market = edge.market
        prediction.recommended_market_label = edge.market_label
        prediction.recommended_market_reason = edge.reason
        prediction.recommended_market_data_ok = edge.data_sufficient
    except Exception as exc:
        logger.warning("[scanner] market_edge indisponible pour %s-%s : %s",
                       fixture.home_name, fixture.away_name, exc)

    # V20 — signaux d'anomalie pré-match (historique similaire, écart
    # cote/modèle, dispersion des scénarios Monte-Carlo). Lecture seule ;
    # n'affecte jamais le scan lui-même en cas d'échec.
    try:
        bookmaker_home_prob = bookmaker_draw_prob = bookmaker_away_prob = None
        if odds.available:
            bookmaker_home_prob = (1.0 / odds.home_win) if odds.home_win else None
            bookmaker_draw_prob = (1.0 / odds.draw) if odds.draw else None
            bookmaker_away_prob = (1.0 / odds.away_win) if odds.away_win else None
        top_scenario_prob = None
        if prediction.top_scores:
            top_scenario_prob = prediction.top_scores[0][1]
        anomaly_report = anomaly.detect_anomalies(
            league_name=info.name,
            confidence_pct=prediction.confidence_pct,
            home_xg=prediction.home_xg,
            away_xg=prediction.away_xg,
            home_win_prob=prediction.home_win_prob,
            draw_prob=prediction.draw_prob,
            away_win_prob=prediction.away_win_prob,
            top_scenario_prob=top_scenario_prob,
            bookmaker_home_prob=bookmaker_home_prob,
            bookmaker_draw_prob=bookmaker_draw_prob,
            bookmaker_away_prob=bookmaker_away_prob,
        )
        chaos_note = anomaly.league_info_chaos_note(info)
        if chaos_note:
            anomaly_report.flags.append(chaos_note)

        # V20.3 — cohérence interne : le pronostic affiché (1X2/BTTS/O2.5)
        # peut sembler contredire le score le plus probable pris isolément
        # (marginal vs modal — voir engine/coherence.py). Ajouté au même
        # rapport que les anomalies pour ne pas multiplier les blocs
        # affichés côté Telegram.
        coherence_flags = coherence.check_coherence(
            predicted_outcome  = getattr(prediction, "predicted_outcome", "") or "",
            home_win_prob_raw  = prediction.home_win_prob_raw,
            draw_prob_raw      = prediction.draw_prob_raw,
            away_win_prob_raw  = prediction.away_win_prob_raw,
            btts_yes           = prediction.btts_yes,
            btts_prob          = prediction.btts_prob,
            ou25_yes           = prediction.ou25_yes,
            over25_prob        = prediction.over25_prob,
            modal_score        = prediction.modal_score,
        )
        anomaly_report.flags.extend(coherence_flags)

        prediction.anomaly_messages = [f.message for f in anomaly_report.flags]
        prediction.anomaly_has_alert = anomaly_report.has_alerts
        # V20.6 — split par sévérité pour un affichage distinct côté
        # formatting.py (voir predictor.PredictionResult.anomaly_warnings/
        # anomaly_notes pour le pourquoi).
        prediction.anomaly_warnings = [f.message for f in anomaly_report.flags if f.severity == "warning"]
        prediction.anomaly_notes    = [f.message for f in anomaly_report.flags if f.severity != "warning"]
    except Exception as exc:
        logger.warning("[scanner] anomaly indisponible pour %s-%s : %s",
                       fixture.home_name, fixture.away_name, exc)

    return ScanResult(
        fixture    = fixture,
        prediction = prediction,
        match_odds = odds,
        report     = report,
        score      = value.value_score(report, prediction.confidence_pct),
        league_info = info,
        kickoff    = kickoff_label(fixture),
        snapshot   = snapshot,
    )


def scan_global(
    tier_filter: int | None = None,
    top_n: int = config.TOP_N_OPPORTUNITIES,
) -> list[ScanResult]:
    results: list[ScanResult] = []
    for snapshot, _info in list_cached(tier_filter):
        try:
            results.append(analyse_snapshot(snapshot))
        except Exception as exc:
            logger.warning("Skipped cached match %s: %s", snapshot.get("cache_key"), exc)
    results.sort(key=lambda item: (item.prediction.confidence_pct, item.kickoff), reverse=True)
    return results[:top_n]


def analyse_all() -> tuple[list[ScanResult], list[str]]:
    results: list[ScanResult] = []
    failures: list[str] = []
    for snapshot in cached_snapshots():
        try:
            results.append(analyse_snapshot(snapshot))
        except Exception as exc:
            label = f"{snapshot.get('home', '?')} vs {snapshot.get('away', '?')}"
            failures.append(f"{label}: {type(exc).__name__}")
            logger.warning("Pre-calculation failed for %s: %s", label, exc)
    results.sort(key=lambda item: (item.kickoff, item.fixture.home_name))
    return results, failures


def scan_tier(tier: int, top_n: int = config.TOP_N_OPPORTUNITIES) -> list[ScanResult]:
    return scan_global(tier_filter=tier, top_n=top_n)


def scan_today(top_n: int = config.TOP_N_OPPORTUNITIES) -> list[ScanResult]:
    return scan_global(top_n=top_n)


def count_today() -> dict:
    rows = list_cached()
    counts = {1: 0, 2: 0, 3: 0}
    by_league: dict[str, int] = {}
    for snapshot, info in rows:
        counts[info.tier] += 1
        by_league[info.name] = by_league.get(info.name, 0) + 1
    return {
        "total": len(rows),
        "tier_1": counts[1],
        "tier_2": counts[2],
        "tier_3": counts[3],
        "by_league": dict(sorted(by_league.items(), key=lambda item: (-item[1], item[0]))),
    }


def analyse_cached_key(key: str) -> ScanResult | None:
    snapshot = cache_store.load_by_key(key)
    return analyse_snapshot(snapshot) if snapshot else None


def analyse_named_match(home_team_query: str, away_team_query: str) -> ScanResult | None:
    home = home_team_query.casefold()
    away = away_team_query.casefold()
    for snapshot in cached_snapshots():
        if home in str(snapshot.get("home", "")).casefold() and away in str(snapshot.get("away", "")).casefold():
            return analyse_snapshot(snapshot)
    return None


def scenario_for(result: ScanResult) -> dict:
    """Draw one new illustrative scenario without changing aggregate probabilities."""
    pred = result.prediction
    return montecarlo.draw_scenario(
        pred.home_xg,
        pred.away_xg,
        chaos_level=pred.chaos_level,
    )


def plausible_scenarios_for(result: "ScanResult") -> "object":
    """
    V15 : Construit les trois scénarios plausibles (ScenarioBundle).
    Remplace le tirage aléatoire illustratif de V14.
    """
    from engine import scenarios as scenarios_module

    pred = result.prediction

    # Extraire les scores H2H orientés depuis le prédicteur si disponibles
    h2h_scores: list[tuple[int, int]] = []
    strength = getattr(pred, "strength_index", {}) or {}
    for side in ("home", "away"):
        raw_h2h = (strength.get(side) or {}).get("h2h_scores") or []
        if raw_h2h:
            for pair in raw_h2h:
                try:
                    h2h_scores.append((int(pair[0]), int(pair[1])))
                except Exception:
                    pass
            break

    return scenarios_module.build_scenarios(
        home_name      = pred.home_name,
        away_name      = pred.away_name,
        home_xg        = pred.home_xg,
        away_xg        = pred.away_xg,
        home_win_prob  = pred.home_win_prob,
        draw_prob      = pred.draw_prob,
        away_win_prob  = pred.away_win_prob,
        home_index     = getattr(pred, "home_index", 50.0),
        away_index     = getattr(pred, "away_index", 50.0),
        chaos_level    = pred.chaos_level,
        h2h_scores     = h2h_scores,
        top_scores     = list(pred.top_scores) if pred.top_scores else [],
        modal_score    = pred.modal_score or "",
        predicted_outcome = getattr(pred, "predicted_outcome", "") or "",
        ou25_yes       = getattr(pred, "ou25_yes", None),
    )


def record_prediction(result: ScanResult) -> int:
    """Persist a prediction for later calibration."""
    pred = result.prediction
    # V20.4 — snapshot brut conservé en base pour que engine.xg_backtest
    # puisse ré-simuler ce match même après que son snapshot ait disparu du
    # cache local (ce qui arrive presque systématiquement avant que le match
    # ne soit réglé — voir engine.tracking et engine.xg_backtest).
    try:
        snapshot_json = json.dumps(result.snapshot) if result.snapshot else None
    except (TypeError, ValueError) as exc:
        logger.warning("[scanner] snapshot non sérialisable pour %s-%s : %s",
                        pred.home_name, pred.away_name, exc)
        snapshot_json = None
    return tracking.record_prediction(
        home_name=pred.home_name,
        away_name=pred.away_name,
        league=result.league_name,
        kickoff=result.kickoff,
        home_win_prob=pred.home_win_prob,
        draw_prob=pred.draw_prob,
        away_win_prob=pred.away_win_prob,
        btts_prob=pred.btts_prob,
        over25_prob=pred.over25_prob,
        modal_score=pred.modal_score,
        confidence_pct=pred.confidence_pct,
        confidence_label=pred.confidence,
        home_xg=pred.home_xg,
        away_xg=pred.away_xg,
        home_win_prob_raw=getattr(pred, "home_win_prob_raw", None),
        draw_prob_raw=getattr(pred, "draw_prob_raw", None),
        away_win_prob_raw=getattr(pred, "away_win_prob_raw", None),
        calibration_version=getattr(pred, "calibration_version", None),
        predicted_outcome=getattr(pred, "predicted_outcome", "") or None,
        league_tier=result.tier,
        forebet_url=str(
            ((result.snapshot or {}).get("forebet") or {}).get("detail_url")
            or ((result.snapshot or {}).get("source_details") or {})
            .get("forebet", {})
            .get("detail_url")
            or ""
        ),
        kickoff_timestamp=(
            int(result.fixture.timestamp)
            if result.fixture.timestamp is not None
            else None
        ),
        snapshot_json=snapshot_json,
    )
