# bot.py

import os
import logging
import threading
import time
import datetime
import json
import pytz
import requests
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, Job, ConversationHandler, MessageHandler, Filters
from telegram.error import Conflict
import html
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')

    def do_HEAD(self):
        # Respond to health check HEAD requests
        self.send_response(200)
        self.end_headers()

def start_health_server():
    port = int(os.environ.get('PORT', 5000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    server.serve_forever()

# --- Глобальный файл напоминаний ---
REMINDERS_FILE = "reminders.json"

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def error_handler(update: Update, context: CallbackContext):
    """
    Handle errors by logging them without crashing the bot.
    """
    if isinstance(context.error, Conflict):
        return
    logger.error("Uncaught exception:", exc_info=context.error)

def subscribe_chat(chat_id):
    try:
        with open("subscribed_chats.json", "r") as f:
            data = f.read().strip()
            chats = json.loads(data) if data else []
    except (FileNotFoundError, json.JSONDecodeError):
        chats = []

    if chat_id not in chats:
        chats.append(chat_id)
        save_chats(chats)

def save_chats(chats):
    with open("subscribed_chats.json", "w") as f:
        json.dump(chats, f)

# Функция ping для предотвращения засыпания на Render
def ping_self(context: CallbackContext):
    """
    Пингует сам себя чтобы не засыпать на Render free tier
    """
    try:
        base_url = os.environ.get('BASE_URL', 'https://telegram-repeat-bot.onrender.com')
        response = requests.get(base_url, timeout=10)
        logger.info(f"Self-ping successful: {response.status_code}")
    except Exception as e:
        logger.warning(f"Self-ping failed: {e}")

# --- /start и /test команды ---
def start(update: Update, context: CallbackContext):
    """
    Обработчик команды /start.
    """
    chat_id = update.effective_chat.id
    logger.info("Received /start from chat %s", chat_id)
    subscribe_chat(chat_id)
    context.bot.send_message(chat_id=chat_id,
                             text="✅ <b>Бот активирован в этом чате</b>",
                             parse_mode=ParseMode.HTML)

def test(update: Update, context: CallbackContext):
    """
    Обработчик команды /test для проверки работы бота.
    """
    chat_id = update.effective_chat.id
    logger.info("Received /test from chat %s", chat_id)
    subscribe_chat(chat_id)
    context.bot.send_message(chat_id=chat_id,
                             text="✅ <b>Бот работает корректно!</b>",
                             parse_mode=ParseMode.HTML)

# --- Константы для ConversationHandler состояний ---
REMINDER_DATE, REMINDER_TEXT = range(2)
DAILY_TIME, DAILY_TEXT = range(2)
WEEKLY_DAY, WEEKLY_TIME, WEEKLY_TEXT = range(3)
REM_DEL_ID = 0

# --- Вспомогательные функции для хранения напоминаний (глобальный список) ---
def load_reminders():
    """
    Load reminders from the JSON file, returning an empty list if the file is missing,
    empty, or contains invalid JSON.
    """
    try:
        with open(REMINDERS_FILE, "r", encoding='utf-8') as f:
            data = f.read().strip()
            if not data:
                return []
            return json.loads(data)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_reminders(reminders):
    with open(REMINDERS_FILE, "w", encoding='utf-8') as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)

def get_next_reminder_id():
    """
    Генерирует следующий ID для напоминания
    """
    reminders = load_reminders()
    if not reminders:
        return "1"
    
    # Найти максимальный ID и добавить 1
    max_id = 0
    for reminder in reminders:
        try:
            reminder_id = int(reminder.get("id", "0"))
            if reminder_id > max_id:
                max_id = reminder_id
        except ValueError:
            continue
    
    return str(max_id + 1)

# --- Обработчики добавления разового напоминания ---
def start_add_one_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("📅 <b>Разовое напоминание</b>\n\nВведите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ\nНапример: 2024-07-10 16:30", parse_mode=ParseMode.HTML)
    return REMINDER_DATE

def receive_reminder_datetime(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
        if dt < datetime.datetime.now():
            update.message.reply_text("⚠️ <b>Ошибка:</b> Дата и время уже прошли.\nВведите корректную дату и время:", parse_mode=ParseMode.HTML)
            return REMINDER_DATE
        context.user_data["reminder_datetime"] = text
        update.message.reply_text("✏️ <b>Текст напоминания</b>\n\nВведите текст напоминания (поддерживаются HTML теги и ссылки):", parse_mode=ParseMode.HTML)
        return REMINDER_TEXT
    except Exception:
        update.message.reply_text("❌ <b>Некорректный формат</b>\n\nВведите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ:", parse_mode=ParseMode.HTML)
        return REMINDER_DATE

def receive_reminder_text(update: Update, context: CallbackContext):
    reminders = load_reminders()
    new_id = get_next_reminder_id()
    reminder_text = update.message.text_html if update.message.text_html else update.message.text.strip()
    
    reminders.append({
        "id": new_id,
        "type": "once",
        "datetime": context.user_data["reminder_datetime"],
        "text": reminder_text
    })
    save_reminders(reminders)
    
    # Планируем напоминание
    schedule_reminder(context.dispatcher.job_queue, reminders[-1])
    
    update.message.reply_text(
        f"✅ <b>Напоминание #{new_id} добавлено</b>\n\n"
        f"📅 <i>{context.user_data['reminder_datetime']}</i>\n"
        f"💬 {reminder_text}", 
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

# --- Обработчики добавления ежедневного напоминания ---
def start_add_daily_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("🔄 <b>Ежедневное напоминание</b>\n\nВведите время в формате ЧЧ:ММ\nНапример: 08:00", parse_mode=ParseMode.HTML)
    return DAILY_TIME

def receive_daily_time(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        time.strptime(text, "%H:%M")
        context.user_data["daily_time"] = text
        update.message.reply_text("✏️ <b>Текст ежедневного напоминания</b>\n\nВведите текст (поддерживаются HTML теги и ссылки):", parse_mode=ParseMode.HTML)
        return DAILY_TEXT
    except Exception:
        update.message.reply_text("❌ <b>Некорректный формат</b>\n\nВведите время в формате ЧЧ:ММ:", parse_mode=ParseMode.HTML)
        return DAILY_TIME

def receive_daily_text(update: Update, context: CallbackContext):
    reminders = load_reminders()
    new_id = get_next_reminder_id()
    reminder_text = update.message.text_html if update.message.text_html else update.message.text.strip()
    
    reminders.append({
        "id": new_id,
        "type": "daily",
        "time": context.user_data["daily_time"],
        "text": reminder_text
    })
    save_reminders(reminders)
    
    # Планируем напоминание
    schedule_reminder(context.dispatcher.job_queue, reminders[-1])
    
    update.message.reply_text(
        f"✅ <b>Ежедневное напоминание #{new_id} добавлено</b>\n\n"
        f"🕐 <i>Каждый день в {context.user_data['daily_time']}</i>\n"
        f"💬 {reminder_text}", 
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

# --- Обработчики добавления еженедельного напоминания ---
def start_add_weekly_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("📆 <b>Еженедельное напоминание</b>\n\nВведите день недели:\nПонедельник, Вторник, Среда, Четверг, Пятница, Суббота, Воскресенье", parse_mode=ParseMode.HTML)
    return WEEKLY_DAY

def receive_weekly_day(update: Update, context: CallbackContext):
    text = update.message.text.strip().lower()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    if text not in days:
        update.message.reply_text("❌ <b>Некорректный день недели</b>\n\nВыберите один из:\nПонедельник, Вторник, Среда, Четверг, Пятница, Суббота, Воскресенье", parse_mode=ParseMode.HTML)
        return WEEKLY_DAY
    context.user_data["weekly_day"] = text
    update.message.reply_text("🕐 <b>Время напоминания</b>\n\nВведите время в формате ЧЧ:ММ:", parse_mode=ParseMode.HTML)
    return WEEKLY_TIME

def receive_weekly_time(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        time.strptime(text, "%H:%M")
        context.user_data["weekly_time"] = text
        update.message.reply_text("✏️ <b>Текст еженедельного напоминания</b>\n\nВведите текст (поддерживаются HTML теги и ссылки):", parse_mode=ParseMode.HTML)
        return WEEKLY_TEXT
    except Exception:
        update.message.reply_text("❌ <b>Некорректный формат</b>\n\nВведите время в формате ЧЧ:ММ:", parse_mode=ParseMode.HTML)
        return WEEKLY_TIME

def receive_weekly_text(update: Update, context: CallbackContext):
    reminders = load_reminders()
    new_id = get_next_reminder_id()
    reminder_text = update.message.text_html if update.message.text_html else update.message.text.strip()
    
    reminders.append({
        "id": new_id,
        "type": "weekly",
        "day": context.user_data["weekly_day"],
        "time": context.user_data["weekly_time"],
        "text": reminder_text
    })
    save_reminders(reminders)
    
    # Планируем напоминание
    schedule_reminder(context.dispatcher.job_queue, reminders[-1])
    
    update.message.reply_text(
        f"✅ <b>Еженедельное напоминание #{new_id} добавлено</b>\n\n"
        f"📅 <i>Каждый {context.user_data['weekly_day'].title()} в {context.user_data['weekly_time']}</i>\n"
        f"💬 {reminder_text}", 
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

# --- Список напоминаний ---
def list_reminders(update: Update, context: CallbackContext):
    reminders = load_reminders()
    if not reminders:
        update.message.reply_text("📭 <b>У вас нет активных напоминаний</b>", parse_mode=ParseMode.HTML)
        return
    
    lines = ["📋 <b>Ваши напоминания:</b>\n"]
    
    # Сортируем по ID для удобства
    reminders.sort(key=lambda x: int(x.get("id", "0")))
    
    for i, r in enumerate(reminders, 1):
        if r["type"] == "once":
            lines.append(f"<b>#{i}</b> [📅 Разово] <i>{r['datetime']}</i>\n💬 {r['text']}\n")
        elif r["type"] == "daily":
            lines.append(f"<b>#{i}</b> [🔄 Ежедневно] <i>{r['time']}</i>\n💬 {r['text']}\n")
        elif r["type"] == "weekly":
            lines.append(f"<b>#{i}</b> [📆 Еженедельно] <i>{r['day'].title()} {r['time']}</i>\n💬 {r['text']}\n")
    
    message_text = "\n".join(lines)
    
    # Telegram имеет лимит на длину сообщения
    if len(message_text) > 4000:
        # Разбиваем на части
        chunks = []
        current_chunk = "📋 <b>Ваши напоминания:</b>\n\n"
        
        for line in lines[1:]:  # Пропускаем заголовок
            if len(current_chunk + line) > 4000:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += line
        
        if current_chunk:
            chunks.append(current_chunk)
        
        for chunk in chunks:
            update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    else:
        update.message.reply_text(message_text, parse_mode=ParseMode.HTML)

# --- Удаление напоминания ---
def start_delete_reminder(update: Update, context: CallbackContext):
    reminders = load_reminders()
    if not reminders:
        update.message.reply_text("📭 <b>У вас нет напоминаний для удаления</b>", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    
    lines = ["🗑 <b>Выберите напоминание для удаления:</b>\nВведите номер:\n"]
    
    # Сортируем по ID для удобства
    reminders.sort(key=lambda x: int(x.get("id", "0")))
    
    for i, r in enumerate(reminders, 1):
        if r["type"] == "once":
            lines.append(f"<b>{i}.</b> [📅 Разово] <i>{r['datetime']}</i>\n💬 {r['text'][:50]}{'...' if len(r['text']) > 50 else ''}")
        elif r["type"] == "daily":
            lines.append(f"<b>{i}.</b> [🔄 Ежедневно] <i>{r['time']}</i>\n💬 {r['text'][:50]}{'...' if len(r['text']) > 50 else ''}")
        elif r["type"] == "weekly":
            lines.append(f"<b>{i}.</b> [📆 Еженедельно] <i>{r['day'].title()} {r['time']}</i>\n💬 {r['text'][:50]}{'...' if len(r['text']) > 50 else ''}")
    
    update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)
    return REM_DEL_ID

def confirm_delete_reminder(update: Update, context: CallbackContext):
    try:
        reminder_number = int(update.message.text.strip())
        reminders = load_reminders()
        
        if reminder_number < 1 or reminder_number > len(reminders):
            update.message.reply_text("❌ <b>Неверный номер</b>\n\nВведите номер от 1 до " + str(len(reminders)), parse_mode=ParseMode.HTML)
            return REM_DEL_ID
        
        # Сортируем по ID для удобства
        reminders.sort(key=lambda x: int(x.get("id", "0")))
        reminder_to_delete = reminders[reminder_number - 1]
        
        # Удаляем напоминание
        all_reminders = load_reminders()
        new_list = [r for r in all_reminders if r["id"] != reminder_to_delete["id"]]
        save_reminders(new_list)
        
        update.message.reply_text(f"✅ <b>Напоминание #{reminder_number} удалено</b>", parse_mode=ParseMode.HTML)
        
        # Перепланируем все напоминания
        reschedule_all_reminders(context.dispatcher.job_queue)
        
    except ValueError:
        update.message.reply_text("❌ <b>Введите номер напоминания</b>", parse_mode=ParseMode.HTML)
        return REM_DEL_ID
    
    return ConversationHandler.END

# --- Очистка всех напоминаний ---
def clear_reminders(update: Update, context: CallbackContext):
    save_reminders([])
    
    # Останавливаем все задания
    job_queue = context.dispatcher.job_queue
    current_jobs = job_queue.jobs()
    for job in current_jobs:
        if hasattr(job.context, 'get') and isinstance(job.context, dict):
            job.schedule_removal()
    
    update.message.reply_text("🗑 <b>Все напоминания удалены</b>", parse_mode=ParseMode.HTML)

# --- Следующее напоминание ---
def next_notification(update: Update, context: CallbackContext):
    reminders = load_reminders()
    if not reminders:
        update.message.reply_text("📭 <b>Нет запланированных напоминаний</b>", parse_mode=ParseMode.HTML)
        return
    
    now = datetime.datetime.now()
    soonest = None
    soonest_time = None
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    
    for r in reminders:
        t = None
        if r["type"] == "once":
            try:
                t = datetime.datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M")
                if t < now:  # Пропускаем прошедшие разовые напоминания
                    continue
            except ValueError:
                continue
        elif r["type"] == "daily":
            try:
                h, m = map(int, r["time"].split(":"))
                candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if candidate < now:
                    candidate += datetime.timedelta(days=1)
                t = candidate
            except ValueError:
                continue
        elif r["type"] == "weekly":
            try:
                weekday = days.index(r["day"])
                h, m = map(int, r["time"].split(":"))
                candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
                days_ahead = (weekday - now.weekday() + 7) % 7
                if days_ahead == 0 and candidate < now:
                    days_ahead = 7
                t = candidate + datetime.timedelta(days=days_ahead)
            except (ValueError, IndexError):
                continue
        
        if t and (soonest_time is None or t < soonest_time):
            soonest_time = t
            soonest = r
    
    if soonest is None:
        update.message.reply_text("📭 <b>Нет запланированных напоминаний</b>", parse_mode=ParseMode.HTML)
        return
    
    time_diff = soonest_time - now
    if time_diff.days > 0:
        time_str = f"через {time_diff.days} дн."
    elif time_diff.seconds > 3600:
        hours = time_diff.seconds // 3600
        time_str = f"через {hours} ч."
    elif time_diff.seconds > 60:
        minutes = time_diff.seconds // 60
        time_str = f"через {minutes} мин."
    else:
        time_str = "менее чем через минуту"
    
    if soonest["type"] == "once":
        msg = f"📅 <b>Ближайшее напоминание</b>\n\n<i>Разово: {soonest['datetime']}</i>\n⏰ {time_str}\n💬 {soonest['text']}"
    elif soonest["type"] == "daily":
        msg = f"🔄 <b>Ближайшее напоминание</b>\n\n<i>Ежедневно: {soonest['time']}</i>\n⏰ {time_str}\n💬 {soonest['text']}"
    elif soonest["type"] == "weekly":
        msg = f"📆 <b>Ближайшее напоминание</b>\n\n<i>Еженедельно: {soonest['day'].title()} {soonest['time']}</i>\n⏰ {time_str}\n💬 {soonest['text']}"
    
    update.message.reply_text(msg, parse_mode=ParseMode.HTML)

def cancel_reminder(update: Update, context: CallbackContext):
    """
    Отмена создания напоминания.
    """
    update.message.reply_text("❌ <b>Операция отменена</b>", parse_mode=ParseMode.HTML)
    return ConversationHandler.END

# --- Scheduling helpers ---
from datetime import datetime, time as dt_time, timedelta

def send_reminder(context: CallbackContext):
    """
    Отправляет текст напоминания всем подписанным чатам.
    """
    reminder = context.job.context
    try:
        with open("subscribed_chats.json", "r") as f:
            chats = json.load(f)
        
        reminder_text = f"🔔 <b>НАПОМИНАНИЕ</b>\n\n{reminder['text']}"
        
        for cid in chats:
            try:
                context.bot.send_message(chat_id=cid, text=reminder_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Failed to send reminder to chat {cid}: {e}")
        
        # Удаляем разовые напоминания после отправки
        if reminder["type"] == "once":
            reminders = load_reminders()
            reminders = [r for r in reminders if r["id"] != reminder["id"]]
            save_reminders(reminders)
            
    except Exception as e:
        logger.error(f"Error in send_reminder: {e}")

def schedule_reminder(job_queue, reminder):
    """
    Добавляет задание в JobQueue для данного напоминания.
    """
    try:
        # Сначала удаляем существующее задание с таким же ID, если есть
        current_jobs = job_queue.jobs()
        for job in current_jobs:
            if hasattr(job, 'name') and job.name == f"reminder_{reminder['id']}":
                job.schedule_removal()
        
        if reminder["type"] == "once":
            run_dt = datetime.strptime(reminder["datetime"], "%Y-%m-%d %H:%M")
            if run_dt > datetime.now():  # Планируем только будущие напоминания
                job_queue.run_once(send_reminder, run_dt, context=reminder, name=f"reminder_{reminder['id']}")
        elif reminder["type"] == "daily":
            h, m = map(int, reminder["time"].split(":"))
            job_queue.run_daily(send_reminder, dt_time(hour=h, minute=m), context=reminder, name=f"reminder_{reminder['id']}")
        elif reminder["type"] == "weekly":
            days_map = {
                "понедельник": 0, "вторник": 1, "среда": 2,
                "четверг": 3, "пятница": 4, "суббота": 5, "воскресенье": 6
            }
            weekday = days_map[reminder["day"].lower()]
            h, m = map(int, reminder["time"].split(":"))
            job_queue.run_daily(
                send_reminder,
                dt_time(hour=h, minute=m),
                context=reminder,
                days=(weekday,),
                name=f"reminder_{reminder['id']}"
            )
    except Exception as e:
        logger.error(f"Error scheduling reminder {reminder.get('id', 'unknown')}: {e}")

def schedule_all_reminders(job_queue):
    """
    Загружает все напоминания и запланировывает их.
    """
    reminders = load_reminders()
    for reminder in reminders:
        schedule_reminder(job_queue, reminder)

def reschedule_all_reminders(job_queue):
    """
    Перепланирует все напоминания (используется после удаления)
    """
    # Останавливаем все текущие задания
    current_jobs = job_queue.jobs()
    for job in current_jobs:
        if hasattr(job, 'name') and job.name and job.name.startswith('reminder_'):
            job.schedule_removal()
    
    # Планируем заново
    schedule_all_reminders(job_queue)

def main():
    token = os.environ['BOT_TOKEN']
    port = int(os.environ.get('PORT', 8000))
    updater = Updater(token=token, use_context=True)
    
    # Reset any existing webhook so polling can start cleanly
    try:
        res = updater.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted: %s", res)
    except Exception as e:
        logger.error("Error deleting webhook: %s", e)
    
    dp = updater.dispatcher
    
    # Добавляем обработчики команд ПЕРВЫМИ
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

    # Добавляем обработчик ошибок
    dp.add_error_handler(error_handler)

    # Запланировать все сохранённые напоминания
    schedule_all_reminders(updater.job_queue)
    
    # Добавляем ping каждые 10 минут для предотвращения засыпания на Render
    updater.job_queue.run_repeating(ping_self, interval=600, first=60)

    # Health check server for Render free tier
    threading.Thread(target=start_health_server, daemon=True).start()
    
    # Always run in polling mode
    updater.bot.delete_webhook(drop_pending_updates=True)
    updater.start_polling(drop_pending_updates=True)
    logger.info("Polling mode started, bot is ready")
    updater.idle()

if __name__ == "__main__":
    main()

