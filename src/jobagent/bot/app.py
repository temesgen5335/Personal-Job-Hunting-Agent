"""Telegram command bot (python-telegram-bot, async). Owner-locked, menu-driven.

/menu opens an inline menu: set Date / Location / Keyword filters, Show jobs (with
tap-to-Apply buttons), Status. /jobs and /apply still work as commands. The current
filter lives in bot_data and is shared by /jobs, the menu, and /apply numbering.

Blocking work (LLM, SMTP, Playwright, SQLite) runs in worker threads; each threaded
helper opens its OWN Store (SQLite is single-thread). Pure logic lives in service.py.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from jobagent.apply import approve_and_send, prepare_application
from jobagent.apply.ats import apply_target
from jobagent.apply.ats_flow import create_ats_application, run_ats
from jobagent.bot.service import (
    DATE_PRESETS,
    MatchFilter,
    apply_callback_data,
    apply_menu_action,
    apply_preview_text,
    filter_summary,
    is_owner,
    parse_callback_data,
    ranked_matches,
    resolve_ranked_job,
    set_keywords,
    status_text,
)
from jobagent.core.schemas import ApplicationStatus
from jobagent.fit import assess_fit
from jobagent.store import Store

MD = ParseMode.MARKDOWN


def _bd(context, key):
    return context.application.bot_data.get(key)


def _db(context) -> str:
    return _bd(context, "settings").db_path


def _flt(context) -> MatchFilter:
    return _bd(context, "filter")


# --- thread helpers: each opens its own Store (SQLite is single-thread) ----------
def _prepare(db_path, job, profile, cv_master, llm):
    store = Store(db_path)
    try:
        return prepare_application(store, job, profile, cv_master, llm)
    finally:
        store.close()


def _approve(db_path, app_id, settings, profile):
    store = Store(db_path)
    try:
        return approve_and_send(store, app_id, settings, profile)
    finally:
        store.close()


def _run_ats(db_path, app_id, profile, shot, submit):
    store = Store(db_path)
    try:
        return run_ats(store, app_id, profile, shot, submit=submit)
    finally:
        store.close()


# --- keyboards -------------------------------------------------------------------
def _btn(text, action, value=""):
    return InlineKeyboardButton(text, callback_data=apply_callback_data(action, value))


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("📋 Show jobs", "show")],
        [_btn("📅 Date", "datemenu"), _btn("📍 Location", "locmenu")],
        [_btn("🔤 Set keywords", "kw"), _btn("🧹 Clear keywords", "kwclear")],
        [_btn("📊 Status", "status")],
    ])


def date_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn(lbl, "date", str(days)) for lbl, days in DATE_PRESETS[:3]],
        [_btn(lbl, "date", str(days)) for lbl, days in DATE_PRESETS[3:]],
        [_btn("⬅ Back", "menu")],
    ])


def loc_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🌍 Remote", "loc", "remote"), _btn("🏢 Hybrid", "loc", "hybrid"), _btn("Any", "loc", "any")],
        [_btn("⬅ Back", "menu")],
    ])


def jobs_kb(count: int) -> InlineKeyboardMarkup:
    nums = [_btn(f"📨 {i}", "apply", str(i)) for i in range(1, count + 1)]
    rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    rows.append([_btn("⬅ Menu", "menu")])
    return InlineKeyboardMarkup(rows)


def menu_text(flt: MatchFilter) -> str:
    return f"🤖 *Personal Job Agent*\n\nFilters: {filter_summary(flt)}\n\nPick an option:"


# --- guards ----------------------------------------------------------------------
async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not is_owner(chat.id if chat else None, _bd(context, "owner_id")):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ This is a private bot.")
        return False
    return True


# --- commands --------------------------------------------------------------------
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        await update.message.reply_text(menu_text(_flt(context)), parse_mode=MD, reply_markup=main_menu_kb())


async def jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    n = 10
    if context.args:
        try:
            n = max(1, min(25, int(context.args[0])))
        except ValueError:
            pass
    await _show_jobs(context, update.message, n)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    store = Store(_db(context))
    try:
        text = status_text(store.stats())
    finally:
        store.close()
    await update.message.reply_text(text, parse_mode=MD)


async def apply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /apply <rank>  (rank from /jobs)")
        return
    try:
        rank = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Rank must be a number, e.g. /apply 3")
        return
    await _start_apply(context, update.message, rank)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Captures keywords when the menu asked for them."""
    if not await _guard(update, context):
        return
    if context.user_data.get("awaiting") == "keyword":
        flt = set_keywords(_flt(context), update.message.text)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            f"✅ Keywords set: {', '.join(flt.keywords) or 'none'}\n\n{menu_text(flt)}",
            parse_mode=MD, reply_markup=main_menu_kb(),
        )


