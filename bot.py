from telegram import KeyboardButton, Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from telegram.constants import ChatAction
from telegram.error import TimedOut, NetworkError

from openai import OpenAI

import html
import json
import os
import re
import traceback
import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode


# ====== CONFIG ======
CONFIG_PATH = Path(__file__).with_name("config.json")
ENV_PATH = Path(__file__).with_name(".env")


def clean_config_dict(raw: dict) -> dict:
    config = {}

    for key, value in raw.items():
        clean_key = str(key).replace("\ufeff", "").strip().replace(" ", "_")

        if isinstance(value, str):
            config[clean_key] = value.strip()
        else:
            config[clean_key] = value

    return config


def load_env_file(path: Path) -> dict:
    if not path.exists():
        return {}

    data = {}

    with open(path, "r", encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key:
                data[key] = value

    return clean_config_dict(data)


def load_config(path: Path) -> dict:
    file_config = {}

    if path.exists():
        with open(path, "r", encoding="utf-8-sig") as f:
            file_config = clean_config_dict(json.load(f))

    env_config = load_env_file(ENV_PATH)
    os_config = clean_config_dict({
        key: value
        for key, value in os.environ.items()
        if key in {
            "TG_TOKEN",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "WEBAPP_URL",
            "WEBAPP_API_URL",
            "WEBAPP_VERSION",
            "WEBAPP_DEV_USER_ID",
        }
    })

    return {**file_config, **env_config, **os_config}


config = load_config(CONFIG_PATH)

print("CONFIG path:", CONFIG_PATH)
print("CONFIG keys:", list(config.keys()))

TG_TOKEN = config.get("TG_TOKEN")
OPENAI_API_KEY = config.get("OPENAI_API_KEY")
MODEL = config.get("OPENAI_MODEL", "gpt-4.1-mini")
WEBAPP_URL = config.get("WEBAPP_URL", "").strip()
WEBAPP_API_URL = config.get("WEBAPP_API_URL", "").strip()
WEBAPP_VERSION = config.get("WEBAPP_VERSION", "11").strip()

print("TG_TOKEN найден:", bool(TG_TOKEN))
print("OPENAI_API_KEY найден:", bool(OPENAI_API_KEY))
print("MODEL:", MODEL)
print("WEBAPP_URL найден:", bool(WEBAPP_URL))
print("WEBAPP_API_URL найден:", bool(WEBAPP_API_URL))
print("WEBAPP_VERSION:", WEBAPP_VERSION)

if not TG_TOKEN:
    raise ValueError("Нет TG_TOKEN в .env или config.json")

if not OPENAI_API_KEY:
    raise ValueError("Нет OPENAI_API_KEY в .env или config.json")


client = OpenAI(api_key=OPENAI_API_KEY)


# ====== НАСТРОЙКИ ======
MEM_DIR = "memory"


# ====== КНОПКИ ======
BTN_START = "▶️ Старт"
BTN_SIMPLE = "💬 Диалог с двойником"
BTN_ADD_QA = "➕ Создать вопрос и ответ"
BTN_GENERATE_Q = "❓ Сгенерировать вопрос"
BTN_TODAY_PATTERN = "📝 Как я сегодня отвечал"
BTN_DELETE_MEMORY = "🧠 Удалить из памяти"
BTN_WEBAPP = "🪟 Mini App"
BTN_HELP = "📋 Функции"
BTN_FEEDBACK_GOOD = "✅ Похоже"
BTN_FEEDBACK_BAD = "❌ Не похоже"
BTN_BACK = "↩️ Назад"


def start_menu():
    return ReplyKeyboardMarkup(
        [[BTN_START]],
        resize_keyboard=True
    )


def main_menu(include_feedback: bool = False):
    launch_url = build_webapp_launch_url()
    webapp_button = (
        KeyboardButton(BTN_WEBAPP, web_app=WebAppInfo(url=launch_url))
        if launch_url
        else BTN_WEBAPP
    )

    rows = [
        [webapp_button],
        [BTN_SIMPLE],
        [BTN_ADD_QA, BTN_GENERATE_Q],
        [BTN_TODAY_PATTERN],
        [BTN_DELETE_MEMORY, BTN_HELP],
    ]

    if include_feedback:
        rows.insert(0, [BTN_FEEDBACK_GOOD, BTN_FEEDBACK_BAD])

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True
    )


def build_webapp_launch_url() -> str:
    if not WEBAPP_URL:
        return ""

    params = {}

    if WEBAPP_VERSION and "v=" not in WEBAPP_URL:
        params["v"] = WEBAPP_VERSION

    params["cache"] = datetime.now().strftime("%Y%m%d%H%M%S")

    if WEBAPP_API_URL and "api=" not in WEBAPP_URL:
        params["api"] = WEBAPP_API_URL

    if not params:
        return WEBAPP_URL

    separator = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{separator}{urlencode(params)}"


def functions_window_text() -> str:
    return """
Функции ИИ-двойника

💬 Диалог с двойником
Пиши обычное сообщение. Двойник отвечает в твоем стиле.

✅ Похоже / ❌ Не похоже
После ответа оцени, похож ли он на тебя. Если не похож, напиши правильный вариант. Это сохранится как паттерн.

📝 Как я сегодня отвечал
Вставь пример своего сегодняшнего ответа. Бот запомнит актуальную манеру общения.

Импорт TG-чата
Вставь экспорт или фрагмент переписки. Бот сохранит сообщения как паттерны без запроса к OpenAI.

➕ Создать вопрос и ответ
Сохраняет ручной Q&A: вопрос и правильный ответ от твоего лица.

❓ Сгенерировать вопрос
Бот сам генерирует вопрос для обучения двойника. Ты отвечаешь, ответ сохраняется.

🧠 Удалить из памяти
Показывает память и дает удалить лишнее. Пример: стиль 2, паттерн 1, qa 3.

API используется только для генерации текста: ответа двойника и вопроса.
""".strip()


def webapp_setup_text() -> str:
    return """
Mini App пока не подключен.

Чтобы кнопка открывала всплывающее приложение в Telegram:
1. Размести папку webapp на HTTPS-хостинге.
2. Скопируй ссылку на index.html.
3. Добавь в .env:
WEBAPP_URL=https://твой-домен/index.html
WEBAPP_API_URL=https://твой-backend-домен
4. Перезапусти бота.

Для полной работы внутри окна нужны две HTTPS-ссылки: сайт Mini App и backend API.
""".strip()


