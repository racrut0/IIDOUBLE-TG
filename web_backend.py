"""Authenticated Mini App API using the same services as Telegram."""
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from pydantic import BaseModel, Field

import bot
import services
import benchmarks
import interlocutors
from storage import BusyUserError, StaleMemoryError
from spending import SpendingLimitError

url = urlsplit(bot.WEBAPP_URL)
default_origin = f"{url.scheme}://{url.netloc}" if url.scheme and url.netloc else ""
APP_ORIGINS = [s.strip() for s in os.environ.get("WEBAPP_ORIGINS", default_origin).split(",") if s.strip()]
INIT_DATA_MAX_AGE = int(os.environ.get("WEBAPP_INIT_DATA_MAX_AGE", "86400"))
app = FastAPI(title="AI Double Web Backend", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=APP_ORIGINS, allow_credentials=False,
                   allow_methods=["POST", "GET", "OPTIONS"], allow_headers=["Content-Type", "ngrok-skip-browser-warning"])

PUBLIC_ROOT = Path(__file__).resolve().parent
PUBLIC_ASSETS = {
    "app.css": "assets/app.css",
    "fonts/Onest-Variable.ttf": "assets/fonts/Onest-Variable.ttf",
    "fonts/OFL.txt": "assets/fonts/OFL.txt",
}


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def miniapp_page():
    return FileResponse(PUBLIC_ROOT / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/assets/{asset_path:path}", include_in_schema=False)
def miniapp_asset(asset_path: str):
    # Only these public files are served; the project directory contains secrets and memory.
    relative = PUBLIC_ASSETS.get(asset_path)
    if relative is None:
        raise HTTPException(404, "Файл не найден")
    return FileResponse(PUBLIC_ROOT / relative)


class ActionRequest(BaseModel):
    initData: str = Field(default="", max_length=16384)
    action: Literal["chat", "generate_question", "save_qa", "today_pattern", "import_tg_chat",
                    "feedback_good", "feedback_correction", "memory", "delete_memory", "help", "stats", "save_style_rule",
                    "budget", "start_training", "answer_training", "memory_catalog", "update_memory", "set_question_topic",
                    "get_import_preview", "select_import_owner", "confirm_import", "cancel_import",
                    "interlocutors", "save_interlocutor", "select_interlocutor", "delete_interlocutor",
                    "benchmark", "create_benchmark", "start_benchmark", "generate_benchmark", "rate_benchmark"]
    text: str = Field(default="", max_length=15 * 1024 * 1024)
    question: str = Field(default="", max_length=8000)
    answer: str = Field(default="", max_length=8000)
    category: str = Field(default="", max_length=80)
    number: int = Field(default=0, ge=0)
    exchange_id: str = Field(default="", max_length=64)
    question_id: str = Field(default="", max_length=64)
    session_id: str = Field(default="", max_length=64)
    card_id: str = Field(default="", max_length=64)
    skip: bool = False
    memory_id: str = Field(default="", max_length=64)
    enabled: bool = True
    topic: str = Field(default="", max_length=40)
    blocked: bool = False
    reason: Literal["", "long", "formal", "emojis", "tone", "invented", "custom"] = ""
    note: str = Field(default="", max_length=1000)
    scope: Literal["situation", "general"] = "situation"
    import_token: str = Field(default="", max_length=64)
    participant_key: str = Field(default="", max_length=64)
    preview_revision: int = Field(default=0, ge=0)
    archive_only: bool = False
    filename: str = Field(default="", max_length=255)
    interlocutor_id: str = Field(default="", max_length=64)
    label: str = Field(default="", max_length=60)
    relationship: str = Field(default="", max_length=30)
    chat_ids: list[str] = Field(default_factory=list, max_length=20)
    chat_id: str = Field(default="", max_length=100)
    run_id: str = Field(default="", max_length=64)
    case_id: str = Field(default="", max_length=64)
    rating: Literal["", "like", "partial", "unlike"] = ""
    retry: bool = False


def verify_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "Открой приложение внутри Telegram")
    if not bot.TG_TOKEN:
        raise HTTPException(503, "Backend ещё не настроен")
    pairs = parse_qsl(init_data, keep_blank_values=True)
    parsed = dict(pairs)
    if len(pairs) != len(parsed):
        raise HTTPException(401, "Некорректная подпись Telegram")
    received_hash = parsed.pop("hash", "")
    check = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", bot.TG_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", received_hash) or not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(401, "Некорректная подпись Telegram")
    try:
        auth_date = int(parsed.get("auth_date", "0"))
        user = json.loads(parsed.get("user", "{}"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(401, "Некорректные данные Telegram") from exc
    if auth_date <= 0 or auth_date > time.time() + 30 or time.time() - auth_date > max(1, INIT_DATA_MAX_AGE):
        raise HTTPException(401, "Сессия истекла. Открой приложение заново.")
    if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
        raise HTTPException(401, "Не определён пользователь Telegram")
    return user


def load_user_mem(init_data):
    user = verify_init_data(init_data)
    uid = str(user["id"])
    mem = bot.load_mem(uid)
    mem["user"] = user
    return uid, mem


def validate_text(payload):
    text = payload.text.strip()
    if payload.action in {"chat", "today_pattern", "feedback_correction", "import_tg_chat", "save_style_rule"} and not text:
        raise HTTPException(400, "Напиши текст для этого действия")
    if payload.action != "import_tg_chat" and len(text) > 16000:
        raise HTTPException(400, "Текст слишком длинный. Сократи сообщение.")
    return text


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/action")
def action(payload: ActionRequest):
    uid, mem = load_user_mem(payload.initData)
    text = validate_text(payload)
    name = payload.action
    try:
        if name == "interlocutors":
            return interlocutors.catalog(bot, uid)
        if name == "save_interlocutor":
            return interlocutors.save(bot, uid, payload.label, payload.relationship, payload.chat_ids, payload.note, payload.interlocutor_id)
        if name == "select_interlocutor":
            return interlocutors.select(bot, uid, payload.interlocutor_id)
        if name == "delete_interlocutor":
            return interlocutors.delete(bot, uid, payload.interlocutor_id)
        if name == "benchmark":
            return benchmarks.get(bot, uid, payload.run_id)
        if name == "create_benchmark":
            return benchmarks.create_suite(bot, uid, payload.chat_id)
        if name == "start_benchmark":
            return benchmarks.start_run(bot, uid)
        if name == "generate_benchmark":
            return benchmarks.generate(bot, uid, payload.run_id, payload.case_id, payload.retry)
        if name == "rate_benchmark":
            return benchmarks.rate(bot, uid, payload.run_id, payload.case_id, payload.rating, payload.note)
        if name == "chat":
            return services.chat(bot, uid, text, mem["user"])
        if name == "generate_question":
            return services.generate_question(bot, uid, mem["user"])
        if name == "budget":
            return {"ok": True, "result": bot.spending.describe(), "budget": bot.spending.status()}
        if name == "start_training":
            return services.start_training(bot, uid)
        if name == "answer_training":
            return services.answer_training(bot, uid, payload.session_id, payload.card_id, payload.answer, payload.skip)
        if name == "memory_catalog":
            return services.memory_catalog(bot, uid)
        if name == "update_memory":
            return services.update_memory(bot, uid, payload.memory_id, text, payload.enabled)
        if name == "set_question_topic":
            return services.set_question_topic(bot, uid, payload.topic, payload.blocked)
        if name == "get_import_preview":
            return services.get_import_preview(bot, uid, payload.import_token)
        if name == "select_import_owner":
            return services.select_import_owner(bot, uid, payload.import_token, payload.participant_key, payload.preview_revision)
        if name == "confirm_import":
            return services.confirm_import(bot, uid, payload.import_token, payload.participant_key, payload.preview_revision, payload.archive_only)
        if name == "cancel_import":
            return services.cancel_import(bot, uid, payload.import_token)
        elif name == "save_qa":
            if not payload.question.strip() or not payload.answer.strip():
                raise ValueError("Нужны ситуация и твой ответ")
            result = services.save_qa(bot, mem, uid, payload.question.strip(), payload.answer.strip(), payload.question_id)
        elif name == "today_pattern":
            result = services.today_pattern(bot, mem, uid, text)
        elif name == "save_style_rule":
            mem["style_rules"] = bot.add_unique(mem["style_rules"], text)
            bot.build_prompt(mem, [], "Привет")
            bot.save_mem(uid, mem)
            result = "Сохранил постоянное правило общения."
        elif name == "import_tg_chat":
            return services.preview_import(bot, uid, text, payload.question.strip(), mem["user"], filename=payload.filename)
        elif name in {"feedback_good", "feedback_correction"}:
            if not payload.exchange_id:
                raise ValueError("Оцени ответ в открытой карточке диалога")
            result = services.feedback(bot, mem, uid, text if name == "feedback_correction" else None, payload.exchange_id,
                payload.reason, payload.note, payload.scope)
        elif name == "memory":
            result = bot.build_memory_overview(mem)
        elif name == "delete_memory":
            category = bot.resolve_category(payload.category)
            if not category:
                raise ValueError("Неизвестный раздел памяти")
            removed = bot.delete_memory_item(mem, category, payload.number)
            if removed is None:
                raise ValueError("Такого номера нет")
            bot.save_mem(uid, mem)
            result = "Запись удалена."
        elif name == "stats":
            counts = {key: bot.get_store().count(uid, key) for key in ("imported_dialogues", "examples", "history", "reaction_examples")}
            recent = bot.get_store().metrics(uid)
            result = (f"Сообщений в архиве: {counts['imported_dialogues']}. Примеров из переписки: {counts['examples']}. "
                      f"Сообщений с двойником: {counts['history']}. Тренировочных ответов: {counts['reaction_examples']}.\n" + bot.evaluate_double_test(mem["feedback"]))
            return {"ok": True, "result": result, "counts": counts, "metrics": recent, "budget": bot.spending.status()}
        else:
            result = bot.functions_window_text()
        return {"ok": True, "result": result}
    except (BusyUserError, StaleMemoryError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except SpendingLimitError as exc:
        return JSONResponse(status_code=402, content={"detail": str(exc), "budget": bot.spending.status()})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc), "budget": bot.spending.status()})
    except Exception as exc:
        return JSONResponse(status_code=502, content={"detail": "Не удалось завершить действие. Возможный расход API учтён с запасом.", "budget": bot.spending.status()})


@app.post("/api/chat/stream")
def stream_chat(payload: ActionRequest):
    user = verify_init_data(payload.initData)
    if payload.action != "chat":
        raise HTTPException(400, "Этот адрес используется для диалога")
    text = validate_text(payload)

    def events():
        try:
            for event in services.chat_events(bot, str(user["id"]), text, user):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except (ValueError, BusyUserError, StaleMemoryError) as exc:
            yield json.dumps({"type": "error", "error": str(exc), "budget": bot.spending.status()}, ensure_ascii=False) + "\n"
        except Exception:
            yield json.dumps({"type": "error", "error": "Ответ не завершён. Возможный расход API учтён с запасом.", "budget": bot.spending.status()}, ensure_ascii=False) + "\n"
    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
