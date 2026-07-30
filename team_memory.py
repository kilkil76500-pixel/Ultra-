"""
engine/team_memory.py — V16 : Mémoire des équipes.

Crée et maintient un profil évolutif par équipe qui capture des traits
comportementaux observés sur la durée :

  - Équipe mentalement fragile (perd des matchs gagnés)
  - Finit fort (beaucoup de buts après 70')
  - Marque souvent après la 70e minute
  - Souffre contre les blocs bas
  - Performe à domicile (grand écart dom./ext.)
  - Tendance aux retournements H2H
  - Résistance après un carton rouge
  - Efficacité sur penalty

Les profils sont sauvegardés en JSON dans le répertoire de cache.
Ils s'enrichissent à chaque règlement de prédiction (/resultat).

Aucun code Telegram. Aucun appel API.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import config

logger = logging.getLogger(__name__)

_MEMORY_FILE = "team_memory.json"
_VERSION = 1


def _memory_path() -> str:
    os.makedirs(config.WEB_CACHE_DIR, exist_ok=True)
    return os.path.join(config.WEB_CACHE_DIR, _MEMORY_FILE)


# ── Profil d'équipe ──────────────────────────────────────────────────────────

@dataclass
class TeamMemoryProfile:
    """Profil mémoriel d'une équipe."""
    name:                 str   = ""
    name_normalized:      str   = ""   # minuscule, pour les lookups

    # Traits de caractère (0.0 à 1.0, 0.5 = neutre/inconnu)
    mental_fragility:     float = 0.5  # perd des matchs qu'elle menait
    late_scorer:          float = 0.5  # marque souvent après 70'
    strong_finisher:      float = 0.5  # améliore ses résultats en 2e mi-temps
    struggles_low_block:  float = 0.5  # difficultés contre les blocs bas
    home_dominance:       float = 0.5  # écart dom/ext significatif
    h2h_reversal:         float = 0.5  # tendance aux retournements H2H
    comeback_ability:     float = 0.5  # capacité à remonter au score

    # Compteurs bruts (pour le calcul des traits)
    total_matches:        int   = 0
    home_wins:            int   = 0
    away_wins:            int   = 0
    late_goals_scored:    int   = 0    # buts après 70' marqués
    late_goals_conceded:  int   = 0    # buts après 70' concédés
    matches_led_then_lost: int  = 0    # menait mais a perdu
    comebacks:            int   = 0    # était mené mais a égalisé/gagné
    goals_scored_total:   int   = 0
    goals_conceded_total: int   = 0
    low_block_matches:    int   = 0    # matchs contre équipes low_block
    low_block_scored:     int   = 0    # buts contre low_block
    h2h_reversals:        int   = 0    # retournements H2H observés

    # V19.14 — fiabilité du modèle SUR cette équipe précise (pas la forme de
    # l'équipe elle-même) : combien de fois le pronostic 1X2 affiché pour un
    # match impliquant cette équipe s'est révélé juste, une fois le résultat
    # connu. Alimenté uniquement par /resultat et /autoresultat — aucune
    # source externe. Sert de signal de risque dans confidence_v2.py :
    # certaines équipes (promotion, mercato agressif, très irrégulières)
    # sont structurellement plus dures à prévoir que ce que les stats
    # saisonnières laissent croire, et ça se voit dans l'historique réel des
    # pronostics, pas seulement dans la forme.
    model_predictions_seen: int = 0
    model_correct_1x2:      int = 0

    # Méta
    last_updated:         float = 0.0  # timestamp Unix
    version:              int   = _VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TeamMemoryProfile":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def model_error_rate(self) -> float:
        """
        Taux d'erreur 1X2 historique du modèle sur cette équipe, dans [0, 1].
        0.0 tant qu'il n'y a pas assez d'échantillons pour que ce soit autre
        chose que du bruit — c'est délibérément neutre (jamais de bonus,
        seulement une pénalité une fois qu'on SAIT), pas une estimation
        prématurée à partir de 1 ou 2 matchs.
        """
        if self.model_predictions_seen < 5:
            return 0.0
        return round(
            1.0 - (self.model_correct_1x2 / self.model_predictions_seen), 3
        )

    def recompute_traits(self) -> None:
        """Recalcule les traits à partir des compteurs bruts."""
        if self.total_matches < 3:
            return   # pas assez de données

        n = self.total_matches

        # Fragilité mentale : % de matchs menés puis perdus
        if n > 0:
            self.mental_fragility = round(
                0.5 * (1 - self.mental_fragility) +
                0.5 * min(1.0, self.matches_led_then_lost / max(1, n / 3)),
                3
            )

        # Buteur tardif : ratio buts après 70' / total buts
        if self.goals_scored_total > 0:
            late_ratio = self.late_goals_scored / self.goals_scored_total
            self.late_scorer = round(0.3 + late_ratio * 0.7, 3)
            # Fort finisseur = marque tard ET concède peu tard
            if self.goals_conceded_total > 0:
                late_conceded_ratio = self.late_goals_conceded / self.goals_conceded_total
                # Fort finisseur si marque beaucoup tard et concède peu tard
                self.strong_finisher = round(
                    max(0.0, min(1.0, 0.5 + (late_ratio - late_conceded_ratio) * 1.5)),
                    3
                )

        # Domination domicile
        total_decisive = self.home_wins + self.away_wins
        if total_decisive > 0:
            home_ratio = self.home_wins / total_decisive
            self.home_dominance = round(home_ratio, 3)

        # Capacité à remonter
        if n > 0:
            self.comeback_ability = round(
                min(1.0, 0.3 + self.comebacks / max(1, n / 4)),
                3
            )

        # Efficacité contre bloc bas
        if self.low_block_matches >= 3 and self.low_block_matches > 0:
            low_block_avg = self.low_block_scored / self.low_block_matches
            # Comparer à la moyenne globale
            global_avg = self.goals_scored_total / max(1, n)
            ratio = low_block_avg / max(0.01, global_avg)
            self.struggles_low_block = round(
                min(1.0, max(0.0, 1.0 - ratio)),  # 0 = facile, 1 = difficile
                3
            )

    def describe(self) -> list[str]:
        """Retourne une liste de traits notables (pour affichage Telegram)."""
        traits: list[str] = []

        # V19.15 — bug trouvé en testant : model_predictions_seen (alimenté
        # par record_model_outcome(), appelé depuis tracking.settle() à
        # chaque /resultat réel) est un compteur INDÉPENDANT de
        # total_matches (alimenté uniquement par update_from_result(), qui
        # n'est appelé nulle part dans le code actuel — voir le commentaire
        # V19.14 dans tracking.settle()). Le trait de fiabilité du modèle
        # était évalué APRÈS le early-return sur total_matches < 3 : avec
        # total_matches bloqué à 0 pour toujours, ce trait précis ne
        # pouvait donc JAMAIS s'afficher, quel que soit le nombre de
        # /resultat traités pour cette équipe — reproduit avec
        # model_predictions_seen=7 et confirmé qu'il ne remontait rien
        # d'autre que "Données insuffisantes". Évalué ici, avant le
        # early-return, pour ne plus en dépendre.
        if self.model_predictions_seen >= 5:
            err = self.model_error_rate
            if err >= 0.65:
                traits.append(
                    f"🎯⚠️ Équipe difficile à prévoir pour le modèle "
                    f"({int(round((1 - err) * 100))}% de pronostics 1X2 "
                    f"justes sur {self.model_predictions_seen})"
                )
            elif err <= 0.30:
                traits.append(
                    f"🎯✅ Équipe bien cernée par le modèle "
                    f"({int(round((1 - err) * 100))}% de pronostics 1X2 "
                    f"justes sur {self.model_predictions_seen})"
                )

        if self.total_matches < 3:
            if traits:
                return traits
            return ["⚠️ Données insuffisantes (< 3 matchs enregistrés)"]

        if self.mental_fragility >= 0.65:
            traits.append("😰 Fragilité mentale : souvent muette après avoir mené")
        elif self.mental_fragility <= 0.30:
            traits.append("🧠 Solidité mentale : gère bien ses avantages")

        if self.late_scorer >= 0.70:
            traits.append("⏰ Buteur tardif : marque souvent après la 70e minute")

        if self.strong_finisher >= 0.65:
            traits.append("💪 Fort finisseur : meilleure 2e mi-temps")
        elif self.strong_finisher <= 0.35:
            traits.append("📉 Faiblesse en fin de match")

        if self.struggles_low_block >= 0.65:
            traits.append("🏰 Difficultés contre les blocs bas")
        elif self.struggles_low_block <= 0.30:
            traits.append("🔑 Efficace contre les défenses basses")

        if self.home_dominance >= 0.75:
            traits.append("🏠 Très fort à domicile")
        elif self.home_dominance <= 0.30:
            traits.append("✈️ Plus fort en déplacement")

        if self.comeback_ability >= 0.65:
            traits.append("🔄 Capacité de remontée notable")

        if not traits:
            traits.append("⚖️ Profil équilibré, pas de trait dominant")

        return traits