# ====== 20 ВОПРОСОВ ======
BASIC_QUESTIONS = [
    "1/20. Как тебя зовут?",
    "2/20. Сколько тебе лет?",
    "3/20. Где ты живешь?",
    "4/20. Чем ты занимаешься?",
    "5/20. Какая у тебя главная цель сейчас?",
    "6/20. Что ты сейчас строишь, развиваешь или изучаешь?",
    "7/20. Что тебе нравится?",
    "8/20. Что тебе не нравится?",
    "9/20. Что тебя обычно раздражает?",
    "10/20. Как ты обычно общаешься в переписке?",
    "11/20. Как ты отвечаешь, когда тебе пишут коротко?",
    "12/20. Как ты отвечаешь, когда нужно объяснить подробно?",
    "13/20. Какие фразы ты часто используешь?",
    "14/20. Какие слова или фразы тебе не свойственны?",
    "15/20. Как ты ведешь себя, когда злишься?",
    "16/20. Как ты ведешь себя, когда тебе весело?",
    "17/20. Какие у тебя принципы?",
    "18/20. Как ты принимаешь решения?",
    "19/20. Что твоя цифровая копия должна помнить всегда?",
    "20/20. Какой должна быть твоя цифровая копия?"
]


# ====== SYSTEM PROMPT ======
SYSTEM = """
Ты цифровая копия пользователя.

Твоя задача:
- отвечать так, как мог бы ответить этот пользователь
- повторять его стиль, тон, манеру, логику и привычки
- имитировать именно владельца памяти, а не собеседника и не ассистента
- опираться только на данные из анкеты, Q&A, событий дня, памяти и переписки
- использовать сохраненные примеры фраз, сокращений, грубости/мягкости и длины сообщений
- не говорить как ИИ-ассистент
- не говорить "чем могу помочь"
- не говорить "я здесь, чтобы помочь"
- не объяснять, что ты бот
- не предлагать помощь, если пользователь просто пишет бытовое сообщение
- не отвечать от имени ChatGPT, OpenAI, разработчика или помощника
- не выдумывать факты
- если данных мало, отвечай нейтрально и коротко
- если стиль пользователя короткий, отвечай коротко
- если стиль пользователя сухой, не сглаживай его слишком сильно
- не переигрывай
- не будь театральным
- не заканчивай каждый ответ вопросом

Анкета пользователя:
{survey}

Вопросы и ответы пользователя:
{qa}

События дня:
{daily_events}

Как пользователь отвечал сегодня:
{daily_state}

Факты о пользователе:
{user_facts}

Предпочтения пользователя:
{user_preferences}

Стиль общения пользователя:
{user_style}

Примеры удачных и исправленных ответов:
{style_examples}

Паттерны человека:
{patterns}

Краткая память:
{summary}
"""


# ====== ПАМЯТЬ ======
def get_mem_path(uid: str) -> str:
    os.makedirs(MEM_DIR, exist_ok=True)
    return os.path.join(MEM_DIR, f"{uid}.json")


def normalize_mem(mem: dict) -> dict:
    mem.setdefault("user", {})
    mem.setdefault("facts", [])
    mem.setdefault("preferences", [])
    mem.setdefault("style", [])
    mem.setdefault("summary", "")
    mem.setdefault("history", [])
    mem.setdefault("message_count", 0)

    mem.setdefault("survey", {
        "completed": False,
        "index": 0,
        "answers": []
    })

    mem.setdefault("mode", "waiting_start")
    mem.setdefault("pending_question", "")
    mem.setdefault("qa", [])
    mem.setdefault("daily_events", [])
    mem.setdefault("daily_state", [])
    mem.setdefault("patterns", [])
    mem.setdefault("imported_chats", [])
    mem.setdefault("imported_dialogues", [])
    mem.setdefault("last_exchange", {})
    mem.setdefault("feedback", [])
    mem.setdefault("good_examples", [])
    mem.setdefault("correction_examples", [])

    return mem


def load_mem(uid: str) -> dict:
    path = get_mem_path(uid)

    if not os.path.exists(path):
        return normalize_mem({})

    try:
        with open(path, "r", encoding="utf-8") as f:
            return normalize_mem(json.load(f))
    except Exception:
        return normalize_mem({})


def save_mem(uid: str, mem: dict):
    path = get_mem_path(uid)
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(mem, f, ensure_ascii=False, indent=2)

    os.replace(temp_path, path)


def update_user_info(update: Update, mem: dict):
    user = update.effective_user

    mem["user"] = {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "language_code": user.language_code
    }


# ====== HELPERS ======
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def add_unique(items: list, value: str, limit: int = 80):
    value = value.strip()

    if value and value not in items:
        items.append(value)

    return items[-limit:]


def tokenize(text: str) -> set:
    return {
        token
        for token in re.findall(r"[a-zа-яё0-9]{3,}", text.lower())
    }


def select_relevant_items(items: list, query: str, limit: int = 20) -> list:
    if not isinstance(items, list):
        return []

    query_tokens = tokenize(query)

    if not query_tokens:
        return items[-limit:]

    scored = []

    for index, item in enumerate(items):
        if not isinstance(item, str):
            continue

        score = len(tokenize(item) & query_tokens)

        if score:
            scored.append((score, index, item))

    if not scored:
        return items[-limit:]

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _, _, item in scored[:limit]]


def select_relevant_records(records: list, query: str, keys: list, limit: int = 20) -> list:
    if not isinstance(records, list):
        return []

    query_tokens = tokenize(query)

    if not query_tokens:
        return records[-limit:]

    scored = []

    for index, item in enumerate(records):
        if not isinstance(item, dict):
            continue

        text = " ".join(str(item.get(key, "")) for key in keys)
        score = len(tokenize(text) & query_tokens)

        if score:
            scored.append((score, index, item))

    if not scored:
        return records[-limit:]

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _, _, item in scored[:limit]]


def format_survey(mem: dict) -> str:
    answers = mem.get("survey", {}).get("answers", [])
    lines = []

    for item in answers:
        q = item.get("question", "")
        a = item.get("answer", "")
        lines.append(f"{q}\nОтвет: {a}")

    return "\n\n".join(lines[-20:])


def format_qa(mem: dict, query: str = "") -> str:
    items = select_relevant_records(
        mem.get("qa", []),
        query,
        ["question", "answer"],
        40
    )
    lines = []

    for item in items:
        date = item.get("date", "")
        q = item.get("question", "")
        a = item.get("answer", "")
        lines.append(f"{date}\nВопрос: {q}\nОтвет: {a}")

    return "\n\n".join(lines)


