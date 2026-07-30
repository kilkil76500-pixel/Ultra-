"""
engine/formatting.py — Couche présentation Telegram.

Ce module regroupe toutes les fonctions de rendu HTML/texte destinées
à l'interface Telegram.  Il ne contient **aucune logique métier** :
pas d'appel API, pas de calcul de probabilités, pas d'accès base de données.

Importé par bot.py ; les modules engine/* ne doivent PAS l'importer
(dépendance uniquement dans le sens bot.py → formatting.py → engine/*).

Fonctions publiques
───────────────────
  Primitives visuelles
    bar(current, total, width)        → barre de progression colorée
    confidence_bar(pct_val, width)    → barre de fiabilité
    pct_int(current, total)           → pourcentage entier
    progress_text(current, total, label, frame)  → bloc de chargement animé
    outcome_badge(home, draw, away)   → émoji du résultat probable
    divider(char, width)              → séparateur texte
    section(title, emoji)             → en-tête de section HTML
    chunk(text)                       → découpe un texte trop long en morceaux
    kickoff(snapshot)                 → heure de coup d'envoi formatée

  Menus Telegram (InlineKeyboardMarkup)
    menu()                            → menu principal
    back_menu()                       → bouton retour seul
    match_keyboard(items, prefix)     → liste de matchs cliquables

  Blocs texte complexes
    cache_text(items, title)          → liste de matchs du cache
    score_distribution_text(top_scores, modal_score)
    extended_stats_text(pred)
    prediction_text(result)           → message complet d'analyse V18
"""

from __future__ import annotations

import html
from typing import Any, TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from engine.utils import pct

if TYPE_CHECKING:
    from engine import scanner


# ── Constantes ────────────────────────────────────────────────────────────────

MAX_MESSAGE = 3800

_FRAMES_SCAN = [
    "🌐 Connexion à Forebet…",
    "📡 Lecture du calendrier…",
    "⚽ Détection des matchs…",
    "🔍 Inspection des fiches…",
    "📊 Collecte des stats…",
    "🏟️  Analyse des ligues…",
    "🔄 Chargement des données…",
    "💡 Traitement en cours…",
    "🧮 Calculs statistiques…",
    "✨ Presque terminé…",
]

_FRAMES_ANALYZE = [
    "🧮 Initialisation du moteur V18…",
    "📐 Calcul des indices de force…",
    "🧠 Analyse tactique automatique…",
    "⚡ xG V16 (tirs, grosses occasions)…",
    "🎲 Monte-Carlo V5 — scénarios…",
    "📊 Distribution des scénarios…",
    "🔬 Analyse BTTS / O/U + tactique…",
    "🔮 Scénarios V18 plausibles…",
    "💡 Indice de confiance sur 100…",
    "✅ Résultat prêt !",
]

_BAR_FILLED = ["🟥", "🟧", "🟨", "🟩", "🟦", "🟪"]
_BAR_EMPTY  = "⬜"
_BAR_WIDTH  = 10

_RESULT_BADGES = {
    "home": "🏠🔥",
    "draw": "🤝✨",
    "away": "✈️⚡",
}
_CONFIDENCE_EMOJI = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}
_RISK_EMOJI       = {"LOW": "✅", "MEDIUM": "⚠️", "HIGH": "❌"}

_STYLE_EMOJI = {
    "offensive":      "⚡",
    "possession":     "🎯",
    "pressing":       "🔥",
    "counter_attack": "🏹",
    "low_block":      "🏰",
    "low_intensity":  "😴",
    "balanced":       "⚖️",
}


# ── Primitives visuelles ──────────────────────────────────────────────────────

def bar(current: int, total: int, width: int = _BAR_WIDTH) -> str:
    """Barre de progression colorée (emojis pleins + carrés vides)."""
    if total <= 0:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, current / total))
    filled = int(ratio * width)
    cells  = [_BAR_FILLED[i % len(_BAR_FILLED)] for i in range(filled)]
    cells += [_BAR_EMPTY] * (width - filled)
    return "".join(cells)


def confidence_bar(pct_val: float, width: int = 10) -> str:
    """Barre de confiance/probabilité (░ pour les cases vides)."""
    filled = round(pct_val / 100 * width)
    cells  = [_BAR_FILLED[i % len(_BAR_FILLED)] for i in range(filled)]
    cells += ["░"] * (width - filled)
    return "".join(cells)


