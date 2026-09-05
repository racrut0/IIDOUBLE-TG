# ИИ-двойник в Telegram

Telegram-бот с Mini App для создания цифрового двойника пользователя. Бот запоминает стиль общения, ответы, Q&A, оценки ответов и импортированные Telegram-чаты.

## Возможности

- Диалог с ИИ-двойником в стиле пользователя.
- Mini App внутри Telegram.
- Генерация коротких вопросов для обучения.
- Сохранение вопросов и ответов.
- Оценка ответа: `Похоже` или редактирование через `Не похоже`.
- Импорт Telegram-чата из `.json`, `.html` или `.txt`.
- Разделение импорта на мои сообщения и сообщения собеседника.
- Просмотр и удаление данных из памяти.

## Стек

- Python
- python-telegram-bot
- FastAPI
- OpenAI API
- Telegram Web Apps
- GitHub Pages

## Структура

bot.py                основной Telegram-бот
web_backend.py        backend для Mini App
requirements.txt      зависимости Python
config.example.json   пример конфига без ключей
index.html            frontend Mini App для GitHub Pages
webapp/index.html     копия frontend Mini App
```

## Запуск

Установить зависимости:

bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Создать локальный `.env` или `config.json` по примеру `config.example.json`:

env
TG_TOKEN=telegram_bot_token
OPENAI_API_KEY=openai_api_key
OPENAI_MODEL=gpt-4.1-mini
WEBAPP_URL=https://your-github-pages-url/
WEBAPP_API_URL=https://your-backend-url
WEBAPP_VERSION=11
```

Запустить бота:

bash
python bot.py

Запустить backend для Mini App:

bash
uvicorn web_backend:app --host 0.0.0.0 --port 8000

Для работы Mini App backend должен быть доступен по HTTPS. Локально можно использовать ngrok, на сервере - домен с HTTPS.

## Что нельзя заливать в GitHub

text
.env
config.json
memory/
.venv/
*.log
```
