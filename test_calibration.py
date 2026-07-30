"""
Tests de engine/calibration.py — chargement/sauvegarde de la calibration.

Avant V19, ce module n'avait pas de test dédié à ses cas limites : que se
passe-t-il si calibration.json n'existe pas encore, ou s'il est corrompu ?
`load_calibration()` gère déjà ces deux cas (retour aux valeurs par défaut),
mais rien ne le figeait comme comportement garanti — ces tests le font.
"""

from __future__ import annotations

import json
import os

from engine import calibration


def _seed(monkeypatch, tmp_path):
    monkeypatch.setattr(calibration.config, "WEB_CACHE_DIR", str(tmp_path))


def test_load_calibration_missing_file_returns_defaults(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    cfg = calibration.load_calibration()
    assert cfg == calibration.CalibrationConfig.default()


def test_load_calibration_corrupted_json_falls_back_to_defaults(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    path = calibration._calib_path()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json,,,")

    cfg = calibration.load_calibration()
    assert cfg == calibration.CalibrationConfig.default()


def test_load_calibration_unknown_fields_are_ignored(monkeypatch, tmp_path):
    """
    Un calibration.json généré par une version future (avec un champ que
    cette version ne connaît pas encore) ne doit jamais faire planter le
    chargement — c'est ce qui permet des mises à niveau sans coordination
    stricte entre le déploiement du code et celui du cache.
    """
    _seed(monkeypatch, tmp_path)
    path = calibration._calib_path()
    data = calibration.CalibrationConfig.default().to_dict()
    data["un_champ_qui_n_existe_pas_encore"] = 42
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    cfg = calibration.load_calibration()
    assert cfg.version == calibration.CalibrationConfig.default().version


def test_save_then_load_roundtrip(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    cfg = calibration.CalibrationConfig.default()
    cfg.prob_multiplier_home = 1.05
    calibration.save_calibration(cfg)

    reloaded = calibration.load_calibration()
    assert reloaded.prob_multiplier_home == 1.05
    assert reloaded.version == 2  # save_calibration incrémente toujours la version


def test_update_rejection_streak_persists_before_any_save(monkeypatch, tmp_path):
    """
    Le compteur de rejets (V19) doit pouvoir progresser même avant qu'un
    premier candidat n'ait jamais été accepté — sinon l'alerte de dérive ne
    se déclencherait jamais tant que /recalibrer n'a rien appliqué.
    """
    _seed(monkeypatch, tmp_path)
    assert not os.path.exists(calibration._calib_path())

    calibration.update_rejection_streak(1)
    assert calibration.load_calibration().consecutive_rejections == 1

    calibration.update_rejection_streak(2)
    assert calibration.load_calibration().consecutive_rejections == 2


def test_update_rejection_streak_does_not_bump_version(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    cfg = calibration.CalibrationConfig.default()
    calibration.save_calibration(cfg)  # version -> 2
    version_before = calibration.load_calibration().version

    calibration.update_rejection_streak(1)

    assert calibration.load_calibration().version == version_before
