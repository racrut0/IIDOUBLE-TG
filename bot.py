from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, BaseUpdateProcessor, MessageHandler, CommandHandler, ContextTypes, filters
from telegram.constants import ChatAction
from telegram.error import TimedOut, NetworkError

import hashlib
import sys
import services
from storage import MemoryStore
from personalization import build_request, refresh_style_profile, owner_rules, clip, summarize_history
from llm import ModelGateway
from spending import SpendingController
from owner_memory import catalog
from questions import build_question_request, clean_question
from retrieval import dialogue_examples
import telegram_workflows

import html
import json
import os
import re
import traceback
import asyncio
import weakref
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode


# ====== CONFIG ======
class PerUserUpdateProcessor(BaseUpdateProcessor):
    """Keep survey/feedback order per owner without blocking other owners."""
    def __init__(self, max_concurrent_updates=16):
        super().__init__(max_concurrent_updates)
        self.locks = weakref.WeakValueDictionary()

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self, update, coroutine):
        uid = getattr(getattr(update, "effective_user", None), "id", "anonymous")
        lock = self.locks.get(uid)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[uid] = lock
        async with lock:
            await coroutine


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
            "MEMORY_DB_PATH", "CONTEXT_TOKEN_BUDGET", "MAX_OUTPUT_TOKENS",
            "API_BUDGET_USD", "API_DAILY_BUDGET_USD", "API_DAILY_REQUEST_LIMIT", "API_USER_DAILY_REQUEST_LIMIT",
        }
    })

    return {**file_config, **env_config, **os_config}


config = load_config(CONFIG_PATH)

TG_TOKEN = config.get("TG_TOKEN", "")
OPENAI_API_KEY = config.get("OPENAI_API_KEY", "")
MODEL = config.get("OPENAI_MODEL", "gpt-6-luna")
WEBAPP_URL = config.get("WEBAPP_URL", "").strip()
WEBAPP_API_URL = config.get("WEBAPP_API_URL", "").strip()
WEBAPP_VERSION = config.get("WEBAPP_VERSION", "12").strip()
CONTEXT_TOKEN_BUDGET = int(config.get("CONTEXT_TOKEN_BUDGET", 2200))
MAX_OUTPUT_TOKENS = int(config.get("MAX_OUTPUT_TOKENS", 240))
_store = None


def get_store():
    global _store
    if _store is None:
        db_path = Path(config.get("MEMORY_DB_PATH", "memory/twin.sqlite3"))
        if not db_path.is_absolute():
            db_path = Path(__file__).parent / db_path
        _store = MemoryStore(db_path, legacy_dir=Path(__file__).parent / "memory")
    return _store


spending = SpendingController(get_store, config.get("API_BUDGET_USD", 4), config.get("API_DAILY_BUDGET_USD", 0.5),
    config.get("API_DAILY_REQUEST_LIMIT", 200), config.get("API_USER_DAILY_REQUEST_LIMIT", 100))
gateway = ModelGateway(OPENAI_API_KEY, MODEL, CONTEXT_TOKEN_BUDGET, spending, MAX_OUTPUT_TOKENS)


# ====== НАСТРОЙКИ ======
MEM_DIR = str(Path(__file__).parent / "memory")


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
BTN_TRAIN = "🎭 Научи отвечать как я"
BTN_SKIP_TRAIN = "⏭ Пропустить ситуацию"
BTN_BUDGET = "💰 Бюджет API"
BTN_IMPORT_CONFIRM = "✅ Подтвердить импорт"
BTN_IMPORT_ARCHIVE = "📁 Только архив"
BTN_IMPORT_CANCEL = "❎ Отменить импорт"


def start_menu():
    return ReplyKeyboardMarkup(
        [[BTN_START]],
        resize_keyboard=True
    )


