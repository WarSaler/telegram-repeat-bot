# bot.py

import os
import logging
import threading
import time
from datetime import datetime, time as dt_time, timedelta
import json
import pytz
import requests
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, Job, ConversationHandler, MessageHandler, Filters
from telegram.error import Conflict, BadRequest
import html
from http.server import BaseHTTPRequestHandler, HTTPServer

# ✅ ИМПОРТ GOOGLE SHEETS ИНТЕГРАЦИИ
try:
    from sheets_integration import SheetsManager
    sheets_manager = SheetsManager()
    SHEETS_AVAILABLE = True
    logger_temp = logging.getLogger(__name__)
    logger_temp.info("✅ Google Sheets integration loaded successfully")
except Exception as e:
    sheets_manager = None
    SHEETS_AVAILABLE = False
    logger_temp = logging.getLogger(__name__)
    logger_temp.warning(f"📵 Google Sheets integration not available: {e}")

# Константа для московского времени
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Время запуска бота
BOT_START_TIME = None

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

def get_moscow_time():
    """Получить текущее московское время"""
    return datetime.now(MOSCOW_TZ)

def moscow_time_to_utc(moscow_dt):
    """Конвертировать московское время в UTC"""
    if isinstance(moscow_dt, str):
        # Если строка, парсим ее как московское время
        naive_dt = datetime.strptime(moscow_dt, "%Y-%m-%d %H:%M")
        moscow_dt = MOSCOW_TZ.localize(naive_dt)
    elif moscow_dt.tzinfo is None:
        # Если naive datetime, считаем его московским
        moscow_dt = MOSCOW_TZ.localize(moscow_dt)
    
    return moscow_dt.astimezone(pytz.UTC)

def utc_to_moscow_time(utc_dt):
    """Конвертировать UTC время в московское"""
    if utc_dt.tzinfo is None:
        utc_dt = pytz.UTC.localize(utc_dt)
    return utc_dt.astimezone(MOSCOW_TZ)

def format_moscow_time(dt):
    """Форматировать время для отображения пользователю"""
    if isinstance(dt, str):
        return dt
    moscow_dt = utc_to_moscow_time(dt) if dt.tzinfo else MOSCOW_TZ.localize(dt)
    return moscow_dt.strftime("%Y-%m-%d %H:%M MSK")

def error_handler(update: Update, context: CallbackContext):
    """
    Handle errors by logging them without crashing the bot.
    """
    if isinstance(context.error, Conflict):
        logger.warning("⚠️ Conflict error: Multiple bot instances detected")
        logger.warning("   This usually means:")
        logger.warning("   1. Another bot instance is running")
        logger.warning("   2. Previous deployment is still active")
        logger.warning("   3. Development and production bots conflict")
        logger.warning("   Continuing to run, conflicts should resolve automatically...")
        return
    elif isinstance(context.error, BadRequest):
        logger.warning(f"⚠️ Bad request: {context.error}")
        return

    logger.error("❌ Uncaught exception:", exc_info=context.error)

def subscribe_chat(chat_id, chat_name="Unknown", chat_type="private", members_count=None):
    try:
        with open("subscribed_chats.json", "r") as f:
            data = f.read().strip()
            chats = json.loads(data) if data else []
    except (FileNotFoundError, json.JSONDecodeError):
        chats = []

    # Проверяем, является ли чат новым
    is_new_chat = chat_id not in chats
    
    if is_new_chat:
        chats.append(chat_id)
        save_chats(chats)
        logger.info(f"🆕 New chat subscribed: {chat_id} ({chat_name})")
        
        # ✅ МГНОВЕННАЯ ЗАПИСЬ В GOOGLE SHEETS
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
            try:
                # Обновляем статистику чата
                sheets_manager.update_chat_stats(chat_id, chat_name, chat_type, members_count)
                
                # Логируем действие подписки
                moscow_time = get_moscow_time().strftime("%Y-%m-%d %H:%M:%S")
                sheets_manager.log_operation(
                    timestamp=moscow_time,
                    action="CHAT_SUBSCRIBE",
                    user_id="SYSTEM",
                    username="AutoSubscribe",
                    chat_id=chat_id,
                    details=f"New chat subscribed: {chat_name} ({chat_type}), Members: {members_count or 'N/A'}",
                    reminder_id=""
                )
                
                # Обновляем список подписанных чатов в Google Sheets
                sheets_manager.sync_subscribed_chats_to_sheets(chats)
                
                logger.info(f"📊 Successfully synced new chat {chat_id} to Google Sheets")
                
            except Exception as e:
                logger.error(f"❌ Error syncing new chat to Google Sheets: {e}")
        elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not initialized - chat {chat_id} subscription not synced")
            logger.warning("   Check GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS environment variables")
        else:
            logger.warning("📵 Google Sheets not available for new chat sync")
    else:
        # Если чат уже существует, обновляем его информацию
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
            try:
                sheets_manager.update_chat_stats(chat_id, chat_name, chat_type, members_count)
                logger.info(f"📊 Updated existing chat {chat_id} info in Google Sheets")
            except Exception as e:
                logger.error(f"❌ Error updating chat info in Google Sheets: {e}")
        elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not initialized - chat {chat_id} info not updated")
            logger.warning("   Check GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS environment variables")

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
        response = requests.get(base_url, timeout=5)
        moscow_time = get_moscow_time().strftime("%H:%M MSK")
        logger.info(f"Self-ping successful at {moscow_time}: {response.status_code}")
    except Exception as e:
        logger.warning(f"Self-ping failed: {e}")

def safe_html_escape(text):
    """
    Безопасно экранирует HTML, сохраняя корректные теги
    """
    if not text:
        return ""
    
    # Список разрешенных HTML тегов
    allowed_tags = ['<b>', '</b>', '<i>', '</i>', '<u>', '</u>', '<s>', '</s>', '<code>', '</code>', '<pre>', '</pre>']
    
    # Простая проверка на корректность HTML
    try:
        # Проверяем, что в тексте нет пустых атрибутов в тегах <a>
        if '<a ' in text and 'href=""' in text:
            # Удаляем пустые ссылки
            text = text.replace('<a href="">', '').replace('</a>', '')
        
        # Проверяем корректность других тегов
        if '<' in text and '>' in text:
            # Если есть HTML теги, возвращаем как есть
            return text
        else:
            # Если нет HTML тегов, экранируем
            return html.escape(text)
    except:
        # В случае ошибки возвращаем экранированный текст
        return html.escape(text)

