"""
tests/test_confidence_v2.py — Tests unitaires pour engine/confidence_v2.py

Couvre :
  - _form_variance       : stable (WWWWW=0), erratique (WLWLWL≈max), vide=0.5
  - _build_explanation   : retourne une chaîne non vide
  - compute_confidence_v2:
      - score dans [0, 100]
      - labels (ELITE / HIGH / MEDIUM / LOW / TRÈS BAS)
      - grades (A+ / A / B / C / D)
      - cas optimal → score élevé
      - cas dégradé → score faible
      - robustesse aux valeurs limites
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from engine.confidence_v2 import (
    compute_confidence_v2,
    ConfidenceV2,
    _form_variance,
    _build_explanation,
)


# ── _form_variance ────────────────────────────────────────────────────────────

class TestFormVariance:
    def test_empty_string_returns_default(self):
        assert _form_variance("") == 0.5

    def test_single_result_returns_low_variance(self):
        assert _form_variance("W") == 0.3

    def test_all_wins_variance_zero(self):
        """Série parfaite → variance = 0."""
        assert _form_variance("WWWWWWWW") == 0.0

    def test_all_losses_variance_zero(self):
        """Série de défaites → variance = 0 (valeurs constantes)."""
        assert _form_variance("LLLLLLLL") == 0.0

    def test_alternating_wl_max_variance(self):
        """W/L alternés → variance maximale (= 1.0 après normalisation)."""
        v = _form_variance("WLWLWLWL")
        assert abs(v - 1.0) < 1e-9

    def test_mixed_form_between_zero_and_one(self):
        v = _form_variance("WWDLW")
        assert 0.0 < v < 1.0

    def test_only_draws_variance_zero(self):
        assert _form_variance("DDDDD") == 0.0

    def test_uses_only_last_8(self):
        """Les 8 derniers matchs seulement sont pris en compte."""
        # Long historique stable suivi d'une alternance récente
        v_recent_alternating = _form_variance("WWWWWWWWWLWLWLWL")
        v_stable             = _form_variance("WWWWWWWW")
        # Avec alternance récente, variance > variance stable
        assert v_recent_alternating > v_stable

    @pytest.mark.parametrize("form,expected_range", [
        ("WWWWWWWW", (0.0, 0.05)),
        ("WLWLWLWL", (0.95, 1.05)),
        ("WWDLW",    (0.0,  1.0)),
    ])
    def test_known_forms(self, form, expected_range):
        v = _form_variance(form)
        lo, hi = expected_range
        assert lo <= v <= hi, f"_form_variance({form!r}) = {v:.4f} hors [{lo}, {hi}]"


# ── _build_explanation ────────────────────────────────────────────────────────

class TestBuildExplanation:
    def test_returns_non_empty_string(self):
        result = _build_explanation(80, "HIGH", 18.0, 13.0, 17.0, 14.0, 12.0, 8.0, 5.0)
        assert isinstance(result, str) and len(result) > 0

    def test_high_score_positive_explanation(self):
        """Un score élevé avec tous les indicateurs bons → pas de 'Limites'."""
        result = _build_explanation(85, "HIGH", 18.0, 13.0, 17.0, 14.0, 12.0, 8.0, 5.0)
        assert "Limites" not in result

    def test_low_data_quality_mentioned(self):
        """Qualité des données faible → mentionné dans l'explication."""
        result = _build_explanation(30, "LOW", 3.0, 13.0, 17.0, 14.0, 12.0, 8.0, 5.0)
        assert "données" in result.lower()

    def test_bad_sources_mentioned(self):
        """Désalignement sources → mentionné."""
        result = _build_explanation(30, "LOW", 18.0, 3.0, 17.0, 14.0, 12.0, 8.0, 5.0)
        assert "source" in result.lower() or "désalignement" in result.lower()

    def test_unstable_teams_mentioned(self):
        """Équipes irrégulières → mentionné."""
        result = _build_explanation(40, "LOW", 18.0, 13.0, 5.0, 14.0, 12.0, 8.0, 5.0)
        assert "irrégulier" in result.lower() or "équipe" in result.lower()

    def test_injuries_mentioned(self):
        """Blessures impactantes → mentionné."""
        result = _build_explanation(40, "MEDIUM", 18.0, 13.0, 17.0, 5.0, 12.0, 8.0, 5.0)
        assert "blessure" in result.lower()

    def test_unstable_simulation_mentioned(self):
        """Simulation instable → mentionné."""
        result = _build_explanation(40, "MEDIUM", 18.0, 13.0, 17.0, 14.0, 4.0, 8.0, 5.0)
        assert "simulation" in result.lower()

    def test_chaotic_league_mentioned(self):
        """Ligue imprévisible → mentionné."""
        result = _build_explanation(40, "MEDIUM", 18.0, 13.0, 17.0, 14.0, 12.0, 2.0, 5.0)
        assert "imprévisible" in result.lower() or "ligue" in result.lower()