def format_daily_events(mem: dict, query: str = "") -> str:
    items = select_relevant_records(
        mem.get("daily_events", []),
        query,
        ["text"],
        30
    )
    lines = []

    for item in items:
        date = item.get("date", "")
        text = item.get("text", "")
        lines.append(f"{date}: {text}")

    return "\n".join(lines)


def format_daily_state(mem: dict, query: str = "") -> str:
    items = select_relevant_records(
        mem.get("daily_state", []),
        query,
        ["text"],
        20
    )
    lines = []

    for item in items:
        date = item.get("date", "")
        text = item.get("text", "")
        lines.append(f"{date}: {text}")

    return "\n".join(lines)


def format_style_examples(mem: dict) -> str:
    lines = []

    for item in mem.get("good_examples", [])[-6:]:
        user_text = item.get("user", "")
        answer = item.get("assistant", "")
        lines.append(f"Удачный пример\nСообщение: {user_text}\nОтвет: {answer}")

    for item in mem.get("correction_examples", [])[-8:]:
        user_text = item.get("user", "")
        correct = item.get("correct_answer", "")
        lines.append(f"Исправленный пример\nСообщение: {user_text}\nКак надо отвечать: {correct}")

    return "\n\n".join(lines)


def format_patterns(mem: dict) -> str:
    return "\n".join(mem.get("patterns", [])[-80:])


def clean_json(text: str) -> str:
    if not text:
        return "{}"

    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```json", "", text)
        text = re.sub(r"^```", "", text)
        text = re.sub(r"```$", "", text)

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1:
        text = text[start:end + 1]

    return text.strip()


def merge_unique(old_list, new_list, limit=50):
    if not isinstance(old_list, list):
        old_list = []

    if not isinstance(new_list, list):
        new_list = []

    for item in new_list:
        if isinstance(item, str):
            item = item.strip()

            if item and item not in old_list:
                old_list.append(item)

    return old_list[-limit:]


# ====== ЛОКАЛЬНАЯ ПАМЯТЬ ======
def extract_local_memory(mem: dict, text: str):
    lower = text.lower()

    fact_triggers = [
        "меня зовут",
        "мне лет",
        "я живу",
        "я из",
        "работаю",
        "учусь",
        "мой город",
        "моя цель",
        "я занимаюсь",
        "я делаю",
    ]

    preference_triggers = [
        "я люблю",
        "я не люблю",
        "мне нравится",
        "мне не нравится",
        "хочу",
        "не хочу",
        "мне нужно",
        "мне надо",
        "запомни",
    ]

    style_triggers = [
        "отвечай коротко",
        "отвечай подробнее",
        "без воды",
        "не переигрывай",
        "пиши проще",
        "пиши на ты",
        "не используй смайлы",
        "не используй эмодзи",
        "не отвечай как ии",
        "не отвечай как агент",
    ]

    if any(t in lower for t in fact_triggers):
        mem["facts"] = add_unique(mem.get("facts", []), text, 100)

    if any(t in lower for t in preference_triggers):
        mem["preferences"] = add_unique(mem.get("preferences", []), text, 100)

    if any(t in lower for t in style_triggers):
        mem["style"] = add_unique(mem.get("style", []), text, 80)

    return mem


def remember_user_pattern(mem: dict, text: str, source: str = "паттерн") -> dict:
    text = text.strip()

    if not text:
        return mem

    if len(text) > 500:
        text = text[:500].rsplit(" ", 1)[0].strip()

    mem["patterns"] = add_unique(
        mem.get("patterns", []),
        f"{source}: {text}",
        120
    )

    mem["style"] = add_unique(
        mem.get("style", []),
        text,
        100
    )

    return mem


def clean_imported_message(text: str) -> str:
    text = html.unescape(str(text))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def flatten_telegram_text(value) -> str:
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts = []

        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))

        return "".join(parts)

    if isinstance(value, dict):
        return str(value.get("text", ""))

    return ""


def normalize_author_name(value: str) -> str:
    value = html.unescape(str(value or "")).strip().lower()
    value = value.lstrip("@")
    value = re.sub(r"\s+", " ", value)
    return value


def build_self_aliases(mem: dict, uid: str = "", owner_name: str = "") -> set[str]:
    aliases = set()

    for item in re.split(r"[,;\n]+", owner_name or ""):
        item = normalize_author_name(item)

        if item:
            aliases.add(item)

    user = mem.get("user", {})

    for key in ["username", "first_name", "last_name"]:
        item = normalize_author_name(user.get(key, ""))

        if item:
            aliases.add(item)

    full_name = normalize_author_name(
        f"{user.get('first_name', '')} {user.get('last_name', '')}"
    )

    if full_name:
        aliases.add(full_name)

    facts = mem.get("facts", [])

    if facts:
        possible_name = normalize_author_name(facts[0])

        if possible_name and not any(char.isdigit() for char in possible_name):
            aliases.add(possible_name)

    if uid:
        aliases.add(str(uid))
        aliases.add(f"user{uid}")

    return aliases


def is_self_author(author: str = "", from_id: str = "", aliases: set[str] | None = None) -> bool:
    aliases = aliases or set()
    author = normalize_author_name(author)
    from_id = normalize_author_name(from_id)

    return bool((author and author in aliases) or (from_id and from_id in aliases))


def make_chat_record(text: str, author: str = "", from_id: str = "", date: str = "", aliases: set[str] | None = None) -> dict | None:
    text = clean_imported_message(text)

    if len(text) < 2:
        return None

    return {
        "date": date,
        "author": clean_imported_message(author),
        "from_id": str(from_id or ""),
        "text": text,
        "role": "me" if is_self_author(author, from_id, aliases) else ("other" if author or from_id else "unknown"),
    }


def extract_json_chat_records(transcript: str, aliases: set[str] | None = None) -> list[dict]:
    try:
        data = json.loads(transcript)
    except json.JSONDecodeError:
        return []

    raw_messages = data.get("messages", data) if isinstance(data, dict) else data

    if not isinstance(raw_messages, list):
        return []

    records = []

    for item in raw_messages:
        if not isinstance(item, dict):
            continue

        if item.get("type") and item.get("type") != "message":
            continue

        record = make_chat_record(
            text=flatten_telegram_text(item.get("text", "")),
            author=item.get("from", ""),
            from_id=item.get("from_id", ""),
            date=item.get("date", ""),
            aliases=aliases,
        )

        if record:
            records.append(record)

    return records