# --- shared views ----------------------------------------------------------------
async def _show_jobs(context, msg, n: int) -> None:
    store = Store(_db(context))
    try:
        ranked = ranked_matches(store, n, _flt(context))
        from jobagent.digest import format_matches
        text = format_matches(ranked)
    finally:
        store.close()
    kb = jobs_kb(len(ranked)) if ranked else main_menu_kb()
    await msg.reply_text(text, disable_web_page_preview=True, reply_markup=kb)


async def _start_apply(context, msg, rank: int) -> None:
    db_path = _db(context)
    store = Store(db_path)
    try:
        job = resolve_ranked_job(store, rank, _flt(context))
        if job is None:
            await msg.reply_text(f"No job #{rank} in the current list. Try /jobs.")
            return
        platform, target_url = apply_target(job)
        ats_app_id = create_ats_application(store, job) if platform else None
    finally:
        store.close()

    # Fit check first — show the confidence + matched/gaps before drafting/filling.
    fit = await asyncio.to_thread(assess_fit, job, _bd(context, "profile"), _bd(context, "cv_master"), _llm())
    await msg.reply_text(fit.format_short(), parse_mode=MD)

    # --- ATS (Tier-2): fill + screenshot preview, then Submit/Cancel ---
    if platform:
        Path("artifacts").mkdir(exist_ok=True)
        shot = f"artifacts/ats_{ats_app_id}.png"
        await msg.reply_text(f"🤖 Filling the {platform} form for preview… (~15s)")
        try:
            result = await asyncio.to_thread(_run_ats, db_path, ats_app_id, _bd(context, "profile"), shot, False)
        except Exception as exc:  # noqa: BLE001
            await msg.reply_text(f"⚠️ Form-fill failed: {exc}\nApply manually: {target_url}")
            return
        if result.screenshot_path and Path(shot).exists():
            with open(shot, "rb") as f:
                await msg.reply_photo(f, caption=result.summary()[:1000])
        else:
            await msg.reply_text(result.summary())
        if result.captcha_detected:
            await msg.reply_text(f"⚠️ CAPTCHA present — finish manually: {result.url}")
            return
        await msg.reply_text(
            "Review the screenshot. Submit this application?",
            reply_markup=InlineKeyboardMarkup([[
                _btn("✅ Submit", "atssubmit", ats_app_id), _btn("✖ Cancel", "cancel", ats_app_id)]]),
        )
        return

    # --- Email (Tier-1): draft assets, then Approve & send / Cancel ---
    llm, cv_master = _bd(context, "llm"), _bd(context, "cv_master")
    if llm is None:
        await msg.reply_text("⚠️ Set an LLM key (e.g. GROQ_API_KEY) to draft email applications.")
        return
    if not cv_master:
        await msg.reply_text("⚠️ config/cv_master.md is missing on the server.")
        return
    await msg.reply_text(f"✍️ Drafting application for {job.get('title')}… (~a few seconds)")
    try:
        bundle = await asyncio.to_thread(_prepare, db_path, job, _bd(context, "profile"), cv_master, llm)
    except Exception as exc:  # noqa: BLE001
        await msg.reply_text(f"⚠️ Drafting failed: {exc}")
        return
    try:
        bio = BytesIO(bundle.cv_markdown.encode())
        bio.name = "Tailored_CV.md"
        await msg.reply_document(bio)
    except Exception:  # noqa: BLE001
        pass
    await msg.reply_text(
        apply_preview_text(bundle), parse_mode=MD, disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            _btn("✅ Approve & send", "approve", bundle.application_id),
            _btn("✖ Cancel", "cancel", bundle.application_id)]]),
    )


