"""Telegram command bot (python-telegram-bot, async). Owner-locked.

Commands: /start /help /jobs [N] /apply <rank> /status, plus inline Approve/Cancel
buttons for applications. Run via scripts/run_bot.py. Pure logic lives in service.py.
"""

from __future__ import annotations

import asyncio
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from jobagent.apply import approve_and_send, prepare_application
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


def _store(context) -> Store:
    return Store(_bd(context, "settings").db_path)


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
    store = _store(context)
    try:
        text = jobs_text(store, n)
    finally:
        store.close()
    await update.message.reply_text(text, disable_web_page_preview=True)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    store = _store(context)
    try:
        text = status_text(store.stats())
    finally:
        store.close()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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

    llm = _bd(context, "llm")
    cv_master = _bd(context, "cv_master")
    if llm is None:
        await update.message.reply_text("⚠️ Set OPENROUTER_API_KEY to draft applications.")
        return
    if not cv_master:
        await update.message.reply_text("⚠️ config/cv_master.md is missing on the server.")
        return

    store = _store(context)
    try:
        job = resolve_ranked_job(store, rank)
        if job is None:
            await update.message.reply_text(f"No job #{rank} in the current shortlist. Try /jobs.")
            return
        await update.message.reply_text(f"✍️ Drafting application for {job.get('title')}… (~a few seconds)")
        # Generation is blocking (LLM HTTP call) — run off the event loop.
        bundle = await asyncio.to_thread(
            prepare_application, store, job, _bd(context, "profile"), cv_master, llm
        )
    finally:
        store.close()

    # Tailored CV as a downloadable file.
    try:
        bio = BytesIO(bundle.cv_markdown.encode())
        bio.name = "Tailored_CV.md"
        await update.message.reply_document(bio)
    except Exception:  # noqa: BLE001 — file send is best-effort; preview still works
        pass

    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve & send", callback_data=apply_callback_data("approve", bundle.application_id)),
        InlineKeyboardButton("✖ Cancel", callback_data=apply_callback_data("cancel", bundle.application_id)),
    ]])
    await update.message.reply_text(
        apply_preview_text(bundle), parse_mode=ParseMode.MARKDOWN,
        reply_markup=buttons, disable_web_page_preview=True,
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat if query.message else None
    if not is_owner(chat.id if chat else None, _bd(context, "owner_id")):
        return
    action, app_id = parse_callback_data(query.data)
    settings = _bd(context, "settings")
    store = _store(context)
    try:
        if action == "cancel":
            store.update_application(app_id, status=ApplicationStatus.skipped.value)
            await query.edit_message_text("✖ Cancelled — not sent.")
        elif action == "approve":
            result = await asyncio.to_thread(
                approve_and_send, store, app_id, settings, _bd(context, "profile")
            )
            await query.edit_message_text(result)
    finally:
        store.close()


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
