# 🚀 Инструкции для деплоя с автовосстановлением v2.0

## 📋 Что было изменено в последнем коммите:

### ✅ Критические исправления:
- **Автовосстановление напоминаний** при запуске бота
- **Периодическая синхронизация** напоминаний каждые 2 часа
- **Экстренное восстановление** при отсутствии активных заданий
- **Детальное логирование** всех процессов восстановления

### 🆕 Новые функции:
- `ensure_reminders_file()` - проверка и восстановление при запуске
- `auto_sync_reminders()` - автосинхронизация с детальным логированием
- `check_active_jobs()` - диагностика активных заданий
- Команда `/status` с uptime и полной диагностикой

## 🏗️ Деплой на Render

### 1. Обновление кода:
1. **Зайдите в Render Dashboard** → ваш проект
2. **Settings** → подключен ли GitHub репозиторий WarSaler/telegram-repeat-bot
3. **Manual Deploy** → Deploy latest commit (83da33f)
4. **Или настройте Auto-Deploy** для автоматического деплоя при push

### 2. Проверка переменных окружения:
Убедитесь что настроены:
```
BOT_TOKEN=your_telegram_bot_token  
GOOGLE_SHEETS_ID=your_google_sheets_id
GOOGLE_SHEETS_CREDENTIALS=your_service_account_json_content
PORT=8000
BASE_URL=https://your-app-name.onrender.com
```

### 3. Мониторинг логов:
После деплоя следите за логами:
```
🔧 Checking subscribed_chats.json...
🔧 Checking reminders.json...
📋 Scheduling all reminders...
🔍 Final status check: X reminder jobs active
📱 Final chats check: Y subscribed chats
📋 Final reminders check: Z reminders loaded
🚀 Bot startup completed successfully!
```

## 🧪 Тестирование автовосстановления

### Сценарий 1: Полное восстановление
1. **Перезапустите бот** на Render
2. **Проверьте логи** - должно быть автовосстановление
3. **Введите `/status`** - проверьте количество напоминаний и заданий
4. **Введите `/restore_reminders`** - протестируйте ручное восстановление

### Сценарий 2: Проверка активных заданий
```
/status
```
**Ожидаемый результат:**
```
🤖 Статус бота
⏰ 2024-06-30 15:30:45 MSK
⏱️ Работает: 0ч 5м

📋 Локальные данные:
• Напоминания: 15
  📅 Разовых: 3
  🔄 Ежедневных: 7
  📆 Еженедельных: 5
• Подписанные чаты: 8

⚙️ Планировщик заданий:
• Активные задания: 15
• Состояние: ✅ Работает

📊 Google Sheets:
• Статус: ✅ Подключен
• Детали: Готов к работе
```

### Сценарий 3: Восстановление данных
```
/restore_reminders
```
**Ожидаемый результат:**
```
✅ Восстановление завершено успешно!

📋 Восстановлено напоминаний: 15
📅 Разовых: 3
🔄 Ежедневных: 7
📆 Еженедельных: 5

📱 Подписанные чаты:
✅ Восстановлено чатов: 8

⏰ Все напоминания перепланированы и активны!
```

## 🔍 Что искать в логах

### ✅ Успешные сообщения:
```
✅ Found X existing reminders
✅ Reminders status: X reminders ready for scheduling
✅ Successfully restored X reminders from Google Sheets
📊 Active reminder jobs: X
🚀 Bot startup completed successfully!
```

### ⚠️ Предупреждения (не критичны):
```
⚠️ Reminders status: starting with empty reminders list
💡 TIP: Use /restore_reminders command to recover data from Google Sheets
📵 Google Sheets not available for reminders sync
```

### 🚨 Критичные ошибки:
```
🚨 CRITICAL: No active reminder jobs scheduled!
🚨 CRITICAL: No local reminders AND auto-sync failed!
🚨 CRITICAL ERROR: No reminders available after auto-sync failure!
```

## 🕐 Автоматические процессы

### Расписание автосинхронизации:
- **Каждый час:** синхронизация подписанных чатов
- **Каждые 2 часа:** синхронизация напоминаний с перепланированием
- **Каждые 5 минут:** ping для предотвращения засыпания

### Что происходит при автосинхронизации:
```
🔄 Starting reminders auto-sync at 15:30 MSK
📋 Current local reminders: 12
🔄 Auto-sync: Updated reminders 12 → 15
✅ Reminders rescheduled after auto-sync at 15:30 MSK
📊 Active jobs after auto-sync: 15
```

## 🎯 Результат тестирования

После успешного деплоя и тестирования:

1. **Бот полностью защищён** от потери напоминаний
2. **Автоматическое восстановление** работает при любых сбоях
3. **Команда `/status`** показывает полную диагностику
4. **Команда `/restore_reminders`** восстанавливает все данные
5. **Периодическая синхронизация** предотвращает потерю данных

**🎉 Теперь бот никогда не потеряет напоминания!** 