def pct_int(current: int, total: int) -> int:
    if total <= 0:
        return 0
    return min(100, int(current / total * 100))


def progress_text(current: int, total: int, label: str, frame: int = 0) -> str:
    """Bloc de chargement animé avec barre + étape courante."""
    pct_val = pct_int(current, total)
    bar_str = bar(current, total)
    if "Forebet" in label or "calendrier" in label or "collecte" in label or "scan" in label.lower():
        frames = _FRAMES_SCAN
    else:
        frames = _FRAMES_ANALYZE
    step_label = frames[frame % len(frames)]
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  {bar_str}  {pct_val}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  {step_label}"
    )


def outcome_badge(home: float, draw: float, away: float, predicted_outcome: str = "") -> str:
    """Retourne l'émoji du résultat le plus probable.

    V17 : si *predicted_outcome* est fourni (calculé avec draw_detection_factor),
    on l'utilise directement pour la décision d'affichage — les nuls sont ainsi
    détectés même quand draw_prob brute est sous-estimée.
    """
    if predicted_outcome in _RESULT_BADGES:
        return _RESULT_BADGES[predicted_outcome]
    values = {"home": home, "draw": draw, "away": away}
    return _RESULT_BADGES[max(values, key=values.get)]


def divider(char: str = "━", width: int = 28) -> str:
    return char * width


def section(title: str, emoji: str = "") -> str:
    prefix = f"{emoji}  " if emoji else ""
    return f"\n{prefix}<b>{title}</b>\n{divider()}"


def chunk(text: str) -> list[str]:
    """Découpe un texte trop long en morceaux ≤ MAX_MESSAGE caractères."""
    if len(text) <= MAX_MESSAGE:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.splitlines():
        if current and length + len(line) + 1 > MAX_MESSAGE:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def kickoff(snapshot: dict[str, Any]) -> str:
    """Retourne l'heure locale française avec son équivalent UTC."""
    timestamp = snapshot.get("timestamp")
    if not timestamp:
        return "heure inconnue"
    from datetime import datetime, timezone, timedelta
    try:
        try:
            from zoneinfo import ZoneInfo
            tz_eu = ZoneInfo("Europe/Paris")
        except Exception:
            # Repli sur UTC+2 si zoneinfo indisponible
            tz_eu = timezone(timedelta(hours=2))
        dt = datetime.fromtimestamp(int(timestamp), tz=tz_eu)
        dt_utc = dt.astimezone(timezone.utc)
        return f"{dt:%H:%M} (heure fr) · {dt_utc:%H:%M} UTC"
    except (ValueError, OSError, TypeError):
        return "heure inconnue"


# ── Menus Telegram ────────────────────────────────────────────────────────────

