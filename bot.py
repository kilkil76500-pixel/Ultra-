"""Telegram interface — Football Intelligence V19 · xG V16 · MC V5 · Tactique · Mémoire."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

import config

config.validate()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from engine import cache_store, scanner, tracking, web_collector
from engine import calibration as calibration_module
from engine import learning as learning_module
from engine import validation as validation_module
from engine import versioning as versioning_module
# ── V16 imports ───────────────────────────────────────────────────────────────
from engine import learning_v2 as learning_v2_module
from engine import auto_learning as auto_learning_module
from engine import team_memory as team_memory_module
from engine import learning_v18 as learning_v18_module
from engine import confidence_v2 as confidence_v2_module
from engine import tactical as tactical_module
from engine import history_query as history_query_module
from engine import xg_backtest as xg_backtest_module
from engine import strength_ablation as strength_ablation_module
from engine import h2h_audit as h2h_audit_module
from engine import league_calibration as league_calibration_module
from engine.utils import pct

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# ── Couche de présentation (séparée de la logique métier) ────────────────────
# Toutes les fonctions de formatage Telegram vivent dans engine/formatting.py.
# On les importe ici avec leurs anciens noms préfixés _ pour ne pas casser
# les call sites existants dans ce fichier.
from engine.formatting import (
    bar               as _bar,
    confidence_bar    as _confidence_bar,
    pct_int           as _pct_int,
    progress_text     as _progress_text,
    outcome_badge     as _outcome_badge,
    divider           as _divider,
    section           as _section,
    chunk             as _chunk,
    kickoff           as _kickoff,
    menu              as _menu,
    back_menu         as _back_menu,
    match_keyboard    as _match_keyboard,
    cache_text        as _cache_text,
    score_distribution_text as _score_distribution_text,
    extended_stats_text as _extended_stats_text,
    prediction_text   as _prediction_text,
    MAX_MESSAGE,
)

# ── Stickers (un seul emoji = grand sticker animé dans Telegram) ───────────────
_STICKER_SCAN_START  = "🔍"
_STICKER_SCAN_DONE   = "🏆"
_STICKER_PREDICT     = "🔮"
_STICKER_FIRE        = "🔥"
_STICKER_GOAL        = "⚽"
_STICKER_ERROR       = "😬"
_STICKER_TROPHY      = "🏆"
_STICKER_CHART       = "📊"
_STICKER_BRAVO       = "🎉"
_STICKER_LEARNING    = "🧠"
_STICKER_CALIBRATE   = "⚙️"
_STICKER_VALIDATE    = "🧪"
_STICKER_VERSIONS    = "📁"
_STICKER_RESULT      = "📌"
_STICKER_DELETED     = "🗑"


# ── Commandes ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    # Grand sticker animé Telegram (emoji seul = grand affichage)
    await msg.reply_text(_STICKER_GOAL)
    await msg.reply_text(
        f"{_divider()}\n"
        "⚽  <b>FOOTBALL INTELLIGENCE</b>\n"
        "<b>V19 · xG · MC V5</b>\n"
        f"{_divider()}\n\n"
        "🔮 <b>100 000 simulations</b> par scénario\n"
        "📊 Indice de Force <b>8 dimensions</b>\n"
        "🧠 Analyse tactique automatique\n"
        "🎯 Confiance <b>sur 100</b> · Mémoire équipes\n\n"
        "👇 <b>Que veux-tu faire ?</b>",
        parse_mode="HTML",
        reply_markup=_menu(),
    )


async def _show_today(message, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.user_data.get("scan_ready"):
        await message.reply_text(
            "⏳ <b>Lance /scan d'abord</b> pour charger les matchs du jour.",
            parse_mode="HTML",
            reply_markup=_back_menu(),
        )
        return
    items = scanner.cached_snapshots()
    ctx.user_data["match_keys"] = [str(item.get("cache_key")) for item in items]
    text   = _cache_text(items)
    chunks = _chunk(text)
    for chunk in chunks[:-1]:
        await message.reply_text(chunk, parse_mode="HTML")
    await message.reply_text(
        chunks[-1],
        parse_mode="HTML",
        reply_markup=_match_keyboard(items) if items else _back_menu(),
    )


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_today(update.effective_message, ctx)


def _parse_scan_label(label: str) -> dict:
    """
    Extrait depuis le label brut de web_collector :
      - step        : 1 | 2 | 3
      - done        : True si l'étape est terminée
      - total       : nombre total de matchs connus
      - processed   : nombre de matchs traités (étape 3)
      - home / away : noms des équipes en cours (étape 3)
    """
    import re as _re
    info: dict = {"step": 1, "done": False, "total": 0, "processed": 0,
                  "home": None, "away": None}

    # "Étape 1/3 — …" / "Étape 2/3 — …" / "Étape 3/3 — …"
    m = _re.search(r"Étape (\d)/3", label)
    if m:
        info["step"] = int(m.group(1))

    # "Terminé — 24 matchs"
    if label.startswith("Terminé"):
        info["step"] = 3
        info["done"] = True
        m = _re.search(r"(\d+) match", label)
        if m:
            info["total"] = int(m.group(1))
            info["processed"] = info["total"]
        return info

    # "Étape 1/3 terminée — 18 match(s) retenu(s)."
    if "terminée" in label:
        info["done"] = True
        m = _re.search(r"(\d+) match", label)
        if m:
            info["total"] = int(m.group(1))
        return info

    # "Étape 2/3 — validation : 5/24".  Do not match the step fraction
    # ("2/3") itself: it is metadata, not a match counter.
    if "validation" in label.lower():
        m = _re.search(r":\s*(\d+)/(\d+)", label)
        if m:
            info["processed"] = int(m.group(1))
            info["total"]     = int(m.group(2))
    else:
        m = _re.search(r"(\d+)\s+match", label)
        if m:
            info["total"] = int(m.group(1))

    # "Étape 3/3 — 12/24 · PSG – Lyon (+8 stats étendues)"
    # Extraire le compteur de matchs traités (distinct de la fraction d'étape "3/3").
    # Sans cette extraction, _build_scan_progress calcule pct_val avec processed=0
    # et la barre reste bloquée à 50 % pendant toute l'étape 3.
    if info["step"] == 3 and not info["done"]:
        m = _re.search(r"—\s*(\d+)/(\d+)\s*·", label)
        if m:
            info["processed"] = int(m.group(1))
            info["total"]     = int(m.group(2))

    # Équipes défilant dans l'étape 3
    m = _re.search(r"·\s*(.+?)\s*–\s*(.+?)\s*\(", label)
    if m:
        info["home"] = m.group(1).strip()
        info["away"] = m.group(2).strip()

    return info


def _build_scan_progress(
    current: int,
    total_steps: int,
    label: str,
    frame: int,
    state: dict,          # mutable state shared across calls
) -> str:
    """
    Construit le message de progression du scan avec :
    - barre arc-en-ciel
    - étape en cours
    - compteur de matchs
    - nom des équipes défilant (étape 3)
    """
    info = _parse_scan_label(label)

    # Mettre à jour l'état partagé
    if info["total"] > state.get("total", 0):
        state["total"] = info["total"]
    if info["processed"] > state.get("processed", 0):
        state["processed"] = info["processed"]
    if info["home"] and info["away"]:
        state["last_home"] = info["home"]
        state["last_away"] = info["away"]
        # Historique des 3 derniers matchs vus
        seen = state.setdefault("seen_teams", [])
        entry = f"{info['home']} – {info['away']}"
        if not seen or seen[-1] != entry:
            seen.append(entry)
            if len(seen) > 3:
                seen.pop(0)

    # Progression globale : on mappe les 3 étapes sur 0-100%
    step      = info["step"]
    processed = state.get("processed", 0)
    total     = state.get("total", 0)

    if step == 1 and not info["done"]:
        pct_val = 5
    elif step == 1 and info["done"]:
        pct_val = 20
    elif step == 2:
        pct_val = 20 + int((processed / max(total, 1)) * 30)
    elif step == 3:
        pct_val = 50 + int((processed / max(total, 1)) * 48)
    else:
        pct_val = 100

    bar = _bar(pct_val, 100)

    # Ligne d'étape
    _STEP_LABELS = {
        1: "🌐 Étape 1/3 — Collecte du calendrier Forebet",
        2: "🔍 Étape 2/3 — Validation des pronostics 1X2",
        3: "📊 Étape 3/3 — Enrichissement des données",
    }
    step_line = _STEP_LABELS.get(step, "🔄 Analyse en cours…")
    if info["done"] and step < 3:
        step_line += " ✅"

    # Compteur de matchs
    if total > 0:
        count_line = f"  ⚽ <b>{total}</b> match{'s' if total > 1 else ''} trouvé{'s' if total > 1 else ''}"
        if step == 3 and processed > 0:
            count_line += f"  ·  <b>{processed}/{total}</b> traités"
    else:
        count_line = "  ⚽ Recherche des matchs…"

    # Équipes défilant
    seen = state.get("seen_teams", [])
    teams_block = ""
    if seen:
        # Afficher les 3 derniers, le dernier en gras
        rows = []
        for i, entry in enumerate(seen):
            if i == len(seen) - 1:
                rows.append(f"  ▶ <b>{html.escape(entry)}</b>")
            else:
                rows.append(f"  ·  <i>{html.escape(entry)}</i>")
        teams_block = "\n".join(rows)

    # Animation frame sur la barre
    spinner_frames = ["⚽", "🔄", "🔍", "📡", "🌐", "⚡", "🎯", "🏟️", "🔥", "💫"]
    spinner = spinner_frames[frame % len(spinner_frames)]

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  {spinner}  {bar}  <b>{pct_val}%</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  {step_line}",
        f"",
        count_line,
    ]
    if teams_block:
        lines += ["", teams_block]

    return "\n".join(lines)


async def _run_scan(message, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = message.chat_id
    lock    = _scan_locks_dict.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        await message.reply_text("⏳ <b>Scan déjà en cours…</b>", parse_mode="HTML")
        return

    async with lock:
        ctx.user_data.pop("scan_ready", None)
        ctx.user_data.pop("precomputed_results", None)
        ctx.user_data.pop("match_keys", None)

        loop  = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[int, int, str]] = asyncio.Queue()

        def progress(current: int, total: int, label: str) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (current, total, label))

        # Sticker de départ + premier message
        await message.reply_text(_STICKER_SCAN_START)
        status_message = await message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  ⚽  🟥⬜⬜⬜⬜⬜⬜⬜⬜⬜  <b>5%</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  🌐 Étape 1/3 — Connexion à Forebet…\n\n"
            "  ⚽ Recherche des matchs…",
            parse_mode="HTML",
        )

        task  = asyncio.create_task(asyncio.to_thread(web_collector.collect_window, progress))
        last_text = ""
        frame     = 1
        current_progress = (0, 3, "Étape 1/3 — recherche des matchs…")
        scan_state: dict = {}   # état partagé : total, processed, seen_teams

        while not task.done():
            try:
                current, total_steps, label = await asyncio.wait_for(queue.get(), timeout=0.8)
                current_progress = (current, total_steps, label)
            except asyncio.TimeoutError:
                current, total_steps, label = current_progress

            text = _build_scan_progress(current, total_steps, label, frame, scan_state)
            frame += 1
            if text != last_text:
                try:
                    await status_message.edit_text(text, parse_mode="HTML")
                    last_text = text
                except Exception:
                    pass  # ignorer les "message not modified"

        try:
            result = await task
        except Exception as exc:
            logger.error("scan failed: %s", exc, exc_info=True)
            await status_message.edit_text(
                "❌ <b>Scan impossible.</b>\nRéessaie dans quelques instants.",
                parse_mode="HTML",
            )
            return

        ctx.user_data["scan_ready"] = True
        items = scanner.cached_snapshots()
        ctx.user_data["match_keys"] = [str(item.get("cache_key")) for item in items]

        # Sticker de célébration
        await message.reply_text(_STICKER_TROPHY)
        n = result["count"]
        new_n = result.get("new_this_scan")
        # V19.5 — l'archive du jour s'accumule maintenant d'un /scan à
        # l'autre (voir collect_window) : on affiche donc le nombre de
        # nouveaux matchs trouvés CE scan à côté du total archivé, pour que
        # ce soit visible qu'un second /scan complète le premier au lieu de
        # tout remplacer.
        new_line = (
            f"  🆕  <b>{new_n}</b> nouveau{'x' if new_n != 1 else ''} ce scan\n"
            if new_n is not None else ""
        )
        # V19.10 — jusqu'ici, un échec du navigateur headless (libs système
        # manquantes, Chromium introuvable, etc.) faisait retomber
        # silencieusement sur un GET statique : le scan "réussissait" quand
        # même, juste avec 5 à 10x moins de matchs, et rien ne le montrait
        # nulle part sauf un WARNING dans les logs serveur. On le rend
        # visible ici.
        sources = result.get("sources", {})
        headless_expected = config.WEB_SCAN_USE_HEADLESS_BROWSER
        headless_used = any(
            str(v).startswith("headless-ok")
            for k, v in sources.items()
            if k in ("forebet", "forebet_live", "forebet_tomorrow")
        )
        warning_line = (
            "\n⚠️ <b>Navigateur headless indisponible</b> — repli sur une "
            "liste partielle (voir les logs serveur pour le détail).\n"
            if headless_expected and not headless_used else ""
        )
        await status_message.edit_text(
            f"{_divider()}\n"
            f"🏆  <b>SCAN TERMINÉ !</b>\n"
            f"{_divider()}\n\n"
            f"  ⚽  <b>{n}</b> match{'s' if n != 1 else ''} archivé{'s' if n != 1 else ''} aujourd'hui\n"
            f"{new_line}"
            f"  ✅  Fiches Forebet sauvegardées\n"
            f"  🔬  Données étendues chargées\n"
            f"{warning_line}\n"
            f"👇 <b>Choisis un match pour l'analyse V18</b>\n"
            f"   <i>(100 000 simulations Monte-Carlo)</i>",
            parse_mode="HTML",
            reply_markup=_match_keyboard(items) if items else _back_menu(),
        )


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_scan(update.effective_message, ctx)


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    today_before = len(await asyncio.to_thread(scanner.cached_snapshots))
    deleted = await asyncio.to_thread(cache_store.delete_cache)
    ctx.user_data.pop("match_keys", None)
    ctx.user_data.pop("precomputed_results", None)
    ctx.user_data.pop("scan_ready", None)

    total_preds, settled_preds = await asyncio.to_thread(tracking.count_predictions)
    cfg = await asyncio.to_thread(calibration_module.load_calibration)
    calib_state = "🟢 calibrée (v" + str(cfg.version) + ")" if cfg.version > 1 else "⚪ poids par défaut"

    await update.effective_message.reply_text(_STICKER_DELETED)
    await update.effective_message.reply_text(
        f"{_divider()}\n"
        "🗑  <b>CACHE DU JOUR SUPPRIMÉ</b>\n"
        f"{_divider()}\n\n"
        f"  📅 <b>{today_before}</b> match(s) du jour effacé(s)\n"
        f"  📂 <b>{deleted}</b> fichier(s) supprimé(s)\n\n"
        "<i>Conservé (non affecté par /delete) :</i>\n"
        f"  📊 <b>{total_preds}</b> pronostic(s) enregistré(s)  "
        f"(<b>{settled_preds}</b> réglé(s))\n"
        f"  ⚙️ Calibration : {calib_state}\n\n"
        "Lance <b>/scan</b> pour une nouvelle collecte.",
        parse_mode="HTML",
        reply_markup=_back_menu(),
    )


async def _predict_index(message, ctx: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    if not ctx.user_data.get("scan_ready"):
        await message.reply_text(
            "⏳ <b>Lance /scan d'abord.</b>",
            parse_mode="HTML",
        )
        return
    items = scanner.cached_snapshots()
    if index < 0 or index >= len(items):
        await message.reply_text(
            "❌ <b>Numéro de match invalide.</b>\nUtilise /today pour voir la liste.",
            parse_mode="HTML",
        )
        return

    snapshot = items[index]
    loop     = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[int, int, str]] = asyncio.Queue()

    def progress(current: int, total: int, label: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (current, total, label))

    # Sticker de départ de l'analyse
    await message.reply_text(_STICKER_PREDICT)
    status_message = await message.reply_text(
        _progress_text(0, 100, "Préparation de l'analyse V19…", 0)
    )
    task       = asyncio.create_task(asyncio.to_thread(scanner.analyse_snapshot, snapshot, progress))
    last_text  = ""
    frame      = 1
    current_progress = (0, 100, "Préparation de l'analyse V19…")

    try:
        while not task.done():
            try:
                current_progress = await asyncio.wait_for(queue.get(), timeout=0.7)
            except asyncio.TimeoutError:
                pass
            current, total, label = current_progress
            text = _progress_text(current, total, label, frame)
            frame += 1
            if text != last_text:
                await status_message.edit_text(text)
                last_text = text

        result  = await task
        pred_id = await asyncio.to_thread(scanner.record_prediction, result)

        # Sticker de résultat
        badge = _outcome_badge(
            result.prediction.home_win_prob,
            result.prediction.draw_prob,
            result.prediction.away_win_prob,
            predicted_outcome=getattr(result.prediction, "predicted_outcome", ""),
        )
        await message.reply_text(_STICKER_FIRE)

        prediction_text = _prediction_text(result, pred_id=pred_id)
        chunks = _chunk(prediction_text)
        if len(chunks) == 1:
            await status_message.edit_text(
                chunks[0], parse_mode="HTML", reply_markup=_back_menu()
            )
        else:
            await status_message.edit_text(chunks[0], parse_mode="HTML")
            for chunk in chunks[1:-1]:
                await message.reply_text(chunk, parse_mode="HTML")
            await message.reply_text(chunks[-1], parse_mode="HTML", reply_markup=_back_menu())

    except Exception as exc:
        logger.error("prediction failed: %s", exc, exc_info=True)
        await status_message.edit_text(
            "❌ <b>Résultat indisponible.</b>\nRéessaie ou lance un nouveau /scan.",
            parse_mode="HTML",
        )


async def cmd_predict(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.user_data.get("scan_ready"):
        await update.effective_message.reply_text(
            "⏳ <b>Lance /scan d'abord.</b>",
            parse_mode="HTML",
        )
        return
    args = ctx.args or []
    if args:
        try:
            await _predict_index(update.effective_message, ctx, int(args[0]) - 1)
        except ValueError:
            await update.effective_message.reply_text(
                "❌ <b>Usage :</b> <code>/predict 2</code>",
                parse_mode="HTML",
            )
        return
    items = scanner.cached_snapshots()
    ctx.user_data["match_keys"] = [str(item.get("cache_key")) for item in items]
    await update.effective_message.reply_text(
        "🎯 <b>Choisis un match :</b>",
        parse_mode="HTML",
        reply_markup=_match_keyboard(items) if items else _back_menu(),
    )


async def cmd_example(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.user_data.get("scan_ready"):
        await update.effective_message.reply_text(
            "⏳ <b>Lance /scan d'abord.</b>",
            parse_mode="HTML",
        )
        return
    items = scanner.cached_snapshots()
    if not items:
        await update.effective_message.reply_text(
            "📭 <b>Aucun match disponible.</b>\nLance <b>/scan</b>.",
            parse_mode="HTML",
        )
        return
    index = random.randrange(len(items))
    await _predict_index(update.effective_message, ctx, index)


def _parse_teams(args: list[str]) -> tuple[str, str] | None:
    full = " ".join(args)
    for sep in (" vs ", " VS ", " v ", " V ", " - "):
        if sep in full:
            home, away = full.split(sep, 1)
            if home.strip() and away.strip():
                return home.strip(), away.strip()
    return None


async def cmd_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.user_data.get("scan_ready"):
        await update.effective_message.reply_text(
            "⏳ <b>Lance /scan d'abord.</b>",
            parse_mode="HTML",
        )
        return
    args = ctx.args or []
    if len(args) == 1 and args[0].isdigit():
        await _predict_index(update.effective_message, ctx, int(args[0]) - 1)
        return
    parsed = _parse_teams(args)
    if not parsed:
        await update.effective_message.reply_text(
            "❌ <b>Usage :</b>\n"
            "  <code>/match 2</code>\n"
            "  <code>/match Équipe A vs Équipe B</code>\n\n"
            "<i>La recherche utilise le cache du dernier /scan.</i>",
            parse_mode="HTML",
        )
        return
    result = await asyncio.to_thread(scanner.analyse_named_match, *parsed)
    if result is None:
        await update.effective_message.reply_text(
            "❌ <b>Match introuvable dans le cache.</b>\nRelance /scan ou vérifie les noms.",
            parse_mode="HTML",
        )
    else:
        pred_id = await asyncio.to_thread(scanner.record_prediction, result)
        await update.effective_message.reply_text(_STICKER_FIRE)
        prediction_text = _prediction_text(result, pred_id=pred_id)
        chunks = _chunk(prediction_text)
        for chunk in chunks[:-1]:
            await update.effective_message.reply_text(chunk, parse_mode="HTML")
        await update.effective_message.reply_text(
            chunks[-1], parse_mode="HTML", reply_markup=_back_menu()
        )


def _parse_score(raw: str) -> tuple[int, int] | None:
    for sep in ("-", ":", "–"):
        if sep in raw:
            left, _, right = raw.partition(sep)
            try:
                h, a = int(left.strip()), int(right.strip())
            except ValueError:
                return None
            return (h, a) if h >= 0 and a >= 0 else None
    return None


async def cmd_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    if len(args) < 2 or not args[0].isdigit():
        pending = await asyncio.to_thread(tracking.find_unsettled, 10)
        lines   = [
            f"{_divider()}",
            "📌  <b>ENREGISTRER UN RÉSULTAT</b>",
            f"{_divider()}\n",
            "  <b>Usage :</b>  <code>/resultat &lt;id&gt; &lt;score&gt;</code>",
            "  <b>Exemple :</b>  <code>/resultat 42 2-1</code>\n",
        ]
        if pending:
            lines.append("  🕒 <b>Pronostics en attente :</b>")
            for prediction_id, home, away, kickoff, _, _, _ in pending:
                lines.append(
                    f"    <b>#{prediction_id}</b>  {html.escape(home)} vs {html.escape(away)}"
                    f"  <i>({kickoff})</i>"
                )
        else:
            lines.append("  <i>Aucun pronostic en attente.</i>")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=_back_menu()
        )
        return

    prediction_id = int(args[0])
    score         = _parse_score(args[1])
    if score is None:
        await update.effective_message.reply_text(
            "❌ <b>Score invalide.</b>\nExemple : <code>/resultat 42 2-1</code>",
            parse_mode="HTML",
        )
        return

    outcome = await asyncio.to_thread(tracking.settle, prediction_id, score[0], score[1])
    if outcome is None:
        await update.effective_message.reply_text(
            f"❌ <b>Pronostic #{prediction_id} introuvable ou déjà réglé.</b>",
            parse_mode="HTML",
        )
        return

    ok_1x2  = "✅" if outcome.correct_1x2  else "❌"
    ok_btts = "✅" if outcome.correct_btts  else "❌"
    ok_ou   = "✅" if outcome.correct_over25 else "❌"

    await update.effective_message.reply_text(_STICKER_RESULT)
    await update.effective_message.reply_text(
        f"{_divider()}\n"
        "📌  <b>RÉSULTAT ENREGISTRÉ</b>\n"
        f"{_divider()}\n\n"
        f"  ⚽  <b>{html.escape(outcome.home_name)}</b>  "
        f"<b>{outcome.result_home} – {outcome.result_away}</b>  "
        f"<b>{html.escape(outcome.away_name)}</b>\n\n"
        f"  {ok_1x2}  Sens du résultat <b>(1X2)</b>\n"
        f"  {ok_btts}  <b>BTTS</b>\n"
        f"  {ok_ou}  <b>Plus/moins de 2,5 buts</b>\n\n"
        f"  🎯 Score de Brier <i>(1X2, 0=parfait)</i> : <b>{outcome.brier_1x2}</b>\n\n"
        "👉 <b>/fiabilite</b> pour voir les stats globales du bot.",
        parse_mode="HTML",
        reply_markup=_back_menu(),
    )


async def cmd_auto_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message   = update.effective_message
    snapshots = await asyncio.to_thread(scanner.cached_snapshots)
    pending   = await asyncio.to_thread(tracking.find_unsettled_for_auto_result, 100)

    if not snapshots:
        await message.reply_text(
            "⚠️ <b>Le cache du jour est vide.</b>\nLance <b>/scan</b> avant /autoresultat.",
            parse_mode="HTML",
            reply_markup=_back_menu(),
        )
        return

    await message.reply_text(_STICKER_CHART)
    status = await message.reply_text(
        f"{_divider()}\n"
        "🔄  <b>VÉRIFICATION DES SCORES</b>\n"
        f"{_divider()}\n\n"
        "  📡 Consultation des pages Forebet…\n"
        "  <i>(seulement les matchs déjà commencés)</i>",
        parse_mode="HTML",
    )

    report = await asyncio.to_thread(web_collector.collect_cached_results, snapshots, pending)

    settled_lines: list[str] = []
    for item in report["settled"]:
        sc      = item["score"]
        outcome = await asyncio.to_thread(
            tracking.settle, int(item["prediction_id"]), int(sc[0]), int(sc[1])
        )
        if outcome is not None:
            verdict = "✅" if outcome.correct_1x2 else "❌"
            settled_lines.append(
                f"  {verdict}  <b>#{outcome.prediction_id}</b>  "
                f"{html.escape(outcome.home_name)} "
                f"<b>{outcome.result_home}–{outcome.result_away}</b> "
                f"{html.escape(outcome.away_name)}"
            )

    pending_count = len(report["pending"])
    lines = [
        f"{_divider()}",
        "🔄  <b>RÉSULTATS AUTOMATIQUES</b>",
        f"{_divider()}\n",
        f"  📅 Matchs en cache du jour :  <b>{report['cached']}</b>",
        f"  ⏰ Matchs terminés contrôlés :  <b>{report['past']}</b>",
        f"  🔍 Scores finaux trouvés :  <b>{report['found']}</b>",
        f"  🌍 Matchs FT vus sur la page Forebet :  <b>{report.get('finished_matches_on_page', 0)}</b>",
        f"  🎯 Correspondances pronostics :  <b>{len(report['settled'])}</b>",
        f"  ✅ Enregistrés :  <b>{len(settled_lines)}</b>",
        f"  🕒 À vérifier plus tard :  <b>{pending_count}</b>",
        "",
    ]
    if settled_lines:
        lines.append("<b>Détail des résultats :</b>")
        lines.extend(settled_lines[:35])
    else:
        lines.append("  <i>Aucun score final suffisamment clair trouvé sur Forebet.</i>")

    if report.get("unmatched_predictions"):
        lines.extend([
            "",
            f"  ⚠️ <b>{len(report['unmatched_predictions'])}</b> pronostic(s) sans correspondance Forebet.",
        ])
    if pending_count:
        lines.extend([
            "",
            "  <i>Les matchs sans score final explicite restent non réglés.</i>",
        ])

    status_breakdown = report.get("status_breakdown") or {}
    if status_breakdown:
        _STATUS_LABELS = {
            "ok":                    "✅ Score trouvé (marqué FT par Forebet)",
            "final-score-not-found": "⏳ Pas encore marqué terminé par Forebet",
            "blocked-cloudflare":    "🚫 Bloqué par Forebet (Cloudflare)",
            "missing-url":          "🔗 Aucune URL Forebet enregistrée",
        }
        lines.extend(["", "<b>Diagnostic (pourquoi certains matchs ne se règlent pas) :</b>"])
        for status_key, count in sorted(status_breakdown.items(), key=lambda kv: -kv[1]):
            if status_key in _STATUS_LABELS:
                label = _STATUS_LABELS[status_key]
            elif status_key.startswith("http-"):
                label = f"🚫 Réponse HTTP {status_key.split('-', 1)[1]}"
            elif status_key.startswith("error-"):
                label = f"⚠️ Erreur réseau ({status_key.split('-', 1)[1]})"
            else:
                label = f"❔ {status_key}"
            lines.append(f"  {label} : <b>{count}</b>")
        if status_breakdown.get("blocked-cloudflare"):
            lines.append(
                "  <i>Forebet a détecté et bloqué les requêtes automatisées "
                "sur ces pages — relance /autoresultat plus tard.</i>"
            )

    await status.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_menu())


async def cmd_learning(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    await msg.reply_text(_STICKER_LEARNING)
    status = await msg.reply_text(
        f"{_divider()}\n"
        "🧠  <b>APPRENTISSAGE V18</b>\n"
        f"{_divider()}\n\n"
        "  🔍 Analyse des erreurs en cours…",
        parse_mode="HTML",
    )
    report = await asyncio.to_thread(learning_module.analyse_errors)

    if report.is_empty:
        await status.edit_text(
            f"{_divider()}\n"
            "📭  <b>AUCUNE DONNÉE</b>\n"
            f"{_divider()}\n\n"
            "  Enregistre des résultats via <b>/resultat</b>\n"
            "  pour activer l'apprentissage.",
            parse_mode="HTML",
            reply_markup=_back_menu(),
        )
        return

    lines = [
        f"{_divider()}",
        "🧠  <b>APPRENTISSAGE V18</b>",
        f"{_divider()}\n",
        f"  📊 Échantillon : <b>{report.n_settled}</b> pronostic(s) réglé(s)\n",
        _divider(),
        "📈  <b>PRÉCISION PAR MARCHÉ</b>",
        _divider(),
    ]
    for name, stats in report.by_market.items():
        if stats.n > 0:
            acc_bar = _confidence_bar(stats.accuracy * 100, 10)
            lines.append(
                f"  <b>{html.escape(name)}</b>\n"
                f"  {acc_bar}  <b>{stats.accuracy * 100:.1f}%</b>  "
                f"<i>({stats.correct}/{stats.n})</i>"
            )

    if report.error_causes:
        lines += ["", _divider("─"), "⚠️  <b>CAUSES D'ERREURS IDENTIFIÉES</b>", _divider("─")]
        for cause in report.error_causes[:4]:
            lines.append(f"  🔸 <b>{html.escape(cause.cause)}</b>  <i>({cause.count}×)</i>")
            lines.append(f"    <i>{html.escape(cause.description)}</i>")

    if report.systematic_biases:
        lines += ["", _divider("─"), "🔬  <b>BIAIS SYSTÉMATIQUES</b>", _divider("─")]
        for bias in report.systematic_biases:
            lines.append(f"  ⚙️ <i>{html.escape(bias)}</i>")

    lines += ["", _divider("─"), f"💡  <b>Recommandation</b>", _divider("─"), f"  <i>{html.escape(report.recommendation)}</i>"]

    if report.n_settled >= 15:
        lines.append("\n  👉 Utilise <b>/recalibrer</b> pour ajuster les poids automatiquement.")

    await status.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_menu())


async def cmd_recalibrate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    V18 : recalibrage sécurisé. Un candidat n'est appliqué QUE s'il est
    prouvé au moins aussi bon que la calibration active sur un lot holdout
    jamais utilisé pour le proposer — la calibration ne régresse jamais.
    Utilise /recalibrerforce pour l'ancien comportement immédiat (non
    recommandé, réservé au diagnostic manuel).
    """
    msg = update.effective_message
    await msg.reply_text(_STICKER_CALIBRATE)
    status = await msg.reply_text(
        f"{_divider()}\n"
        "⚙️  <b>RECALIBRAGE SÉCURISÉ V18</b>\n"
        f"{_divider()}\n\n"
        "  🔧 Analyse et backtest en cours…\n"
        "  <i>(un candidat n'est jamais appliqué s'il régresse)</i>",
        parse_mode="HTML",
    )
    report = await asyncio.to_thread(auto_learning_module.run_auto_learning)

    if not report.attempted:
        await status.edit_text(
            f"{_divider()}\n"
            "⏳  <b>DONNÉES INSUFFISANTES</b>\n"
            f"{_divider()}\n\n"
            f"  <i>{html.escape(report.reason)}</i>",
            parse_mode="HTML",
            reply_markup=_back_menu(),
        )
        return

    header = (
        f"✅  <b>CALIBRATION v{report.new_version} APPLIQUÉE</b>"
        if report.accepted
        else "🛡️  <b>CALIBRATION INCHANGÉE</b>"
    )
    text = auto_learning_module.format_auto_learning_report(report)
    lines = [
        f"{_divider()}",
        f"{header}",
        f"{_divider()}\n",
        text,
        "",
        "  👉 <b>/versions</b> pour l'historique complet.",
    ]
    await status.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_menu())