def main_menu(include_feedback: bool = False):
    rows = [
        [BTN_WEBAPP],
        [BTN_SIMPLE],
        [BTN_ADD_QA, BTN_GENERATE_Q],
        [BTN_TRAIN, BTN_BUDGET],
        [telegram_workflows.PEOPLE, telegram_workflows.BENCHMARK],
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


def webapp_menu():
    launch_url = build_webapp_launch_url()
    if not launch_url:
        return None
    # Reply-keyboard web_app buttons do not receive Telegram initData.
    # An inline button supplies the signed identity required by our backend.
    return InlineKeyboardMarkup([[InlineKeyboardButton("Открыть Mini App", web_app=WebAppInfo(url=launch_url))]])


async def webapp_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE = None):
    markup = webapp_menu()
    if markup is None:
        await safe_reply(update, webapp_setup_text(), reply_markup=main_menu())
        return
    await safe_reply(update, "Открой двойника кнопкой ниже:", reply_markup=markup)


def functions_window_text() -> str:
    return """
Функции ИИ-двойника

💬 Диалог с двойником
Пиши обычное сообщение. Двойник отвечает в твоем стиле.

✅ Похоже / ❌ Не похоже
После ответа оцени, похож ли он на тебя. Если не похож, напиши правильный вариант. Сохранится ситуация и твой вариант ответа.

📝 Как я сегодня отвечал
Вставь пример своего сегодняшнего ответа. Бот запомнит актуальную манеру общения.

Импорт TG-чата
Пришли экспорт или фрагмент переписки. Проверь участников и примеры, затем подтверди импорт.
До подтверждения файл не участвует в обучении. API-токены не расходуются.

➕ Создать вопрос и ответ
Сохраняет ручной Q&A: вопрос и правильный ответ от твоего лица.

❓ Сгенерировать вопрос
Бот задаёт вопрос по известной детали твоей жизни. Ответ сохраняется, следующие вопросы уточняют его.

🎭 Научи отвечать как я
Семь ситуаций: напиши свои ответы. Сессия работает локально и не расходует API-токены.

💰 Бюджет API
Показывает расход приложения и остаток его лимита. Сброс памяти не сбрасывает расход.

👥 Собеседники
Выбери человека, тип отношений и его переписки. История и примеры учитываются отдельно.

🧪 Проверка сходства
20 постоянных ситуаций из переписки. Подготовка и твои оценки бесплатны, получение каждого ответа — API.

🧠 Удалить из памяти
Показывает память и дает удалить лишнее. Пример: стиль 2, паттерн 1, qa 3.

API используется для генерации текста: ответа двойника, вопроса и ответа в проверке сходства.
""".strip()