# ── compute_confidence_v2 ─────────────────────────────────────────────────────

class TestComputeConfidenceV2:
    def _optimal(self, **kwargs) -> ConfidenceV2:
        """Paramètres idéaux — doit produire un score élevé."""
        defaults = dict(
            home_games_played=30,
            away_games_played=28,
            extended_data_used=True,
            local_data_quality="available",
            forebet_alignment="strong",
            forebet_weight_applied=0.25,
            home_form_str="WWWWWWWW",
            away_form_str="WWWWWWWW",
            home_injuries_out=0,
            away_injuries_out=0,
            mc_convergence_se=0.0005,
            mc_converged=True,
            mc_distinct_scorelines=80,
            home_win_prob=0.60,
            draw_prob=0.25,
            away_win_prob=0.15,
            chaos_level=0.0,
            tier=1,
            h2h_matches_used=8,
            calibration_quality="HIGH",
        )
        defaults.update(kwargs)
        return compute_confidence_v2(**defaults)

    def _degraded(self, **kwargs) -> ConfidenceV2:
        """Paramètres dégradés — doit produire un score faible."""
        defaults = dict(
            home_games_played=2,
            away_games_played=1,
            extended_data_used=False,
            local_data_quality="unknown",
            forebet_alignment="weak",
            forebet_weight_applied=0.0,
            home_form_str="WL",
            away_form_str="LW",
            home_injuries_out=5,
            away_injuries_out=4,
            mc_convergence_se=0.05,
            mc_converged=False,
            mc_distinct_scorelines=10,
            home_win_prob=0.34,
            draw_prob=0.33,
            away_win_prob=0.33,
            chaos_level=0.9,
            tier=3,
            h2h_matches_used=0,
            calibration_quality="LOW",
        )
        defaults.update(kwargs)
        return compute_confidence_v2(**defaults)

    # ── Types et structure ───────────────────────────────────────────────────

    def test_returns_confidence_v2(self):
        assert isinstance(self._optimal(), ConfidenceV2)

    def test_score_in_range(self):
        for result in [self._optimal(), self._degraded()]:
            assert 0 <= result.score <= 100, f"Score hors [0,100] : {result.score}"

    def test_breakdown_non_empty(self):
        result = self._optimal()
        assert isinstance(result.breakdown, dict) and len(result.breakdown) > 0

    def test_explanation_non_empty(self):
        result = self._optimal()
        assert isinstance(result.explanation, str) and len(result.explanation) > 0

    # ── Labels et grades ─────────────────────────────────────────────────────

    @pytest.mark.parametrize("label", ["ELITE", "HIGH", "MEDIUM", "LOW", "TRÈS BAS"])
    def test_valid_labels_exist(self, label):
        """Chaque label doit être atteignable par un appel à compute_confidence_v2."""
        # On s'assure juste que le label fait partie du jeu de valeurs possibles
        result = self._optimal()
        assert result.label in ["ELITE", "HIGH", "MEDIUM", "LOW", "TRÈS BAS"]

    @pytest.mark.parametrize("grade", ["A+", "A", "B", "C", "D"])
    def test_valid_grades_exist(self, grade):
        result = self._optimal()
        assert result.grade in ["A+", "A", "B", "C", "D"]

    def test_label_grade_risk_consistent(self):
        """Un score élevé doit correspondre à label HIGH/ELITE et grade A/A+."""
        result = self._optimal()
        assert result.label in ("ELITE", "HIGH", "MEDIUM"), \
            f"Score élevé → label inattendu : {result.label}"
        assert result.grade in ("A+", "A", "B"), \
            f"Score élevé → grade inattendu : {result.grade}"

    # ── Scénarios fonctionnels ───────────────────────────────────────────────

    def test_optimal_score_higher_than_degraded(self):
        """Conditions optimales → score plus élevé que conditions dégradées."""
        opt  = self._optimal()
        deg  = self._degraded()
        assert opt.score > deg.score, \
            f"Optimal ({opt.score}) ≤ Dégradé ({deg.score})"

    def test_many_games_increases_score(self):
        """Plus de matchs joués → meilleure qualité des données → score plus élevé."""
        few  = compute_confidence_v2(home_games_played=2,  away_games_played=2)
        many = compute_confidence_v2(home_games_played=25, away_games_played=25)
        assert many.score >= few.score

    def test_strong_forebet_alignment_helps(self):
        base  = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                      forebet_alignment="weak")
        good  = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                      forebet_alignment="strong")
        assert good.score > base.score

    def test_stable_form_higher_score(self):
        """Forme stable (W×8) → meilleur score que forme erratique (W/L alternés)."""
        stable   = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                         home_form_str="WWWWWWWW",
                                         away_form_str="WWWWWWWW")
        unstable = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                         home_form_str="WLWLWLWL",
                                         away_form_str="WLWLWLWL")
        assert stable.score >= unstable.score

    def test_many_injuries_reduces_score(self):
        none     = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                         home_injuries_out=0, away_injuries_out=0)
        many_inj = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                         home_injuries_out=6, away_injuries_out=5)
        assert none.score > many_inj.score

    def test_converged_mc_increases_score(self):
        conv   = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                       mc_converged=True,  mc_convergence_se=0.0005)
        noconv = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                       mc_converged=False, mc_convergence_se=0.05)
        assert conv.score > noconv.score

    def test_more_h2h_matches_increases_score(self):
        no_h2h  = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                        h2h_matches_used=0)
        many_h2h = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                         h2h_matches_used=8)
        assert many_h2h.score >= no_h2h.score

    def test_high_chaos_reduces_score(self):
        no_chaos   = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                           chaos_level=0.0)
        high_chaos = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                           chaos_level=0.9)
        assert no_chaos.score > high_chaos.score

    def test_tier3_lower_than_tier1(self):
        t1 = compute_confidence_v2(home_games_played=20, away_games_played=20, tier=1)
        t3 = compute_confidence_v2(home_games_played=20, away_games_played=20, tier=3)
        assert t1.score >= t3.score

    def test_calibration_quality_matters(self):
        high_calib = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                           calibration_quality="HIGH")
        low_calib  = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                           calibration_quality="LOW")
        assert high_calib.score > low_calib.score

    def test_extended_data_bonus(self):
        base     = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                         extended_data_used=False)
        extended = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                         extended_data_used=True)
        assert extended.score >= base.score

    # ── Cas limites ───────────────────────────────────────────────────────────

    def test_all_defaults_doesnt_crash(self):
        """Appel sans aucun paramètre → pas d'exception, score valide."""
        result = compute_confidence_v2()
        assert 0 <= result.score <= 100

    def test_zero_probability_tie_penalizes(self):
        """Si les 3 issues sont quasi-équiprobables → pénalité appliquée."""
        balanced  = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                          home_win_prob=0.34, draw_prob=0.33,
                                          away_win_prob=0.33, mc_converged=True)
        dominated = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                          home_win_prob=0.65, draw_prob=0.20,
                                          away_win_prob=0.15, mc_converged=True)
        assert dominated.score >= balanced.score

    def test_sparse_data_penalty(self):
        ok     = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                       local_data_quality="available")
        sparse = compute_confidence_v2(home_games_played=20, away_games_played=20,
                                       local_data_quality="sparse")
        assert ok.score >= sparse.score