def extract_html_chat_records(transcript: str, aliases: set[str] | None = None) -> list[dict]:
    records = []
    starts = [match.start() for match in re.finditer(r'<div\s+class="[^"]*\bmessage\b[^"]*"', transcript, flags=re.IGNORECASE)]
    current_author = ""

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(transcript)
        block = transcript[start:end]
        author_match = re.search(
            r'<div\s+class="[^"]*\bfrom_name\b[^"]*"[^>]*>(.*?)</div>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if author_match:
            current_author = clean_imported_message(author_match.group(1))

        text_match = re.search(
            r'<div\s+class="[^"]*\btext\b[^"]*"[^>]*>(.*?)</div>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if not text_match:
            continue

        record = make_chat_record(
            text=text_match.group(1),
            author=current_author,
            aliases=aliases,
        )

        if record:
            records.append(record)

    return records


def extract_text_chat_records(transcript: str, aliases: set[str] | None = None) -> list[dict]:
    records = []

    for raw_line in transcript.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        line = re.sub(r"^\[\d{1,2}\.\d{1,2}\.\d{2,4},?\s+\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", line)
        line = re.sub(r"^\d{1,2}\.\d{1,2}\.\d{2,4},?\s+\d{1,2}:\d{2}\s+-\s+", "", line)
        author = ""

        if ": " in line:
            author, line = line.split(": ", 1)

        lower = line.lower()
        skip_markers = [
            "<media omitted>",
            "photo",
            "video",
            "sticker",
            "voice message",
            "joined",
            "left",
            "сообщение удалено",
            "медиафайл",
        ]

        if any(marker in lower for marker in skip_markers):
            continue

        record = make_chat_record(line, author=author, aliases=aliases)

        if record:
            records.append(record)

    return records


def extract_chat_records(transcript: str, aliases: set[str] | None = None) -> list[dict]:
    transcript = transcript.strip()

    records = extract_json_chat_records(transcript, aliases)

    if not records and "<html" in transcript.lower():
        records = extract_html_chat_records(transcript, aliases)

    if not records:
        records = extract_text_chat_records(transcript, aliases)

    return records[-300:]


def extract_chat_messages(transcript: str) -> list[str]:
    return [record["text"] for record in extract_chat_records(transcript)]


def import_telegram_chat_to_memory(mem: dict, transcript: str, uid: str = "", owner_name: str = "") -> tuple[dict, dict]:
    aliases = build_self_aliases(mem, uid, owner_name)
    records = extract_chat_records(transcript, aliases)
    my_messages = [record for record in records if record["role"] == "me"]
    other_messages = [record for record in records if record["role"] == "other"]
    unknown_messages = [record for record in records if record["role"] == "unknown"]

    for record in my_messages[-100:]:
        mem = remember_user_pattern(mem, record["text"], "мое сообщение из Telegram")

    mem.setdefault("imported_chats", [])
    mem["imported_chats"].append({
        "date": now_str(),
        "messages_count": len(records),
        "my_messages_count": len(my_messages),
        "other_messages_count": len(other_messages),
        "unknown_messages_count": len(unknown_messages),
        "self_aliases": sorted(alias for alias in aliases if alias),
        "sample": [
            f"{record['role']}: {record.get('author') or 'unknown'}: {record['text']}"
            for record in records[-20:]
        ],
    })
    mem["imported_chats"] = mem["imported_chats"][-20:]
    mem.setdefault("imported_dialogues", [])
    mem["imported_dialogues"].extend({
        "date": record.get("date", ""),
        "author": record.get("author", ""),
        "from_id": record.get("from_id", ""),
        "role": record["role"],
        "text": record["text"],
    } for record in records[-200:])
    mem["imported_dialogues"] = mem["imported_dialogues"][-800:]

    stats = {
        "total": len(records),
        "mine": len(my_messages),
        "other": len(other_messages),
        "unknown": len(unknown_messages),
    }
    return mem, stats


def apply_survey_answer_to_memory(mem: dict, question_index: int, answer: str):
    facts_indexes = [0, 1, 2, 3, 4, 5, 16, 17, 18, 19]
    preferences_indexes = [6, 7, 8, 13]
    style_indexes = [9, 10, 11, 12, 14, 15]

    if question_index in facts_indexes:
        mem["facts"] = add_unique(mem.get("facts", []), answer, 120)

    if question_index in preferences_indexes:
        mem["preferences"] = add_unique(mem.get("preferences", []), answer, 120)

    if question_index in style_indexes:
        mem["style"] = add_unique(mem.get("style", []), answer, 100)

    return mem


# ====== PROMPTS ======
def normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def is_casual_status_question(text: str) -> bool:
    normalized = normalize_for_compare(text)
    normalized = normalized.strip(" ?!.,")

    return normalized in {
        "как дела",
        "как делишки",
        "как ты",
        "че как",
        "чё как",
        "как оно",
        "как жизнь",
    }


def build_prompt(mem: dict, history: list, user_text: str) -> str:
    history_text = ""

    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        history_text += f"{role}: {content}\n"

    user_facts = "\n".join(select_relevant_items(mem.get("facts", []), user_text, 50))
    user_preferences = "\n".join(select_relevant_items(mem.get("preferences", []), user_text, 50))
    user_style = "\n".join(select_relevant_items(mem.get("style", []), user_text, 40))
    summary = mem.get("summary", "")
    situational_rule = ""

    if is_casual_status_question(user_text):
        situational_rule = """
Ситуация: пользователь спросил обычное "как дела?".
Ответь 1-4 словами, максимально бытово.
Не упоминай учебу, проект, нейросеть, работу или цели, если этого прямо не спросили.
Подходящие форматы: "норм", "да норм", "ну норм", "да по идее".
"""

    return f"""
{SYSTEM.format(
    survey=format_survey(mem),
    qa=format_qa(mem, user_text),
    daily_events=format_daily_events(mem, user_text),
    daily_state=format_daily_state(mem, user_text),
    user_facts=user_facts,
    user_preferences=user_preferences,
    user_style=user_style,
    style_examples=format_style_examples(mem),
    patterns=format_patterns(mem),
    summary=summary
)}

Последняя переписка:
{history_text}

Сообщение:
{user_text}

{situational_rule}

Ответь как цифровая копия владельца памяти. Используй его манеру, длину фраз и лексику.
Не отвечай как помощник. Не объясняй свои действия. Не добавляй сервисные фразы.
"""


def build_self_talk_prompt(mem: dict) -> str:
    user_facts = "\n".join(mem.get("facts", [])[-50:])
    user_preferences = "\n".join(mem.get("preferences", [])[-50:])
    user_style = "\n".join(mem.get("style", [])[-40:])

    return f"""
Сделай короткий внутренний разговор цифровой копии пользователя с самой собой.

Важно:
- это не разговор с ассистентом
- это внутренний диалог человека
- стиль должен быть похож на пользователя
- без пафоса
- без фраз "чем могу помочь"
- 3-4 короткие реплики
- опирайся на данные пользователя

Анкета:
{format_survey(mem)}

Q&A:
{format_qa(mem)}

События дня:
{format_daily_events(mem)}

Текущее состояние и дневник:
{format_daily_state(mem)}

Факты:
{user_facts}

Предпочтения:
{user_preferences}

Стиль:
{user_style}

Краткая память:
{mem.get("summary", "")}

Формат:
Я 1: ...
Я 2: ...
"""


def build_generate_question_prompt(mem: dict) -> str:
    return f"""
Сгенерируй один короткий вопрос для обучения цифровой копии пользователя.

Задача вопроса:
- лучше понять стиль, реакции, привычки и паттерны человека
- постепенно подстраиваться под уже сохраненные данные пользователя
- если памяти мало, спроси простой бытовой вопрос
- если память уже есть, спроси про конкретный паттерн, реакцию или привычку
- вопрос должен быть конкретным и легким для быстрого ответа
- максимум 12 слов
- без списка вариантов
- без пояснений
- только один вопрос
- без кавычек и нумерации

Уже сохраненные Q&A:
{format_qa(mem)}

Факты:
{chr(10).join(mem.get("facts", [])[-30:])}

Предпочтения:
{chr(10).join(mem.get("preferences", [])[-30:])}

Стиль:
{chr(10).join(mem.get("style", [])[-25:])}

Паттерны:
{format_patterns(mem)}
"""


def normalize_generated_question(question: str) -> str:
    lines = [line.strip() for line in question.splitlines() if line.strip()]
    question = lines[0] if lines else "Как ты обычно отвечаешь на обычное приветствие?"
    question = re.sub(r"^[\-\*\d\.\)\s]+", "", question).strip(" \"'«»")

    if len(question) > 140:
        question = question[:140].rsplit(" ", 1)[0].rstrip(" ,.;:-")

    words = question.split()

    if len(words) > 12:
        words = words[:12]
        weak_endings = {"и", "а", "но", "или", "если", "когда", "что", "как", "для", "в", "на", "с", "по", "про", "о", "об"}

        while words and words[-1].lower().strip(" ,.;:-?!") in weak_endings:
            words.pop()

        question = " ".join(words).rstrip(" ,.;:-")

    if question and question[-1] not in "?!.…":
        question += "?"

    return question


# ====== OPENAI ======
def ask_openai(prompt: str, max_tokens: int = 350, temperature: float = 0.7) -> str:
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        max_output_tokens=max_tokens,
        temperature=temperature
    )

    text = getattr(response, "output_text", None)

    if text:
        return text

    return "Не смог ответить."


def update_memory_with_ai(mem: dict, user_text: str, bot_answer: str) -> dict:
    return mem


def analyze_imported_chat(mem: dict, transcript: str) -> dict:
    return {
        "facts": [],
        "preferences": [],
        "style": [],
        "summary": ""
    }


def evaluate_double_test(items: list) -> str:
    return "Тест двойника отключен."


# ====== TELEGRAM SEND ======
async def safe_reply(update: Update, text: str, reply_markup=None):
    try:
        await update.message.reply_text(text, reply_markup=reply_markup)
    except (TimedOut, NetworkError):
        print("Telegram timeout при отправке сообщения")
    except Exception as e:
        print(f"Ошибка отправки Telegram-сообщения: {e}")


async def send_long_message(update: Update, text: str, reply_markup=None):
    max_len = 3500

    for i in range(0, len(text), max_len):
        chunk = text[i:i + max_len]

        try:
            await update.message.reply_text(
                chunk,
                reply_markup=reply_markup if i == 0 else None
            )
        except (TimedOut, NetworkError):
            print("Telegram timeout при отправке длинного сообщения")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Ошибка отправки длинного сообщения: {e}")

        await asyncio.sleep(0.1)


# ====== MEMORY UI HELPERS ======
CATEGORY_ALIASES = {
    "fact": "facts",
    "facts": "facts",
    "факт": "facts",
    "факты": "facts",
    "preference": "preferences",
    "preferences": "preferences",
    "prefs": "preferences",
    "предпочтение": "preferences",
    "предпочтения": "preferences",
    "хочу": "preferences",
    "style": "style",
    "стиль": "style",
    "манера": "style",
    "pattern": "patterns",
    "patterns": "patterns",
    "паттерн": "patterns",
    "паттерны": "patterns",
    "qa": "qa",
    "q&a": "qa",
    "вопрос": "qa",
    "вопросы": "qa",
    "сегодня": "daily_state",
    "день": "daily_state",
    "daily_state": "daily_state",
    "импорт": "imported_chats",
    "импорты": "imported_chats",
    "telegram": "imported_chats",
    "tg": "imported_chats",
}

CATEGORY_TITLES = {
    "facts": "Факты",
    "preferences": "Предпочтения",
    "style": "Стиль",
    "patterns": "Паттерны",
    "qa": "Вопросы и ответы",
    "daily_state": "Как отвечал сегодня",
    "imported_chats": "Импорты Telegram",
}


def resolve_category(value: str) -> str | None:
    return CATEGORY_ALIASES.get(value.lower().strip())


def format_numbered_list(title: str, items: list) -> str:
    if not items:
        return f"{title}: пусто"

    lines = [f"{title}:"]

    for index, item in enumerate(items, 1):
        lines.append(f"{index}. {item}")

    return "\n".join(lines)


def build_memory_overview(mem: dict) -> str:
    return "\n\n".join([
        format_numbered_list("Факты", mem.get("facts", [])),
        format_numbered_list("Предпочтения", mem.get("preferences", [])),
        format_numbered_list("Стиль", mem.get("style", [])),
        format_numbered_list("Паттерны", mem.get("patterns", [])),
        format_numbered_list("Как отвечал сегодня", [
            f"{item.get('date', '')}: {item.get('text', '')}"
            for item in mem.get("daily_state", [])
        ]),
        format_numbered_list("Q&A", [
            f"{item.get('question', '')} -> {item.get('answer', '')}"
            for item in mem.get("qa", [])
        ]),
        format_numbered_list("Импорты Telegram", [
            (
                f"{item.get('date', '')}: всего {item.get('messages_count', 0)}, "
                f"твои {item.get('my_messages_count', 0)}, "
                f"собеседника {item.get('other_messages_count', 0)}, "
                f"не определил {item.get('unknown_messages_count', 0)}"
            )
            for item in mem.get("imported_chats", [])
        ]),
        "Чтобы удалить: напиши раздел и номер. Пример: стиль 2 или qa 1",
    ])


def build_search_results(mem: dict, query: str) -> str:
    blocks = []

    for key in ["facts", "preferences", "style"]:
        items = select_relevant_items(mem.get(key, []), query, 8)
        blocks.append(format_numbered_list(CATEGORY_TITLES[key], items))

    qa_items = select_relevant_records(mem.get("qa", []), query, ["question", "answer"], 6)

    if qa_items:
        lines = ["Q&A:"]

        for index, item in enumerate(qa_items, 1):
            lines.append(f"{index}. Вопрос: {item.get('question', '')}\nОтвет: {item.get('answer', '')}")

        blocks.append("\n".join(lines))

    event_items = select_relevant_records(mem.get("daily_events", []), query, ["text"], 6)

    if event_items:
        lines = ["События:"]

        for index, item in enumerate(event_items, 1):
            lines.append(f"{index}. {item.get('date', '')}: {item.get('text', '')}")

        blocks.append("\n".join(lines))

    state_items = select_relevant_records(mem.get("daily_state", []), query, ["text"], 6)

    if state_items:
        lines = ["Дневник:"]

        for index, item in enumerate(state_items, 1):
            lines.append(f"{index}. {item.get('date', '')}: {item.get('text', '')}")

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def delete_memory_item(mem: dict, category: str, item_number: int) -> str | None:
    items = mem.get(category, [])
    index = item_number - 1

    if index < 0 or index >= len(items):
        return None

    removed = items.pop(index)
    mem[category] = items

    if isinstance(removed, dict):
        if category == "qa":
            return f"{removed.get('question', '')} -> {removed.get('answer', '')}"

        return json.dumps(removed, ensure_ascii=False)

    return str(removed)


# ====== COMMANDS ======
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)

    mem = load_mem(uid)
    update_user_info(update, mem)

    if not mem["survey"]["completed"]:
        mem["mode"] = "waiting_start"
        save_mem(uid, mem)

        await safe_reply(
            update,
            "нажми старт, чтобы создать свою цифровую копию",
            reply_markup=start_menu()
        )
        return

    mem["mode"] = "chat"
    save_mem(uid, mem)

    await safe_reply(
        update,
        "готово. выбери режим",
        reply_markup=main_menu()
    )