# --- callback router -------------------------------------------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat if query.message else None
    if not is_owner(chat.id if chat else None, _bd(context, "owner_id")):
        return
    action, value = parse_callback_data(query.data)
    flt = _flt(context)

    # --- menu navigation / filters ---
    if action == "menu":
        await query.edit_message_text(menu_text(flt), parse_mode=MD, reply_markup=main_menu_kb())
    elif action == "datemenu":
        await query.edit_message_text("📅 Show jobs posted within:", reply_markup=date_menu_kb())
    elif action == "locmenu":
        await query.edit_message_text("📍 Location:", reply_markup=loc_menu_kb())
    elif action in ("date", "loc", "kwclear"):
        apply_menu_action(flt, action, value)
        await query.edit_message_text(menu_text(flt), parse_mode=MD, reply_markup=main_menu_kb())
    elif action == "kw":
        context.user_data["awaiting"] = "keyword"
        await query.message.reply_text("🔤 Send keywords (space/comma separated), e.g. `frontend react ai`", parse_mode=MD)
    elif action == "status":
        store = Store(_db(context))
        try:
            text = status_text(store.stats())
        finally:
            store.close()
        await query.edit_message_text(text, parse_mode=MD,
                                      reply_markup=InlineKeyboardMarkup([[_btn("⬅ Menu", "menu")]]))
    elif action == "show":
        await _show_jobs(context, query.message, 10)

    # --- apply flow ---
    elif action == "apply":
        try:
            await _start_apply(context, query.message, int(value))
        except ValueError:
            pass
    elif action == "cancel":
        store = Store(_db(context))
        try:
            store.update_application(value, status=ApplicationStatus.skipped.value)
        finally:
            store.close()
        await query.edit_message_text("✖ Cancelled — not submitted.")
    elif action == "approve":
        try:
            result = await asyncio.to_thread(_approve, _db(context), value, _bd(context, "settings"), _bd(context, "profile"))
        except Exception as exc:  # noqa: BLE001
            result = f"⚠️ Send failed: {exc}"
        await query.edit_message_text(result)
    elif action == "atssubmit":
        Path("artifacts").mkdir(exist_ok=True)
        shot = f"artifacts/ats_{value}_submit.png"
        try:
            result = await asyncio.to_thread(_run_ats, _db(context), value, _bd(context, "profile"), shot, True)
        except Exception as exc:  # noqa: BLE001
            await query.edit_message_text(f"⚠️ Submit failed: {exc}")
            return
        if result.captcha_detected:
            await query.edit_message_text(f"⚠️ CAPTCHA blocked submit. Finish manually: {result.url}")
        elif result.submitted:
            await query.edit_message_text(f"✅ Submitted to {result.platform}.")
        else:
            await query.edit_message_text(f"Couldn't find the submit button. Finish manually: {result.url}")


def build_application(settings, profile, llm, cv_master: str) -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data.update(
        settings=settings, owner_id=settings.telegram_destination,
        profile=profile, llm=llm, cv_master=cv_master, filter=MatchFilter(),
    )
    app.add_handler(CommandHandler(["start", "help", "menu"], menu))
    app.add_handler(CommandHandler("jobs", jobs))
    app.add_handler(CommandHandler("apply", apply_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