# --- /start и /test команды ---
def start(update: Update, context: CallbackContext):
    """
    Обработчик команды /start.
    """
    try:
    chat_id = update.effective_chat.id
        moscow_time = get_moscow_time().strftime("%H:%M MSK")
        logger.info(f"Received /start from chat {chat_id} at {moscow_time}")
        
        # Получаем информацию о чате
        chat = update.effective_chat
        chat_name = chat.title if chat.title else f"@{chat.username}" if chat.username else str(chat.first_name or "Private")
        chat_type = chat.type
        members_count = None
        try:
            if chat_type in ["group", "supergroup"]:
                members_count = context.bot.get_chat_members_count(chat_id)
        except:
            pass
        
        subscribe_chat(chat_id, chat_name, chat_type, members_count)
    context.bot.send_message(chat_id=chat_id,
                                 text="✅ <b>Бот активирован в этом чате</b>\n⏰ <i>Время работы: московское (MSK)</i>",
                             parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        try:
            context.bot.send_message(chat_id=update.effective_chat.id,
                                   text="✅ Бот активирован в этом чате\n⏰ Время работы: московское (MSK)")
        except:
            pass

def test(update: Update, context: CallbackContext):
    """
    Обработчик команды /test для проверки работы бота.
    """
    try:
    chat_id = update.effective_chat.id
        moscow_time = get_moscow_time().strftime("%H:%M MSK")
        logger.info(f"Received /test from chat {chat_id} at {moscow_time}")
        
        # Получаем информацию о чате
        chat = update.effective_chat
        chat_name = chat.title if chat.title else f"@{chat.username}" if chat.username else str(chat.first_name or "Private")
        chat_type = chat.type
        members_count = None
        try:
            if chat_type in ["group", "supergroup"]:
                members_count = context.bot.get_chat_members_count(chat_id)
        except:
            pass
        
        subscribe_chat(chat_id, chat_name, chat_type, members_count)
        current_time = get_moscow_time().strftime("%Y-%m-%d %H:%M MSK")
    context.bot.send_message(chat_id=chat_id,
                                 text=f"✅ <b>Бот работает корректно!</b>\n⏰ <i>Текущее время: {current_time}</i>",
                             parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error in test command: {e}")
        try:
            current_time = get_moscow_time().strftime("%Y-%m-%d %H:%M MSK")
            context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=f"✅ Бот работает корректно!\n⏰ Текущее время: {current_time}")
        except:
            pass

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
    try:
        with open(REMINDERS_FILE, "w", encoding='utf-8') as f:
            json.dump(reminders, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving reminders: {e}")

def get_next_reminder_id():
    """
    Генерирует следующий ID для напоминания
    """
    try:
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
    except Exception as e:
        logger.error(f"Error generating reminder ID: {e}")
        return "1"

# --- Обработчики добавления разового напоминания ---
def start_add_one_reminder(update: Update, context: CallbackContext):
    try:
        current_time = get_moscow_time().strftime("%Y-%m-%d %H:%M MSK")
        update.message.reply_text(f"📅 <b>Разовое напоминание</b>\n\nВведите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ\nНапример: 2024-07-10 16:30\n\n<i>⏰ Сейчас: {current_time}</i>", parse_mode=ParseMode.HTML)
        return REMINDER_DATE
    except Exception as e:
        logger.error(f"Error in start_add_one_reminder: {e}")
        current_time = get_moscow_time().strftime("%Y-%m-%d %H:%M MSK")
        update.message.reply_text(f"📅 Разовое напоминание\n\nВведите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ\nНапример: 2024-07-10 16:30\n\n⏰ Сейчас: {current_time}")
    return REMINDER_DATE

def receive_reminder_datetime(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        # Парсим введенное время как московское
        moscow_dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
        moscow_dt = MOSCOW_TZ.localize(moscow_dt)
        
        # Проверяем, что время в будущем
        if moscow_dt < get_moscow_time():
            try:
                update.message.reply_text("⚠️ <b>Ошибка:</b> Дата и время уже прошли.\nВведите корректную дату и время в московском времени:", parse_mode=ParseMode.HTML)
            except:
                update.message.reply_text("⚠️ Ошибка: Дата и время уже прошли.\nВведите корректную дату и время в московском времени:")
            return REMINDER_DATE
        
        context.user_data["reminder_datetime"] = text
        context.user_data["reminder_datetime_moscow"] = moscow_dt
        try:
            update.message.reply_text("✏️ <b>Текст напоминания</b>\n\nВведите текст напоминания (поддерживаются HTML теги и ссылки):", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text("✏️ Текст напоминания\n\nВведите текст напоминания:")
        return REMINDER_TEXT
    except Exception:
        try:
            update.message.reply_text("❌ <b>Некорректный формат</b>\n\nВведите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ (московское время):", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text("❌ Некорректный формат\n\nВведите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ (московское время):")
        return REMINDER_DATE

def receive_reminder_text(update: Update, context: CallbackContext):
    try:
    reminders = load_reminders()
        new_id = get_next_reminder_id()
        reminder_text = update.message.text_html if update.message.text_html else update.message.text.strip()
        
        # Безопасно обрабатываем HTML
        reminder_text = safe_html_escape(reminder_text)
        
    reminders.append({
        "id": new_id,
        "type": "once",
        "datetime": context.user_data["reminder_datetime"],
            "text": reminder_text
    })
    save_reminders(reminders)
        
        # ✅ ИНТЕГРАЦИЯ С GOOGLE SHEETS
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
            try:
                chat_id = update.effective_chat.id
                chat = update.effective_chat
                chat_name = chat.title if chat.title else f"@{chat.username}" if chat.username else str(chat.first_name or "Private")
                username = update.effective_user.username or update.effective_user.first_name or "Unknown"
                
                # Логируем действие
                sheets_manager.log_reminder_action("CREATE", update.effective_user.id, username, chat_id, f"Created reminder: {reminder_text[:50]}...", new_id)
                
                # Синхронизируем напоминание
                reminder_data = {
                    "id": new_id,
                    "text": reminder_text,
                    "time": context.user_data["reminder_datetime"],
                    "type": "once",
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "created_at": get_moscow_time().strftime("%Y-%m-%d %H:%M:%S"),
                    "username": username
                }
                sheets_manager.sync_reminder(reminder_data, "CREATE")
                
                # Обновляем количество напоминаний для чата
                sheets_manager.update_reminders_count(chat_id)
                
                logger.info(f"📊 Successfully synced reminder #{new_id} to Google Sheets")
            except Exception as e:
                logger.error(f"❌ Error syncing reminder to Google Sheets: {e}")
        elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not initialized - reminder #{new_id} not synced")
            logger.warning("   Check GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS environment variables")
        else:
            logger.warning("📵 Google Sheets not available for reminder sync")
        
        # Планируем напоминание
        schedule_reminder(context.dispatcher.job_queue, reminders[-1])
        
        try:
            update.message.reply_text(
                f"✅ <b>Напоминание #{new_id} добавлено</b>\n\n"
                f"📅 <i>{context.user_data['reminder_datetime']}</i>\n"
                f"💬 {reminder_text}", 
                parse_mode=ParseMode.HTML
            )
        except:
            update.message.reply_text(f"✅ Напоминание #{new_id} добавлено: {context.user_data['reminder_datetime']}")
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in receive_reminder_text: {e}")
        update.message.reply_text("❌ Ошибка при добавлении напоминания")
    return ConversationHandler.END

# --- Обработчики добавления ежедневного напоминания ---
def start_add_daily_reminder(update: Update, context: CallbackContext):
    try:
        current_time = get_moscow_time().strftime("%H:%M MSK")
        update.message.reply_text(f"🔄 <b>Ежедневное напоминание</b>\n\nВведите время в формате ЧЧ:ММ\nНапример: 08:00\n\n<i>⏰ Сейчас: {current_time}</i>", parse_mode=ParseMode.HTML)
        return DAILY_TIME
    except:
        current_time = get_moscow_time().strftime("%H:%M MSK")
        update.message.reply_text(f"🔄 Ежедневное напоминание\n\nВведите время в формате ЧЧ:ММ\nНапример: 08:00\n\n⏰ Сейчас: {current_time}")
    return DAILY_TIME

def receive_daily_time(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        time.strptime(text, "%H:%M")
        context.user_data["daily_time"] = text
        try:
            update.message.reply_text("✏️ <b>Текст ежедневного напоминания</b>\n\nВведите текст (поддерживаются HTML теги и ссылки):\n<i>⏰ Время указано московское (MSK)</i>", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text("✏️ Текст ежедневного напоминания\n\nВведите текст:\n⏰ Время указано московское (MSK)")
        return DAILY_TEXT
    except Exception:
        try:
            update.message.reply_text("❌ <b>Некорректный формат</b>\n\nВведите время в формате ЧЧ:ММ (московское время):", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text("❌ Некорректный формат\n\nВведите время в формате ЧЧ:ММ (московское время):")
        return DAILY_TIME

def receive_daily_text(update: Update, context: CallbackContext):
    try:
    reminders = load_reminders()
        new_id = get_next_reminder_id()
        reminder_text = update.message.text_html if update.message.text_html else update.message.text.strip()
        reminder_text = safe_html_escape(reminder_text)
        
    reminders.append({
        "id": new_id,
        "type": "daily",
        "time": context.user_data["daily_time"],
            "text": reminder_text
    })
    save_reminders(reminders)
        
        # ✅ ИНТЕГРАЦИЯ С GOOGLE SHEETS
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
            try:
                chat_id = update.effective_chat.id
                chat = update.effective_chat
                chat_name = chat.title if chat.title else f"@{chat.username}" if chat.username else str(chat.first_name or "Private")
                username = update.effective_user.username or update.effective_user.first_name or "Unknown"
                
                # Логируем действие
                sheets_manager.log_reminder_action("CREATE", update.effective_user.id, username, chat_id, f"Created daily reminder: {reminder_text[:50]}...", new_id)
                
                # Синхронизируем напоминание
                reminder_data = {
                    "id": new_id,
                    "text": reminder_text,
                    "time": context.user_data["daily_time"],
                    "type": "daily",
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "created_at": get_moscow_time().strftime("%Y-%m-%d %H:%M:%S"),
                    "username": username
                }
                sheets_manager.sync_reminder(reminder_data, "CREATE")
                
                # Обновляем количество напоминаний для чата
                sheets_manager.update_reminders_count(chat_id)
                
                logger.info(f"📊 Successfully synced daily reminder #{new_id} to Google Sheets")
            except Exception as e:
                logger.error(f"❌ Error syncing daily reminder to Google Sheets: {e}")
        elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not initialized - daily reminder #{new_id} not synced")
            logger.warning("   Check GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS environment variables")
        else:
            logger.warning("📵 Google Sheets not available for daily reminder sync")
        
        # Планируем напоминание
        schedule_reminder(context.dispatcher.job_queue, reminders[-1])
        
        try:
            update.message.reply_text(
                f"✅ <b>Ежедневное напоминание #{new_id} добавлено</b>\n\n"
                f"🕐 <i>Каждый день в {context.user_data['daily_time']}</i>\n"
                f"💬 {reminder_text}", 
                parse_mode=ParseMode.HTML
            )
        except:
            update.message.reply_text(f"✅ Ежедневное напоминание #{new_id} добавлено: {context.user_data['daily_time']}")
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in receive_daily_text: {e}")
        update.message.reply_text("❌ Ошибка при добавлении напоминания")
    return ConversationHandler.END

# --- Обработчики добавления еженедельного напоминания ---
def start_add_weekly_reminder(update: Update, context: CallbackContext):
    try:
        current_time = get_moscow_time().strftime("%H:%M MSK")
        update.message.reply_text(f"📆 <b>Еженедельное напоминание</b>\n\nВведите день недели:\nПонедельник, Вторник, Среда, Четверг, Пятница, Суббота, Воскресенье\n\n<i>⏰ Сейчас: {current_time}</i>", parse_mode=ParseMode.HTML)
        return WEEKLY_DAY
    except:
        current_time = get_moscow_time().strftime("%H:%M MSK")
        update.message.reply_text(f"📆 Еженедельное напоминание\n\nВведите день недели:\nПонедельник, Вторник, Среда, Четверг, Пятница, Суббота, Воскресенье\n\n⏰ Сейчас: {current_time}")
    return WEEKLY_DAY

def receive_weekly_day(update: Update, context: CallbackContext):
    # ✅ ЗАЩИТА ОТ None - исправление AttributeError
    if not update.message or not update.message.text:
        try:
            update.message.reply_text("❌ <b>Сообщение не получено</b>\n\nПожалуйста, введите день недели текстом:", parse_mode=ParseMode.HTML)
        except:
            if update.message:
                update.message.reply_text("❌ Сообщение не получено. Введите день недели:")
        return WEEKLY_DAY
    
    text = update.message.text.strip().lower()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    if text not in days:
        try:
            update.message.reply_text("❌ <b>Некорректный день недели</b>\n\nВыберите один из:\nПонедельник, Вторник, Среда, Четверг, Пятница, Суббота, Воскресенье", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text("❌ Некорректный день недели\n\nВыберите один из:\nПонедельник, Вторник, Среда, Четверг, Пятница, Суббота, Воскресенье")
        return WEEKLY_DAY
    context.user_data["weekly_day"] = text
    try:
        update.message.reply_text("🕐 <b>Время напоминания</b>\n\nВведите время в формате ЧЧ:ММ:", parse_mode=ParseMode.HTML)
    except:
        update.message.reply_text("🕐 Время напоминания\n\nВведите время в формате ЧЧ:ММ:")
    return WEEKLY_TIME

def receive_weekly_time(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    try:
        time.strptime(text, "%H:%M")
        context.user_data["weekly_time"] = text
        try:
            update.message.reply_text("✏️ <b>Текст еженедельного напоминания</b>\n\nВведите текст (поддерживаются HTML теги и ссылки):\n<i>⏰ Время указано московское (MSK)</i>", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text("✏️ Текст еженедельного напоминания\n\nВведите текст:\n⏰ Время указано московское (MSK)")
        return WEEKLY_TEXT
    except Exception:
        try:
            update.message.reply_text("❌ <b>Некорректный формат</b>\n\nВведите время в формате ЧЧ:ММ (московское время):", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text("❌ Некорректный формат\n\nВведите время в формате ЧЧ:ММ (московское время):")
        return WEEKLY_TIME

def receive_weekly_text(update: Update, context: CallbackContext):
    try:
    reminders = load_reminders()
        new_id = get_next_reminder_id()
        reminder_text = update.message.text_html if update.message.text_html else update.message.text.strip()
        reminder_text = safe_html_escape(reminder_text)
        
    reminders.append({
        "id": new_id,
        "type": "weekly",
        "day": context.user_data["weekly_day"],
        "time": context.user_data["weekly_time"],
            "text": reminder_text
    })
    save_reminders(reminders)
        
        # ✅ ИНТЕГРАЦИЯ С GOOGLE SHEETS
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
            try:
                chat_id = update.effective_chat.id
                chat = update.effective_chat
                chat_name = chat.title if chat.title else f"@{chat.username}" if chat.username else str(chat.first_name or "Private")
                username = update.effective_user.username or update.effective_user.first_name or "Unknown"
                
                # Логируем действие
                sheets_manager.log_reminder_action("CREATE", update.effective_user.id, username, chat_id, f"Created weekly reminder: {reminder_text[:50]}...", new_id)
                
                # Синхронизируем напоминание
                reminder_data = {
                    "id": new_id,
                    "text": reminder_text,
                    "time": f"{context.user_data['weekly_day']} {context.user_data['weekly_time']}",
                    "type": "weekly",
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "created_at": get_moscow_time().strftime("%Y-%m-%d %H:%M:%S"),
                    "username": username,
                    "days_of_week": context.user_data["weekly_day"]
                }
                sheets_manager.sync_reminder(reminder_data, "CREATE")
                
                # Обновляем количество напоминаний для чата
                sheets_manager.update_reminders_count(chat_id)
                
                logger.info(f"📊 Successfully synced weekly reminder #{new_id} to Google Sheets")
            except Exception as e:
                logger.error(f"❌ Error syncing weekly reminder to Google Sheets: {e}")
        elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not initialized - weekly reminder #{new_id} not synced")
            logger.warning("   Check GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS environment variables")
        else:
            logger.warning("📵 Google Sheets not available for weekly reminder sync")
        
        # Планируем напоминание
        schedule_reminder(context.dispatcher.job_queue, reminders[-1])
        
        try:
            update.message.reply_text(
                f"✅ <b>Еженедельное напоминание #{new_id} добавлено</b>\n\n"
                f"📅 <i>Каждый {context.user_data['weekly_day'].title()} в {context.user_data['weekly_time']}</i>\n"
                f"💬 {reminder_text}", 
                parse_mode=ParseMode.HTML
            )
        except:
            update.message.reply_text(f"✅ Еженедельное напоминание #{new_id} добавлено: {context.user_data['weekly_day'].title()} {context.user_data['weekly_time']}")
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in receive_weekly_text: {e}")
        update.message.reply_text("❌ Ошибка при добавлении напоминания")
    return ConversationHandler.END

# --- Список напоминаний ---
def list_reminders(update: Update, context: CallbackContext):
    try:
    reminders = load_reminders()
    if not reminders:
            try:
                update.message.reply_text("📭 <b>У вас нет активных напоминаний</b>", parse_mode=ParseMode.HTML)
            except:
                update.message.reply_text("📭 У вас нет активных напоминаний")
        return
        
        lines = ["📋 Ваши напоминания:\n"]
        
        # Сортируем по ID для удобства
        reminders.sort(key=lambda x: int(x.get("id", "0")))
        
        for i, r in enumerate(reminders, 1):
            try:
                safe_text = safe_html_escape(r.get('text', ''))
        if r["type"] == "once":
                    lines.append(f"{i}. [📅 Разово] {r['datetime']}\n💬 {safe_text}\n")
        elif r["type"] == "daily":
                    lines.append(f"{i}. [🔄 Ежедневно] {r['time']}\n💬 {safe_text}\n")
        elif r["type"] == "weekly":
                    lines.append(f"{i}. [📆 Еженедельно] {r['day'].title()} {r['time']}\n💬 {safe_text}\n")
            except Exception as e:
                logger.error(f"Error formatting reminder {i}: {e}")
                lines.append(f"{i}. [Ошибка формата]\n")
        
        message_text = "\n".join(lines)
        
        # Telegram имеет лимит на длину сообщения
        if len(message_text) > 4000:
            # Разбиваем на части
            chunks = []
            current_chunk = "📋 Ваши напоминания:\n\n"
            
            for line in lines[1:]:  # Пропускаем заголовок
                if len(current_chunk + line) > 4000:
                    chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += line
            
            if current_chunk:
                chunks.append(current_chunk)
            
            for chunk in chunks:
                try:
                    update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
                except:
                    # Fallback без HTML
                    clean_chunk = chunk.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
                    update.message.reply_text(clean_chunk)
        else:
            try:
                update.message.reply_text(message_text, parse_mode=ParseMode.HTML)
            except:
                # Fallback без HTML
                clean_text = message_text.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
                update.message.reply_text(clean_text)
                
    except Exception as e:
        logger.error(f"Error in list_reminders: {e}")
        update.message.reply_text("❌ Ошибка при загрузке списка напоминаний")

# --- Удаление напоминания ---
def start_delete_reminder(update: Update, context: CallbackContext):
    try:
    reminders = load_reminders()
    if not reminders:
            try:
                update.message.reply_text("📭 <b>У вас нет напоминаний для удаления</b>", parse_mode=ParseMode.HTML)
            except:
                update.message.reply_text("📭 У вас нет напоминаний для удаления")
        return ConversationHandler.END
        
        lines = ["🗑 Выберите напоминание для удаления:\nВведите номер:\n"]
        
        # Сортируем по ID для удобства
        reminders.sort(key=lambda x: int(x.get("id", "0")))
        
        for i, r in enumerate(reminders, 1):
            try:
                text_preview = r.get('text', '')[:50]
                if len(r.get('text', '')) > 50:
                    text_preview += '...'
                    
        if r["type"] == "once":
                    lines.append(f"{i}. [📅 Разово] {r['datetime']}\n💬 {text_preview}")
        elif r["type"] == "daily":
                    lines.append(f"{i}. [🔄 Ежедневно] {r['time']}\n💬 {text_preview}")
        elif r["type"] == "weekly":
                    lines.append(f"{i}. [📆 Еженедельно] {r['day'].title()} {r['time']}\n💬 {text_preview}")
            except Exception as e:
                logger.error(f"Error formatting reminder for deletion {i}: {e}")
                lines.append(f"{i}. [Ошибка формата]")
        
        try:
            update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)
        except:
            # Fallback без HTML
            clean_text = "\n\n".join(lines).replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
            update.message.reply_text(clean_text)
        
    return REM_DEL_ID
        
    except Exception as e:
        logger.error(f"Error in start_delete_reminder: {e}")
        update.message.reply_text("❌ Ошибка при загрузке напоминаний для удаления")
        return ConversationHandler.END

def confirm_delete_reminder(update: Update, context: CallbackContext):
    try:
        reminder_number = int(update.message.text.strip())
    reminders = load_reminders()
        
        if reminder_number < 1 or reminder_number > len(reminders):
            try:
                update.message.reply_text("❌ <b>Неверный номер</b>\n\nВведите номер от 1 до " + str(len(reminders)), parse_mode=ParseMode.HTML)
            except:
                update.message.reply_text(f"❌ Неверный номер\n\nВведите номер от 1 до {len(reminders)}")
            return REM_DEL_ID
        
        # Сортируем по ID для удобства
        reminders.sort(key=lambda x: int(x.get("id", "0")))
        reminder_to_delete = reminders[reminder_number - 1]
        
        # ✅ СИНХРОНИЗАЦИЯ С GOOGLE SHEETS ПРИ УДАЛЕНИИ
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
            try:
                chat_id = update.effective_chat.id
                chat = update.effective_chat
                chat_name = chat.title if chat.title else f"@{chat.username}" if chat.username else str(chat.first_name or "Private")
                username = update.effective_user.username or update.effective_user.first_name or "Unknown"
                
                # Логируем действие удаления
                sheets_manager.log_reminder_action("DELETE", update.effective_user.id, username, chat_id, f"Deleted reminder: {reminder_to_delete.get('text', '')[:50]}...", reminder_to_delete.get('id'))
                
                # Синхронизируем удаление - устанавливаем статус "Deleted"
                reminder_data = {
                    "id": reminder_to_delete.get('id'),
                    "text": reminder_to_delete.get('text', ''),
                    "time": reminder_to_delete.get('datetime') or reminder_to_delete.get('time', ''),
                    "type": reminder_to_delete.get('type', ''),
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "created_at": reminder_to_delete.get('created_at', ''),
                    "username": reminder_to_delete.get('username', username),
                    "last_sent": reminder_to_delete.get('last_sent', ''),
                    "days_of_week": reminder_to_delete.get('day', '') if reminder_to_delete.get('type') == 'weekly' else reminder_to_delete.get('days_of_week', '')
                }
                
                # ВАЖНО: Используем действие "DELETE" для установки статуса "Deleted"
                sheets_manager.sync_reminder(reminder_data, "DELETE")
                
                # Обновляем количество напоминаний для чата
                sheets_manager.update_reminders_count(chat_id)
                
                logger.info(f"📊 Successfully synced reminder #{reminder_to_delete.get('id')} deletion to Google Sheets (status: Deleted)")
                
            except Exception as e:
                logger.error(f"❌ Error syncing reminder deletion to Google Sheets: {e}")
                # Продолжаем удаление даже если синхронизация не удалась
        elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not initialized - reminder #{reminder_to_delete.get('id')} deletion not synced")
            logger.warning("   Check GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS environment variables")
    else:
            logger.warning("📵 Google Sheets not available for reminder deletion sync")
        
        # Удаляем напоминание из локального файла
        all_reminders = load_reminders()
        new_list = [r for r in all_reminders if r["id"] != reminder_to_delete["id"]]
        save_reminders(new_list)
        
        try:
            update.message.reply_text(f"✅ <b>Напоминание #{reminder_number} удалено</b>\n<i>Статус в Google Sheets изменен на Deleted</i>", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text(f"✅ Напоминание #{reminder_number} удалено")
        
        # Перепланируем все напоминания
        reschedule_all_reminders(context.dispatcher.job_queue)
        
    except ValueError:
        try:
            update.message.reply_text("❌ <b>Введите номер напоминания</b>", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text("❌ Введите номер напоминания")
        return REM_DEL_ID
    except Exception as e:
        logger.error(f"Error in confirm_delete_reminder: {e}")
        update.message.reply_text("❌ Ошибка при удалении напоминания")
    
    return ConversationHandler.END

# --- Очистка всех напоминаний ---
def clear_reminders(update: Update, context: CallbackContext):
    try:
        # Получаем все текущие напоминания перед удалением для синхронизации
        all_reminders = load_reminders()
        reminders_count = len(all_reminders)
        
        # ✅ СИНХРОНИЗАЦИЯ С GOOGLE SHEETS ПРИ МАССОВОМ УДАЛЕНИИ
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized and all_reminders:
            try:
                chat_id = update.effective_chat.id
                chat = update.effective_chat
                chat_name = chat.title if chat.title else f"@{chat.username}" if chat.username else str(chat.first_name or "Private")
                username = update.effective_user.username or update.effective_user.first_name or "Unknown"
                
                # Отправляем сообщение о начале операции
                try:
                    progress_message = update.message.reply_text(
                        f"🔄 <b>Удаление всех напоминаний...</b>\n\n"
                        f"📊 Обновление Google Sheets для {reminders_count} напоминаний...",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    progress_message = update.message.reply_text(f"🔄 Удаление {reminders_count} напоминаний...")
                
                # Логируем начало массового удаления
                sheets_manager.log_reminder_action("CLEAR_ALL", update.effective_user.id, username, chat_id, f"Started mass deletion of {reminders_count} reminders", "")
                
                # Синхронизируем каждое напоминание - устанавливаем статус "Deleted"
                # Для больших количеств добавляем батчинг с перерывами
                synced_count = 0
                failed_count = 0
                batch_size = 5  # Обрабатываем по 5 напоминаний за раз
                batch_delay = 10.0  # 10 секунд между батчами
                
                for i, reminder in enumerate(all_reminders):
                    try:
                        # Проверяем, нужен ли перерыв между батчами
                        if i > 0 and i % batch_size == 0:
                            logger.info(f"📦 Completed batch {i//batch_size}, waiting {batch_delay}s before next batch...")
                            
                            # Обновляем сообщение о прогрессе между батчами
                            try:
                                context.bot.edit_message_text(
                                    chat_id=progress_message.chat_id,
                                    message_id=progress_message.message_id,
                                    text=f"🔄 <b>Пауза между батчами...</b>\n\n"
                                         f"✅ Обработано: {i}/{reminders_count}\n"
                                         f"✅ Успешно: {synced_count}\n"
                                         f"❌ Ошибки: {failed_count}\n\n"
                                         f"⏱️ Ожидание {batch_delay}s...",
                                    parse_mode=ParseMode.HTML
                                )
                            except:
                                pass
                            
                            time.sleep(batch_delay)
                        
                        reminder_data = {
                            "id": reminder.get('id'),
                            "text": reminder.get('text', ''),
                            "time": reminder.get('datetime') or reminder.get('time', ''),
                            "type": reminder.get('type', ''),
                            "chat_id": chat_id,
                            "chat_name": chat_name,
                            "created_at": reminder.get('created_at', ''),
                            "username": reminder.get('username', username),
                            "last_sent": reminder.get('last_sent', ''),
                            "days_of_week": reminder.get('day', '') if reminder.get('type') == 'weekly' else reminder.get('days_of_week', '')
                        }
                        
                        # ВАЖНО: Используем действие "DELETE" для установки статуса "Deleted"
                        success = sheets_manager.sync_reminder(reminder_data, "DELETE")
                        if success:
                            synced_count += 1
                        else:
                            failed_count += 1
                            logger.warning(f"⚠️ Failed to sync reminder #{reminder.get('id')} deletion")
                        
                        # Добавляем задержку между операциями для предотвращения rate limiting
                        # Увеличиваем задержку до 1-2 секунд для соблюдения лимита 60 запросов/минуту
                        if i < len(all_reminders) - 1:  # Не задерживаем после последнего
                            # Прогрессивная задержка: больше времени для более поздних операций
                            base_delay = 1.0  # Базовая задержка 1 секунда
                            progressive_delay = (i % batch_size) * 0.2  # Дополнительная задержка внутри батча
                            total_delay = base_delay + progressive_delay
                            time.sleep(total_delay)
                            logger.debug(f"⏱️ Waiting {total_delay:.1f}s before next sync operation ({i+2}/{len(all_reminders)})")
                        
                    except Exception as e:
                        logger.error(f"❌ Error syncing reminder #{reminder.get('id')} deletion: {e}")
                        failed_count += 1
                        # Продолжаем даже если одно напоминание не синхронизировалось
                
                # Обновляем сообщение о прогрессе после синхронизации напоминаний
                try:
                    context.bot.edit_message_text(
                        chat_id=progress_message.chat_id,
                        message_id=progress_message.message_id,
                        text=f"🔄 <b>Обновление статистики...</b>\n\n"
                             f"✅ Напоминания: {synced_count}/{reminders_count}\n"
                             f"❌ Ошибки: {failed_count}",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    pass
                
                # Увеличенная задержка перед обновлением статистики
                time.sleep(2.0)  # Увеличиваем с 0.5 до 2 секунд
                
                # Обновляем количество напоминаний для чата (должно стать 0)
                count_update_success = sheets_manager.update_reminders_count(chat_id)
                
                # Финальное логирование
                sheets_manager.log_reminder_action("CLEAR_ALL_COMPLETE", update.effective_user.id, username, chat_id, f"Completed mass deletion. Synced: {synced_count}/{reminders_count}, Failed: {failed_count}", "")
                
                logger.info(f"📊 Mass deletion summary: {synced_count}/{reminders_count} reminders synced, {failed_count} failed")
                
                # Обновляем сообщение о прогрессе
                try:
                    context.bot.edit_message_text(
                        chat_id=progress_message.chat_id,
                        message_id=progress_message.message_id,
                        text=f"🔄 <b>Завершение удаления...</b>\n\n"
                             f"✅ Google Sheets обновлен ({synced_count}/{reminders_count})"
                             f"\n{'⚠️ Статистика чатов: ошибка обновления' if not count_update_success else '✅ Статистика чатов обновлена'}",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    pass
                
            except Exception as e:
                logger.error(f"❌ Error syncing mass deletion to Google Sheets: {e}")
                # Продолжаем удаление даже если синхронизация не удалась
        elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not initialized - mass deletion of {reminders_count} reminders not synced")
            logger.warning("   Check GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS environment variables")
        elif not all_reminders:
            logger.info("📭 No reminders to delete")
        else:
            logger.warning("📵 Google Sheets not available for mass deletion sync")
        
        # Удаляем все напоминания из локального файла
    save_reminders([])
        
        # Останавливаем все задания
        job_queue = context.dispatcher.job_queue
        current_jobs = job_queue.jobs()
        for job in current_jobs:
            if hasattr(job, 'name') and job.name and job.name.startswith('reminder_'):
                job.schedule_removal()
        
        # Финальное сообщение пользователю
        if reminders_count > 0:
            try:
                # Если есть progress_message, обновляем его
                if 'progress_message' in locals():
                    # Формируем детальное сообщение с учетом успешности операций
                    if 'synced_count' in locals() and 'failed_count' in locals():
                        if failed_count == 0:
                            final_text = f"🗑 <b>Все напоминания удалены ({reminders_count})</b>\n<i>✅ Статус всех напоминаний в Google Sheets изменен на Deleted</i>"
                        else:
                            final_text = f"🗑 <b>Напоминания удалены ({reminders_count})</b>\n<i>✅ Синхронизировано: {synced_count}/{reminders_count}\n⚠️ Ошибок синхронизации: {failed_count}</i>"
                    else:
                        final_text = f"🗑 <b>Все напоминания удалены ({reminders_count})</b>\n<i>⚠️ Google Sheets недоступен</i>"
                    
                    context.bot.edit_message_text(
                        chat_id=progress_message.chat_id,
                        message_id=progress_message.message_id,
                        text=final_text,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    update.message.reply_text(
                        f"🗑 <b>Все напоминания удалены ({reminders_count})</b>\n"
                        f"<i>Статус в Google Sheets изменен на Deleted</i>", 
                        parse_mode=ParseMode.HTML
                    )
            except:
                update.message.reply_text(f"🗑 Все напоминания удалены ({reminders_count})")
        else:
            try:
                update.message.reply_text("📭 <b>Напоминаний для удаления не найдено</b>", parse_mode=ParseMode.HTML)
            except:
                update.message.reply_text("📭 Напоминаний для удаления не найдено")
            
    except Exception as e:
        logger.error(f"Error in clear_reminders: {e}")
        update.message.reply_text("❌ Ошибка при очистке напоминаний")

# --- Восстановление напоминаний из Google Sheets ---
def restore_reminders(update: Update, context: CallbackContext):
    """Восстановление активных напоминаний из Google Sheets"""
    try:
        # Проверяем доступность Google Sheets
        if not SHEETS_AVAILABLE or not sheets_manager:
            try:
                update.message.reply_text(
                    "❌ <b>Google Sheets недоступен</b>\n\n"
                    "📵 Интеграция с Google Sheets не настроена.\n"
                    "Обратитесь к администратору для настройки.",
                    parse_mode=ParseMode.HTML
                )
            except:
                update.message.reply_text("❌ Google Sheets недоступен")
            return
        
        if not sheets_manager.is_initialized:
            try:
                update.message.reply_text(
                    "❌ <b>Google Sheets не инициализирован</b>\n\n"
                    "🔧 Проверьте переменные окружения:\n"
                    "• GOOGLE_SHEETS_ID\n"
                    "• GOOGLE_SHEETS_CREDENTIALS",
                    parse_mode=ParseMode.HTML
                )
            except:
                update.message.reply_text("❌ Google Sheets не инициализирован")
            return
        
        # Отправляем сообщение о начале восстановления
        try:
            progress_message = update.message.reply_text(
                "🔄 <b>Восстановление данных...</b>\n\n"
                "📊 Получение данных из Google Sheets...\n"
                "🔄 Восстановление напоминаний и чатов...",
                parse_mode=ParseMode.HTML
            )
        except:
            progress_message = update.message.reply_text("🔄 Восстановление данных...")
        
        # Получаем информацию о пользователе для логирования
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name or "Unknown"
        
        # Логируем начало операции восстановления
        if sheets_manager.is_initialized:
            try:
                moscow_time = get_moscow_time().strftime("%Y-%m-%d %H:%M:%S")
                sheets_manager.log_operation(
                    timestamp=moscow_time,
                    action="RESTORE_ALL_START",
                    user_id=str(user_id),
                    username=username,
                    chat_id=chat_id,
                    details="Manual restore reminders and chats command initiated",
                    reminder_id=""
                )
            except Exception as e:
                logger.error(f"Error logging restore start: {e}")
        
        # Обновляем сообщение о прогрессе
        try:
            context.bot.edit_message_text(
                chat_id=progress_message.chat_id,
                message_id=progress_message.message_id,
                text="🔄 <b>Восстановление данных...</b>\n\n"
                     "📱 Восстановление подписанных чатов...",
                parse_mode=ParseMode.HTML
            )
        except:
            pass
        
        # ДОПОЛНИТЕЛЬНО: Восстанавливаем подписанные чаты
        chats_restored = False
        chats_count = 0
        chats_message = ""
        
        try:
            success_chats = sheets_manager.restore_subscribed_chats_file()
            if success_chats:
                # Получаем количество восстановленных чатов
                try:
                    with open("subscribed_chats.json", "r") as f:
                        restored_chats = json.load(f)
                        chats_count = len(restored_chats)
                        chats_restored = True
                        chats_message = f"Восстановлено чатов: {chats_count}"
                        logger.info(f"✅ Successfully restored {chats_count} chats for user {username}")
                except:
                    chats_message = "Чаты восстановлены (количество не определено)"
                    chats_restored = True
            else:
                chats_message = "Чаты не восстановлены (возможно, список пуст в Google Sheets)"
                logger.warning(f"⚠️ Failed to restore chats for user {username}")
        except Exception as e:
            chats_message = f"Ошибка восстановления чатов: {str(e)}"
            logger.error(f"❌ Error restoring chats for user {username}: {e}")
        
        # Обновляем сообщение о прогрессе
        try:
            context.bot.edit_message_text(
                chat_id=progress_message.chat_id,
                message_id=progress_message.message_id,
                text="🔄 <b>Восстановление данных...</b>\n\n"
                     "📋 Восстановление напоминаний...",
                parse_mode=ParseMode.HTML
            )
        except:
            pass
        
        # Восстанавливаем напоминания
        success, message = sheets_manager.restore_reminders_from_sheets()
        
        if success:
            # Перепланируем все напоминания
            reschedule_all_reminders(context.dispatcher.job_queue)
            
            # Получаем количество восстановленных напоминаний
            try:
                restored_reminders = load_reminders()
                count = len(restored_reminders)
                
                # Подсчитываем по типам
                once_count = sum(1 for r in restored_reminders if r.get('type') == 'once')
                daily_count = sum(1 for r in restored_reminders if r.get('type') == 'daily')
                weekly_count = sum(1 for r in restored_reminders if r.get('type') == 'weekly')
                
                # Формируем итоговое сообщение
                final_message = (
                    f"✅ <b>Восстановление завершено успешно!</b>\n\n"
                    f"📋 <b>Восстановлено напоминаний: {count}</b>\n"
                    f"📅 Разовых: {once_count}\n"
                    f"🔄 Ежедневных: {daily_count}\n"
                    f"📆 Еженедельных: {weekly_count}\n\n"
                    f"📱 <b>Подписанные чаты:</b>\n"
                    f"{'✅ ' + chats_message if chats_restored else '⚠️ ' + chats_message}\n\n"
                    f"⏰ Все напоминания перепланированы и активны!\n"
                    f"<i>Команды: /list_reminders для просмотра</i>"
                )
                
                try:
                    context.bot.edit_message_text(
                        chat_id=progress_message.chat_id,
                        message_id=progress_message.message_id,
                        text=final_message,
                        parse_mode=ParseMode.HTML
                    )
                except:
                    # Fallback без HTML
                    clean_message = (
                        f"✅ Восстановление завершено успешно!\n\n"
                        f"📋 Восстановлено напоминаний: {count}\n"
                        f"📅 Разовых: {once_count}\n"
                        f"🔄 Ежедневных: {daily_count}\n"
                        f"📆 Еженедельных: {weekly_count}\n\n"
                        f"📱 Подписанные чаты:\n"
                        f"{chats_message}\n\n"
                        f"⏰ Все напоминания перепланированы и активны!"
                    )
                    update.message.reply_text(clean_message)
                
                logger.info(f"✅ Successfully restored {count} reminders and {chats_count if chats_restored else 0} chats for user {username} (ID: {user_id})")
                
            except Exception as e:
                logger.error(f"Error getting restored data count: {e}")
                try:
                    context.bot.edit_message_text(
                        chat_id=progress_message.chat_id,
                        message_id=progress_message.message_id,
                        text=f"✅ <b>Восстановление завершено!</b>\n\n"
                             f"📋 {message}\n"
                             f"📱 {chats_message}",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    update.message.reply_text(f"✅ Восстановление завершено!\n\n📋 {message}\n📱 {chats_message}")
        
        else:
            # Ошибка восстановления напоминаний
            try:
                context.bot.edit_message_text(
                    chat_id=progress_message.chat_id,
                    message_id=progress_message.message_id,
                    text=f"❌ <b>Ошибка восстановления напоминаний</b>\n\n"
                         f"📋 {message}\n\n"
                         f"📱 <b>Подписанные чаты:</b>\n"
                         f"{'✅ ' + chats_message if chats_restored else '⚠️ ' + chats_message}\n\n"
                         f"💡 <i>Попробуйте:</i>\n"
                         f"• Проверить доступ к Google Sheets\n"
                         f"• Убедиться, что в листе есть активные напоминания\n"
                         f"• Обратиться к администратору",
                    parse_mode=ParseMode.HTML
                )
            except:
                update.message.reply_text(f"❌ Ошибка восстановления напоминаний\n\n📋 {message}\n📱 {chats_message}")
            
            logger.error(f"❌ Failed to restore reminders for user {username}: {message}")
        
        # Логируем завершение операции
        if sheets_manager.is_initialized:
            try:
                moscow_time = get_moscow_time().strftime("%Y-%m-%d %H:%M:%S")
                sheets_manager.log_operation(
                    timestamp=moscow_time,
                    action="RESTORE_ALL_COMPLETE",
                    user_id=str(user_id),
                    username=username,
                    chat_id=chat_id,
                    details=f"Manual restore {'successful' if success else 'failed'}: Reminders: {message}, Chats: {chats_message}",
                    reminder_id=""
                )
            except Exception as e:
                logger.error(f"Error logging restore completion: {e}")
                
    except Exception as e:
        logger.error(f"Error in restore_reminders: {e}")
        try:
            update.message.reply_text(
                "❌ <b>Критическая ошибка восстановления</b>\n\n"
                "Обратитесь к администратору системы.",
                parse_mode=ParseMode.HTML
            )
        except:
            update.message.reply_text("❌ Критическая ошибка восстановления")

# --- Следующее напоминание ---
def next_notification(update: Update, context: CallbackContext):
    try:
    reminders = load_reminders()
    if not reminders:
            try:
                update.message.reply_text("📭 <b>Нет запланированных напоминаний</b>", parse_mode=ParseMode.HTML)
            except:
                update.message.reply_text("📭 Нет запланированных напоминаний")
        return
        
        now_moscow = get_moscow_time()
    soonest = None
    soonest_time = None
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        
    for r in reminders:
        t = None
        if r["type"] == "once":
                try:
                    # Парсим как московское время
                    naive_dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M")
                    t = MOSCOW_TZ.localize(naive_dt)
                    if t < now_moscow:  # Пропускаем прошедшие разовые напоминания
                        continue
                except ValueError:
                    continue
        elif r["type"] == "daily":
                try:
            h, m = map(int, r["time"].split(":"))
                    candidate = now_moscow.replace(hour=h, minute=m, second=0, microsecond=0)
                    if candidate < now_moscow:
                        candidate += timedelta(days=1)
            t = candidate
                except ValueError:
                    continue
        elif r["type"] == "weekly":
                try:
            weekday = days.index(r["day"])
            h, m = map(int, r["time"].split(":"))
                    candidate = now_moscow.replace(hour=h, minute=m, second=0, microsecond=0)
                    days_ahead = (weekday - now_moscow.weekday() + 7) % 7
                    if days_ahead == 0 and candidate < now_moscow:
                days_ahead = 7
                    t = candidate + timedelta(days=days_ahead)
                except (ValueError, IndexError):
                    continue
            
            if t and (soonest_time is None or t < soonest_time):
            soonest_time = t
            soonest = r
        
    if soonest is None:
            try:
                update.message.reply_text("📭 <b>Нет запланированных напоминаний</b>", parse_mode=ParseMode.HTML)
            except:
                update.message.reply_text("📭 Нет запланированных напоминаний")
        return
        
        time_diff = soonest_time - now_moscow
        
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
        
        safe_text = safe_html_escape(soonest.get('text', ''))
        current_time = now_moscow.strftime("%H:%M MSK")
        
    if soonest["type"] == "once":
            reminder_time = soonest_time.strftime("%Y-%m-%d %H:%M MSK")
            msg = f"📅 <b>Ближайшее напоминание</b>\n\n🕐 Разово: {reminder_time}\n⏰ {time_str}\n💬 {safe_text}\n\n<i>Сейчас: {current_time}</i>"
    elif soonest["type"] == "daily":
            reminder_time = soonest_time.strftime("%H:%M MSK")
            msg = f"🔄 <b>Ближайшее напоминание</b>\n\n🕐 Ежедневно: {reminder_time}\n⏰ {time_str}\n💬 {safe_text}\n\n<i>Сейчас: {current_time}</i>"
    elif soonest["type"] == "weekly":
            reminder_time = soonest_time.strftime("%H:%M MSK")
            msg = f"📆 <b>Ближайшее напоминание</b>\n\n🕐 Еженедельно: {soonest['day'].title()} {reminder_time}\n⏰ {time_str}\n💬 {safe_text}\n\n<i>Сейчас: {current_time}</i>"
        
        try:
            update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        except:
            # Fallback без HTML
            clean_msg = msg.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
            update.message.reply_text(clean_msg)
            
    except Exception as e:
        logger.error(f"Error in next_notification: {e}")
        update.message.reply_text("❌ Ошибка при поиске ближайшего напоминания")

def cancel_reminder(update: Update, context: CallbackContext):
    """
    Отмена создания напоминания.
    """
    try:
        update.message.reply_text("❌ <b>Операция отменена</b>", parse_mode=ParseMode.HTML)
    except:
        update.message.reply_text("❌ Операция отменена")
    return ConversationHandler.END

# --- Scheduling helpers ---

def send_reminder(context: CallbackContext):
    """
    Отправляет текст напоминания всем подписанным чатам.
    """
    try:
    reminder = context.job.context
        
        # Пытаемся загрузить чаты с автовосстановлением
        try:
    with open("subscribed_chats.json", "r") as f:
        chats = json.load(f)
                if not chats or len(chats) == 0:
                    raise ValueError("Empty chats list")
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
            logger.warning(f"⚠️ Problem with subscribed_chats.json: {e}")
            logger.info("🔧 Attempting emergency restore...")
            if ensure_subscribed_chats_file():
                try:
                    with open("subscribed_chats.json", "r") as f:
                        chats = json.load(f)
                    logger.info(f"✅ Emergency restore successful, loaded {len(chats)} chats")
                except:
                    logger.error("❌ Emergency restore failed, no reminders will be sent")
                    return
            else:
                logger.error("❌ Emergency restore failed, no reminders will be sent")
                return
        
        moscow_time = get_moscow_time().strftime("%H:%M MSK")
        utc_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        reminder_text = f"🔔 <b>НАПОМИНАНИЕ</b> <i>({moscow_time})</i>\n\n{reminder.get('text', '')}"
        reminder_id = reminder.get('id', 'unknown')
        
        # 📊 Логируем начало отправки в Google Sheets
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
            try:
                sheets_manager.log_send_history(
                    utc_time=utc_time,
                    moscow_time=moscow_time,
                    reminder_id=reminder_id,
                    chat_id="ALL",
                    status="SENDING",
                    error="",
                    text_preview=reminder.get('text', '')[:50] + "..." if len(reminder.get('text', '')) > 50 else reminder.get('text', '')
                )
                logger.info(f"📊 Logged reminder sending start for #{reminder_id} in Google Sheets")
            except Exception as e:
                logger.error(f"❌ Error logging send start to Google Sheets: {e}")
        elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not initialized - reminder #{reminder_id} sending start not logged")
        
        # Отправляем каждому чату
        total_sent = 0
        total_failed = 0
        
    for cid in chats:
            delivery_status = "SUCCESS"
            error_details = ""
            
            try:
                context.bot.send_message(chat_id=cid, text=reminder_text, parse_mode=ParseMode.HTML)
                logger.info(f"✅ Reminder sent to chat {cid} at {moscow_time}")
                total_sent += 1
                
            except Exception as e:
                logger.error(f"❌ Failed to send reminder to chat {cid}: {e}")
                error_details = str(e)
                delivery_status = "FAILED"
                
                # Fallback без HTML
                try:
                    clean_text = reminder_text.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
                    context.bot.send_message(chat_id=cid, text=clean_text)
                    logger.info(f"✅ Fallback reminder sent to chat {cid} at {moscow_time}")
                    delivery_status = "SUCCESS_FALLBACK"
                    error_details = f"HTML failed: {str(e)}, sent as plain text"
                    total_sent += 1
                    
                except Exception as e2:
                    logger.error(f"❌ Failed to send fallback reminder to chat {cid}: {e2}")
                    error_details = f"HTML failed: {str(e)}, Plain text failed: {str(e2)}"
                    total_failed += 1
            
            # 📊 Логируем каждую отправку в Google Sheets
            if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
                try:
                    sheets_manager.log_send_history(
                        utc_time=utc_time,
                        moscow_time=moscow_time,
                        reminder_id=reminder_id,
                        chat_id=str(cid),
                        status=delivery_status,
                        error=error_details,
                        text_preview=reminder.get('text', '')[:50] + "..." if len(reminder.get('text', '')) > 50 else reminder.get('text', '')
                    )
                except Exception as e:
                    logger.error(f"❌ Error logging send to Google Sheets for chat {cid}: {e}")
        
        # 📊 Итоговый лог в Google Sheets
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
            try:
                final_status = "COMPLETED" if total_failed == 0 else f"PARTIAL ({total_sent}/{total_sent + total_failed})"
                sheets_manager.log_send_history(
                    utc_time=utc_time,
                    moscow_time=moscow_time,
                    reminder_id=reminder_id,
                    chat_id="SUMMARY",
                    status=final_status,
                    error=f"Sent: {total_sent}, Failed: {total_failed}",
                    text_preview=f"Total chats: {len(chats)}"
                )
                logger.info(f"📊 Logged final summary for reminder #{reminder_id}: {total_sent} sent, {total_failed} failed")
            except Exception as e:
                logger.error(f"❌ Error logging final summary to Google Sheets: {e}")
        elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not initialized - final summary for reminder #{reminder_id} not logged")
        
        logger.info(f"📈 Reminder #{reminder_id} delivery summary: {total_sent} sent, {total_failed} failed")
        
        # Удаляем разовые напоминания после отправки
        if reminder.get("type") == "once":
        reminders = load_reminders()
            reminders = [r for r in reminders if r.get("id") != reminder.get("id")]
        save_reminders(reminders)
            logger.info(f"🗑️ One-time reminder #{reminder_id} removed after sending")
            
            # 📊 Логируем удаление в Google Sheets
            if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
                try:
                    sheets_manager.sync_reminder(reminder, "DELETE")
                    logger.info(f"📊 Successfully synced reminder #{reminder_id} deletion to Google Sheets")
                except Exception as e:
                    logger.error(f"❌ Error syncing reminder deletion to Google Sheets: {e}")
            elif SHEETS_AVAILABLE and sheets_manager and not sheets_manager.is_initialized:
                logger.warning(f"📵 Google Sheets not initialized - reminder #{reminder_id} deletion not synced")
            
    except Exception as e:
        logger.error(f"❌ Critical error in send_reminder: {e}")
        
        # 📊 Логируем критическую ошибку в Google Sheets
        if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
            try:
                utc_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                moscow_time = get_moscow_time().strftime("%H:%M MSK")
                sheets_manager.log_send_history(
                    utc_time=utc_time,
                    moscow_time=moscow_time,
                    reminder_id=reminder.get('id', 'unknown') if 'reminder' in locals() else 'unknown',
                    chat_id="ERROR",
                    status="CRITICAL_ERROR",
                    error=str(e),
                    text_preview="Critical error in send_reminder function"
                )
            except:
                pass  # Не логируем ошибку логирования, чтобы не создать бесконечный цикл

def schedule_reminder(job_queue, reminder):
    """
    Добавляет задание в JobQueue для данного напоминания с учетом московского времени.
    """
    try:
        # Сначала удаляем существующее задание с таким же ID, если есть
        current_jobs = job_queue.jobs()
        for job in current_jobs:
            if hasattr(job, 'name') and job.name == f"reminder_{reminder.get('id')}":
                job.schedule_removal()
        
    if reminder["type"] == "once":
            # Парсим как московское время и конвертируем в UTC для планировщика
            moscow_dt = datetime.strptime(reminder["datetime"], "%Y-%m-%d %H:%M")
            moscow_dt = MOSCOW_TZ.localize(moscow_dt)
            utc_dt = moscow_dt.astimezone(pytz.UTC).replace(tzinfo=None)
            
            if moscow_dt > get_moscow_time():  # Планируем только будущие напоминания
                job_queue.run_once(send_reminder, utc_dt, context=reminder, name=f"reminder_{reminder.get('id')}")
                logger.info(f"Scheduled one-time reminder {reminder.get('id')} for {moscow_dt.strftime('%Y-%m-%d %H:%M MSK')}")
                
    elif reminder["type"] == "daily":
        h, m = map(int, reminder["time"].split(":"))
            # Создаем время в московском часовом поясе, затем конвертируем в UTC
            moscow_time = dt_time(hour=h, minute=m)
            # Для ежедневных напоминаний нужно учесть смещение UTC
            utc_hour = (h - 3) % 24  # MSK = UTC+3
            utc_time = dt_time(hour=utc_hour, minute=m)
            
            job_queue.run_daily(send_reminder, utc_time, context=reminder, name=f"reminder_{reminder.get('id')}")
            logger.info(f"Scheduled daily reminder {reminder.get('id')} for {h:02d}:{m:02d} MSK (UTC: {utc_hour:02d}:{m:02d})")
            
    elif reminder["type"] == "weekly":
        days_map = {
            "понедельник": 0, "вторник": 1, "среда": 2,
            "четверг": 3, "пятница": 4, "суббота": 5, "воскресенье": 6
        }
        weekday = days_map[reminder["day"].lower()]
        h, m = map(int, reminder["time"].split(":"))
            
            # Конвертируем московское время в UTC
            utc_hour = (h - 3) % 24  # MSK = UTC+3
            utc_time = dt_time(hour=utc_hour, minute=m)
            
        job_queue.run_daily(
            send_reminder,
                utc_time,
            context=reminder,
            days=(weekday,),
                name=f"reminder_{reminder.get('id')}"
        )
            logger.info(f"Scheduled weekly reminder {reminder.get('id')} for {reminder['day']} {h:02d}:{m:02d} MSK")
            
    except Exception as e:
        logger.error(f"Error scheduling reminder {reminder.get('id', 'unknown')}: {e}")

def schedule_all_reminders(job_queue):
    """
    Загружает все напоминания и запланировывает их.
    """
    try:
    reminders = load_reminders()
    for reminder in reminders:
        schedule_reminder(job_queue, reminder)
    except Exception as e:
        logger.error(f"Error scheduling all reminders: {e}")

def reschedule_all_reminders(job_queue):
    """
    Перепланирует все напоминания (используется после удаления)
    """
    try:
        # Останавливаем все текущие задания
        current_jobs = job_queue.jobs()
        for job in current_jobs:
            if hasattr(job, 'name') and job.name and job.name.startswith('reminder_'):
                job.schedule_removal()
        
        # Планируем заново
        schedule_all_reminders(job_queue)
    except Exception as e:
        logger.error(f"Error rescheduling reminders: {e}")

# --- Функции автовосстановления подписок ---

def ensure_subscribed_chats_file():
    """Проверяет и восстанавливает subscribed_chats.json при необходимости"""
    try:
        # Проверяем существует ли файл и не пустой ли он
        with open("subscribed_chats.json", "r") as f:
            chats = json.load(f)
            if chats and len(chats) > 0:
                logger.info(f"✅ Found {len(chats)} existing subscribed chats")
                return True  # Файл в порядке
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass  # Файл отсутствует или поврежден
    
    # Детальная диагностика доступности Google Sheets
    logger.warning("⚠️ subscribed_chats.json is missing or empty. Attempting restore from Google Sheets...")
    logger.info(f"🔍 Google Sheets availability check:")
    logger.info(f"   SHEETS_AVAILABLE: {SHEETS_AVAILABLE}")
    logger.info(f"   sheets_manager exists: {sheets_manager is not None}")
    
    if sheets_manager:
        logger.info(f"   sheets_manager.is_initialized: {sheets_manager.is_initialized}")
        
        # Проверяем переменные окружения
        sheets_id = os.environ.get('GOOGLE_SHEETS_ID')
        sheets_creds = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
        logger.info(f"   GOOGLE_SHEETS_ID present: {bool(sheets_id)}")
        logger.info(f"   GOOGLE_SHEETS_CREDENTIALS present: {bool(sheets_creds)}")
        
        if sheets_id:
            logger.info(f"   Using Sheet ID: {sheets_id[:20]}...{sheets_id[-10:] if len(sheets_id) > 30 else sheets_id}")
    
    if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
        if sheets_manager.restore_subscribed_chats_file():
            logger.info("✅ Successfully restored subscribed chats from Google Sheets")
            return True
        else:
            logger.error("❌ Failed to restore from Google Sheets")
    else:
        logger.warning("📵 Google Sheets not available for restoration")
        logger.warning("   This means:")
        logger.warning("   1. Check GOOGLE_SHEETS_ID environment variable")
        logger.warning("   2. Check GOOGLE_SHEETS_CREDENTIALS environment variable") 
        logger.warning("   3. Verify Google Sheets API access")
    
    # Создаем пустой файл как fallback с подробным объяснением
    logger.warning("📝 Creating empty subscribed_chats.json as fallback")
    logger.warning("⚠️  ВНИМАНИЕ: Бот не сможет отправлять напоминания без подписанных чатов!")
    logger.warning("   Для работы бота нужно:")
    logger.warning("   1. Запустить команду /start в Telegram чатах")
    logger.warning("   2. Настроить Google Sheets интеграцию")
    
    with open("subscribed_chats.json", "w") as f:
        json.dump([], f)
    
    return False

def ensure_reminders_file():
    """🆕 Проверяет и восстанавливает reminders.json при необходимости"""
    try:
        # Проверяем существует ли файл и не пустой ли он
        existing_reminders = load_reminders()
        if existing_reminders and len(existing_reminders) > 0:
            logger.info(f"✅ Found {len(existing_reminders)} existing reminders")
            return True, len(existing_reminders)  # Файл в порядке
    except Exception:
        pass  # Файл отсутствует или поврежден
    
    # Детальная диагностика доступности Google Sheets для напоминаний
    logger.warning("⚠️ reminders.json is missing, empty or corrupted. Attempting restore from Google Sheets...")
    logger.info(f"🔍 Google Sheets reminders restore check:")
    
    if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
        logger.info("   ✅ Google Sheets available for reminders restore")
        try:
            success, message = sheets_manager.restore_reminders_from_sheets()
            if success:
                restored_reminders = load_reminders()
                restored_count = len(restored_reminders)
                logger.info(f"✅ Successfully restored {restored_count} reminders from Google Sheets")
                return True, restored_count
            else:
                logger.error(f"❌ Failed to restore reminders from Google Sheets: {message}")
        except Exception as e:
            logger.error(f"❌ Exception during reminders restore: {e}")
    else:
        logger.warning("📵 Google Sheets not available for reminders restoration")
        logger.warning("   This means:")
        logger.warning("   1. Check GOOGLE_SHEETS_ID environment variable")
        logger.warning("   2. Check GOOGLE_SHEETS_CREDENTIALS environment variable") 
        logger.warning("   3. Verify Google Sheets API access")
        logger.warning("   4. Ensure reminders exist in Google Sheets with 'Active' status")
    
    # Создаем пустой файл как fallback
    logger.warning("📝 Creating empty reminders.json as fallback")
    logger.warning("⚠️ ВНИМАНИЕ: Бот не сможет отправлять напоминания без активных заданий!")
    logger.warning("   Для работы бота нужно:")
    logger.warning("   1. Создать напоминания командами /remind, /remind_daily, /remind_weekly")
    logger.warning("   2. Или восстановить из Google Sheets командой /restore_reminders")
    
    save_reminders([])
    return False, 0

def auto_sync_subscribed_chats(context: CallbackContext):
    """Автоматическая синхронизация subscribed_chats.json с Google Sheets каждые 5 минут"""
    try:
        moscow_time = get_moscow_time().strftime("%H:%M MSK")
        logger.info(f"🔄 Starting 5-minute sync at {moscow_time}")
        
        if SHEETS_AVAILABLE and sheets_manager:
            success = sheets_manager.sync_subscribed_chats_from_sheets()
            if success:
                logger.info(f"✅ 5-minute sync completed successfully at {moscow_time}")
            else:
                logger.warning(f"⚠️ 5-minute sync had issues at {moscow_time}")
        else:
            logger.warning(f"📵 Google Sheets not available for sync at {moscow_time}")
            
    except Exception as e:
        logger.error(f"❌ Error in 5-minute sync: {e}")

def auto_sync_reminders(context: CallbackContext):
    """🆕 Автоматическая синхронизация напоминаний с Google Sheets каждые 5 минут"""
    try:
        moscow_time = get_moscow_time().strftime("%H:%M MSK")
        logger.info(f"🔄 Starting reminders auto-sync at {moscow_time}")
        
        if not SHEETS_AVAILABLE or not sheets_manager or not sheets_manager.is_initialized:
            logger.warning(f"📵 Google Sheets not available for reminders sync at {moscow_time}")
            return
        
        try:
            # Проверяем есть ли локальные напоминания
            current_reminders = load_reminders()
            current_count = len(current_reminders)
            
            logger.info(f"📋 Current local reminders: {current_count}")
            
            # Пытаемся получить напоминания из Google Sheets
            success, message = sheets_manager.restore_reminders_from_sheets()
            
            if success:
                synced_reminders = load_reminders()
                synced_count = len(synced_reminders)
                
                if synced_count != current_count:
                    logger.info(f"🔄 Auto-sync: Updated reminders {current_count} → {synced_count}")
                    logger.info(f"🛡️ File completely overwritten - no duplicates possible")
                    
                    # Перепланируем все напоминания
                    reschedule_all_reminders(context.dispatcher.job_queue)
                    logger.info(f"✅ Reminders rescheduled after auto-sync at {moscow_time}")
                    
                    # Проверяем активные задания после перепланирования
                    active_jobs_after = check_active_jobs(context.dispatcher.job_queue)
                    logger.info(f"📊 Active jobs after auto-sync: {active_jobs_after}")
                    
                    # Логируем в Google Sheets
                    if sheets_manager.is_initialized:
                        try:
                            sheets_manager.log_operation(
                                timestamp=moscow_time,
                                action="AUTO_SYNC_REMINDERS",
                                user_id="SYSTEM",
                                username="AutoSync",
                                chat_id=0,
                                details=f"Auto-sync updated reminders: {current_count} → {synced_count}, active jobs: {active_jobs_after}, no duplicates",
                                reminder_id=""
                            )
                        except:
                            pass
                else:
                    logger.info(f"✅ Auto-sync: Reminders already in sync ({current_count} items) at {moscow_time}")
                    logger.info(f"🛡️ No changes needed - all reminders unique")
            else:
                logger.warning(f"⚠️ Auto-sync reminders failed at {moscow_time}: {message}")
                
                # При неудаче автосинхронизации проверяем есть ли хотя бы локальные напоминания
                if current_count == 0:
                    logger.warning("🚨 CRITICAL: No local reminders AND auto-sync failed!")
                    logger.warning("   This means NO reminders will be sent until manual intervention")
                    logger.warning("   Recommended action: use /restore_reminders command")
                
        except Exception as e:
            logger.error(f"❌ Error during reminders auto-sync: {e}")
            
            # Проверяем критическое состояние
            try:
                current_reminders = load_reminders()
                if len(current_reminders) == 0:
                    logger.error("🚨 CRITICAL ERROR: No reminders available after auto-sync failure!")
            except:
                logger.error("🚨 CRITICAL ERROR: Cannot access reminders file!")
            
    except Exception as e:
        logger.error(f"❌ Critical error in auto_sync_reminders: {e}")

def check_active_jobs(job_queue):
    """🆕 Проверяет активные задания напоминаний и выводит статистику"""
    try:
        current_jobs = job_queue.jobs()
        reminder_jobs = [job for job in current_jobs if hasattr(job, 'name') and job.name and job.name.startswith('reminder_')]
        
        logger.info(f"📊 Active reminder jobs: {len(reminder_jobs)}")
        
        if len(reminder_jobs) > 0:
            logger.info("📋 Active reminder jobs list:")
            for job in reminder_jobs:
                # 🔧 ИСПРАВЛЕНО: безопасная проверка атрибута next_run
                try:
                    if hasattr(job, 'next_run') and job.next_run:
                        next_run_moscow = utc_to_moscow_time(job.next_run)
                        logger.info(f"   • {job.name}: next run at {next_run_moscow}")
                    else:
                        logger.info(f"   • {job.name}: scheduled (time info unavailable)")
                except Exception as attr_error:
                    logger.info(f"   • {job.name}: scheduled (next_run attribute error)")
        else:
            logger.warning("⚠️ NO ACTIVE REMINDER JOBS FOUND!")
            logger.warning("   This means reminders will not be sent!")
            logger.warning("   Possible reasons:")
            logger.warning("   1. reminders.json is empty")
            logger.warning("   2. All reminders are in the past")
            logger.warning("   3. Scheduling failed")
            
        return len(reminder_jobs)
        
    except Exception as e:
        logger.error(f"❌ Error checking active jobs: {e}")
        return 0

def emergency_restore_subscribed_chats(context: CallbackContext):
    """Экстренное восстановление при критической ошибке отправки"""
    try:
        logger.warning("🚨 Emergency restore triggered - checking subscribed_chats.json")
        
        # Проверяем текущий файл
        try:
            with open("subscribed_chats.json", "r") as f:
                chats = json.load(f)
                if chats and len(chats) > 0:
                    logger.info(f"📋 Current file contains {len(chats)} chats - no restore needed")
                    return
        except:
            pass
        
        # Файл поврежден или пуст - восстанавливаем
        logger.warning("🔧 Attempting emergency restore from Google Sheets")
        ensure_subscribed_chats_file()
        
    except Exception as e:
        logger.error(f"❌ Error in emergency restore: {e}")

def bot_status(update: Update, context: CallbackContext):
    """🆕 Диагностика состояния бота"""
    try:
        current_time = get_moscow_time().strftime("%Y-%m-%d %H:%M:%S MSK")
        
        # Рассчитываем время работы бота
        uptime_info = ""
        if BOT_START_TIME:
            uptime_delta = get_moscow_time() - BOT_START_TIME
            hours = uptime_delta.seconds // 3600
            minutes = (uptime_delta.seconds % 3600) // 60
            if uptime_delta.days > 0:
                uptime_info = f"⏱️ <i>Работает: {uptime_delta.days}д {hours}ч {minutes}м</i>\n"
            else:
                uptime_info = f"⏱️ <i>Работает: {hours}ч {minutes}м</i>\n"
        
        # Проверяем локальные файлы
        try:
            reminders = load_reminders()
            reminders_count = len(reminders)
        except:
            reminders_count = 0
            
        try:
            with open("subscribed_chats.json", "r") as f:
                chats = json.load(f)
                chats_count = len(chats)
        except:
            chats_count = 0
        
        # Проверяем активные задания
        current_jobs = context.dispatcher.job_queue.jobs()
        reminder_jobs = [job for job in current_jobs if hasattr(job, 'name') and job.name and job.name.startswith('reminder_')]
        active_jobs_count = len(reminder_jobs)
        
        # Проверяем Google Sheets
        sheets_status = "❌ Недоступен"
        sheets_details = "Не инициализирован"
        
        if SHEETS_AVAILABLE and sheets_manager:
            if sheets_manager.is_initialized:
                sheets_status = "✅ Подключен"
                sheets_details = "Готов к работе"
            else:
                sheets_status = "⚠️ Не инициализирован"
                sheets_details = "Проверьте переменные окружения"
        
        # Подсчитываем типы напоминаний
        once_count = sum(1 for r in reminders if r.get('type') == 'once')
        daily_count = sum(1 for r in reminders if r.get('type') == 'daily')
        weekly_count = sum(1 for r in reminders if r.get('type') == 'weekly')
        
        # Формируем сообщение
        status_msg = (
            f"🤖 <b>Статус бота</b>\n"
            f"⏰ <i>{current_time}</i>\n"
            f"{uptime_info}\n"
            
            f"📋 <b>Локальные данные:</b>\n"
            f"• Напоминания: {reminders_count}\n"
            f"  📅 Разовых: {once_count}\n"
            f"  🔄 Ежедневных: {daily_count}\n"
            f"  📆 Еженедельных: {weekly_count}\n"
            f"• Подписанные чаты: {chats_count}\n\n"
            
            f"⚙️ <b>Планировщик заданий:</b>\n"
            f"• Активные задания: {active_jobs_count}\n"
            f"• Состояние: {'✅ Работает' if active_jobs_count > 0 else '❌ Нет заданий!'}\n\n"
            
            f"📊 <b>Google Sheets:</b>\n"
            f"• Статус: {sheets_status}\n"
            f"• Детали: {sheets_details}\n\n"
            
            f"🔧 <b>Диагностика:</b>\n"
        )
        
        # 🆕 Информация об автосинхронизации
        sync_info = ""
        try:
            now_moscow = get_moscow_time()
            sync_jobs = []
            
            for job in current_jobs:
                if hasattr(job, 'callback') and job.callback:
                    if job.callback.__name__ == 'auto_sync_subscribed_chats':
                        sync_jobs.append(('chats', job, '🔄 Чаты', 'каждые 5 мин'))
                    elif job.callback.__name__ == 'auto_sync_reminders':
                        sync_jobs.append(('reminders', job, '📋 Напоминания', 'каждые 5 мин'))
                    elif job.callback.__name__ == 'ping_self':
                        sync_jobs.append(('ping', job, '🏓 Ping', 'каждые 5 мин'))
            
            if sync_jobs:
                sync_info += f"🔄 <b>Автосинхронизация:</b>\n"
                
                for sync_type, job, name, period in sync_jobs:
                    try:
                        if hasattr(job, 'next_run') and job.next_run:
                            next_run_moscow = utc_to_moscow_time(job.next_run)
                            time_diff = next_run_moscow - now_moscow
                            
                            # Форматируем время до следующей синхронизации
                            if time_diff.total_seconds() < 0:
                                time_str = "сейчас"
                            elif time_diff.total_seconds() < 60:
                                seconds = int(time_diff.total_seconds())
                                time_str = f"через {seconds}с"
                            elif time_diff.total_seconds() < 3600:
                                minutes = int(time_diff.total_seconds() // 60)
                                seconds = int(time_diff.total_seconds() % 60)
                                time_str = f"через {minutes}м {seconds}с"
                            else:
                                hours = int(time_diff.total_seconds() // 3600)
                                minutes = int((time_diff.total_seconds() % 3600) // 60)
                                time_str = f"через {hours}ч {minutes}м"
                            
                            sync_info += f"• {name}: {time_str} ({period})\n"
                        else:
                            sync_info += f"• {name}: запланировано ({period})\n"
                    except Exception:
                        sync_info += f"• {name}: активно ({period})\n"
                
                sync_info += "\n"
        except Exception as e:
            logger.error(f"Error getting sync info: {e}")
            sync_info = "🔄 <b>Автосинхронизация:</b> ошибка получения данных\n\n"
        
        # Добавляем рекомендации
        if reminders_count == 0:
            status_msg += "⚠️ Нет напоминаний - создайте их или используйте /restore_reminders\n"
        
        if active_jobs_count == 0:
            status_msg += "🚨 КРИТИЧНО: Нет активных заданий! Напоминания не будут отправляться!\n"
            status_msg += "💡 Решение: /restore_reminders для восстановления\n"
        
        if chats_count == 0:
            status_msg += "📭 Нет подписанных чатов - напоминания некому отправлять\n"
            
        if not SHEETS_AVAILABLE or not sheets_manager or not sheets_manager.is_initialized:
            status_msg += "📵 Google Sheets недоступен - автовосстановление отключено\n"
        
        # Добавляем информацию о синхронизации ПЕРЕД ближайшими заданиями
        status_msg += f"\n{sync_info}"
        
        # Информация о ближайших заданиях
        if active_jobs_count > 0:
            status_msg += f"\n📅 <b>Ближайшие задания:</b>\n"
            jobs_info = []
            for job in reminder_jobs[:3]:  # Показываем только 3 ближайших
                # 🔧 ИСПРАВЛЕНО: безопасная проверка атрибута next_run
                try:
                    if hasattr(job, 'next_run') and job.next_run:
                        next_run_moscow = utc_to_moscow_time(job.next_run)
                        job_name = job.name.replace('reminder_', '#')
                        jobs_info.append(f"• {job_name}: {next_run_moscow.strftime('%d.%m %H:%M')}")
                    else:
                        job_name = job.name.replace('reminder_', '#')
                        jobs_info.append(f"• {job_name}: запланировано")
                except Exception:
                    job_name = job.name.replace('reminder_', '#')
                    jobs_info.append(f"• {job_name}: запланировано")
            
            if jobs_info:
                status_msg += "\n".join(jobs_info)
                if active_jobs_count > 3:
                    status_msg += f"\n• ... и ещё {active_jobs_count - 3} заданий"
        
        try:
            update.message.reply_text(status_msg, parse_mode=ParseMode.HTML)
        except:
            # Fallback без HTML
            clean_msg = status_msg.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
            update.message.reply_text(clean_msg)
            
    except Exception as e:
        logger.error(f"Error in bot_status: {e}")
        try:
            update.message.reply_text("❌ <b>Ошибка получения статуса бота</b>", parse_mode=ParseMode.HTML)
        except:
            update.message.reply_text("❌ Ошибка получения статуса бота")

def main():
    try:
        global BOT_START_TIME
        BOT_START_TIME = get_moscow_time()
        
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
        
        # ✅ ПРОВЕРЯЕМ И ВОССТАНАВЛИВАЕМ ПОДПИСКИ ПРИ ЗАПУСКЕ
        logger.info("🔧 Checking subscribed_chats.json...")
        ensure_subscribed_chats_file()
        
        # 🆕 ПРОВЕРЯЕМ И ВОССТАНАВЛИВАЕМ НАПОМИНАНИЯ ПРИ ЗАПУСКЕ
        logger.info("🔧 Checking reminders.json...")
        reminders_restored, reminders_count = ensure_reminders_file()
        if reminders_restored:
            logger.info(f"✅ Reminders status: {reminders_count} reminders ready for scheduling")
        else:
            logger.warning(f"⚠️ Reminders status: starting with empty reminders list")
            logger.warning("💡 TIP: Use /restore_reminders command to recover data from Google Sheets")
        
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
        dp.add_handler(CommandHandler("restore_reminders", restore_reminders))
    dp.add_handler(CommandHandler("next", next_notification))
        dp.add_handler(CommandHandler("status", bot_status))

        # Добавляем обработчик ошибок
        dp.add_error_handler(error_handler)

    # Запланировать все сохранённые напоминания
        logger.info("📋 Scheduling all reminders...")
    schedule_all_reminders(updater.job_queue)
        
        # 🆕 ПРОВЕРЯЕМ АКТИВНЫЕ ЗАДАНИЯ ПОСЛЕ ПЛАНИРОВАНИЯ
        active_jobs_count = check_active_jobs(updater.job_queue)
        if active_jobs_count == 0:
            logger.warning("⚠️ CRITICAL: No active reminder jobs scheduled!")
            logger.warning("   Attempting immediate reminders restore...")
            
            # Попытка экстренного восстановления
            if SHEETS_AVAILABLE and sheets_manager and sheets_manager.is_initialized:
                try:
                    success, message = sheets_manager.restore_reminders_from_sheets()
                    if success:
                        logger.info("✅ Emergency restore successful, rescheduling...")
                        reschedule_all_reminders(updater.job_queue)
                        final_jobs_count = check_active_jobs(updater.job_queue)
                        logger.info(f"🔄 After emergency restore: {final_jobs_count} active jobs")
                    else:
                        logger.error(f"❌ Emergency restore failed: {message}")
                except Exception as e:
                    logger.error(f"❌ Exception during emergency restore: {e}")
        
        # Добавляем ping каждые 5 минут для предотвращения засыпания на Render
        updater.job_queue.run_repeating(ping_self, interval=300, first=30)
        
        # ✅ АВТОМАТИЧЕСКАЯ СИНХРОНИЗАЦИЯ ПОДПИСОК КАЖДЫЕ 5 МИНУТ
        updater.job_queue.run_repeating(auto_sync_subscribed_chats, interval=300, first=300)  # Каждые 5 мин, первый через 5 мин
        logger.info("🔄 Scheduled 5-minute subscribed chats sync")
        
        # 🆕 АВТОМАТИЧЕСКАЯ СИНХРОНИЗАЦИЯ НАПОМИНАНИЙ КАЖДЫЕ 5 МИНУТ
        updater.job_queue.run_repeating(auto_sync_reminders, interval=300, first=600)  # Каждые 5 мин, первый через 10 мин
        logger.info("🔄 Scheduled 5-minute reminders auto-sync")

    # Health check server for Render free tier
    threading.Thread(target=start_health_server, daemon=True).start()
        
        # Улучшенная обработка конфликтов при запуске
        logger.info("🚀 Starting bot with enhanced conflict handling...")
        
    # Always run in polling mode
        try:
    updater.bot.delete_webhook(drop_pending_updates=True)
            
            # Дополнительная пауза для разрешения конфликтов
            time.sleep(2)
            
            updater.start_polling(drop_pending_updates=True, timeout=10, read_latency=5)
            logger.info("✅ Bot started successfully in polling mode")
            
            # Финальная проверка состояния через 30 секунд
            time.sleep(30)
            final_check_jobs = check_active_jobs(updater.job_queue)
            logger.info(f"🔍 Final status check: {final_check_jobs} reminder jobs active")
            
            # Проверяем подписанные чаты
            try:
                with open("subscribed_chats.json", "r") as f:
                    final_chats = json.load(f)
                    logger.info(f"📱 Final chats check: {len(final_chats)} subscribed chats")
            except:
                logger.warning("⚠️ Final chats check: subscribed_chats.json not accessible")
            
            # Проверяем напоминания  
            try:
                final_reminders = load_reminders()
                logger.info(f"📋 Final reminders check: {len(final_reminders)} reminders loaded")
            except:
                logger.warning("⚠️ Final reminders check: reminders.json not accessible")
            
            logger.info("🚀 Bot startup completed successfully!")
            
            updater.idle()
            
        except Exception as e:
            logger.error(f"❌ Error starting bot polling: {e}")
            # Fallback: попытка повторного запуска через 10 секунд
            logger.info("🔄 Attempting fallback restart in 10 seconds...")
            time.sleep(10)
    updater.start_polling(drop_pending_updates=True)
            logger.info("✅ Bot started successfully (fallback mode)")
    updater.idle()

    except Exception as e:
        logger.error(f"Critical error in main: {e}")
        
if __name__ == "__main__":
    main()
