"""Authenticated Mini App API using the same services as Telegram."""
import hashlib
import hmac
import json
import os
import re
import time
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import bot
import services
from storage import BusyUserError, StaleMemoryError

url = urlsplit(bot.WEBAPP_URL)
default_origin = f"{url.scheme}://{url.netloc}" if url.scheme and url.netloc else ""
APP_ORIGINS = [s.strip() for s in os.environ.get("WEBAPP_ORIGINS", default_origin).split(",") if s.strip()]
INIT_DATA_MAX_AGE = int(os.environ.get("WEBAPP_INIT_DATA_MAX_AGE", "86400"))
app = FastAPI(title="AI Double Web Backend", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=APP_ORIGINS, allow_credentials=False,
                   allow_methods=["POST", "GET", "OPTIONS"], allow_headers=["Content-Type", "ngrok-skip-browser-warning"])


class ActionRequest(BaseModel):
    initData: str = Field(default="", max_length=16384)
    action: Literal["chat", "generate_question", "save_qa", "today_pattern", "import_tg_chat",
                    "feedback_good", "feedback_correction", "memory", "delete_memory", "help", "stats", "save_style_rule"]
    text: str = Field(default="", max_length=15 * 1024 * 1024)
    question: str = Field(default="", max_length=8000)
    answer: str = Field(default="", max_length=8000)
    category: str = Field(default="", max_length=80)
    number: int = Field(default=0, ge=0)
    exchange_id: str = Field(default="", max_length=64)
    question_id: str = Field(default="", max_length=64)


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
        if name == "chat":
            return services.chat(bot, uid, text, mem["user"])
        if name == "generate_question":
            return services.generate_question(bot, uid, mem["user"])
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
            result = services.import_chat(bot, mem, uid, text, payload.question.strip())
        elif name in {"feedback_good", "feedback_correction"}:
            if not payload.exchange_id:
                raise ValueError("Оцени ответ в открытой карточке диалога")
            result = services.feedback(bot, mem, uid, text if name == "feedback_correction" else None, payload.exchange_id)
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
            counts = {key: bot.get_store().count(uid, key) for key in ("imported_dialogues", "examples", "history")}
            recent = bot.get_store().metrics(uid)
            result = (f"Сообщений в архиве: {counts['imported_dialogues']}. Примеров из переписки: {counts['examples']}. "
                      f"Сообщений с двойником: {counts['history']}.\n" + bot.evaluate_double_test(mem["feedback"]))
            return {"ok": True, "result": result, "counts": counts, "metrics": recent}
        else:
            result = bot.functions_window_text()
        return {"ok": True, "result": result}
    except (BusyUserError, StaleMemoryError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "Не удалось завершить действие. Попробуй ещё раз.") from exc


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
            yield json.dumps({"type": "error", "error": str(exc)}, ensure_ascii=False) + "\n"
        except Exception:
            yield json.dumps({"type": "error", "error": "Ответ не завершён. Попробуй ещё раз."}, ensure_ascii=False) + "\n"
    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
