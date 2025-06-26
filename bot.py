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


# --- Константы для ConversationHandler состояний ---
REMINDER_DATE, REMINDER_TEXT = range(2)
DAILY_TIME, DAILY_TEXT = range(2)
WEEKLY_DAY, WEEKLY_TIME, WEEKLY_TEXT = range(3)
REM_DEL_ID = 0

# --- Вспомогательные функции для хранения напоминаний (можно заменить на вашу реализацию) ---
def get_reminders_filename(chat_id):
    return f"reminders_{chat_id}.json"

def load_reminders(chat_id):
    try:
        with open(get_reminders_filename(chat_id), "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def save_reminders(chat_id, reminders):
    with open(get_reminders_filename(chat_id), "w") as f:
        json.dump(reminders, f)

# --- Обработчики добавления разового напоминания ---
def start_add_one_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("Введите дату и время напоминания в формате ГГГГ-ММ-ДД ЧЧ:ММ (например, 2024-07-10 16:30):")
    return REMINDER_DATE

def receive_reminder_datetime(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
        if dt < datetime.datetime.now():
            update.message.reply_text("Дата и время уже прошли. Введите корректную дату и время:")
            return REMINDER_DATE
        context.user_data["reminder_datetime"] = text
        update.message.reply_text("Теперь введите текст напоминания:")
        return REMINDER_TEXT
    except Exception:
        update.message.reply_text("Некорректный формат. Введите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ:")
        return REMINDER_DATE

def receive_reminder_text(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    dt_str = context.user_data.get("reminder_datetime")
    reminders = load_reminders(chat_id)
    reminder_id = str(uuid4())
    reminders.append({
        "id": reminder_id,
        "type": "once",
        "datetime": dt_str,
        "text": text
    })
    save_reminders(chat_id, reminders)
    update.message.reply_text(f"✅ Напоминание добавлено: {dt_str}\n{text}")
    return ConversationHandler.END

# --- Обработчики добавления ежедневного напоминания ---
def start_add_daily_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("Введите время для ежедневного напоминания в формате ЧЧ:ММ (например, 08:00):")
    return DAILY_TIME

def receive_daily_time(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        time.strptime(text, "%H:%M")
        context.user_data["daily_time"] = text
        update.message.reply_text("Введите текст ежедневного напоминания:")
        return DAILY_TEXT
    except Exception:
        update.message.reply_text("Некорректный формат. Введите время в формате ЧЧ:ММ:")
        return DAILY_TIME

def receive_daily_text(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    time_str = context.user_data.get("daily_time")
    reminders = load_reminders(chat_id)
    reminder_id = str(uuid4())
    reminders.append({
        "id": reminder_id,
        "type": "daily",
        "time": time_str,
        "text": text
    })
    save_reminders(chat_id, reminders)
    update.message.reply_text(f"✅ Ежедневное напоминание добавлено: {time_str}\n{text}")
    return ConversationHandler.END

# --- Обработчики добавления еженедельного напоминания ---
def start_add_weekly_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("Введите день недели для напоминания (например, понедельник):")
    return WEEKLY_DAY

def receive_weekly_day(update: Update, context: CallbackContext):
    text = update.message.text.strip().lower()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    if text not in days:
        update.message.reply_text("Некорректный день недели. Введите, например: понедельник")
        return WEEKLY_DAY
    context.user_data["weekly_day"] = text
    update.message.reply_text("Введите время напоминания (ЧЧ:ММ):")
    return WEEKLY_TIME

def receive_weekly_time(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        time.strptime(text, "%H:%M")
        context.user_data["weekly_time"] = text
        update.message.reply_text("Введите текст еженедельного напоминания:")
        return WEEKLY_TEXT
    except Exception:
        update.message.reply_text("Некорректный формат. Введите время в формате ЧЧ:ММ:")
        return WEEKLY_TIME

def receive_weekly_text(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    day = context.user_data.get("weekly_day")
    time_str = context.user_data.get("weekly_time")
    reminders = load_reminders(chat_id)
    reminder_id = str(uuid4())
    reminders.append({
        "id": reminder_id,
        "type": "weekly",
        "day": day,
        "time": time_str,
        "text": text
    })
    save_reminders(chat_id, reminders)
    update.message.reply_text(f"✅ Еженедельное напоминание добавлено: {day.title()} {time_str}\n{text}")
    return ConversationHandler.END

# --- Список напоминаний ---
def list_reminders(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    reminders = load_reminders(chat_id)
    if not reminders:
        update.message.reply_text("У вас нет напоминаний.")
        return
    lines = []
    for idx, r in enumerate(reminders, 1):
        if r["type"] == "once":
            lines.append(f"{idx}. [Разово] {r['datetime']}: {r['text']}")
        elif r["type"] == "daily":
            lines.append(f"{idx}. [Ежедневно] {r['time']}: {r['text']}")
        elif r["type"] == "weekly":
            lines.append(f"{idx}. [Еженедельно] {r['day'].title()} {r['time']}: {r['text']}")
    update.message.reply_text("\n".join(lines))

# --- Удаление напоминания ---
def start_delete_reminder(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    reminders = load_reminders(chat_id)
    if not reminders:
        update.message.reply_text("У вас нет напоминаний для удаления.")
        return ConversationHandler.END
    lines = []
    for idx, r in enumerate(reminders, 1):
        if r["type"] == "once":
            lines.append(f"{idx}. [Разово] {r['datetime']}: {r['text']}")
        elif r["type"] == "daily":
            lines.append(f"{idx}. [Ежедневно] {r['time']}: {r['text']}")
        elif r["type"] == "weekly":
            lines.append(f"{idx}. [Еженедельно] {r['day'].title()} {r['time']}: {r['text']}")
    update.message.reply_text("Выберите номер напоминания для удаления:\n" + "\n".join(lines))
    return REM_DEL_ID

def confirm_delete_reminder(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    reminders = load_reminders(chat_id)
    try:
        idx = int(update.message.text.strip()) - 1
        if idx < 0 or idx >= len(reminders):
            raise ValueError
        removed = reminders.pop(idx)
        save_reminders(chat_id, reminders)
        update.message.reply_text("✅ Напоминание удалено.")
    except Exception:
        update.message.reply_text("Некорректный номер. Операция отменена.")
    return ConversationHandler.END

# --- Очистка всех напоминаний ---
def clear_reminders(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    save_reminders(chat_id, [])
    update.message.reply_text("Все напоминания удалены.")

# --- Следующее напоминание ---
def next_notification(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    reminders = load_reminders(chat_id)
    if not reminders:
        update.message.reply_text("Нет запланированных напоминаний.")
        return
    # Найти ближайшее напоминание
    now = datetime.datetime.now()
    soonest = None
    soonest_time = None
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    for r in reminders:
        t = None
        if r["type"] == "once":
            t = datetime.datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M")
        elif r["type"] == "daily":
            h, m = map(int, r["time"].split(":"))
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate < now:
                candidate += datetime.timedelta(days=1)
            t = candidate
        elif r["type"] == "weekly":
            weekday = days.index(r["day"])
            h, m = map(int, r["time"].split(":"))
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            days_ahead = (weekday - now.weekday() + 7) % 7
            if days_ahead == 0 and candidate < now:
                days_ahead = 7
            t = candidate + datetime.timedelta(days=days_ahead)
        if soonest_time is None or t < soonest_time:
            soonest_time = t
            soonest = r
    if soonest is None:
        update.message.reply_text("Нет запланированных напоминаний.")
        return
    if soonest["type"] == "once":
        msg = f"[Разово] {soonest['datetime']}: {soonest['text']}"
    elif soonest["type"] == "daily":
        msg = f"[Ежедневно] {soonest['time']}: {soonest['text']}"
    elif soonest["type"] == "weekly":
        msg = f"[Еженедельно] {soonest['day'].title()} {soonest['time']}: {soonest['text']}"
    update.message.reply_text(f"Ближайшее напоминание:\n{msg}")

def cancel_reminder(update: Update, context: CallbackContext):
    """
    Отмена создания напоминания.
    """
    update.message.reply_text("❌ Операция отменена.")
    return ConversationHandler.END

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