def menu() -> InlineKeyboardMarkup:
    """Menu principal du bot."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔎  Scanner les matchs", callback_data="menu:scan"),
            InlineKeyboardButton("📅  Matchs du jour",     callback_data="menu:today"),
        ],
        [
            InlineKeyboardButton("🎯  Choisir un match",   callback_data="menu:predict"),
            InlineKeyboardButton("🎲  Match aléatoire",    callback_data="menu:example"),
        ],
        [
            InlineKeyboardButton("📊  Fiabilité du bot",   callback_data="menu:fiabilite"),
            InlineKeyboardButton("🗑  Vider le cache",     callback_data="menu:delete"),
        ],
        [
            InlineKeyboardButton("🔄  Résultats auto",     callback_data="menu:autoresultat"),
            InlineKeyboardButton("ℹ️  Aide",               callback_data="menu:help"),
        ],
        [
            InlineKeyboardButton("🧠  Apprentissage V18",  callback_data="menu:apprentissage"),
            InlineKeyboardButton("⚙️  Recalibrer",         callback_data="menu:recalibrer"),
        ],
        [
            InlineKeyboardButton("🧪  Valider",            callback_data="menu:valider"),
            InlineKeyboardButton("📁  Versions",           callback_data="menu:versions"),
        ],
        [
            InlineKeyboardButton("🧬  Apprentissage V2",   callback_data="menu:apprentissage2"),
            InlineKeyboardButton("🧠  Mémoire équipes",    callback_data="menu:memoire"),
        ],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠  Menu principal", callback_data="menu:home")]
    ])


def match_keyboard(
    items: list[dict[str, Any]],
    prefix: str = "pick",
    page: int = 0,
    page_size: int = 99,
) -> InlineKeyboardMarkup:
    """Clavier inline paginé : page_size matchs par page + navigation ◀️/▶️ + retour menu."""
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end   = min(start + page_size, total)

    buttons: list[list[InlineKeyboardButton]] = []
    for idx in range(start, end):
        item  = items[idx]
        home  = str(item.get("home") or "Home")
        away  = str(item.get("away") or "Away")
        label = f"⚽ {idx + 1}. {home} – {away}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"{prefix}:{idx}")])

    # Barre de navigation inter-pages (uniquement si plusieurs pages)
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Préc.", callback_data=f"page:{page - 1}"))
        nav.append(InlineKeyboardButton(
            f"📄 {page + 1}/{total_pages}  ({start + 1}–{end} sur {total})",
            callback_data="noop",
        ))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Suiv. ▶️", callback_data=f"page:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🏠  Menu principal", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


# ── Blocs texte ───────────────────────────────────────────────────────────────

def cache_text(items: list[dict[str, Any]], title: str = "Matchs disponibles") -> str:
    """Liste des matchs disponibles dans le cache."""
    if not items:
        return (
            f"{divider()}\n"
            "📭  <b>AUCUN MATCH</b>\n"
            f"{divider()}\n\n"
            "Lance <b>/scan</b> pour charger les matchs du jour."
        )
    header = (
        f"{divider()}\n"
        f"📅  {html.escape(title)}\n"
        f"⚽  <b>{len(items)} match{'s' if len(items) > 1 else ''} "
        f"trouvé{'s' if len(items) > 1 else ''}</b>\n"
        f"{divider()}\n"
    )
    lines = [header]
    for index, item in enumerate(items, start=1):
        home = html.escape(str(item.get("home", "?")))
        away = html.escape(str(item.get("away", "?")))
        kick = kickoff(item)
        lines.append(
            f"  <b>{index}.</b> 🏠 {home}\n"
            f"       ✈️  {away}\n"
            f"       🕒 <i>{kick}</i>\n"
        )
    lines.append("👇 <b>Appuie sur un match ci-dessous pour lancer l'analyse</b>")
    return "\n".join(lines)


def score_distribution_text(top_scores: list[tuple[str, float]], modal_score: str) -> str:
    """Distribution des scorelines simulés (Monte-Carlo)."""
    if not top_scores:
        return ""
    total = sum(prob for _, prob in top_scores)
    lines = [
        f"\n{divider('─')}",
        "📊  <b>DISTRIBUTION DES SCORELINES</b>",
        "<i>Monte-Carlo · 100 000 simulations</i>",
        divider("─"),
        "  ⚠️ <i>Fréquences simulées — pas un pronostic fixe</i>\n",
    ]
    for i, (score, prob) in enumerate(top_scores, start=1):
        bar_w  = 10
        filled = round(prob * bar_w / max(total, 0.001))
        bar_s  = "".join(_BAR_FILLED[j % len(_BAR_FILLED)] for j in range(filled))
        bar_s += "░" * (bar_w - filled)
        marker = "  ← 🔝" if score == modal_score else ""
        lines.append(f"  {i}. <b>{score:>5}</b>  {bar_s}  {prob * 100:4.1f}%{marker}")

    if len(top_scores) >= 2:
        top_pct = top_scores[0][1] * 100
        lines.append(
            f"\n💡 Score le + fréquent : <b>{html.escape(modal_score)}</b> "
            f"→ {top_pct:.1f}% des simulations\n"
            f"   Dispersion sur <b>{len(top_scores)}</b> scorelines différents"
        )
    return "\n".join(lines)


def extended_stats_text(pred: Any) -> str:
    """Bloc indice de force V18 (si données étendues disponibles)."""
    if not getattr(pred, "extended_data_used", False):
        return ""
    home_idx = getattr(pred, "home_index", 50.0)
    away_idx = getattr(pred, "away_index", 50.0)
    return (
        f"\n{divider('─')}\n"
        f"🔬  <b>INDICE DE FORCE V18</b>\n"
        f"{divider('─')}\n"
        f"  🏠 <b>{html.escape(pred.home_name)}</b>  →  <b>{home_idx:.1f} / 100</b>\n"
        f"  ✈️  <b>{html.escape(pred.away_name)}</b>  →  <b>{away_idx:.1f} / 100</b>\n"
        f"  <i>Attaque · Défense · Forme · H2H · Terrain</i>\n"
        f"  <i>Classement · Motivation · Physique</i>"
    )


def prediction_text(result: "scanner.ScanResult", pred_id: int | None = None) -> str:
    """
    Message complet d'analyse V18 pour un match.

    C'est le plus grand bloc de formatage du bot — environ 200 lignes
    de construction HTML pour Telegram.  Aucune logique métier ici :
    toutes les valeurs viennent de `result.prediction` et `result.snapshot`.
    """
    pred       = result.prediction
    snapshot   = result.snapshot or {}
    badge      = outcome_badge(pred.home_win_prob, pred.draw_prob, pred.away_win_prob,
                               predicted_outcome=getattr(pred, "predicted_outcome", ""))
    conf_emoji = _CONFIDENCE_EMOJI.get(pred.confidence, "⚪")
    risk_emoji = _RISK_EMOJI.get(pred.risk_level, "⚪")
    conf_bar   = confidence_bar(pred.confidence_pct)

    # En-tête du match
    lines = [
        divider(),
        f"{badge}  <b>ANALYSE V18</b>",
        divider(),
        "",
        f"  🏠 <b>{html.escape(pred.home_name)}</b>",
        f"  ⚔️   vs",
        f"  ✈️  <b>{html.escape(pred.away_name)}</b>",
        f"  🕒 <i>{result.kickoff}</i>",
        f"  🏟️  <i>{html.escape(result.league_name)}</i>",
        "",
    ]

    # Probabilités 1X2
    home_bar  = confidence_bar(pred.home_win_prob * 100, 8)
    draw_bar  = confidence_bar(pred.draw_prob * 100, 8)
    away_bar  = confidence_bar(pred.away_win_prob * 100, 8)
    _outcome_probs = {
        "home": pred.home_win_prob,
        "draw": pred.draw_prob,
        "away": pred.away_win_prob,
    }
    _predicted_outcome = getattr(pred, "predicted_outcome", "") or max(
        _outcome_probs, key=_outcome_probs.get
    )
    _predicted_prob = _outcome_probs.get(_predicted_outcome, 0.0)
    _outcome_label = {
        "home": f"Victoire {html.escape(pred.home_name)}",
        "draw": "Match nul",
        "away": f"Victoire {html.escape(pred.away_name)}",
    }.get(_predicted_outcome, "—")
    lines += [
        divider(),
        "🧮  <b>PROBABILITÉS 1X2</b>",
        divider(),
        f"  🏠 Domicile    {home_bar}  <b>{pct(pred.home_win_prob)}</b>",
        f"  🤝 Nul         {draw_bar}  <b>{pct(pred.draw_prob)}</b>",
        f"  ✈️  Extérieur  {away_bar}  <b>{pct(pred.away_win_prob)}</b>",
        "",
        f"  👉 <b>Pronostic du bot : {_outcome_label} — {pct(_predicted_prob)}</b>",
        "",
    ]

    # xG
    lines += [
        divider(),
        "⚽  <b>OBJECTIFS xG</b>",
        divider(),
        f"  🏠 <b>{html.escape(pred.home_name)}</b>",
        f"     λ Poisson = <b>{pred.home_xg}</b>  │  moy. sim. = <b>{pred.mc_mean_home_goals:.2f}</b>",
        f"  ✈️  <b>{html.escape(pred.away_name)}</b>",
        f"     λ Poisson = <b>{pred.away_xg}</b>  │  moy. sim. = <b>{pred.mc_mean_away_goals:.2f}</b>",
        "",
    ]

    # Analyse tactique V18
    home_style_emoji = _STYLE_EMOJI.get(pred.home_tactical_style, "⚽")
    away_style_emoji = _STYLE_EMOJI.get(pred.away_tactical_style, "⚽")
    home_style_label = pred.home_tactical_style.replace("_", " ").title()
    away_style_label = pred.away_tactical_style.replace("_", " ").title()
    tact_lines = [
        divider("─"),
        "🧠  <b>ANALYSE TACTIQUE V18</b>",
        divider("─"),
        f"  🏠 <b>{html.escape(pred.home_name)}</b>  {home_style_emoji}  <i>{html.escape(home_style_label)}</i>",
    ]
    if pred.home_tactical_desc:
        tact_lines.append(f"     <i>{html.escape(pred.home_tactical_desc)}</i>")
    tact_lines.append(
        f"  ✈️  <b>{html.escape(pred.away_name)}</b>  {away_style_emoji}  <i>{html.escape(away_style_label)}</i>"
    )
    if pred.away_tactical_desc:
        tact_lines.append(f"     <i>{html.escape(pred.away_tactical_desc)}</i>")
    if pred.tactical_btts_adj != 0 or pred.tactical_over25_adj != 0:
        btts_sign = "+" if pred.tactical_btts_adj >= 0 else ""
        ou_sign   = "+" if pred.tactical_over25_adj >= 0 else ""
        tact_lines.append(
            f"  📐 Ajust. tactiques :  BTTS {btts_sign}{pred.tactical_btts_adj * 100:.1f}%  "
            f"·  O/U2.5 {ou_sign}{pred.tactical_over25_adj * 100:.1f}%"
        )
    tact_lines.append("")
    lines += tact_lines

    # Indice de force V13
    ext_text = extended_stats_text(pred)
    if ext_text:
        lines.append(ext_text)
        lines.append("")

    # Composantes de force
    strength = getattr(pred, "strength_index", {})
    if strength:
        lines += [divider("─"), "🔩  <b>COMPOSANTES DE L'INDICE</b>", divider("─")]
        for side, label in (("home", "🏠"), ("away", "✈️")):
            comp = (strength.get(side) or {}).get("composantes") or {}
            if comp:
                parts = "  ·  ".join(f"<i>{html.escape(k)}</i> {v:.1f}" for k, v in comp.items())
                lines.append(f"  {label}  {parts}")
        lines.append("")

    # Distribution des scores
    dist_text = score_distribution_text(pred.top_scores, pred.modal_score)
    if dist_text:
        lines.append(dist_text)

    # Périodes de but
    if pred.timing_windows:
        lines += ["", divider("─"), "⏱  <b>PÉRIODES DE BUT ACTIVES</b>", divider("─")]
        for w in pred.timing_windows:
            prob_bar = confidence_bar(w.probability * 100, 6)
            lines.append(
                f"  • <b>{html.escape(w.label)}</b>  {prob_bar}  "
                f"{w.probability * 100:.1f}%  <i>({w.sample_goals:,} simulés)</i>"
            )
        lines.append("")

    if pred.goal_minutes:
        minutes = "  ".join(
            f"<b>{m}'</b> <i>({p * 100:.0f}%)</i>"
            for m, p in pred.goal_minutes[:5]
        )
        lines.append(f"📍 <b>Minutes fréquentes :</b>  {minutes}\n")

    # Marchés dérivés — V17 : recommandation binaire calibrée
    btts_bar   = confidence_bar(pred.btts_prob * 100, 6)
    over25_bar = confidence_bar(pred.over25_prob * 100, 6)
    over35_bar = confidence_bar(pred.over35_prob * 100, 6)
    _btts_label  = "✅ <b>OUI</b>"  if getattr(pred, "btts_yes",  pred.btts_prob  >= 0.56) else "❌ <b>NON</b>"
    _ou25_label  = "✅ <b>OVER</b>" if getattr(pred, "ou25_yes",  pred.over25_prob >= 0.54) else "❌ <b>UNDER</b>"

    # Double chance — dérivée directement des probabilités 1X2 (aucune donnée
    # supplémentaire requise). On recommande l'option qui exclut le résultat
    # le moins probable des trois (= la double chance de plus forte probabilité).
    _dc_options = {
        "1X": pred.home_win_prob + pred.draw_prob,
        "12": pred.home_win_prob + pred.away_win_prob,
        "X2": pred.draw_prob + pred.away_win_prob,
    }
    _dc_recommended = max(_dc_options, key=_dc_options.get)

    lines += [
        divider(),
        "📈  <b>MARCHÉS DÉRIVÉS</b>",
        divider(),
        f"  ⚡ BTTS (2 équipes marquent)  {btts_bar}  <b>{pct(pred.btts_prob)}</b>  → {_btts_label}",
        f"  📊 Plus de 2,5 buts           {over25_bar}  <b>{pct(pred.over25_prob)}</b>  → {_ou25_label}",
        f"  📉 Moins de 2,5 buts          {confidence_bar(pred.under25_prob * 100, 6)}  <b>{pct(pred.under25_prob)}</b>",
        f"  📊 Plus de 3,5 buts           {over35_bar}  <b>{pct(pred.over35_prob)}</b>",
        f"  🔀 BTTS & +2,5 buts           {confidence_bar(pred.btts_yes_over25_prob * 100, 6)}  <b>{pct(pred.btts_yes_over25_prob)}</b>",
        f"  🔀 BTTS & −2,5 buts           {confidence_bar(pred.btts_yes_under25_prob * 100, 6)}  <b>{pct(pred.btts_yes_under25_prob)}</b>",
        "",
        "  🛡️  <b>Double chance</b>",
        f"    1X  {html.escape(pred.home_name)} ou nul        {confidence_bar(_dc_options['1X'] * 100, 6)}  <b>{pct(_dc_options['1X'])}</b>{'  → ✅ recommandée' if _dc_recommended == '1X' else ''}",
        f"    12  Pas de nul                 {confidence_bar(_dc_options['12'] * 100, 6)}  <b>{pct(_dc_options['12'])}</b>{'  → ✅ recommandée' if _dc_recommended == '12' else ''}",
        f"    X2  Nul ou {html.escape(pred.away_name)}        {confidence_bar(_dc_options['X2'] * 100, 6)}  <b>{pct(_dc_options['X2'])}</b>{'  → ✅ recommandée' if _dc_recommended == 'X2' else ''}",
        "",
    ]

    # V19.13 — marché conseillé d'après la fiabilité historique réelle du
    # bot (pas la probabilité affichée, mais son taux de réussite passé à
    # confiance comparable). N'apparaît que si l'historique est jugé
    # suffisant par engine.market_edge — sinon on affiche une phrase neutre
    # plutôt qu'un pourcentage qui donnerait une fausse impression de précision.
    if getattr(pred, "recommended_market_data_ok", False) and getattr(pred, "recommended_market_reason", ""):
        lines += [
            divider("─"),
            "💡  <b>MARCHÉ CONSEILLÉ</b>  <i>(d'après l'historique réel du bot)</i>",
            divider("─"),
            f"  👉 <b>{html.escape(pred.recommended_market_label)}</b>",
            f"  <i>{html.escape(pred.recommended_market_reason)}</i>",
            "",
        ]
    elif getattr(pred, "recommended_market_reason", ""):
        lines += [
            divider("─"),
            "💡  <b>MARCHÉ CONSEILLÉ</b>",
            divider("─"),
            f"  <i>{html.escape(pred.recommended_market_reason)}</i>",
            "",
        ]

    # V20.6 — deux blocs distincts au lieu d'un seul mélangé : les signaux
    # de fiabilité réelle (⚠️ taux d'échec historique, écart cote/modèle —
    # "sois prudent") et les notes purement explicatives (ℹ️ dispersion des
    # scénarios, tier de ligue, cohérence marginal/modal — "ces chiffres
    # corrects ne se contredisent pas vraiment") avaient le même poids
    # visuel sous un même "🔍 ANOMALIES DÉTECTÉES", ce qui donnait
    # l'impression que tout le pronostic était fragile même quand la
    # plupart des lignes étaient juste de la pédagogie.
    if getattr(pred, "anomaly_warnings", None):
        lines += [
            divider("─"),
            "⚠️  <b>SIGNAUX DE FIABILITÉ</b>",
            divider("─"),
        ]
        for message in pred.anomaly_warnings:
            lines.append(f"  {html.escape(message)}")
        lines.append("")

    if getattr(pred, "anomaly_notes", None):
        lines += [
            divider("─"),
            "ℹ️  <b>NOTES EXPLICATIVES</b>",
            "  <i>Pourquoi certains chiffres peuvent sembler se contredire — rien d'alarmant.</i>",
            divider("─"),
        ]
        for message in pred.anomaly_notes:
            lines.append(f"  {html.escape(message)}")
        lines.append("")
    elif getattr(pred, "anomaly_messages", None) and not getattr(pred, "anomaly_warnings", None):
        # Repli si jamais anomaly_warnings/anomaly_notes ne sont pas peuplés
        # (ancien PredictionResult construit sans passer par scanner.py) —
        # non-régressif : on retombe sur l'ancien rendu à liste unique.
        lines += [
            divider("─"),
            "🔍  <b>ANOMALIES DÉTECTÉES</b>",
            divider("─"),
        ]
        for message in pred.anomaly_messages:
            lines.append(f"  {html.escape(message)}")
        lines.append("")

    # Référence Forebet
    forebet    = snapshot.get("forebet") or {}
    validation = snapshot.get("validation") or {}
    forebet_lines = []
    if validation:
        probs = validation.get("probabilities") or {}
        if probs:
            forebet_lines.append(
                f"  🌐 <b>Forebet 1X2 :</b>  "
                f"🏠 {probs.get('home_win', '—')}%  "
                f"🤝 {probs.get('draw', '—')}%  "
                f"✈️ {probs.get('away_win', '—')}%"
            )
    if forebet and forebet.get("score"):
        fh, fa = forebet["score"]
        forebet_lines.append(
            f"  📋 <b>Pronostic Forebet :</b> <b>{fh}-{fa}</b>  "
            f"<i>(référence algo — non contraignant)</i>"
        )
        if pred.forebet_score and pred.forebet_score_prob > 0:
            rank_str = f"#{pred.forebet_score_rank}" if pred.forebet_score_rank else "hors top-5"
            forebet_lines.append(
                f"  → Proba simulée pour <b>{html.escape(pred.forebet_score)}</b> : "
                f"<b>{pred.forebet_score_prob * 100:.2f}%</b>  <i>({rank_str})</i>"
            )
    if forebet_lines:
        lines += [divider("─"), "🌐  <b>RÉFÉRENCE FOREBET</b>", divider("─")]
        lines.extend(forebet_lines)
        lines.append("")

    # Qualité de l'analyse
    extended_flag = (
        "✅ données étendues Forebet"
        if getattr(pred, "extended_data_used", False)
        else "⚠️ données de base"
    )
    lines += [
        divider(),
        "🔬  <b>QUALITÉ DE L'ANALYSE</b>",
        divider(),
        f"  {conf_emoji} Confiance    {conf_bar}  <b>{pred.confidence_pct:.0f}%</b>  <i>({pred.confidence})</i>",
        f"  🎓 Grade : <b>{pred.confidence_v2_grade}</b>  ({html.escape(pred.confidence_v2_label)})  "
        f"<i>— détail du % ci-dessus, voir la répartition dans /fiabilite</i>",
        f"  {risk_emoji} Risque modèle :  <b>{pred.risk_level}</b>",
        f"  🎲 <b>{pred.mc_iterations:,}</b> simulations  ·  <b>{pred.distinct_scorelines}</b> scorelines distincts",
        f"  🌀 Chaos : <b>{pred.chaos_level:.3f}</b>  │  Émotion tribunes : <b>{pred.crowd_emotion:.2f}</b>",
    ]
    if pred.h2h_matches:
        lines.append(f"  ⚔️  H2H utilisés : <b>{pred.h2h_matches}</b> matchs")
    if pred.injuries_home or pred.injuries_away:
        lines.append(
            f"  🏥 Absences :  🏠 <b>{pred.injuries_home}</b>  ✈️ <b>{pred.injuries_away}</b>"
        )
    lines.append(f"  📦 Source : <i>{extended_flag}</i>")
    lines += [
        "",
        divider("═"),
        "  ⚠️ <i>Ces probabilités sont des estimations statistiques.</i>",
        "  <i>Le football reste imprévisible.</i>",
        "  <i>Ne pas utiliser comme conseils de paris.</i>",
    ]

    # V20.2 — pied de page identifiant, restauré indépendamment du bloc
    # "Scénarios V18" (retiré par décision produit confirmée). Ce pronostic
    # a besoin d'un identifiant visible pour que /resultat <id> <score>
    # reste utilisable sans devoir lancer /resultat à vide pour le
    # retrouver dans la liste des pronostics en attente.
    if pred_id is not None:
        lines += [
            "",
            divider("─"),
            f"🆔  Pronostic n°<b>{pred_id}</b>",
            "📥  Quand le match sera terminé, envoie :",
            f"   <code>/resultat {pred_id} 2-1</code>  (score réel)",
        ]

    return "\n".join(lines)