async def profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mem = load_mem(uid)

    text = json.dumps(mem, ensure_ascii=False, indent=2)
    await send_long_message(update, text)


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    path = get_mem_path(uid)

    if os.path.exists(path):
        os.remove(path)

    await safe_reply(
        update,
        "память очищена. напиши /start",
        reply_markup=ReplyKeyboardRemove()
    )


async def memory_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mem = load_mem(uid)
    await send_long_message(update, build_memory_overview(mem), reply_markup=main_menu())


async def facts_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mem = load_mem(uid)
    await send_long_message(
        update,
        format_numbered_list("Факты", mem.get("facts", [])),
        reply_markup=main_menu()
    )


async def preferences_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mem = load_mem(uid)
    await send_long_message(
        update,
        format_numbered_list("Предпочтения", mem.get("preferences", [])),
        reply_markup=main_menu()
    )


async def style_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mem = load_mem(uid)
    await send_long_message(
        update,
        format_numbered_list("Стиль", mem.get("style", [])),
        reply_markup=main_menu()
    )


async def remember_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mem = load_mem(uid)
    args = ctx.args or []

    if not args:
        await safe_reply(
            update,
            "пример: /remember факт я люблю короткие ответы",
            reply_markup=main_menu()
        )
        return

    category = resolve_category(args[0])

    if category:
        value = " ".join(args[1:]).strip()
    else:
        category = "facts"
        value = " ".join(args).strip()

    if not value:
        await safe_reply(update, "напиши текст, который нужно запомнить", reply_markup=main_menu())
        return

    mem[category] = add_unique(mem.get(category, []), value, 120)
    save_mem(uid, mem)

    await safe_reply(update, f"сохранил в раздел: {CATEGORY_TITLES[category]}", reply_markup=main_menu())