async def cmd_recalibrate_force(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ancien comportement V16 : applique le recalibrage immédiatement, sans
    backtest holdout. Réservé à un diagnostic manuel explicite — préférer
    /recalibrer, qui ne peut jamais régresser."""
    msg = update.effective_message
    await msg.reply_text(_STICKER_CALIBRATE)
    status = await msg.reply_text(
        f"{_divider()}\n"
        "⚠️  <b>RECALIBRAGE FORCÉ</b>\n"
        f"{_divider()}\n\n"
        "  🔧 Application immédiate, sans backtest…",
        parse_mode="HTML",
    )
    cfg = await asyncio.to_thread(calibration_module.run_recalibration)

    if cfg.n_samples < 15:
        await status.edit_text(
            f"{_divider()}\n"
            "⏳  <b>DONNÉES INSUFFISANTES</b>\n"
            f"{_divider()}\n\n"
            f"  📊 {cfg.n_samples} prédictions réglées\n"
            "  📌 <b>Minimum requis : 15</b>\n\n"
            "  Continue à enregistrer des résultats\n"
            "  via <b>/resultat</b>.",
            parse_mode="HTML",
            reply_markup=_back_menu(),
        )
        return

    qual_emoji = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(cfg.calibration_quality, "⚪")
    lines = [
        f"{_divider()}",
        f"✅  <b>CALIBRATION v{cfg.version} APPLIQUÉE</b>",
        f"{_divider()}\n",
        f"  {qual_emoji} Qualité : <b>{cfg.calibration_quality}</b>",
        f"  📊 Basé sur <b>{cfg.n_samples}</b> prédictions réglées\n",
        _divider(),
        "🔧  <b>AJUSTEMENTS APPLIQUÉS</b>",
        _divider(),
        f"  📈 Seuil confiance HIGH   : <b>{cfg.confidence_high_threshold:.1f}%</b>",
        f"  📉 Seuil confiance MEDIUM : <b>{cfg.confidence_medium_threshold:.1f}%</b>",
        f"  🏠 Multiplicateur domicile   : <b>×{cfg.prob_multiplier_home:.3f}</b>",
        f"  ✈️  Multiplicateur extérieur  : <b>×{cfg.prob_multiplier_away:.3f}</b>",
        f"  🤝 Multiplicateur nul         : <b>×{cfg.prob_multiplier_draw:.3f}</b>",
        f"  ⚡ Seuil BTTS     : <b>{cfg.btts_threshold:.2f}</b>",
        f"  📊 Seuil O/U 2.5  : <b>{cfg.ou25_threshold:.2f}</b>",
        "",
        "  ⚠️ <i>Appliqué SANS validation holdout — vérifie avec /valider,</i>",
        "  <i>et reviens en arrière avec /versions en cas de doute.</i>",
    ]
    await status.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_menu())


async def cmd_validate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    await msg.reply_text(_STICKER_VALIDATE)
    status = await msg.reply_text(
        f"{_divider()}\n"
        "🧪  <b>VALIDATION HOLDOUT V18</b>\n"
        f"{_divider()}\n\n"
        "  🔬 Validation en cours…\n"
        "  <i>(70% calibration / 30% holdout)</i>",
        parse_mode="HTML",
    )
    report = await asyncio.to_thread(validation_module.run_validation)

    if not report.is_valid:
        await status.edit_text(
            f"{_divider()}\n"
            "⏳  <b>PAS ENCORE PRÊT</b>\n"
            f"{_divider()}\n\n"
            f"  <i>{html.escape(report.recommendation)}</i>",
            parse_mode="HTML",
            reply_markup=_back_menu(),
        )
        return

    calib = report.calibration_set
    hold  = report.holdout_set

    if report.regression_detected:
        reg_badge = "⚠️  <b>RÉGRESSION DÉTECTÉE</b>"
    elif report.improvement_detected:
        reg_badge = "📈  <b>AMÉLIORATION CONFIRMÉE</b>"
    else:
        reg_badge = "✅  <b>STABLE</b>"

    def _acc_bar(v: float) -> str:
        return _confidence_bar(v * 100, 8)

    lines = [
        f"{_divider()}",
        f"🧪  <b>VALIDATION HOLDOUT V18</b>",
        f"{_divider()}\n",
        f"  {reg_badge}\n",
        f"  Total réglés :    <b>{report.n_total}</b>",
        f"  Calibration :     <b>{report.n_calibration}</b>  <i>(plus anciens)</i>",
        f"  Holdout :         <b>{report.n_holdout}</b>  <i>(plus récents)</i>\n",
        _divider(),
        "📊  <b>CALIBRATION</b>  <i>(entraînement)</i>",
        _divider(),
        f"  Précision 1X2  :  {_acc_bar(calib.accuracy_1x2)}  <b>{calib.accuracy_1x2 * 100:.1f}%</b>",
        f"  Brier 1X2      :  <b>{calib.brier_1x2:.3f}</b>  <i>(0 = parfait)</i>",
        f"  Précision BTTS :  {_acc_bar(calib.accuracy_btts)}  <b>{calib.accuracy_btts * 100:.1f}%</b>",
        f"  Précision O/U  :  {_acc_bar(calib.accuracy_ou25)}  <b>{calib.accuracy_ou25 * 100:.1f}%</b>\n",
        _divider(),
        "🔬  <b>HOLDOUT</b>  <i>(jamais vu)</i>",
        _divider(),
        f"  Précision 1X2  :  {_acc_bar(hold.accuracy_1x2)}  <b>{hold.accuracy_1x2 * 100:.1f}%</b>",
        f"  Brier 1X2      :  <b>{hold.brier_1x2:.3f}</b>",
        f"  Précision BTTS :  {_acc_bar(hold.accuracy_btts)}  <b>{hold.accuracy_btts * 100:.1f}%</b>",
        f"  Précision O/U  :  {_acc_bar(hold.accuracy_ou25)}  <b>{hold.accuracy_ou25 * 100:.1f}%</b>\n",
        _divider("─"),
        f"  💡 <i>{html.escape(report.recommendation)}</i>",
    ]
    if report.regression_detected:
        lines.append("\n  👉 <b>/versions</b> pour revenir à une version précédente.")

    await status.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_menu())


async def cmd_versions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg      = update.effective_message
    versions = await asyncio.to_thread(versioning_module.version_summary)
    current  = await asyncio.to_thread(versioning_module.current_version)

    if not versions:
        await msg.reply_text(
            f"{_divider()}\n"
            "📭  <b>AUCUNE VERSION</b>\n"
            f"{_divider()}\n\n"
            "  Utilise <b>/recalibrer</b> pour créer la première version.",
            parse_mode="HTML",
            reply_markup=_back_menu(),
        )
        return

    args = ctx.args or []
    if args and args[0].isdigit():
        ver_num = int(args[0])
        ok      = await asyncio.to_thread(versioning_module.restore_version, ver_num)
        if ok:
            await msg.reply_text(_STICKER_BRAVO)
            await msg.reply_text(
                f"{_divider()}\n"
                f"✅  <b>VERSION v{ver_num} RESTAURÉE</b>\n"
                f"{_divider()}\n\n"
                f"  📁 La calibration active est maintenant <b>v{ver_num}</b>.\n"
                f"  👉 <b>/valider</b> pour mesurer l'impact.",
                parse_mode="HTML",
                reply_markup=_back_menu(),
            )
        else:
            await msg.reply_text(
                f"❌ <b>Version v{ver_num} introuvable.</b>",
                parse_mode="HTML",
                reply_markup=_back_menu(),
            )
        return

    await msg.reply_text(_STICKER_VERSIONS)
    lines = [
        f"{_divider()}",
        "📁  <b>HISTORIQUE DES CALIBRATIONS</b>",
        f"{_divider()}\n",
        f"  Version active : <b>v{current}</b>\n",
        _divider(),
    ]
    for vi in versions[:10]:
        marker = "  ← <b>active</b>" if vi["version"] == current else ""
        lines.append(f"  📌 {html.escape(vi['summary_line'])}{marker}")
    lines += [
        "",
        _divider("─"),
        "  🔁 <b>Pour restaurer une version :</b>",
        "  <code>/versions 3</code>  → restaure la v3",
        "  Puis <b>/valider</b> pour mesurer l'impact.",
    ]
    await msg.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_menu())


async def cmd_learning_v2(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    V18 : Auto-apprentissage avancé par ligue / équipe / mois, PLUS boucle
    d'auto-amélioration sécurisée : le bot propose une calibration candidate
    à partir de ses erreurs passées, la backteste sur des résultats jamais
    utilisés pour la proposer, et ne l'applique QUE si elle est prouvée non
    régressive. Sinon la calibration active reste inchangée à l'identique.
    """
    msg = update.effective_message
    await msg.reply_text(_STICKER_LEARNING)
    status = await msg.reply_text(
        f"{_divider()}\n"
        "🧬  <b>APPRENTISSAGE V2</b>\n"
        f"{_divider()}\n\n"
        "  🔍 Analyse segmentée en cours…",
        parse_mode="HTML",
    )
    report = await asyncio.to_thread(learning_v2_module.analyse_v2)
    text   = learning_v2_module.format_learning_report_v2(report)
    chunks = _chunk(text)
    await status.edit_text(chunks[0], parse_mode="HTML", reply_markup=_back_menu())
    for chunk in chunks[1:]:
        await msg.reply_text(chunk, parse_mode="HTML", reply_markup=_back_menu())

    # ── V18 : cycle d'auto-amélioration sécurisée ────────────────────────────
    al_status = await msg.reply_text(
        f"{_divider()}\n"
        "🔒  <b>AUTO-AMÉLIORATION</b>\n"
        f"{_divider()}\n\n"
        "  🧪 Backtest du candidat sur données jamais vues…",
        parse_mode="HTML",
    )
    al_report = await asyncio.to_thread(auto_learning_module.run_auto_learning)
    al_text   = auto_learning_module.format_auto_learning_report(al_report)
    al_chunks = _chunk(al_text)
    await al_status.edit_text(al_chunks[0], parse_mode="HTML", reply_markup=_back_menu())
    for chunk in al_chunks[1:]:
        await msg.reply_text(chunk, parse_mode="HTML", reply_markup=_back_menu())


async def cmd_learning_v18(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    V18 : Analyse profonde des prédictions réglées — catalogue de scénarios
    d'erreur et mesure du biais xG.

    V19.14 — cette commande n'écrit plus rien dans la calibration : elle est
    purement informative. Toute recalibration passe par /recalibrer, qui
    recalcule et backteste avant d'appliquer quoi que ce soit.
    """
    msg = update.effective_message
    await msg.reply_text(_STICKER_LEARNING)
    status = await msg.reply_text(
        f"{_divider()}\n"
        "🧠  <b>APPRENTISSAGE V18 — ANALYSE PROFONDE</b>\n"
        f"{_divider()}\n\n"
        "  🔬 Analyse des scénarios d'erreur en cours…\n"
        "  📊 Calcul du biais xG…",
        parse_mode="HTML",
    )
    try:
        report = await asyncio.to_thread(learning_v18_module.run_v18_analysis)
        if report.n_settled < 30:
            await status.edit_text(
                "⚠️ Pas assez de prédictions réglées pour l'apprentissage V18.\n"
                f"Minimum requis : 30 | Disponible : {report.n_settled}",
                parse_mode="HTML",
            )
            return

        # Sauvegarder le catalogue de scénarios (lecture seule, sans effet
        # sur la calibration)
        await asyncio.to_thread(learning_v18_module.save_scenario_catalog, report)

        # Formater le rapport (informatif — voir docstring)
        text = learning_v18_module.format_v18_report_telegram(report)

        chunks = _chunk(text)
        await status.edit_text(chunks[0], parse_mode="HTML", reply_markup=_back_menu())
        for chunk in chunks[1:]:
            await msg.reply_text(chunk, parse_mode="HTML", reply_markup=_back_menu())
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(
            f"❌ Erreur apprentissage V18 : <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )


async def cmd_backtest_xg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    V20.4 — Backtest de xg_global_multiplier par ré-simulation complète du
    pipeline de prédiction, validé par split chronologique calibration/
    holdout (voir engine.xg_backtest). Un candidat n'est appliqué à
    calibration.json QUE s'il ne régresse pas sur le holdout jamais vu
    pendant la recherche — même philosophie de sécurité que /recalibrer.
    """
    msg = update.effective_message
    await msg.reply_text(_STICKER_CALIBRATE)
    status = await msg.reply_text(
        f"{_divider()}\n"
        "🧪  <b>BACKTEST xG_GLOBAL_MULTIPLIER</b>\n"
        f"{_divider()}\n\n"
        "  🔁 Ré-exécution du pipeline complet sur les matchs réglés…\n"
        "  <i>(un candidat n'est jamais appliqué s'il régresse sur le holdout)</i>",
        parse_mode="HTML",
    )
    try:
        report = await asyncio.to_thread(xg_backtest_module.backtest_xg_multiplier)
        if not report.attempted:
            await status.edit_text(
                f"{_divider()}\n"
                "⏳  <b>DONNÉES INSUFFISANTES</b>\n"
                f"{_divider()}\n\n"
                f"  <i>{html.escape(report.reason)}</i>",
                parse_mode="HTML",
                reply_markup=_back_menu(),
            )
            return

        applied = False
        if report.accepted:
            applied = await asyncio.to_thread(xg_backtest_module.apply_candidate, report)

        header = (
            f"✅  <b>xG_GLOBAL_MULTIPLIER APPLIQUÉ ({report.candidate_multiplier})</b>"
            if applied
            else "🛡️  <b>MULTIPLICATEUR INCHANGÉ</b>"
        )
        lines = [
            f"{_divider()}",
            header,
            f"{_divider()}\n",
            f"⚙️ Actif : <b>{report.active_multiplier:.3f}</b> · "
            f"Candidat testé : <b>{report.candidate_multiplier:.3f}</b>",
            f"📦 {report.n_matched_snapshots} matchs réglés ré-simulés "
            f"({report.n_calibration} calibration / {report.n_holdout} holdout)",
            "",
            html.escape(report.reason),
        ]
        await status.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_menu())
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(
            f"❌ Erreur backtest xG : <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )


async def cmd_audit_force(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    V20.7 — Audit d'ablation des 8 composantes de l'indice de force (voir
    engine.strength_ablation) : neutralise chaque composante à tour de
    rôle et rejoue le pipeline complet sur l'historique réglé, pour mesurer
    si elle aide vraiment ou dilue le signal des autres. Purement
    diagnostique — ne modifie jamais calibration.json, contrairement à
    /recalibrer ou /backtestxg. Coûteux (plusieurs minutes) : 9 scénarios
    (8 composantes + référence) rejoués sur tout l'historique réglé.
    """
    msg = update.effective_message
    await msg.reply_text(_STICKER_CALIBRATE)
    status = await msg.reply_text(
        f"{_divider()}\n"
        "🔬  <b>AUDIT D'ABLATION — INDICE DE FORCE</b>\n"
        f"{_divider()}\n\n"
        "  🔁 Ré-exécution du pipeline complet, composante par composante…\n"
        "  <i>(purement diagnostique — n'applique jamais rien automatiquement)</i>",
        parse_mode="HTML",
    )
    try:
        report = await asyncio.to_thread(strength_ablation_module.run_ablation_audit)
        text = strength_ablation_module.format_ablation_report(report)
        await status.edit_text(text, parse_mode="HTML", reply_markup=_back_menu())
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(
            f"❌ Erreur audit d'ablation : <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )


async def cmd_audit_h2h(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    V20.8 — Audit du canal H2H (voir engine.h2h_audit) : compare le
    comportement actuel, H2H désactivé, et un mode incluant
    home_factor/away_factor (signal calculé par engine.h2h mais jamais
    branché avant ce correctif). Purement diagnostique.
    """
    msg = update.effective_message
    await msg.reply_text(_STICKER_CALIBRATE)
    status = await msg.reply_text(
        f"{_divider()}\n"
        "🔬  <b>AUDIT DU CANAL H2H</b>\n"
        f"{_divider()}\n\n"
        "  🔁 Ré-exécution du pipeline complet, mode par mode…\n"
        "  <i>(purement diagnostique — n'applique jamais rien automatiquement)</i>",
        parse_mode="HTML",
    )
    try:
        report = await asyncio.to_thread(h2h_audit_module.run_h2h_audit)
        text = h2h_audit_module.format_h2h_audit_report(report)
        await status.edit_text(text, parse_mode="HTML", reply_markup=_back_menu())
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(
            f"❌ Erreur audit H2H : <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )


async def cmd_memoire(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """V18 : Affiche la mémoire des équipes ou le profil d'une équipe."""
    msg  = update.effective_message
    args = (msg.text or "").split(maxsplit=1)
    mgr  = team_memory_module.get_manager()

    if len(args) > 1:
        # Chercher le profil d'une équipe spécifique
        team_name = args[1].strip()
        profile   = mgr.get(team_name)
        if profile.total_matches == 0:
            await msg.reply_text(
                f"🧠 Aucun profil mémoire pour <b>{html.escape(team_name)}</b>.\n\n"
                "  Enregistre des résultats via <b>/resultat</b> pour construire la mémoire.",
                parse_mode="HTML",
                reply_markup=_back_menu(),
            )
            return

        traits = profile.describe()
        lines  = [
            f"{_divider()}",
            f"🧠  <b>MÉMOIRE V18</b>",
            f"{_divider()}\n",
            f"  <b>{html.escape(team_name)}</b> — {profile.total_matches} match(s) en mémoire\n",
            _divider(),
        ]
        for trait in traits:
            lines.append(f"  {trait}")

        lines += [
            "",
            _divider("─"),
            f"  🏠 Victoires domicile : {profile.home_wins}",
            f"  ✈️  Victoires extérieur : {profile.away_wins}",
            f"  🔄 Remontées observées : {profile.comebacks}",
            f"  ⏰ Buts après 70' : {profile.late_goals_scored}",
        ]
        await msg.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_menu())
        return

    # Liste de toutes les équipes
    teams = mgr.list_teams()
    if not teams:
        await msg.reply_text(
            f"{_divider()}\n"
            "📭  <b>MÉMOIRE VIDE</b>\n"
            f"{_divider()}\n\n"
            "  Aucune équipe en mémoire.\n\n"
            "  Enregistre des résultats via <b>/resultat</b>\n"
            "  pour construire la mémoire des équipes.",
            parse_mode="HTML",
            reply_markup=_back_menu(),
        )
        return

    await msg.reply_text(_STICKER_LEARNING)
    lines = [
        f"{_divider()}",
        "🧠  <b>MÉMOIRE DES ÉQUIPES V18</b>",
        f"{_divider()}\n",
        f"  {len(teams)} équipe(s) en mémoire\n",
        _divider(),
    ]
    for key, profile in teams[:20]:
        n = profile.total_matches
        if n == 0:
            continue
        # Trait principal
        traits = profile.describe()
        main_trait = traits[0] if traits else "⚖️ Équilibré"
        lines.append(
            f"  <b>{html.escape(profile.name or key)}</b> ({n} matchs)\n"
            f"    {main_trait}"
        )
    if len(teams) > 20:
        lines.append(f"\n  ... et {len(teams) - 20} autre(s) — <code>/memoire [équipe]</code>")

    lines += ["", _divider("─"), "  💡 <code>/memoire PSG</code> pour le profil complet d'une équipe"]

    chunks = _chunk("\n".join(lines))
    await msg.reply_text(chunks[0], parse_mode="HTML", reply_markup=_back_menu())
    for chunk in chunks[1:]:
        await msg.reply_text(chunk, parse_mode="HTML", reply_markup=_back_menu())


_HISTORY_USAGE = (
    "💡 <b>Usage :</b>\n"
    "<code>/historique ligue=Premier conf_min=60 conf_max=90 issue=home</code>\n\n"
    "Filtres disponibles (tous optionnels, combinables) :\n"
    "  <code>ligue=</code>       sous-chaîne du nom de ligue\n"
    "  <code>conf_min=</code> / <code>conf_max=</code>   confiance (0-100)\n"
    "  <code>issue=</code>       home | draw | away (pronostic affiché)\n"
    "  <code>xg_min=</code> / <code>xg_max=</code>       xG total (modèle)\n\n"
    "⚠️ Le bot ne collecte pas la possession, les tirs ou les corners : "
    "seules les données qu'il calcule lui-même (probabilités, xG modèle, "
    "confiance, ligue, marché) sont interrogeables."
)


def _parse_history_args(raw: str) -> history_query_module.HistoryFilter:
    f = history_query_module.HistoryFilter()
    for token in raw.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        key = key.strip().lower()
        value = value.strip()
        try:
            if key == "ligue":
                f.league_contains = value
            elif key == "conf_min":
                f.min_confidence = float(value)
            elif key == "conf_max":
                f.max_confidence = float(value)
            elif key == "issue":
                f.predicted_outcome = value.lower()
            elif key == "xg_min":
                f.min_xg_total = float(value)
            elif key == "xg_max":
                f.max_xg_total = float(value)
        except ValueError:
            continue
    return f


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """V20 : Requête libre sur l'historique des prédictions réglées."""
    msg  = update.effective_message
    args = (msg.text or "").split(maxsplit=1)
    if len(args) <= 1:
        await msg.reply_text(_HISTORY_USAGE, parse_mode="HTML", reply_markup=_back_menu())
        return

    f = _parse_history_args(args[1])
    result = await asyncio.to_thread(history_query_module.run_query, f)
    text = history_query_module.format_query_result(result)
    chunks = _chunk(text)
    await msg.reply_text(chunks[0], parse_mode="HTML", reply_markup=_back_menu())
    for chunk in chunks[1:]:
        await msg.reply_text(chunk, parse_mode="HTML", reply_markup=_back_menu())


async def cmd_league_calibration(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    V20 : recalibrage sécurisé de la pénalité de confiance par tier de
    ligue, à partir de la précision 1X2 réellement mesurée. Un candidat
    n'est appliqué que si l'ordre de fiabilité proposé tient toujours sur
    un lot holdout jamais vu — même philosophie que /recalibrer, portée
    volontairement plus modeste (3 tiers, pas de walk-forward multi-
    fenêtres — voir la docstring d'engine/league_calibration.py).
    """
    msg = update.effective_message
    status = await msg.reply_text(
        f"{_divider()}\n"
        "🌍  <b>RECALIBRAGE DES LIGUES — V20</b>\n"
        f"{_divider()}\n\n"
        "  🔧 Analyse par tier et vérification holdout en cours…",
        parse_mode="HTML",
    )
    report = await asyncio.to_thread(league_calibration_module.run_league_calibration)
    text = league_calibration_module.format_report(report)
    await status.edit_text(text, parse_mode="HTML", reply_markup=_back_menu())


async def cmd_calibration(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    report = await asyncio.to_thread(tracking.calibration_report)
    if report.n_settled == 0:
        await update.effective_message.reply_text(
            f"{_divider()}\n"
            "📭  <b>AUCUN RÉSULTAT</b>\n"
            f"{_divider()}\n\n"
            "  Utilise <b>/resultat &lt;id&gt; &lt;score&gt;</b>\n"
            "  après chaque match pour commencer.\n\n"
            "  <i>(30+ matchs recommandés pour des stats solides)</i>",
            parse_mode="HTML",
            reply_markup=_back_menu(),
        )
        return

    await update.effective_message.reply_text(_STICKER_CHART)
    lines = [
        f"{_divider()}",
        "📊  <b>FIABILITÉ RÉELLE DU BOT</b>",
        f"{_divider()}\n",
        f"  Échantillon : <b>{report.n_settled}</b> pronostic(s) réglé(s)\n",
        _divider(),
        "🎯  <b>RÉSULTATS GLOBAUX</b>",
        _divider(),
        f"  🧮 Précision 1X2  :  {_confidence_bar(report.accuracy_1x2 * 100)}  <b>{report.accuracy_1x2 * 100:.1f}%</b>",
        f"  🎯 Score de Brier :  <b>{report.brier_1x2}</b>  <i>(0 = parfait, 2 = pire cas)</i>",
        f"  ⚡ Précision BTTS :  {_confidence_bar(report.accuracy_btts * 100)}  <b>{report.accuracy_btts * 100:.1f}%</b>",
        f"  📊 Précision O/U  :  {_confidence_bar(report.accuracy_over25 * 100)}  <b>{report.accuracy_over25 * 100:.1f}%</b>",
        "",
        _divider("─"),
        "🔬  <b>PAR NIVEAU DE CONFIANCE</b>",
        _divider("─"),
    ]
    for label in ("HIGH", "MEDIUM", "LOW"):
        bucket = report.by_confidence.get(label)
        _CONF_EMOJI = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}
        emoji  = _CONF_EMOJI.get(label, "⚪")
        if bucket and bucket.n:
            acc_bar = _confidence_bar(bucket.accuracy_1x2 * 100, 8)
            lines.append(
                f"  {emoji} <b>{label}</b>  n=<b>{bucket.n}</b>\n"
                f"    {acc_bar}  <b>{bucket.accuracy_1x2 * 100:.1f}%</b>  "
                f"Brier <b>{bucket.brier_avg:.3f}</b>"
            )
        else:
            lines.append(f"  {emoji} <b>{label}</b>  <i>(aucun résultat encore)</i>")
    lines += [
        "",
        _divider("─"),
        "  💡 <i>Si HIGH ne bat pas nettement LOW, les poids méritent</i>",
        "  <i>d'être recalibrés — lance <b>/recalibrer</b>.</i>",
    ]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_back_menu()
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_STICKER_GOAL)
    await update.effective_message.reply_text(
        f"{_divider()}\n"
        "⚽  <b>FOOTBALL INTELLIGENCE V19</b>\n"
        f"{_divider()}\n\n"
        "🔮 Monte-Carlo · 100 000 simulations\n"
        "📊 Indice de Force · 8 dimensions\n"
        "🎯 Scénarios plausibles V18\n"
        "🧠 Apprentissage automatique\n\n"
        f"{_divider()}\n"
        "  <code>/start</code>         Ouvre le menu principal\n"
        "  <code>/help</code>          Affiche cette aide\n\n"
        f"{_divider()}\n"
        "⚽  <b>ANALYSE</b>\n"
        f"{_divider()}\n"
        "  <code>/scan</code>          Recense les matchs (10h)\n"
        "  <code>/today</code>         Liste les matchs en cache\n"
        "  <code>/predict 2</code>     Analyse le match n°2\n"
        "  <code>/match A vs B</code>  Recherche dans le cache\n"
        "  <code>/example</code>       Analyse un match aléatoire\n\n"
        f"{_divider()}\n"
        "📌  <b>RÉSULTATS & FIABILITÉ</b>\n"
        f"{_divider()}\n"
        "  <code>/resultat 42 2-1</code>  Enregistre le score réel\n"
        "  <code>/autoresultat</code>      Vérifie les matchs terminés\n"
        "  <code>/fiabilite</code>         Précision réelle mesurée\n\n"
        f"{_divider()}\n"
        "🧠  <b>APPRENTISSAGE (V18 — sécurisé)</b>\n"
        f"{_divider()}\n"
        "  <code>/apprentissage</code>    Analyse des erreurs par marché\n"
        "  <code>/apprentissage2</code>   Analyse V2 + auto-amélioration 🔒✨\n"
        "  <code>/recalibrer</code>       Recalibrage sécurisé (jamais de régression)\n"
        "  <code>/recalibrerforce</code>  Recalibrage immédiat, sans backtest\n"
        "  <code>/valider</code>          Validation holdout temporelle\n"
        "  <code>/versions</code>         Historique des calibrations\n"
        "  <code>/versions 3</code>       Restaurer la v3\n"
        "  <code>/backtestxg</code>       Backtest xG_multiplier (holdout sécurisé)\n"
        "  <code>/auditforce</code>       Audit d'ablation de l'indice de force (diagnostique)\n"
        "  <code>/audith2h</code>         Audit du canal H2H (diagnostique)\n\n"
        f"{_divider()}\n"
        "🧬  <b>V18 — NOUVELLES FONCTIONS</b>\n"
        f"{_divider()}\n"
        "  <code>/memoire</code>          Mémoire de toutes les équipes\n"
        "  <code>/memoire PSG</code>      Profil mémoire d'une équipe\n"
        "  <code>/apprentissagev18</code> Analyse profonde des scénarios\n\n"
        f"{_divider()}\n"
        "🔬  <b>V20 — MÉMOIRE STATISTIQUE & ANOMALIES</b>\n"
        f"{_divider()}\n"
        "  <code>/historique ligue=... conf_min=60</code>\n"
        "                          Requête libre sur l'historique réglé\n"
        "  <code>/recalibrerligues</code>  Recalibrage appris par tier de ligue\n"
        "  🔍 Anomalies pré-match  Affichées automatiquement dans /predict\n"
        "                          (historique similaire, écart cote/modèle)\n\n"
        f"{_divider()}\n"
        "  <code>/delete</code>           Supprimer le cache\n\n"
        f"{_divider('═')}\n"
        "  ⚠️ <i>Ces probabilités sont des estimations statistiques.</i>\n"
        "  <i>Ne pas utiliser comme conseils de paris.</i>",
        parse_mode="HTML",
        reply_markup=_back_menu(),
    )


# ── Callback router ───────────────────────────────────────────────────────────

async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    message = query.message
    if message is None:
        return
    data = query.data or ""

    if data == "menu:home":
        await message.edit_text(
            f"{_divider()}\n"
            "⚽  <b>FOOTBALL INTELLIGENCE</b>\n"
            f"{_divider()}\n\n"
            "👇 <b>Que veux-tu faire ?</b>",
            parse_mode="HTML",
            reply_markup=_menu(),
        )
    elif data == "menu:scan":
        await _run_scan(message, ctx)
    elif data == "menu:today":
        await _show_today(message, ctx)
    elif data == "menu:predict":
        await cmd_predict(update, ctx)
    elif data == "menu:example":
        await cmd_example(update, ctx)
    elif data == "menu:help":
        await cmd_help(update, ctx)
    elif data == "menu:delete":
        await cmd_delete(update, ctx)
    elif data == "menu:fiabilite":
        await cmd_calibration(update, ctx)
    elif data == "menu:autoresultat":
        await cmd_auto_result(update, ctx)
    elif data == "menu:apprentissage":
        await cmd_learning(update, ctx)
    elif data == "menu:recalibrer":
        await cmd_recalibrate(update, ctx)
    elif data == "menu:valider":
        await cmd_validate(update, ctx)
    elif data == "menu:versions":
        await cmd_versions(update, ctx)
    elif data == "menu:apprentissage2":
        await cmd_learning_v2(update, ctx)
    elif data == "menu:memoire":
        await cmd_memoire(update, ctx)
    elif data.startswith("page:"):
        # Navigation entre pages du clavier de matchs
        try:
            page  = int(data.split(":", 1)[1])
            items = scanner.cached_snapshots()
            if items:
                await message.edit_reply_markup(
                    reply_markup=_match_keyboard(items, page=page)
                )
        except Exception:
            pass
    elif data == "noop":
        # Bouton indicateur de page — aucune action
        pass
    elif data.startswith("pick:"):
        try:
            await _predict_index(message, ctx, int(data.split(":", 1)[1]))
        except ValueError:
            await message.reply_text("❌ Sélection invalide.")


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram error: %s", ctx.error, exc_info=ctx.error)


# ── Main ──────────────────────────────────────────────────────────────────────

_scan_locks_dict: dict[int, asyncio.Lock] = {}


def main() -> None:
    logger.info("Starting Football Intelligence V19")
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("scan",          cmd_scan))
    app.add_handler(CommandHandler("delete",        cmd_delete))
    app.add_handler(CommandHandler("today",         cmd_today))
    app.add_handler(CommandHandler("predict",       cmd_predict))
    app.add_handler(CommandHandler("match",         cmd_match))
    app.add_handler(CommandHandler("example",       cmd_example))
    app.add_handler(CommandHandler("resultat",      cmd_result))
    app.add_handler(CommandHandler("autoresultat",  cmd_auto_result))
    app.add_handler(CommandHandler("fiabilite",     cmd_calibration))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("apprentissage", cmd_learning))
    app.add_handler(CommandHandler("recalibrer",      cmd_recalibrate))
    app.add_handler(CommandHandler("recalibrerforce", cmd_recalibrate_force))
    app.add_handler(CommandHandler("valider",       cmd_validate))
    app.add_handler(CommandHandler("versions",      cmd_versions))
    app.add_handler(CommandHandler("apprentissage2", cmd_learning_v2))
    app.add_handler(CommandHandler("apprentissagev18", cmd_learning_v18))
    app.add_handler(CommandHandler("memoire",       cmd_memoire))
    app.add_handler(CommandHandler("historique",    cmd_history))
    app.add_handler(CommandHandler("backtestxg",    cmd_backtest_xg))
    app.add_handler(CommandHandler("auditforce",     cmd_audit_force))
    app.add_handler(CommandHandler("audith2h",       cmd_audit_h2h))
    app.add_handler(CommandHandler("recalibrerligues", cmd_league_calibration))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_error_handler(error_handler)
    logger.info("Bot polling — V19")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
