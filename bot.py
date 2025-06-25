# bot.py

import os
import logging
import threading
import time
import datetime
import json
from uuid import uuid4
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytz
import requests
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, Job
from telegram.error import Conflict

# — Настройка логирования —
logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# — Переменные окружения —
BOT_TOKEN       = os.environ['BOT_TOKEN']
PORT            = int(os.environ.get("PORT", "8000"))
BASE_URL        = os.environ.get("BASE_URL")
CHATS_FILE      = 'chats.json'
REMINDERS_FILE  = 'reminders.json'

# — Московская зона (для времени напоминаний) —
MSK = pytz.timezone("Europe/Moscow")

# — HTTP-healthcheck для Render —
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

# — Запуск HTTP-сервера —
def run_http_server():
    HTTPServer(('0.0.0.0', PORT), HealthHandler).serve_forever()

# — Keep-alive, чтобы контейнер не спал —
def keep_alive():
    if not BASE_URL:
        logger.warning("BASE_URL не задан, keep-alive отключён")
        return
    while True:
        try:
            requests.get(BASE_URL, timeout=5)
            logger.info("Keep-alive pinged %s", BASE_URL)
        except Exception as e:
            logger.warning("Keep-alive failed: %s", e)
        time.sleep(300)

# — Работа с файлами (чаты и напоминания) —
def load_chats():
    try:
        with open(CHATS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return []

def save_chats(chats):
    with open(CHATS_FILE, 'w') as f:
        json.dump(chats, f)


def load_reminders():
    try:
        with open(REMINDERS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return []

def save_reminders(reminders):
    with open(REMINDERS_FILE, 'w') as f:
        json.dump(reminders, f)

# — Callback для отправки напоминаний —
def reminder_callback(context: CallbackContext):
    job: Job = context.job
    data = job.context
    chat_id = data['chat_id']
    text = data['text']
    context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

    # Если одноразовое, удаляем его
    if data.get('type') == 'once':
        reminders = load_reminders()
        reminders = [r for r in reminders if r['id'] != data['id']]
        save_reminders(reminders)

# — Планирование всех напоминаний при старте —
def schedule_all_reminders(job_queue):
    reminders = load_reminders()
    now = datetime.datetime.now(MSK)
    for rem in reminders:
        data = rem.copy()
        rem_type = rem.get('type')

        if rem_type == 'once':
            send_dt = datetime.datetime.fromisoformat(rem['send_time']).astimezone(MSK)
            if send_dt > now:
                delay = (send_dt - now).total_seconds()
                job_queue.run_once(reminder_callback, delay, context=data)

        elif rem_type == 'daily':
            hh, mm = map(int, rem['time'].split(':'))
            job_queue.run_daily(reminder_callback,
                                time=datetime.time(hh, mm),
                                context=data)

        elif rem_type == 'weekly':
            hh, mm = map(int, rem['time'].split(':'))
            days = rem.get('days', [])  # 0=Monday ... 6=Sunday
            job_queue.run_daily(reminder_callback,
                                time=datetime.time(hh, mm),
                                days=tuple(days),
                                context=data)

# — Уведомляем все чаты (боевые уведомления остаются прежними) —
def broadcast(text: str, context: CallbackContext):
    chats = load_chats()
    for chat_id in chats:
        try:
            context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("Не удалось отправить в %s: %s", chat_id, e)

# — Обработчик ошибок —
def error_handler(update: Update, context: CallbackContext):
    if isinstance(context.error, Conflict):
        return
    logger.error("Необработанная ошибка", exc_info=context.error)

# — Команды бота —
def start(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    chats = load_chats()
    if chat_id not in chats:
        chats.append(chat_id)
        save_chats(chats)
    update.message.reply_text("✅ Бот активирован в этом чате.")


def test(update: Update, context: CallbackContext):
    update.message.reply_text("✅ Тестовое напоминание!")

# — Одноразовое напоминание —
def add_one_reminder(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    args = context.args
    if len(args) < 3:
        update.message.reply_text("Использование: /remind YYYY-MM-DD HH:MM текст")
        return
    date_str, time_str = args[0], args[1]
    text = ' '.join(args[2:])
    try:
        dt = MSK.localize(datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
    except ValueError:
        update.message.reply_text("Неверный формат даты/времени")
        return
    if dt <= datetime.datetime.now(MSK):
        update.message.reply_text("Время должно быть в будущем.")
        return
    rem_id = str(uuid4())
    rem = {'id': rem_id, 'type': 'once', 'chat_id': chat_id, 'text': text, 'send_time': dt.isoformat()}
    reminders = load_reminders() + [rem]
    save_reminders(reminders)
    delay = (dt - datetime.datetime.now(MSK)).total_seconds()
    context.job_queue.run_once(reminder_callback, delay, context=rem)
    update.message.reply_text(f"✅ Одноразовое напоминание {rem_id} установлено на {dt.strftime('%Y-%m-%d %H:%M')}")

# — Ежедневное напоминание —
def add_daily_reminder(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    args = context.args
    if len(args) < 2:
        update.message.reply_text("Использование: /remind_daily HH:MM текст")
        return
    time_str = args[0]
    text = ' '.join(args[1:])
    try:
        hh, mm = map(int, time_str.split(':'))
    except Exception:
        update.message.reply_text("Неверный формат времени")
        return
    rem_id = str(uuid4())
    rem = {'id': rem_id, 'type': 'daily', 'chat_id': chat_id, 'text': text, 'time': time_str}
    reminders = load_reminders() + [rem]
    save_reminders(reminders)
    context.job_queue.run_daily(reminder_callback,
                                time=datetime.time(hh, mm),
                                context=rem)
    update.message.reply_text(f"✅ Ежедневное напоминание {rem_id} на {time_str}")

# — Еженедельное напоминание —
def add_weekly_reminder(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    args = context.args
    if len(args) < 3:
        update.message.reply_text("Использование: /remind_weekly DDD HH:MM текст (DDD=Mon,Tue,...)")
        return
    day_str, time_str = args[0], args[1]
    text = ' '.join(args[2:])
    days_map = {'Mon':0,'Tue':1,'Wed':2,'Thu':3,'Fri':4,'Sat':5,'Sun':6}
    if day_str not in days_map:
        update.message.reply_text("День указать как Mon, Tue, Wed, Thu, Fri, Sat или Sun")
        return
    try:
        hh, mm = map(int, time_str.split(':'))
    except Exception:
        update.message.reply_text("Неверный формат времени")
        return
    rem_id = str(uuid4())
    rem = {
        'id': rem_id,
        'type': 'weekly',
        'chat_id': chat_id,
        'text': text,
        'time': time_str,
        'days': [days_map[day_str]]
    }
    reminders = load_reminders() + [rem]
    save_reminders(reminders)
    context.job_queue.run_daily(reminder_callback,
                                time=datetime.time(hh, mm),
                                days=(days_map[day_str],),
                                context=rem)
    update.message.reply_text(f"✅ Еженедельное напоминание {rem_id} каждый {day_str} в {time_str}")

# — Список активных напоминаний —
def list_reminders(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    reminders = [r for r in load_reminders() if r['chat_id'] == chat_id]
    if not reminders:
        update.message.reply_text("У вас нет активных напоминаний.")
        return
    lines = ["📋 <b>Ваши напоминания:</b>"]
    for r in reminders:
        line = f"ID: {r['id']} | {r['type']}"
        if r['type'] == 'once': line += f" @ {r['send_time']}"
        else: line += f" @ {r['time']}"
        if r['type'] == 'weekly': line += f" on {r['days']}"
        line += f" → {r['text']}"
        lines.append(line)
    update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# — Удаление напоминания по ID —
def del_reminder(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        update.message.reply_text("Использование: /del_reminder ID")
        return
    rem_id = args[0]
    reminders = load_reminders()
    new = [r for r in reminders if r['id'] != rem_id]
    if len(new) == len(reminders):
        update.message.reply_text("Напоминание не найдено.")
        return
    save_reminders(new)
    # отменяем задачи
    for job in context.job_queue.get_jobs():
        if hasattr(job, 'context') and job.context.get('id') == rem_id:
            job.schedule_removal()
    update.message.reply_text(f"✅ Напоминание {rem_id} удалено.")

# — Точка входа —
def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    updater = Updater(token=BOT_TOKEN, use_context=True)
    updater.bot.delete_webhook()
    dp = updater.dispatcher
    dp.add_error_handler(error_handler)

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("test", test))
    dp.add_handler(CommandHandler("remind", add_one_reminder))
    dp.add_handler(CommandHandler("remind_daily", add_daily_reminder))
    dp.add_handler(CommandHandler("remind_weekly", add_weekly_reminder))
    dp.add_handler(CommandHandler("list_reminders", list_reminders))
    dp.add_handler(CommandHandler("del_reminder", del_reminder))

    # Запланировать все сохранённые напоминания
    schedule_all_reminders(updater.job_queue)

    updater.start_polling(drop_pending_updates=True)
    logger.info("Polling начат, бот готов к работе")
    updater.idle()

if __name__ == "__main__":
    main()