async def forget_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mem = load_mem(uid)
    args = ctx.args or []

    if len(args) < 2:
        await safe_reply(update, "пример: /forget факт 2", reply_markup=main_menu())
        return

    category = resolve_category(args[0])

    if not category:
        await safe_reply(update, "разделы: факт, предпочтения, стиль, паттерн, qa, сегодня, ответ", reply_markup=main_menu())
        return

    try:
        index = int(args[1]) - 1
    except ValueError:
        await safe_reply(update, "номер должен быть числом", reply_markup=main_menu())
        return

    removed = delete_memory_item(mem, category, index + 1)

    if removed is None:
        await safe_reply(update, "такого номера нет", reply_markup=main_menu())
        return

    save_mem(uid, mem)

    await safe_reply(update, f"удалил: {removed}", reply_markup=main_menu())


async def mode_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, "режимы ответа отключены", reply_markup=main_menu())


async def search_memory_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mem = load_mem(uid)
    query = " ".join(ctx.args or []).strip()

    if not query:
        await safe_reply(update, "пример: /search_memory проект", reply_markup=main_menu())
        return

    await send_long_message(update, build_search_results(mem, query), reply_markup=main_menu())


async def import_chat_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mem = load_mem(uid)
    transcript = " ".join(ctx.args or []).strip()

    if not transcript:
        await safe_reply(
            update,
            "вставь фрагмент после команды: /import_chat текст переписки",
            reply_markup=main_menu()
        )
        return

    await safe_reply(update, "анализирую переписку...")

    try:
        data = await asyncio.to_thread(analyze_imported_chat, mem, transcript)
        mem["facts"] = merge_unique(mem.get("facts", []), data.get("facts", []), 120)
        mem["preferences"] = merge_unique(mem.get("preferences", []), data.get("preferences", []), 120)
        mem["style"] = merge_unique(mem.get("style", []), data.get("style", []), 100)

        summary = data.get("summary", "")

        if isinstance(summary, str) and summary.strip():
            mem["summary"] = summary.strip()[:1500]

        save_mem(uid, mem)
        await safe_reply(update, "импортировал полезное в память", reply_markup=main_menu())
    except Exception as e:
        await safe_reply(update, f"не смог импортировать: {e}", reply_markup=main_menu())


