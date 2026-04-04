# Salon Bot v14

Telegram-бот для автоматизации записи клиентов к мастеру маникюра / в салон красоты.

## Возможности

- Онлайн-запись клиентов на услуги с выбором дня и времени
- Управление расписанием мастера (блокировка дней, просмотр записей)
- Портфолио работ с фото (управление через бот)
- Напоминания клиентам за 24 часа и 2 часа до записи
- Приглашение на повторный визит через 21 день
- Отзывы клиентов после оказания услуги
- Чёрный список нежелательных клиентов
- Экспорт записей в Excel
- Защита от дублей нажатий и rate limiting

## Стек

- Python 3.12
- python-telegram-bot 21.x (polling)
- SQLite
- openpyxl (Excel-экспорт)

## Быстрый старт

```bash
git clone https://github.com/pavel-ai-dev/salon-bot.git
cd salon-bot
pip install -r requirements.txt
cp .env.example .env
# Заполни .env своими данными
python main.py
```

## Настройка

Все параметры задаются через `.env`:

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен Telegram-бота |
| `ADMIN_IDS` | Telegram ID мастера |
| `MASTER_USERNAME` | Имя мастера |
| `MASTER_ADDRESS` | Адрес для напоминаний |
| `TIMEZONE` | Часовой пояс (Europe/Moscow) |

## Структура

```
main.py                — точка входа, фоновые задачи
app/bot/handlers/
  client.py            — флоу клиента (запись, отмена, перенос)
  master.py            — панель мастера (расписание, услуги, портфолио)
app/bot/helpers.py     — утилиты, клавиатуры
app/core/
  settings.py          — настройки из .env
  database.py          — SQLite, миграции
app/models/domain.py   — модели данных
app/services/
  services.py          — бизнес-логика
  excel_worker.py      — экспорт в Excel
```

## Docker

```bash
docker build -t salon-bot .
docker run -d --env-file .env salon-bot
```

## Лицензия

MIT
