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
from telegram.ext import Updater, CommandHandler, CallbackContext, Job, ConversationHandler, MessageHandler, Filters
import html
REMINDER_DATE, REMINDER_TEXT = range(2)
DAILY_TIME, DAILY_TEXT = range(2, 4)
WEEKLY_DAY, WEEKLY_TIME, WEEKLY_TEXT = range(4, 7)

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
    text = data.get('text')
    # Broadcast to all connected chats instead of single chat
    broadcast(text, context)

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
                                context=data,
                                timezone=MSK)

        elif rem_type == 'weekly':
            hh, mm = map(int, rem['time'].split(':'))
            days = rem.get('days', [])  # 0=Monday ... 6=Sunday
            job_queue.run_daily(reminder_callback,
                                time=datetime.time(hh, mm),
                                days=tuple(days),
                                context=data,
                                timezone=MSK)



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

def start_add_one_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("Введите дату и время напоминания (ДД.MM.ГГГГ ЧЧ:ММ) или /cancel для отмены")
    return REMINDER_DATE

def receive_reminder_datetime(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        dt = MSK.localize(datetime.datetime.strptime(text, "%d.%m.%Y %H:%M"))
        if dt <= datetime.datetime.now(MSK):
            update.message.reply_text("Время должно быть в будущем. Повторите ввод или /cancel")
            return REMINDER_DATE
        context.user_data['reminder_dt'] = dt
        update.message.reply_text("Введите текст напоминания или /cancel для отмены")
        return REMINDER_TEXT
    except ValueError:
        update.message.reply_text("Неверный формат. Используйте формат ДД.MM.ГГГГ ЧЧ:ММ или /cancel")
        return REMINDER_DATE

def receive_reminder_text(update: Update, context: CallbackContext):
    # Preserve Telegram-formatted links by rebuilding text with HTML tags
    msg = update.message
    raw = msg.text or ""
    entities = msg.entities or []
    html_text = ""
    last = 0
    for ent in entities:
        if ent.type in ("text_link", "url"):
            # append escaped text before the entity
            html_text += html.escape(raw[last:ent.offset])
            label = raw[ent.offset:ent.offset + ent.length]
            url = ent.url if ent.type == "text_link" else label
            html_text += f'<a href="{html.escape(url)}">{html.escape(label)}</a>'
            last = ent.offset + ent.length
    # append any remaining text
    html_text += html.escape(raw[last:])
    text = html_text
    dt = context.user_data.pop('reminder_dt')
    reminders = load_reminders()
    # assign short incremental ID
    max_id = max((int(r['id']) for r in reminders), default=0)
    new_id = str(max_id + 1)
    rem = {'id': new_id, 'type': 'once', 'chat_id': update.effective_chat.id,
           'text': text, 'send_time': dt.isoformat(), 'source': 'user'}
    reminders.append(rem)
    save_reminders(reminders)
    delay = (dt - datetime.datetime.now(MSK)).total_seconds()
    context.job_queue.run_once(reminder_callback, delay, context=rem)
    update.message.reply_text(f"✅ Напоминание {new_id} установлено на {dt.strftime('%d.%m.%Y %H:%M')}", parse_mode=ParseMode.HTML)
    return ConversationHandler.END

# --- DAILY REMINDER CONVERSATION ---
def start_add_daily_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("Введите время ежедневного напоминания (ЧЧ:ММ) или /cancel для отмены")
    return DAILY_TIME

def receive_daily_time(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        hh, mm = map(int, text.split(':'))
    except Exception:
        update.message.reply_text("Неверный формат. Используйте ЧЧ:ММ или /cancel")
        return DAILY_TIME
    context.user_data['daily_time'] = text
    update.message.reply_text("Введите текст ежедневного напоминания или /cancel для отмены")
    return DAILY_TEXT

def receive_daily_text(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    # build HTML like in receive_reminder_text
    msg = update.message
    raw = msg.text or ""
    entities = msg.entities or []
    html_text = ""
    last = 0
    for ent in entities:
        if ent.type in ("text_link", "url"):
            html_text += html.escape(raw[last:ent.offset])
            label = raw[ent.offset:ent.offset + ent.length]
            url = ent.url if ent.type == "text_link" else label
            html_text += f'<a href="{html.escape(url)}">{html.escape(label)}</a>'
            last = ent.offset + ent.length
    html_text += html.escape(raw[last:])
    time_str = context.user_data.pop('daily_time')
    reminders = load_reminders()
    max_id = max((int(r['id']) for r in reminders), default=0)
    new_id = str(max_id + 1)
    rem = {'id': new_id, 'type': 'daily', 'time': time_str, 'text': html_text, 'source': 'user'}
    reminders.append(rem)
    save_reminders(reminders)
    # subscribe chat if needed
    chats = load_chats()
    if chat_id not in chats:
        chats.append(chat_id)
        save_chats(chats)
    hh, mm = map(int, time_str.split(':'))
    context.job_queue.run_daily(reminder_callback,
                                time=datetime.time(hh, mm),
                                context=rem,
                                timezone=MSK)
    context.bot.send_message(chat_id=user_id,
                             text=f"✅ Ежедневное напоминание {new_id} установлено на {time_str}",
                             parse_mode=ParseMode.HTML)
    return ConversationHandler.END

# --- WEEKLY REMINDER CONVERSATION ---
def start_add_weekly_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("Введите день недели для напоминания (Пн,Вт,Ср,Чт,Пт,Сб,Вс) или /cancel")
    return WEEKLY_DAY

def receive_weekly_day(update: Update, context: CallbackContext):
    day_str = update.message.text.strip()
    days_map = {'Пн':0,'Вт':1,'Ср':2,'Чт':3,'Пт':4,'Сб':5,'Вс':6}
    if day_str not in days_map:
        update.message.reply_text("День указать как Пн, Вт, Ср, Чт, Пт, Сб или Вс")
        return WEEKLY_DAY
    context.user_data['weekly_day'] = days_map[day_str]
    update.message.reply_text("Введите время еженедельного напоминания (ЧЧ:ММ) или /cancel для отмены")
    return WEEKLY_TIME

def receive_weekly_time(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        hh, mm = map(int, text.split(':'))
    except Exception:
        update.message.reply_text("Неверный формат. Используйте ЧЧ:ММ или /cancel")
        return WEEKLY_TIME
    context.user_data['weekly_time'] = text
    update.message.reply_text("Введите текст еженедельного напоминания или /cancel для отмены")
    return WEEKLY_TEXT

def receive_weekly_text(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    msg = update.message
    raw = msg.text or ""
    entities = msg.entities or []
    html_text = ""
    last = 0
    for ent in entities:
        if ent.type in ("text_link", "url"):
            html_text += html.escape(raw[last:ent.offset])
            label = raw[ent.offset:ent.offset + ent.length]
            url = ent.url if ent.type == "text_link" else label
            html_text += f'<a href="{html.escape(url)}">{html.escape(label)}</a>'
            last = ent.offset + ent.length
    html_text += html.escape(raw[last:])
    day_num = context.user_data.pop('weekly_day')
    time_str = context.user_data.pop('weekly_time')
    reminders = load_reminders()
    max_id = max((int(r['id']) for r in reminders), default=0)
    new_id = str(max_id + 1)
    rem = {'id': new_id, 'type': 'weekly', 'days':[day_num], 'time': time_str, 'text': html_text, 'source': 'user'}
    reminders.append(rem)
    save_reminders(reminders)
    chats = load_chats()
    if chat_id not in chats:
        chats.append(chat_id)
        save_chats(chats)
    hh, mm = map(int, time_str.split(':'))
    context.job_queue.run_daily(reminder_callback,
                                time=datetime.time(hh, mm),
                                days=(day_num,),
                                context=rem,
                                timezone=MSK)
    days_map = {'Пн':0,'Вт':1,'Ср':2,'Чт':3,'Пт':4,'Сб':5,'Вс':6}
    ru_days = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']
    update.message.reply_text(f"✅ Еженедельное напоминание {new_id} установлено каждый {ru_days[day_num]} в {time_str}")
    return ConversationHandler.END

def cancel_reminder(update: Update, context: CallbackContext):
    update.message.reply_text("Операция отменена.", parse_mode=ParseMode.HTML)
    return ConversationHandler.END

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

# # — Одноразовое напоминание —
# def add_one_reminder(update: Update, context: CallbackContext):
#     chat_id = update.effective_chat.id
#     args = context.args
#     if len(args) < 3:
#         update.message.reply_text("Использование: /remind YYYY-MM-DD HH:MM текст")
#         return
#     date_str, time_str = args[0], args[1]
#     text = ' '.join(args[2:])
#     try:
#         dt = MSK.localize(datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
#     except ValueError:
#         update.message.reply_text("Неверный формат даты/времени")
#         return
#     if dt <= datetime.datetime.now(MSK):
#         update.message.reply_text("Время должно быть в будущем.")
#         return
#     rem_id = str(uuid4())
#     rem = {'id': rem_id, 'type': 'once', 'chat_id': chat_id, 'text': text, 'send_time': dt.isoformat()}
#     reminders = load_reminders() + [rem]
#     save_reminders(reminders)
#     delay = (dt - datetime.datetime.now(MSK)).total_seconds()
#     context.job_queue.run_once(reminder_callback, delay, context=rem)
#     update.message.reply_text(f"✅ Одноразовое напоминание {rem_id} установлено на {dt.strftime('%Y-%m-%d %H:%M')}")

# — Ежедневное напоминание —

# — Список активных напоминаний —
def list_reminders(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    reminders = load_reminders()
    user_id = update.effective_user.id
    if not reminders:
        context.bot.send_message(chat_id=user_id, text="У вас нет активных напоминаний.")
        return
    lines = ["📋 <b>Все активные напоминания:</b>"]
    ru_types = {'once': 'одноразовое', 'daily': 'ежедневное', 'weekly': 'еженедельное'}
    ru_days = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']
    for r in reminders:
        typ = r.get('type', '')
        typ_ru = ru_types.get(typ, typ)
        if typ == 'once':
            dt = datetime.datetime.fromisoformat(r['send_time']).astimezone(MSK)
            time_str = dt.strftime("%d.%m.%Y %H:%M")
            line = f"ID: {r['id']} | {typ_ru} @ {time_str} → {r['text']}"
        elif typ == 'daily':
            line = f"ID: {r['id']} | {typ_ru} @ {r['time']} → {r['text']}"
        elif typ == 'weekly':
            day_num = r.get('days', [None])[0]
            day_ru = ru_days[day_num] if day_num is not None and 0 <= day_num < 7 else "?"
            line = f"ID: {r['id']} | {typ_ru} {day_ru} @ {r['time']} → {r['text']}"
        else:
            line = f"ID: {r['id']} | {typ_ru} → {r['text']}"
        lines.append(line)
    context.bot.send_message(chat_id=user_id, text="\n".join(lines), parse_mode=ParseMode.HTML)

# — Ближайшее уведомление из SCHEDULE —
def next_notification(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    now = datetime.datetime.now(MSK)
    best, best_dt = None, None
    for r in load_reminders():
        typ = r['type']
        if typ == 'once':
            dt = datetime.datetime.fromisoformat(r['send_time']).astimezone(MSK)
            next_dt = dt
        elif typ == 'daily':
            hh, mm = map(int, r['time'].split(':'))
            dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            next_dt = dt if dt > now else dt + datetime.timedelta(days=1)
        elif typ == 'weekly':
            hh, mm = map(int, r['time'].split(':'))
            day = r['days'][0]
            dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            days_ahead = (day - dt.weekday() + 7) % 7
            if days_ahead == 0 and dt <= now:
                days_ahead = 7
            next_dt = dt + datetime.timedelta(days=days_ahead)
        else:
            continue
        if best_dt is None or next_dt < best_dt:
            best, best_dt = r, next_dt
    if not best:
        context.bot.send_message(chat_id=user_id, text="Нет активных напоминаний.", parse_mode=ParseMode.HTML)
        return
    send_str = best_dt.strftime("%d.%m.%Y %H:%M")
    context.bot.send_message(
        chat_id=user_id,
        text=f"📅 Ближайшее уведомление в {send_str}:\n{best['text']}",
        parse_mode=ParseMode.HTML
    )

# — Удаление напоминания по ID —
def del_reminder(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        context.bot.send_message(chat_id=user_id, text="Использование: /del_reminder ID")
        return
    rem_id = args[0]
    reminders = load_reminders()
    new = [r for r in reminders if r['id'] != rem_id]
    if len(new) == len(reminders):
        context.bot.send_message(chat_id=user_id, text="Напоминание не найдено.")
        return
    save_reminders(new)
    # отменяем задачи
    for job in context.job_queue.get_jobs():
        if hasattr(job, 'context') and job.context.get('id') == rem_id:
            job.schedule_removal()
    context.bot.send_message(chat_id=user_id, text=f"✅ Напоминание {rem_id} удалено.")



# — Команды для управления напоминаниями и статичными уведомлениями —
def clear_reminders(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    reminders = load_reminders()
    # Remove all reminders (since only dynamic now)
    new = []
    save_reminders(new)
    # cancel all user jobs
    for job in context.job_queue.get_jobs():
        if getattr(job.context, 'get', lambda k: None)('source') == 'user':
            job.schedule_removal()
    context.bot.send_message(chat_id=user_id, text="✅ Все напоминания пользователя удалены.")


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
    conv = ConversationHandler(
        entry_points=[CommandHandler("remind", start_add_one_reminder)],
        states={
            REMINDER_DATE: [MessageHandler(Filters.text & ~Filters.command, receive_reminder_datetime)],
            REMINDER_TEXT: [MessageHandler(Filters.text & ~Filters.command, receive_reminder_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel_reminder)],
    )
    dp.add_handler(conv)
    # dp.add_handler(CommandHandler("remind_daily", add_daily_reminder))
    # dp.add_handler(CommandHandler("remind_weekly", add_weekly_reminder))
    conv_daily = ConversationHandler(
        entry_points=[CommandHandler("remind_daily", start_add_daily_reminder)],
        states={
            DAILY_TIME: [MessageHandler(Filters.text & ~Filters.command, receive_daily_time)],
            DAILY_TEXT: [MessageHandler(Filters.text & ~Filters.command, receive_daily_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel_reminder)],
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
    )
    dp.add_handler(conv_weekly)
    dp.add_handler(CommandHandler("list_reminders", list_reminders))
    dp.add_handler(CommandHandler("del_reminder", del_reminder))
    dp.add_handler(CommandHandler("clear_reminders", clear_reminders))
    dp.add_handler(CommandHandler("next", next_notification))

    # Запланировать все сохранённые напоминания
    schedule_all_reminders(updater.job_queue)
    # schedule_notifications(updater.job_queue)  # Удалено

    updater.start_polling(drop_pending_updates=True)
    logger.info("Polling начат, бот готов к работе")
    updater.idle()

if __name__ == "__main__":
    main()