# ====== SURVEY ======
async def start_survey(update: Update, mem: dict, uid: str):
    survey = mem["survey"]
    index = survey.get("index", 0)

    if index >= len(BASIC_QUESTIONS):
        survey["completed"] = True
        mem["mode"] = "chat"
        save_mem(uid, mem)

        await safe_reply(
            update,
            "опрос уже закончен. выбери режим",
            reply_markup=main_menu()
        )
        return

    mem["mode"] = "survey"
    save_mem(uid, mem)

    await safe_reply(
        update,
        "начинаем. отвечай на вопросы по одному",
        reply_markup=ReplyKeyboardRemove()
    )

    await safe_reply(update, BASIC_QUESTIONS[index])


async def handle_survey(update: Update, mem: dict, uid: str, text: str):
    survey = mem["survey"]
    index = survey.get("index", 0)

    if survey.get("completed"):
        mem["mode"] = "chat"
        save_mem(uid, mem)

        await safe_reply(
            update,
            "опрос уже закончен. выбери режим",
            reply_markup=main_menu()
        )
        return

    if index >= len(BASIC_QUESTIONS):
        survey["completed"] = True
        mem["mode"] = "chat"
        save_mem(uid, mem)

        await safe_reply(
            update,
            "опрос закончен. выбери режим",
            reply_markup=main_menu()
        )
        return

    question = BASIC_QUESTIONS[index]

    survey["answers"].append({
        "question": question,
        "answer": text
    })

    mem = apply_survey_answer_to_memory(mem, index, text)

    index += 1
    survey["index"] = index

    if index >= len(BASIC_QUESTIONS):
        survey["completed"] = True
        mem["mode"] = "chat"
        save_mem(uid, mem)

        await safe_reply(
            update,
            "опрос закончен. копия создана. теперь выбери режим",
            reply_markup=main_menu()
        )
        return

    save_mem(uid, mem)
    await safe_reply(update, BASIC_QUESTIONS[index])


# ====== EXTRA FLOWS ======
async def handle_today_pattern(update: Update, mem: dict, uid: str, text: str):
    mem["daily_state"].append({
        "date": now_str(),
        "text": text
    })

    mem["daily_state"] = mem["daily_state"][-90:]
    mem = remember_user_pattern(mem, text, "сегодня")
    mem["mode"] = "chat"
    save_mem(uid, mem)

    await safe_reply(update, "запомнил, как ты сегодня отвечал", reply_markup=main_menu())


async def handle_feedback_good(update: Update, mem: dict, uid: str):
    last = mem.get("last_exchange", {})

    if not last:
        await safe_reply(update, "пока нет ответа для оценки", reply_markup=main_menu())
        return

    item = {
        "date": now_str(),
        "user": last.get("user", ""),
        "assistant": last.get("assistant", "")
    }

    mem["feedback"].append({**item, "rating": "good"})
    mem["good_examples"].append(item)
    mem = remember_user_pattern(mem, last.get("assistant", ""), "удачный ответ")
    mem["feedback"] = mem["feedback"][-120:]
    mem["good_examples"] = mem["good_examples"][-40:]
    save_mem(uid, mem)

    await safe_reply(update, "сохранил как удачный пример", reply_markup=main_menu())


async def handle_feedback_bad(update: Update, mem: dict, uid: str):
    if not mem.get("last_exchange"):
        await safe_reply(update, "пока нет ответа для исправления", reply_markup=main_menu())
        return

    mem["mode"] = "feedback_correction"
    save_mem(uid, mem)

    await safe_reply(update, "напиши, как надо было ответить")


async def handle_feedback_correction(update: Update, mem: dict, uid: str, text: str):
    last = mem.get("last_exchange", {})

    if not last:
        mem["mode"] = "chat"
        save_mem(uid, mem)
        await safe_reply(update, "ответ для исправления потерялся", reply_markup=main_menu())
        return

    if normalize_for_compare(text) == normalize_for_compare(last.get("assistant", "")):
        await safe_reply(
            update,
            "ты не изменил ответ. напиши правильный вариант, иначе я снова запомню плохой",
            reply_markup=main_menu()
        )
        return

    item = {
        "date": now_str(),
        "user": last.get("user", ""),
        "bad_answer": last.get("assistant", ""),
        "correct_answer": text
    }

    mem["feedback"].append({**item, "rating": "corrected"})
    mem["correction_examples"].append(item)
    mem["qa"].append({
        "date": now_str(),
        "question": last.get("user", ""),
        "answer": text
    })
    mem = remember_user_pattern(mem, text, "исправленный ответ")

    mem["feedback"] = mem["feedback"][-120:]
    mem["correction_examples"] = mem["correction_examples"][-50:]
    mem["qa"] = mem["qa"][-120:]
    mem["mode"] = "chat"
    save_mem(uid, mem)

    await safe_reply(update, "исправление сохранил", reply_markup=main_menu())


async def handle_memory_delete(update: Update, mem: dict, uid: str, text: str):
    parts = text.split()

    if len(parts) < 2:
        await safe_reply(update, "напиши раздел и номер. пример: стиль 2 или qa 1")
        return

    category = resolve_category(parts[0])

    if not category:
        await safe_reply(update, "разделы: факт, предпочтение, стиль, паттерн, qa, сегодня, ответ")
        return

    try:
        item_number = int(parts[1])
    except ValueError:
        await safe_reply(update, "номер должен быть числом")
        return

    removed = delete_memory_item(mem, category, item_number)

    if removed is None:
        await safe_reply(update, "такого номера нет")
        return

    mem["mode"] = "chat"
    save_mem(uid, mem)

    await safe_reply(update, f"удалил: {removed}", reply_markup=main_menu())


