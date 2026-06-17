"""Telegram command bot (python-telegram-bot, async). Owner-locked.

Commands: /start /help /jobs [N] /status. Run via scripts/run_bot.py.
The telegram import is module-level here (this module is only imported when
actually running the bot); pure logic lives in service.py for testing.
"""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from jobagent.bot.service import HELP_TEXT, is_owner, jobs_text, status_text
from jobagent.store import Store


def _store(context: ContextTypes.DEFAULT_TYPE) -> Store:
    return Store(context.application.bot_data["db_path"])


def _owner_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    return context.application.bot_data.get("owner_id")


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not is_owner(chat_id, _owner_id(context)):
        if update.message:
            await update.message.reply_text("⛔ This is a private bot.")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
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


def build_application(token: str, owner_id: int | None, db_path: str) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["owner_id"] = owner_id
    app.bot_data["db_path"] = db_path
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("jobs", jobs))
    app.add_handler(CommandHandler("status", status))
    return app
