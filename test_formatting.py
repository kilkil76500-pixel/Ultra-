"""
Tests de engine/formatting.py — la couche présentation Telegram.

Avant V19, ce module (le plus visible pour l'utilisateur final : c'est ce
qu'il lit dans chaque message) n'avait aucun test dédié. On se concentre ici
sur les primitives pures (pas d'objets Telegram réels à construire) :
bar/pct_int/chunk/divider/section/outcome_badge/kickoff.
"""

from __future__ import annotations

import pytest

from engine import formatting


# ── bar() / confidence_bar() ──────────────────────────────────────────────────

def test_bar_empty_at_zero_progress():
    result = formatting.bar(0, 10)
    assert result == "⬜" * 10


def test_bar_full_at_complete_progress():
    result = formatting.bar(10, 10)
    # Entièrement rempli : aucune case vide.
    assert "⬜" not in result


def test_bar_handles_zero_total_without_crashing():
    # total <= 0 est un cas limite réel (scan à 0 match) : ne doit jamais lever.
    assert formatting.bar(0, 0) == "⬜" * 10


def test_bar_clamps_current_above_total():
    # current > total (ex: compteur désynchronisé) doit rester borné, pas planter.
    result = formatting.bar(999, 10)
    assert "⬜" not in result


def test_confidence_bar_length_matches_width():
    result = formatting.confidence_bar(65.0, width=10)
    assert len(result) == 10  # chaque caractère est une "cellule" logique


# ── pct_int() ──────────────────────────────────────────────────────────────────

def test_pct_int_zero_total_returns_zero():
    assert formatting.pct_int(5, 0) == 0


def test_pct_int_normal_case():
    assert formatting.pct_int(5, 10) == 50


def test_pct_int_never_exceeds_100():
    assert formatting.pct_int(20, 10) == 100


# ── chunk() ────────────────────────────────────────────────────────────────────

def test_chunk_short_text_is_untouched():
    text = "un message court"
    assert formatting.chunk(text) == [text]


def test_chunk_splits_long_text_under_limit():
    long_text = "\n".join(f"ligne {i}" for i in range(2000))
    chunks = formatting.chunk(long_text)
    assert len(chunks) > 1
    assert all(len(c) <= formatting.MAX_MESSAGE for c in chunks)


def test_chunk_preserves_all_lines():
    lines = [f"ligne {i}" for i in range(2000)]
    long_text = "\n".join(lines)
    chunks = formatting.chunk(long_text)
    rebuilt = "\n".join(chunks)
    assert rebuilt.count("ligne ") == len(lines)


# ── divider() / section() ─────────────────────────────────────────────────────

def test_divider_default_width():
    assert formatting.divider() == "━" * 28


def test_section_includes_title_and_divider():
    out = formatting.section("Analyse", emoji="🔬")
    assert "Analyse" in out
    assert "🔬" in out
    assert formatting.divider() in out


# ── outcome_badge() ────────────────────────────────────────────────────────────

def test_outcome_badge_uses_predicted_outcome_when_given():
    # Le nul peut être le résultat prédit même si draw_prob brute n'est pas
    # la plus haute (draw_detection_factor) — predicted_outcome doit primer.
    badge = formatting.outcome_badge(home=0.5, draw=0.2, away=0.3, predicted_outcome="draw")
    assert badge == formatting._RESULT_BADGES["draw"]


def test_outcome_badge_falls_back_to_highest_probability():
    badge = formatting.outcome_badge(home=0.6, draw=0.2, away=0.2)
    assert badge == formatting._RESULT_BADGES["home"]


# ── kickoff() ──────────────────────────────────────────────────────────────────

def test_kickoff_missing_timestamp_returns_placeholder():
    assert formatting.kickoff({}) == "heure inconnue"


def test_kickoff_invalid_timestamp_does_not_crash():
    assert formatting.kickoff({"timestamp": "not-a-number"}) == "heure inconnue"


def test_kickoff_formats_valid_timestamp():
    # 2026-01-01 12:00:00 UTC
    result = formatting.kickoff({"timestamp": 1767268800})
    assert result.endswith("UTC")
    assert ":" in result
