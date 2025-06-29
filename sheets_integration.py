# sheets_integration.py

import os
import json
import logging
from datetime import datetime
import pytz
from typing import Dict, List, Any, Optional
import gspread
from google.oauth2.service_account import Credentials

# Константы
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
logger = logging.getLogger(__name__)

class SheetsManager:
    def __init__(self):
        self.credentials = None
        self.client = None
        self.spreadsheet = None
        self.sheet_id = None
        self.is_initialized = False
        self._init_sheets()
    
    def _init_sheets(self):
        """Инициализация Google Sheets"""
        try:
            # Получаем данные из переменных окружения
            sheets_creds = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
            self.sheet_id = os.environ.get('GOOGLE_SHEETS_ID')
            
            if not sheets_creds or not self.sheet_id:
                logger.warning("Google Sheets credentials or ID not found in environment variables")
                return
            
            # Парсим JSON credentials
            creds_data = json.loads(sheets_creds)
            
            # Настраиваем scopes
            scopes = [
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive'
            ]
            
            # Создаем credentials
            self.credentials = Credentials.from_service_account_info(creds_data, scopes=scopes)
            self.client = gspread.authorize(self.credentials)
            self.spreadsheet = self.client.open_by_key(self.sheet_id)
            
            # Создаем необходимые листы
            self._setup_sheets()
            self.is_initialized = True
            logger.info("Google Sheets integration initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize Google Sheets: {e}")
            self.is_initialized = False
    
    def _setup_sheets(self):
        """Создание и настройка необходимых листов"""
        try:
            # Список необходимых листов с их заголовками
            sheets_config = {
                'Reminders': [
                    'ID', 'Text', 'Time_MSK', 'Type', 'Chat_ID', 'Chat_Name', 
                    'Status', 'Created_At', 'Username', 'Last_Sent', 'Days_Of_Week'
                ],
                'Send_History': [
                    'Timestamp_UTC', 'Timestamp_MSK', 'Reminder_ID', 'Chat_ID', 
                    'Status', 'Error', 'Text_Preview'
                ],
                'Chat_Stats': [
                    'Chat_ID', 'Chat_Name', 'Chat_Type', 'Reminders_Count', 
                    'Last_Activity', 'Members_Count', 'First_Seen'
                ],
                'Operation_Logs': [
                    'Timestamp_UTC', 'Timestamp_MSK', 'Action', 'User_ID', 
                    'Username', 'Chat_ID', 'Details', 'Reminder_ID'
                ]
            }
            
            existing_sheets = [sheet.title for sheet in self.spreadsheet.worksheets()]
            
            for sheet_name, headers in sheets_config.items():
                if sheet_name not in existing_sheets:
                    # Создаем новый лист
                    worksheet = self.spreadsheet.add_worksheet(
                        title=sheet_name, 
                        rows=1000, 
                        cols=len(headers)
                    )
                    # Добавляем заголовки
                    worksheet.append_row(headers)
                    logger.info(f"Created sheet: {sheet_name}")
                else:
                    # Проверяем и обновляем заголовки существующего листа
                    worksheet = self.spreadsheet.worksheet(sheet_name)
                    current_headers = worksheet.row_values(1)
                    if current_headers != headers:
                        worksheet.clear()
                        worksheet.append_row(headers)
                        logger.info(f"Updated headers for sheet: {sheet_name}")
                        
        except Exception as e:
            logger.error(f"Error setting up sheets: {e}")
    
    def log_reminder_action(self, action: str, user_id: int, username: str, 
                          chat_id: int, details: str, reminder_id: int = None):
        """Логирование действий с напоминаниями"""
        if not self.is_initialized:
            return
        
        try:
            worksheet = self.spreadsheet.worksheet('Operation_Logs')
            now_utc = datetime.now(pytz.UTC)
            now_msk = now_utc.astimezone(MOSCOW_TZ)
            
            row = [
                now_utc.strftime('%Y-%m-%d %H:%M:%S'),
                now_msk.strftime('%Y-%m-%d %H:%M:%S'),
                action,
                user_id,
                username or 'Unknown',
                chat_id,
                details,
                reminder_id or ''
            ]
            
            worksheet.append_row(row)
            logger.info(f"Logged action: {action} by {username}")
            
        except Exception as e:
            logger.error(f"Error logging reminder action: {e}")
    
    def sync_reminder(self, reminder: Dict[str, Any], action: str = 'UPDATE'):
        """Синхронизация напоминания с Google Sheets"""
        if not self.is_initialized:
            return
        
        try:
            worksheet = self.spreadsheet.worksheet('Reminders')
            
            # Подготавливаем данные для записи
            row_data = [
                reminder.get('id', ''),
                reminder.get('text', ''),
                reminder.get('time', ''),
                reminder.get('type', ''),
                reminder.get('chat_id', ''),
                reminder.get('chat_name', ''),
                'Active' if action != 'DELETE' else 'Deleted',
                reminder.get('created_at', ''),
                reminder.get('username', ''),
                reminder.get('last_sent', ''),
                str(reminder.get('days_of_week', ''))
            ]
            
            if action == 'CREATE':
                # Добавляем новую запись
                worksheet.append_row(row_data)
            elif action == 'UPDATE' or action == 'DELETE':
                # Ищем существующую запись и обновляем
                records = worksheet.get_all_records()
                row_to_update = None
                
                for i, record in enumerate(records):
                    if str(record.get('ID')) == str(reminder.get('id')):
                        row_to_update = i + 2  # +2 потому что индексы с 1 и есть заголовок
                        break
                
                if row_to_update:
                    # Обновляем существующую строку
                    for col, value in enumerate(row_data, 1):
                        worksheet.update_cell(row_to_update, col, value)
                else:
                    # Если не найдена, добавляем новую
                    worksheet.append_row(row_data)
            
            logger.info(f"Synced reminder {reminder.get('id')} with action {action}")
            
        except Exception as e:
            logger.error(f"Error syncing reminder: {e}")
    
    def log_reminder_sent(self, reminder_id: int, chat_id: int, status: str, 
                         error: str = None, text_preview: str = ''):
        """Логирование отправленных напоминаний"""
        if not self.is_initialized:
            return
        
        try:
            worksheet = self.spreadsheet.worksheet('Send_History')
            now_utc = datetime.now(pytz.UTC)
            now_msk = now_utc.astimezone(MOSCOW_TZ)
            
            row = [
                now_utc.strftime('%Y-%m-%d %H:%M:%S'),
                now_msk.strftime('%Y-%m-%d %H:%M:%S'),
                reminder_id,
                chat_id,
                status,
                error or '',
                text_preview[:50] + '...' if len(text_preview) > 50 else text_preview
            ]
            
            worksheet.append_row(row)
            logger.info(f"Logged reminder sent: {reminder_id} to {chat_id}")
            
        except Exception as e:
            logger.error(f"Error logging reminder sent: {e}")
    
    def update_chat_stats(self, chat_id: int, chat_name: str, chat_type: str, 
                         members_count: int = None):
        """Обновление статистики чатов"""
        if not self.is_initialized:
            return
        
        try:
            worksheet = self.spreadsheet.worksheet('Chat_Stats')
            records = worksheet.get_all_records()
            
            # Ищем существующую запись
            row_to_update = None
            existing_record = None
            
            for i, record in enumerate(records):
                if str(record.get('Chat_ID')) == str(chat_id):
                    row_to_update = i + 2
                    existing_record = record
                    break
            
            now_msk = datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S')
            
            if row_to_update:
                # Обновляем существующую запись
                worksheet.update_cell(row_to_update, 2, chat_name)  # Chat_Name
                worksheet.update_cell(row_to_update, 3, chat_type)  # Chat_Type
                worksheet.update_cell(row_to_update, 5, now_msk)    # Last_Activity
                if members_count is not None:
                    worksheet.update_cell(row_to_update, 6, members_count)  # Members_Count
            else:
                # Создаем новую запись
                row = [
                    chat_id,
                    chat_name,
                    chat_type,
                    0,  # Reminders_Count - будет обновляться отдельно
                    now_msk,  # Last_Activity
                    members_count or 0,  # Members_Count
                    now_msk   # First_Seen
                ]
                worksheet.append_row(row)
            
            logger.info(f"Updated chat stats for {chat_id}")
            
        except Exception as e:
            logger.error(f"Error updating chat stats: {e}")
    
    def update_reminders_count(self, chat_id: int):
        """Обновление количества напоминаний для чата"""
        if not self.is_initialized:
            return
        
        try:
            # Подсчитываем активные напоминания для чата
            reminders_sheet = self.spreadsheet.worksheet('Reminders')
            reminders = reminders_sheet.get_all_records()
            
            active_count = sum(1 for r in reminders 
                             if str(r.get('Chat_ID')) == str(chat_id) 
                             and r.get('Status') == 'Active')
            
            # Обновляем статистику чата
            chat_stats_sheet = self.spreadsheet.worksheet('Chat_Stats')
            records = chat_stats_sheet.get_all_records()
            
            for i, record in enumerate(records):
                if str(record.get('Chat_ID')) == str(chat_id):
                    chat_stats_sheet.update_cell(i + 2, 4, active_count)  # Reminders_Count
                    break
            
            logger.info(f"Updated reminders count for chat {chat_id}: {active_count}")
            
        except Exception as e:
            logger.error(f"Error updating reminders count: {e}")
    
    def backup_all_reminders(self, reminders: List[Dict[str, Any]]):
        """Полное резервное копирование всех напоминаний"""
        if not self.is_initialized:
            return
        
        try:
            worksheet = self.spreadsheet.worksheet('Reminders')
            
            # Очищаем лист (кроме заголовков)
            worksheet.clear()
            headers = [
                'ID', 'Text', 'Time_MSK', 'Type', 'Chat_ID', 'Chat_Name', 
                'Status', 'Created_At', 'Username', 'Last_Sent', 'Days_Of_Week'
            ]
            worksheet.append_row(headers)
            
            # Добавляем все напоминания
            for reminder in reminders:
                row_data = [
                    reminder.get('id', ''),
                    reminder.get('text', ''),
                    reminder.get('time', ''),
                    reminder.get('type', ''),
                    reminder.get('chat_id', ''),
                    reminder.get('chat_name', ''),
                    'Active',
                    reminder.get('created_at', ''),
                    reminder.get('username', ''),
                    reminder.get('last_sent', ''),
                    str(reminder.get('days_of_week', ''))
                ]
                worksheet.append_row(row_data)
            
            logger.info(f"Backed up {len(reminders)} reminders to Google Sheets")
            
        except Exception as e:
            logger.error(f"Error backing up reminders: {e}")

# Глобальный экземпляр
sheets_manager = SheetsManager() 