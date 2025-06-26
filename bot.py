# bot.py

import os
import logging
import threading
import time
import datetime
import json
from uuid import uuid4
import pytz
import requests
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, Job, ConversationHandler, MessageHandler, Filters
import html
from telegram.ext import MessageHandler
from telegram.error import Conflict

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def error_handler(update: Update, context: CallbackContext):
    """
    Handle errors by logging them without crashing the bot.
    """
    logger.error("Uncaught exception:", exc_info=context.error)

def subscribe_chat(chat_id):
    try:
        with open("subscribed_chats.json", "r") as f:
            chats = json.load(f)
    except FileNotFoundError:
        chats = []

    if chat_id not in chats:
        chats.append(chat_id)
        save_chats(chats)

def save_chats(chats):
    with open("subscribed_chats.json", "w") as f:
        json.dump(chats, f)

def subscribe_and_pass(update: Update, context: CallbackContext):
    """
    Автоподписка текущего чата на рассылку при вводе любой команды.
    """
    subscribe_chat(update.effective_chat.id)

# --- /start и /test команды ---
def start(update: Update, context: CallbackContext):
    """
    Обработчик команды /start.
    """
    chat_id = update.effective_chat.id
    subscribe_chat(chat_id)
    context.bot.send_message(chat_id=chat_id,
                             text="Привет! Я бот-напоминатель, используй /remind для создания напоминаний.",
                             parse_mode=ParseMode.HTML)

def test(update: Update, context: CallbackContext):
    """
    Обработчик команды /test для проверки работы бота.
    """
    chat_id = update.effective_chat.id
    subscribe_chat(chat_id)
    context.bot.send_message(chat_id=chat_id,
                             text="✅ Бот работает корректно!",
                             parse_mode=ParseMode.HTML)

# ... все ваши функции (оставьте как есть) ...

def main():
    updater = Updater(token=os.environ['BOT_TOKEN'], use_context=True)
    dp = updater.dispatcher
    dp.add_error_handler(error_handler)

    # auto-subscribe any chat when a command is used
    dp.add_handler(MessageHandler(Filters.command, subscribe_and_pass), group=0)

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("test", test))
    conv = ConversationHandler(
        entry_points=[CommandHandler("remind", start_add_one_reminder)],
        states={
            REMINDER_DATE: [MessageHandler(Filters.text & ~Filters.command, receive_reminder_datetime)],
            REMINDER_TEXT: [MessageHandler(Filters.text & ~Filters.command, receive_reminder_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel_reminder)],
        allow_reentry=True,
    )
    dp.add_handler(conv)
    conv_daily = ConversationHandler(
        entry_points=[CommandHandler("remind_daily", start_add_daily_reminder)],
        states={
            DAILY_TIME: [MessageHandler(Filters.text & ~Filters.command, receive_daily_time)],
            DAILY_TEXT: [MessageHandler(Filters.text & ~Filters.command, receive_daily_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel_reminder)],
        allow_reentry=True,
    )
    dp.add_handler(conv_daily)

    conv_weekly = ConversationHandler(
        entry_points=[CommandHandler("remind_weekly", start_add_weekly_reminder)],
        states={
            WEEKLY_DAY: [MessageHandler(Filters.text & ~Filters.command, receive_weekly_day)],
            WEEKLY_TIME: [MessageHandler(Filters.text & ~Filters.command, receive_weekly_time)],
            WEEKLY_TEXT: [MessageHandler(Filters.text & ~Filters.command, receive_weekly_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel_reminder)],
        allow_reentry=True,
    )
    dp.add_handler(conv_weekly)
    dp.add_handler(CommandHandler("list_reminders", list_reminders))
    conv_del = ConversationHandler(
        entry_points=[CommandHandler("del_reminder", start_delete_reminder)],
        states={REM_DEL_ID: [MessageHandler(Filters.text & ~Filters.command, confirm_delete_reminder)]},
        fallbacks=[CommandHandler("cancel", cancel_reminder)],
        allow_reentry=True,
    )
    dp.add_handler(conv_del)
    dp.add_handler(CommandHandler("clear_reminders", clear_reminders))
    dp.add_handler(CommandHandler("next", next_notification))

    # Запланировать все сохранённые напоминания
    schedule_all_reminders(updater.job_queue)

    logger.info("Polling начат, бот готов к работе")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