# ── Gestionnaire de mémoire ───────────────────────────────────────────────────

class TeamMemoryManager:
    """Gère le chargement et la sauvegarde des profils."""

    def __init__(self) -> None:
        self._store: dict[str, TeamMemoryProfile] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        path = _memory_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            for key, data in raw.items():
                try:
                    self._store[key] = TeamMemoryProfile.from_dict(data)
                except Exception as exc:
                    logger.warning("[team_memory] Skip invalid profile %s: %s", key, exc)
        except Exception as exc:
            logger.error("[team_memory] Load failed: %s", exc)

    def save(self) -> None:
        if not self._dirty:
            return
        path = _memory_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {k: v.to_dict() for k, v in self._store.items()},
                    f, indent=2, ensure_ascii=False
                )
            self._dirty = False
        except Exception as exc:
            logger.error("[team_memory] Save failed: %s", exc)

    def _key(self, name: str) -> str:
        return name.strip().lower()

    def record_model_outcome(self, team_name: str, was_correct: bool) -> None:
        """
        V19.14 — Enregistre si le pronostic 1X2 affiché pour un match
        impliquant cette équipe était correct, une fois le résultat réel
        connu. Appelé depuis engine.tracking.settle() pour CHAQUE équipe du
        match (domicile et extérieur), donc automatiquement à chaque
        /resultat ou /autoresultat — jamais besoin de le déclencher à la
        main. Ne fait aucun appel réseau, aucune API externe.
        """
        if not team_name:
            return
        p = self.get(team_name)
        p.model_predictions_seen += 1
        if was_correct:
            p.model_correct_1x2 += 1
        p.last_updated = time.time()
        self._dirty = True

    def get(self, name: str) -> TeamMemoryProfile:
        key = self._key(name)
        if key not in self._store:
            self._store[key] = TeamMemoryProfile(
                name=name, name_normalized=key, last_updated=time.time()
            )
        return self._store[key]

    def update_from_result(
        self,
        team_name:     str,
        side:          str,   # "home" | "away"
        goals_scored:  int,
        goals_conceded: int,
        win:           bool,
        late_goals_scored:   int = 0,
        late_goals_conceded: int = 0,
        was_leading_then_lost: bool = False,
        came_back:     bool  = False,
        opp_style:     str   = "balanced",
    ) -> None:
        """Met à jour le profil d'une équipe après un résultat réel."""
        p = self.get(team_name)
        p.total_matches     += 1
        p.goals_scored_total += goals_scored
        p.goals_conceded_total += goals_conceded
        p.late_goals_scored  += late_goals_scored
        p.late_goals_conceded += late_goals_conceded

        if win:
            if side == "home":
                p.home_wins += 1
            else:
                p.away_wins += 1

        if was_leading_then_lost:
            p.matches_led_then_lost += 1

        if came_back:
            p.comebacks += 1

        if opp_style == "low_block":
            p.low_block_matches += 1
            p.low_block_scored  += goals_scored

        p.last_updated = time.time()
        p.recompute_traits()
        self._dirty = True

    def list_teams(self) -> list[tuple[str, TeamMemoryProfile]]:
        """Retourne tous les profils triés par nombre de matchs."""
        return sorted(
            self._store.items(),
            key=lambda x: x[1].total_matches,
            reverse=True
        )

    def reset_team(self, name: str) -> bool:
        key = self._key(name)
        if key in self._store:
            del self._store[key]
            self._dirty = True
            return True
        return False


