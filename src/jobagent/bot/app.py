"""Telegram command bot (python-telegram-bot, async). Owner-locked.

Commands: /start /help /jobs [N] /apply <rank> /status, plus inline buttons:
- email postings → Approve & send / Cancel
- ATS postings (Greenhouse/Lever/Ashby) → fill + screenshot preview, then Submit / Cancel

Blocking work (LLM, SMTP, Playwright, SQLite) runs in worker threads via
asyncio.to_thread. SQLite forbids sharing a connection across threads, so each
threaded helper opens its OWN Store from db_path. Pure logic lives in service.py.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from jobagent.apply import approve_and_send, prepare_application
from jobagent.apply.ats import apply_target
from jobagent.apply.ats_flow import create_ats_application, run_ats
from jobagent.bot.service import (
    HELP_TEXT,
    apply_callback_data,
    apply_preview_text,
    is_owner,
    jobs_text,
    parse_callback_data,
    resolve_ranked_job,
    status_text,
)
from jobagent.core.schemas import ApplicationStatus
from jobagent.store import Store


def _bd(context, key):
    return context.application.bot_data.get(key)


def _db(context) -> str:
    return _bd(context, "settings").db_path


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


# --- guards ----------------------------------------------------------------------
async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not is_owner(chat.id if chat else None, _bd(context, "owner_id")):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ This is a private bot.")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    n = 10
    if context.args:
        try:
            n = max(1, min(25, int(context.args[0])))
        except ValueError:
            pass
    store = Store(_db(context))
    try:
        text = jobs_text(store, n)
    finally:
        store.close()
    await update.message.reply_text(text, disable_web_page_preview=True)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    store = Store(_db(context))
    try:
        text = status_text(store.stats())
    finally:
        store.close()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


def _buttons(*specs):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=apply_callback_data(a, i)) for t, a, i in specs]])


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

    db_path = _db(context)
    store = Store(db_path)
    try:
        job = resolve_ranked_job(store, rank)
        if job is None:
            await update.message.reply_text(f"No job #{rank} in the current shortlist. Try /jobs.")
            return
        platform, target_url = apply_target(job)
        ats_app_id = create_ats_application(store, job) if platform else None
    finally:
        store.close()

    # --- ATS (Tier-2) path: fill + screenshot preview, then Submit/Cancel ---
    if platform:
        Path("artifacts").mkdir(exist_ok=True)
        shot = f"artifacts/ats_{ats_app_id}.png"
        await update.message.reply_text(f"🤖 Filling the {platform} form for preview… (~15s)")
        try:
            result = await asyncio.to_thread(_run_ats, db_path, ats_app_id, _bd(context, "profile"), shot, False)
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"⚠️ Form-fill failed: {exc}\nApply manually: {target_url}")
            return
        if result.screenshot_path and Path(shot).exists():
            with open(shot, "rb") as f:
                await update.message.reply_photo(f, caption=result.summary()[:1000])
        else:
            await update.message.reply_text(result.summary())
        if result.captcha_detected:
            await update.message.reply_text(f"⚠️ CAPTCHA present — finish manually: {result.url}")
            return
        await update.message.reply_text(
            "Review the screenshot. Submit this application?",
            reply_markup=_buttons(("✅ Submit", "atssubmit", ats_app_id), ("✖ Cancel", "cancel", ats_app_id)),
        )
        return

    # --- Email (Tier-1) path: draft assets, then Approve & send/Cancel ---
    llm, cv_master = _bd(context, "llm"), _bd(context, "cv_master")
    if llm is None:
        await update.message.reply_text("⚠️ Set an LLM key (e.g. GROQ_API_KEY) to draft email applications.")
        return
    if not cv_master:
        await update.message.reply_text("⚠️ config/cv_master.md is missing on the server.")
        return
    await update.message.reply_text(f"✍️ Drafting application for {job.get('title')}… (~a few seconds)")
    try:
        bundle = await asyncio.to_thread(_prepare, db_path, job, _bd(context, "profile"), cv_master, llm)
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"⚠️ Drafting failed: {exc}")
        return
    try:
        bio = BytesIO(bundle.cv_markdown.encode())
        bio.name = "Tailored_CV.md"
        await update.message.reply_document(bio)
    except Exception:  # noqa: BLE001 — best effort
        pass
    await update.message.reply_text(
        apply_preview_text(bundle), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
        reply_markup=_buttons(("✅ Approve & send", "approve", bundle.application_id),
                              ("✖ Cancel", "cancel", bundle.application_id)),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat if query.message else None
    if not is_owner(chat.id if chat else None, _bd(context, "owner_id")):
        return
    action, app_id = parse_callback_data(query.data)
    db_path = _db(context)

    if action == "cancel":
        store = Store(db_path)
        try:
            store.update_application(app_id, status=ApplicationStatus.skipped.value)
        finally:
            store.close()
        await query.edit_message_text("✖ Cancelled — not submitted.")

    elif action == "approve":  # email send
        try:
            result = await asyncio.to_thread(_approve, db_path, app_id, _bd(context, "settings"), _bd(context, "profile"))
        except Exception as exc:  # noqa: BLE001
            result = f"⚠️ Send failed: {exc}"
        await query.edit_message_text(result)

    elif action == "atssubmit":  # ATS form submit
        Path("artifacts").mkdir(exist_ok=True)
        shot = f"artifacts/ats_{app_id}_submit.png"
        try:
            result = await asyncio.to_thread(_run_ats, db_path, app_id, _bd(context, "profile"), shot, True)
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
        profile=profile, llm=llm, cv_master=cv_master,
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("jobs", jobs))
    app.add_handler(CommandHandler("apply", apply_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(on_button))
    return app