async def handle_generate_question(update: Update, mem: dict, uid: str):
    prompt = build_generate_question_prompt(mem)
    raw_question = await asyncio.to_thread(ask_openai, prompt, 80, 0.75)
    question = normalize_generated_question(raw_question)

    mem["pending_question"] = question
    mem["mode"] = "qa_answer"
    save_mem(uid, mem)

    await safe_reply(update, f"{question}\n\nнапиши свой ответ, я сохраню это как Q&A")


# ====== BUTTONS ======
async def handle_button(update: Update, mem: dict, uid: str, text: str) -> bool:
    if text == BTN_BACK:
        mem["mode"] = "chat"
        save_mem(uid, mem)

        await safe_reply(update, "вернулся в меню", reply_markup=main_menu())
        return True

    if text == BTN_FEEDBACK_GOOD:
        await handle_feedback_good(update, mem, uid)
        return True

    if text == BTN_FEEDBACK_BAD:
        await handle_feedback_bad(update, mem, uid)
        return True

    if text == BTN_START:
        if mem["survey"]["completed"]:
            mem["mode"] = "chat"
            save_mem(uid, mem)

            await safe_reply(
                update,
                "опрос уже пройден. выбери режим",
                reply_markup=main_menu()
            )
            return True

        await start_survey(update, mem, uid)
        return True

    if text == BTN_SIMPLE:
        mem["mode"] = "chat"
        save_mem(uid, mem)

        await safe_reply(
            update,
            "пиши сообщение, отвечу как двойник",
            reply_markup=main_menu()
        )
        return True

    if text == BTN_ADD_QA:
        mem["mode"] = "qa_question"
        mem["pending_question"] = ""
        save_mem(uid, mem)

        await safe_reply(update, "напиши вопрос, который нужно сохранить")
        return True

    if text == BTN_GENERATE_Q:
        await handle_generate_question(update, mem, uid)
        return True

    if text == BTN_TODAY_PATTERN:
        mem["mode"] = "today_pattern"
        save_mem(uid, mem)

        await safe_reply(update, "вставь пример того, как ты сегодня отвечал")
        return True

    if text == BTN_DELETE_MEMORY:
        mem["mode"] = "memory_delete"
        save_mem(uid, mem)

        await send_long_message(update, build_memory_overview(mem), reply_markup=main_menu())
        return True

    if text == BTN_WEBAPP:
        await safe_reply(update, webapp_setup_text(), reply_markup=main_menu())
        return True

    if text == BTN_HELP:
        await send_long_message(update, functions_window_text(), reply_markup=main_menu())
        return True

    return False


# ====== MODES ======
async def handle_qa_question(update: Update, mem: dict, uid: str, text: str):
    mem["pending_question"] = text
    mem["mode"] = "qa_answer"
    save_mem(uid, mem)

    await safe_reply(update, "теперь напиши ответ на этот вопрос")


async def handle_qa_answer(update: Update, mem: dict, uid: str, text: str):
    question = mem.get("pending_question", "").strip()

    if not question:
        mem["mode"] = "qa_question"
        save_mem(uid, mem)

        await safe_reply(update, "вопрос потерялся. напиши вопрос заново")
        return

    mem["qa"].append({
        "date": now_str(),
        "question": question,
        "answer": text
    })
    mem = remember_user_pattern(mem, text, "ответ в Q&A")

    mem["pending_question"] = ""
    mem["mode"] = "chat"

    save_mem(uid, mem)

    await safe_reply(
        update,
        "сохранил вопрос и ответ",
        reply_markup=main_menu()
    )


async def handle_day_events(update: Update, mem: dict, uid: str, text: str):
    mem["daily_events"].append({
        "date": now_str(),
        "text": text
    })

    mem["daily_events"] = mem["daily_events"][-150:]
    mem["mode"] = "chat"

    save_mem(uid, mem)

    await safe_reply(
        update,
        "события дня сохранил",
        reply_markup=main_menu()
    )


async def handle_chat(update: Update, mem: dict, uid: str, text: str):
    mem = extract_local_memory(mem, text)
    mem = remember_user_pattern(mem, text, "сообщение пользователя")

    full_history = mem.get("history", [])
    recent_history = full_history[-10:]

    prompt = build_prompt(
        mem=mem,
        history=recent_history,
        user_text=text
    )

    answer = await asyncio.to_thread(
        ask_openai,
        prompt,
        60 if is_casual_status_question(text) else 400,
        0.45 if is_casual_status_question(text) else 0.7
    )

    full_history.append({
        "role": "user",
        "content": text
    })

    full_history.append({
        "role": "assistant",
        "content": answer
    })

    mem["history"] = full_history
    mem["message_count"] = mem.get("message_count", 0) + 1
    mem["last_exchange"] = {
        "date": now_str(),
        "user": text,
        "assistant": answer
    }

    save_mem(uid, mem)

    await send_long_message(update, answer, reply_markup=main_menu(include_feedback=True))


# ====== MAIN HANDLER ======
async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        uid = str(update.effective_user.id)
        text = update.message.text.strip()

        try:
            await ctx.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING
            )
        except Exception:
            pass

        mem = load_mem(uid)
        update_user_info(update, mem)

        if await handle_button(update, mem, uid, text):
            return

        if mem.get("mode") == "waiting_start":
            await safe_reply(
                update,
                "нажми кнопку старт",
                reply_markup=start_menu()
            )
            return

        if not mem["survey"]["completed"] or mem.get("mode") == "survey":
            await handle_survey(update, mem, uid, text)
            return

        mode = mem.get("mode", "chat")

        if mode == "qa_question":
            await handle_qa_question(update, mem, uid, text)
            return

        if mode == "qa_answer":
            await handle_qa_answer(update, mem, uid, text)
            return

        if mode == "feedback_correction":
            await handle_feedback_correction(update, mem, uid, text)
            return

        if mode == "today_pattern":
            await handle_today_pattern(update, mem, uid, text)
            return

        if mode == "memory_delete":
            await handle_memory_delete(update, mem, uid, text)
            return

        await handle_chat(update, mem, uid, text)

    except Exception as e:
        traceback.print_exc()

        error_text = str(e)

        if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
            await safe_reply(
                update,
                "лимит OpenAI API закончился или слишком много запросов. попробуй позже"
            )
        elif "Timed out" in error_text or "TimedOut" in error_text:
            await safe_reply(
                update,
                "Telegram долго отвечал. попробуй еще раз"
            )
        else:
            await safe_reply(update, f"ошибка: {e}")


def main():
    app = (
        ApplicationBuilder()
        .token(TG_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