# ── Singleton global ──────────────────────────────────────────────────────────

_manager: TeamMemoryManager | None = None


def get_manager() -> TeamMemoryManager:
    global _manager
    if _manager is None:
        _manager = TeamMemoryManager()
    return _manager


def get_profile(team_name: str) -> TeamMemoryProfile:
    return get_manager().get(team_name)


def save_all() -> None:
    if _manager is not None:
        _manager.save()


def format_team_memory(
    home_name: str,
    away_name: str,
) -> str:
    """Formatte les profils mémoire de deux équipes pour Telegram."""
    mgr = get_manager()
    home_p = mgr.get(home_name)
    away_p = mgr.get(away_name)

    lines = ["🧠 <b>Mémoire des équipes V16</b>"]

    for name, profile in [(home_name, home_p), (away_name, away_p)]:
        n = profile.total_matches
        # V19.15 — même bug que describe() (voir son commentaire) : ce garde
        # se basait uniquement sur total_matches, qui n'est alimenté par
        # aucun appelant actuel. Une équipe avec 0 total_matches mais un
        # historique /resultat réel (model_predictions_seen) affichait donc
        # "aucun historique enregistré" alors que describe() avait
        # justement un trait de fiabilité à montrer.
        if n == 0 and profile.model_predictions_seen == 0:
            lines.append(f"\n  <b>{name}</b> — aucun historique enregistré")
            continue
        suffix = f"{n} match(s) en mémoire" if n else f"{profile.model_predictions_seen} pronostic(s) réglé(s)"
        lines.append(f"\n  <b>{name}</b> ({suffix})")
        for trait in profile.describe():
            lines.append(f"    {trait}")

    return "\n".join(lines)