def webapp_setup_text() -> str:
    return """
Mini App пока не подключен.

Для быстрого запуска проекта на Windows:
1. Закрой окно, в котором запущен bot.py.
2. В папке проекта открой Start Mini App.cmd.
3. Дождись сообщения «Бот запущен», отправь /start, нажми Mini App и затем «Открыть Mini App» под сообщением бота.

Бот, интерфейс и временный HTTPS-адрес запускаются вместе. Окно запуска должно оставаться открытым.
Для постоянного размещения настрой WEBAPP_URL и WEBAPP_API_URL по README.md.
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


# ====== ПАМЯТЬ ======
def get_mem_path(uid: str) -> str:
    os.makedirs(MEM_DIR, exist_ok=True)
    return os.path.join(MEM_DIR, f"{uid}.json")


def normalize_mem(mem: dict) -> dict:
    defaults = {
        "user": {}, "facts": [], "preferences": [], "style": [], "style_rules": [],
        "summary": "", "style_profile": "", "history": [], "message_count": 0,
        "survey": {"completed": False, "index": 0, "answers": []},
        "mode": "waiting_start", "pending_question": "", "pending_question_id": "", "qa": [], "daily_events": [],
        "question_state": {}, "generated_questions": [],
        "reaction_examples": [], "training_session": {}, "training_round": 0, "memory_controls": [], "owner_facts": [],
        "pending_feedback_id": "",
        "pending_import_token": "",
        "pending_import_revision": 0, "pending_import_participant": "",
        "daily_state": [], "patterns": [], "training_samples": [], "examples": [], "import_facts": [],
        "imported_chats": [], "imported_dialogues": [], "last_exchange": {},
        "feedback": [], "good_examples": [], "correction_examples": [],
        "interlocutors": [], "active_interlocutor_id": "", "conversation_summaries": {},
        "ui_people": {}, "ui_benchmark": {},
    }
    for key, value in defaults.items():
        mem.setdefault(key, value)
    return mem


def load_mem(uid: str) -> dict:
    return normalize_mem(get_store().load(uid))


def save_mem(uid: str, mem: dict):
    get_store().save(uid, mem)


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
    # Storage is lossless. Only the model context has a token budget.
    return items


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
    if source == "сообщение пользователя" or not text.strip():
        return mem
    mem.setdefault("training_samples", []).append({"text": text.strip(), "source": source})
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
    author, from_id = normalize_author_name(author), normalize_author_name(from_id)
    id_aliases = {alias for alias in aliases if re.fullmatch(r"(?:user)?[0-9]+", alias)}
    if from_id and id_aliases:
        return from_id in id_aliases
    return bool((author and author in aliases) or (from_id and from_id in aliases))


def make_chat_record(text: str, author: str = "", from_id: str = "", date: str = "", aliases: set[str] | None = None) -> dict | None:
    text = clean_imported_message(text)
    if not text:
        return None
    return {"date": date, "author": clean_imported_message(author), "from_id": str(from_id or ""),
            "text": text, "role": "me" if is_self_author(author, from_id, aliases) else ("other" if author or from_id else "unknown")}


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
        if not isinstance(item, dict) or item.get("type", "message") != "message":
            continue
        record = make_chat_record(flatten_telegram_text(item.get("text", "")), item.get("from", ""),
                                  item.get("from_id", ""), item.get("date", ""), aliases)
        if record:
            record.update(message_id=str(item.get("id", "")), reply_to=str(item.get("reply_to_message_id", "")),
                          forwarded=bool(item.get("forwarded_from")))
            records.append(record)
    return records


def extract_html_chat_records(transcript: str, aliases: set[str] | None = None) -> list[dict]:
    records = []
    starts = [match.start() for match in re.finditer(r'<div\s+class="[^"]*\bmessage\b[^"]*"', transcript, flags=re.IGNORECASE)]
    current_author = ""

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(transcript)
        block = transcript[start:end]
        forwarded = re.search(r'<div\s+class="[^"]*\bforwarded\b', block, re.I)
        header = block[:forwarded.start()] if forwarded else block
        author_match = re.search(
            r'<div\s+class="[^"]*\bfrom_name\b[^"]*"[^>]*>(.*?)</div>',
            header,
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
            record["forwarded"] = bool(forwarded)
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
    if not transcript:
        return []
    if transcript.startswith(("{", "[")):
        try:
            payload = json.loads(transcript)
        except json.JSONDecodeError as exc:
            # A timestamped TXT export also starts with '['.
            if not re.match(r"^\[\d{1,2}\.", transcript):
                raise ValueError("Повреждён JSON экспорта. Загрузи исходный файл целиком.") from exc
            payload = None
        if payload is not None:
            records = extract_json_chat_records(transcript, aliases)
        else:
            records = extract_text_chat_records(transcript, aliases)
    elif "<html" in transcript.lower() or '<div class="message' in transcript.lower():
        payload = None
        records = extract_html_chat_records(transcript, aliases)
    else:
        payload = None
        records = extract_text_chat_records(transcript, aliases)
    chat_id = str(payload.get("id", "")) if isinstance(payload, dict) else ""
    if not chat_id:
        authors = sorted({r.get("from_id") or r.get("author", "") for r in records})
        chat_id = hashlib.sha256(json.dumps(authors, ensure_ascii=False).encode()).hexdigest()[:20]
    for index, record in enumerate(records):
        record["chat_id"] = chat_id
        identity = record.get("message_id") or hashlib.sha256(
            json.dumps([index, record.get("date"), record.get("author"), record["text"]], ensure_ascii=False).encode()
        ).hexdigest()
        record["id"] = chat_id + ":" + identity
    return records


def extract_chat_messages(transcript: str) -> list[str]:
    return [record["text"] for record in extract_chat_records(transcript)]


def import_telegram_chat_to_memory(mem: dict, transcript: str, uid: str = "", owner_name: str = "") -> tuple[dict, dict]:
    aliases = build_self_aliases(mem, uid, owner_name)
    records = extract_chat_records(transcript, aliases)
    return merge_import_records(mem, records)


def merge_import_records(mem, records):
    examples = dialogue_examples(records)
    incoming_ids = {record["id"] for record in records}
    mem["import_facts"] = [item for item in mem["import_facts"]
        if item.get("id", "").rsplit(":", 1)[0] not in incoming_ids]
    for record in records:
        if record["role"] == "me" and not record.get("forwarded"):
            extracted = extract_local_memory(normalize_mem({}), record["text"])
            for category in ("facts", "preferences"):
                for value in extracted[category]:
                    mem["import_facts"].append({"id": record["id"] + ":" + category,
                        "chat_id": record["chat_id"], "category": category, "text": value,
                        "date": record.get("date", "")})
    for key, incoming in (("imported_dialogues", records), ("examples", examples)):
        existing = {r["id"]: r for r in mem.get(key, []) if isinstance(r, dict) and "id" in r}
        existing.update({r["id"]: r for r in incoming})
        mem[key] = list(existing.values())
    mem["style_profile"] = ""
    refresh_style_profile(mem, records)
    stats = {"total": len(records), "mine": sum(r["role"] == "me" for r in records),
             "other": sum(r["role"] == "other" for r in records), "unknown": sum(r["role"] == "unknown" for r in records),
             "pairs": len(examples)}
    if records:
        chat_id = records[0]["chat_id"]
        mem["imported_chats"] = [c for c in mem["imported_chats"] if c.get("id") != chat_id]
        mem["imported_chats"].append({"id": chat_id, "date": now_str(), "messages_count": len(records),
                                    "my_messages_count": stats["mine"], "other_messages_count": stats["other"],
                                    "unknown_messages_count": stats["unknown"], "pairs": stats["pairs"]})
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


def build_prompt(mem: dict, history: list, user_text: str):
    return build_request(mem, history, user_text, CONTEXT_TOKEN_BUDGET)


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


def build_generate_question_prompt(mem: dict):
    return build_question_request(mem, budget=CONTEXT_TOKEN_BUDGET)


def normalize_generated_question(question: str) -> str:
    return clean_question(question)


# ====== OPENAI ======
def ask_openai(prompt, max_tokens: int = 350, temperature: float = 0.7) -> str:
    return gateway.complete(prompt, max_tokens, temperature)


def update_memory_with_ai(mem: dict, user_text: str, bot_answer: str) -> dict:
    # Compatibility entry point: summarisation is local and never trains on bot output.
    summarize_history(mem)
    return mem


def analyze_imported_chat(mem: dict, transcript: str) -> dict:
    staged = normalize_mem({"user": mem.get("user", {})})
    staged, _ = import_telegram_chat_to_memory(staged, transcript, str(mem.get("user", {}).get("id", "")))
    return {key: staged[key] for key in ("facts", "preferences", "examples", "style_profile")}


def evaluate_double_test(items: list) -> str:
    rated = [item for item in items if isinstance(item, dict) and item.get("rating") in {"good", "corrected"}]
    if not rated:
        return "Пока нет оценок владельца."
    good = sum(item["rating"] == "good" for item in rated)
    return f"Подтверждено владельцем: {good}/{len(rated)} ({100 * good / len(rated):.0f}%)."


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
    "reaction_examples": "reaction_examples",
    "тренировки": "reaction_examples",
    "style_rules": "style_rules",
    "правила": "style_rules",
    "correction_examples": "correction_examples",
    "исправления": "correction_examples",
    "good_examples": "good_examples",
    "удачные": "good_examples",
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
    "reaction_examples": "Тренировки",
    "style_rules": "Постоянные правила",
    "correction_examples": "Исправления",
    "good_examples": "Подтверждённые ответы",
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
        format_numbered_list("Постоянные правила (style_rules)", mem.get("style_rules", [])),
        format_numbered_list("Исправления (correction_examples)", [
            f"{i.get('user', '')} -> {i.get('correct_answer', '')}" for i in mem.get("correction_examples", [])
        ]),
        format_numbered_list("Подтверждённые ответы (good_examples)", [
            f"{i.get('user', '')} -> {i.get('assistant', '')}" for i in mem.get("good_examples", [])
        ]),
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
        format_numbered_list("Тренировки (reaction_examples)", [
            f"{item.get('context', '')} -> {item.get('answer', '')}" for item in mem.get("reaction_examples", [])
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
        "Здесь показаны исходные записи архива. Уточнения и отключение сведений доступны в Mini App → Управлять сведениями.\n"
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

    if category == "imported_chats" and isinstance(removed, dict) and removed.get("id"):
        chat_id = removed["id"]
        pending = mem.store.find_question(mem.uid, question_id=mem.get("pending_question_id", "")) if hasattr(mem, "store") else None
        if hasattr(mem, "store"):
            mem.store.delete_chat(mem.uid, chat_id)
        for key in ("imported_dialogues", "examples", "import_facts", "generated_questions", "reaction_examples", "owner_facts"):
            mem[key] = [item for item in mem.get(key, []) if item.get("chat_id") != chat_id]
        mem["qa"] = [item for item in mem.get("qa", []) if item.get("question_chat_id") != chat_id]
        if any(card.get("chat_id") == chat_id for card in mem.get("training_session", {}).get("cards", [])):
            mem["training_session"] = {}
            if mem.get("mode") == "training_answer":
                mem["mode"] = "chat"
        if pending and pending.get("chat_id") == chat_id:
            mem["pending_question"], mem["pending_question_id"] = "", ""
            if mem.get("mode") == "qa_answer":
                mem["mode"] = "chat"
        mem["style_profile"] = ""

    if category == "qa" and isinstance(removed, dict) and removed.get("id"):
        mem["owner_facts"] = [item for item in mem.get("owner_facts", []) if item.get("source_id") != removed["id"]]

    remaining_ids = {entry["id"] for entry in catalog(mem)}
    mem["memory_controls"] = [entry for entry in mem.get("memory_controls", []) if entry["id"] in remaining_ids]

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
    get_store().reset(str(update.effective_user.id))
    await safe_reply(update, "память очищена. напиши /start", reply_markup=ReplyKeyboardRemove())


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
        format_numbered_list("Постоянные правила", mem.get("style_rules", [])) + "\n\n" + format_numbered_list("Стиль из прежней памяти", mem.get("style", [])),
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

    if category == "style":
        category = "style_rules"

    if category:
        value = " ".join(args[1:]).strip()
    else:
        category = "facts"
        value = " ".join(args).strip()

    if not value:
        await safe_reply(update, "напиши текст, который нужно запомнить", reply_markup=main_menu())
        return

    if category not in {"facts", "preferences", "style_rules", "patterns"}:
        await safe_reply(update, "Используй раздел: факт, предпочтения или стиль.")
        return
    mem[category] = add_unique(mem.get(category, []), value, 120)
    build_prompt(mem, [], "Привет")
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
    transcript = update.message.text.partition(" ")[2].strip()
    mem = load_mem(uid)
    update_user_info(update, mem)
    try:
        if not transcript:
            result = services.get_import_preview(sys.modules[__name__], uid)
            if result["preview"]:
                await show_import_preview(update, uid, result)
            else:
                await safe_reply(update, "Пришли файл экспорта .json/.html/.txt или текст после /import_chat.")
            return
        result = await asyncio.to_thread(services.preview_import, sys.modules[__name__], uid, transcript, "", mem["user"], True)
        await show_import_preview(update, uid, result)
    except ValueError as exc:
        await safe_reply(update, str(exc))


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
        "answer": text,
        "date": now_str(),
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
    result = services.today_pattern(sys.modules[__name__], mem, uid, text)
    mem["mode"] = "chat"
    save_mem(uid, mem)
    await safe_reply(update, result, reply_markup=main_menu())


async def handle_feedback_good(update: Update, mem: dict, uid: str):
    result = services.feedback(sys.modules[__name__], mem, uid)
    await safe_reply(update, result, reply_markup=main_menu())


async def handle_feedback_bad(update: Update, mem: dict, uid: str):
    if not mem.get("last_exchange"):
        await safe_reply(update, "пока нет ответа для исправления", reply_markup=main_menu())
        return

    mem["mode"] = "feedback_correction"
    mem["pending_feedback_id"] = mem["last_exchange"].get("id", "")
    save_mem(uid, mem)

    await safe_reply(update, "напиши, как надо было ответить")


async def handle_feedback_correction(update: Update, mem: dict, uid: str, text: str):
    result = services.feedback(sys.modules[__name__], mem, uid, corrected=text, expected_id=mem.get("pending_feedback_id", ""))
    mem["mode"] = "feedback_reason"
    save_mem(uid, mem)
    await safe_reply(update, result + "\nЧто было не так? Можно выбрать причину или вернуться в меню.",
        reply_markup=ReplyKeyboardMarkup([["Слишком длинно", "Слишком официально"], ["Лишние эмодзи", "Неподходящий тон"],
            ["Выдуманы факты", BTN_BACK]], resize_keyboard=True))


async def handle_feedback_reason(update, mem, uid, text):
    reason = next((key for key, (title, _) in services.REASONS.items() if title.lower() == text.lower()), "")
    if not reason:
        await safe_reply(update, "Выбери причину кнопкой или нажми «Назад».")
        return
    result = services.feedback_reason(sys.modules[__name__], uid, mem.get("pending_feedback_id", ""), reason)
    current = load_mem(uid)
    current["mode"], current["pending_feedback_id"] = "chat", ""
    save_mem(uid, current)
    await safe_reply(update, result, reply_markup=main_menu())


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
    result = await asyncio.to_thread(services.generate_question, sys.modules[__name__], uid, mem.get("user"), True)
    await safe_reply(update, f"{result['result']}\n\nНапиши свой ответ — следующий вопрос учтёт его.")


# ====== BUTTONS ======
async def handle_button(update: Update, mem: dict, uid: str, text: str) -> bool:
    if text == telegram_workflows.PEOPLE:
        await telegram_workflows.open_people(sys.modules[__name__], update, uid)
        return True
    if text == telegram_workflows.BENCHMARK:
        await telegram_workflows.open_benchmark(sys.modules[__name__], update, uid)
        return True
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

    if text == BTN_TRAIN:
        result = services.start_training(sys.modules[__name__], uid, telegram=True)
        await safe_reply(update, result["result"], reply_markup=training_menu())
        return True

    if text == BTN_SKIP_TRAIN:
        await handle_training_answer(update, mem, uid, "", skip=True)
        return True

    if text == BTN_BUDGET:
        await safe_reply(update, spending.describe(), reply_markup=main_menu())
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
        mem["pending_question_id"] = ""
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
        await webapp_command(update)
        return True

    if text == BTN_HELP:
        await send_long_message(update, functions_window_text(), reply_markup=main_menu())
        return True

    return False


# ====== MODES ======
def import_menu():
    return ReplyKeyboardMarkup([[BTN_IMPORT_CONFIRM], [BTN_IMPORT_ARCHIVE, BTN_IMPORT_CANCEL]], resize_keyboard=True)


async def show_import_preview(update, uid, result):
    preview = result["preview"]
    pending = get_store().pending_import(uid, preview["token"])
    mem = load_mem(uid)
    if not pending or pending["generation"] != mem.generation or pending["data"]["revision"] != preview["revision"]:
        raise ValueError("Предпросмотр изменился или был сброшен. Открой /import_chat заново.")
    mem["pending_import_token"], mem["mode"] = preview["token"], "import_review"
    mem["pending_import_revision"], mem["pending_import_participant"] = preview["revision"], preview["selected"]
    save_mem(uid, mem)
    await send_long_message(update, result["result"], reply_markup=import_menu())


async def handle_import_review(update, mem, uid, text):
    token = mem.get("pending_import_token", "")
    api = sys.modules[__name__]
    if text in {BTN_IMPORT_CANCEL, BTN_BACK}:
        result = services.cancel_import(api, uid, token)
        await safe_reply(update, result["result"], reply_markup=main_menu())
        return
    result = services.get_import_preview(api, uid, token)
    preview = result["preview"]
    revision = mem.get("pending_import_revision", 0)
    if preview["revision"] != revision:
        await safe_reply(update, "Выбор автора изменился. Проверь обновлённый предпросмотр.")
        await show_import_preview(update, uid, result)
        return
    if text in {BTN_IMPORT_CONFIRM, BTN_IMPORT_ARCHIVE}:
        result = await asyncio.to_thread(services.confirm_import, api, uid, token,
            mem.get("pending_import_participant", ""), revision, text == BTN_IMPORT_ARCHIVE)
        await safe_reply(update, result["result"], reply_markup=main_menu())
    elif text.isdecimal() and 1 <= int(text) <= len(preview["participants"]):
        participant = preview["participants"][int(text) - 1]
        result = services.select_import_owner(api, uid, token, participant["key"], revision)
        await show_import_preview(update, uid, result)
    else:
        await safe_reply(update, "Напиши свой номер из списка участников либо выбери подтверждение, архив или отмену.", reply_markup=import_menu())


def training_menu():
    return ReplyKeyboardMarkup([[BTN_SKIP_TRAIN], [BTN_BACK]], resize_keyboard=True)


async def handle_training_answer(update, mem, uid, text, skip=False):
    session = mem.get("training_session", {})
    card = services.current_card(session)
    if not card:
        await safe_reply(update, "Сначала открой «Научи отвечать как я».", reply_markup=main_menu())
        return
    result = services.answer_training(sys.modules[__name__], uid, session["id"], card["id"], text, skip, telegram=True)
    await safe_reply(update, result["result"], reply_markup=training_menu() if result["card"] else main_menu())


async def handle_qa_question(update: Update, mem: dict, uid: str, text: str):
    mem["pending_question"] = text
    mem["pending_question_id"] = ""
    mem["mode"] = "qa_answer"
    save_mem(uid, mem)

    await safe_reply(update, "теперь напиши ответ на этот вопрос")


async def handle_qa_answer(update: Update, mem: dict, uid: str, text: str):
    question = mem.get("pending_question", "").strip()
    if not question:
        await safe_reply(update, "Вопрос потерялся. Создай новый вопрос.")
        return
    result = services.save_qa(sys.modules[__name__], mem, uid, question, text, mem.get("pending_question_id", ""))
    mem["pending_question"], mem["pending_question_id"], mem["mode"] = "", "", "chat"
    save_mem(uid, mem)
    await safe_reply(update, result, reply_markup=main_menu())


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
    result = await asyncio.to_thread(services.chat, sys.modules[__name__], uid, text, mem.get("user"))
    await send_long_message(update, result["result"], reply_markup=main_menu(include_feedback=True))


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

        if mem.get("mode") == "import_review":
            await handle_import_review(update, mem, uid, text)
            return

        if await telegram_workflows.handle_flow(sys.modules[__name__], update, mem, uid, text):
            return

        if await handle_button(update, mem, uid, text):
            return

        if mem.get("mode") == "training_answer":
            await handle_training_answer(update, mem, uid, text)
            return

        if mem.get("mode") == "feedback_reason":
            await handle_feedback_reason(update, mem, uid, text)
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


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document or Path(document.file_name or "").suffix.lower() not in {".json", ".html", ".htm", ".txt"}:
        await safe_reply(update, "Поддерживаются экспорты .json, .html и .txt.")
        return
    if (document.file_size or 0) > 15 * 1024 * 1024:
        await safe_reply(update, "Экспорт должен быть не больше 15 МБ.")
        return
    try:
        file = await document.get_file()
        transcript = (await file.download_as_bytearray()).decode("utf-8-sig")
        uid = str(update.effective_user.id)
        mem = load_mem(uid)
        update_user_info(update, mem)
        result = await asyncio.to_thread(services.preview_import, sys.modules[__name__], uid, transcript, "", mem["user"], True, document.file_name or "")
        await show_import_preview(update, uid, result)
    except UnicodeError:
        await safe_reply(update, "Не удалось прочитать экспорт. Проверь кодировку UTF-8.")
    except ValueError as exc:
        await safe_reply(update, str(exc))


def main():
    if not TG_TOKEN or not OPENAI_API_KEY:
        raise ValueError("Укажи TG_TOKEN и OPENAI_API_KEY в .env или config.json")
    app = (ApplicationBuilder().token(TG_TOKEN).concurrent_updates(PerUserUpdateProcessor()).connect_timeout(30).read_timeout(60)
           .write_timeout(60).pool_timeout(60).build())
    for name, callback in {
        "start": start, "memory": memory_command, "forget": forget_command, "reset": reset,
        "profile": profile, "facts": facts_command, "preferences": preferences_command,
        "style": style_command, "remember": remember_command, "search_memory": search_memory_command,
        "import_chat": import_chat_command, "miniapp": webapp_command,
    }.items():
        app.add_handler(CommandHandler(name, callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()


if __name__ == "__main__":
    main()
