import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import bot


APP_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("WEBAPP_ORIGINS", "*").split(",")
    if origin.strip()
]

INIT_DATA_MAX_AGE = int(os.environ.get("WEBAPP_INIT_DATA_MAX_AGE", "86400"))
DEV_USER_ID = (
    os.environ.get("WEBAPP_DEV_USER_ID", "").strip()
    or bot.config.get("WEBAPP_DEV_USER_ID", "")
)

app = FastAPI(title="AI Double Web Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=APP_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


class ActionRequest(BaseModel):
    initData: str = ""
    action: str
    text: str = ""
    question: str = ""
    answer: str = ""
    category: str = ""
    number: int = 0


def verify_init_data(init_data: str) -> dict:
    if DEV_USER_ID and not init_data:
        return {"id": int(DEV_USER_ID)}

    if not init_data:
        raise HTTPException(status_code=401, detail="Telegram initData is missing")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", "")

    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram initData hash is missing")

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(parsed.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot.TG_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Telegram initData hash is invalid")

    auth_date = int(parsed.get("auth_date", "0") or "0")

    if INIT_DATA_MAX_AGE > 0 and auth_date and time.time() - auth_date > INIT_DATA_MAX_AGE:
        raise HTTPException(status_code=401, detail="Telegram initData is expired")

    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Telegram user data is invalid") from exc

    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user id is missing")

    return user


def load_user_mem(init_data: str) -> tuple[str, dict]:
    user = verify_init_data(init_data)
    uid = str(user["id"])
    mem = bot.load_mem(uid)
    mem["user"] = {
        "id": user.get("id"),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "language_code": user.get("language_code"),
    }
    return uid, mem


def chat(mem: dict, uid: str, text: str) -> str:
    mem = bot.extract_local_memory(mem, text)
    mem = bot.remember_user_pattern(mem, text, "сообщение пользователя")
    history = mem.get("history", [])
    prompt = bot.build_prompt(mem, history[-10:], text)
    answer = bot.ask_openai(
        prompt,
        60 if bot.is_casual_status_question(text) else 400,
        0.45 if bot.is_casual_status_question(text) else 0.7,
    )

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": answer})
    mem["history"] = history[-80:]
    mem["message_count"] = mem.get("message_count", 0) + 1
    mem["last_exchange"] = {
        "date": bot.now_str(),
        "user": text,
        "assistant": answer,
    }
    bot.save_mem(uid, mem)
    return answer


def generate_question(mem: dict) -> str:
    question = bot.ask_openai(bot.build_generate_question_prompt(mem), 80, 0.75)
    return bot.normalize_generated_question(question)


def save_qa(mem: dict, uid: str, question: str, answer: str) -> str:
    mem["qa"].append({
        "date": bot.now_str(),
        "question": question,
        "answer": answer,
    })
    mem["qa"] = mem["qa"][-120:]
    mem = bot.remember_user_pattern(mem, answer, "ответ в Q&A")
    bot.save_mem(uid, mem)
    return "Сохранил вопрос и ответ."


def today_pattern(mem: dict, uid: str, text: str) -> str:
    mem["daily_state"].append({
        "date": bot.now_str(),
        "text": text,
    })
    mem["daily_state"] = mem["daily_state"][-90:]
    mem = bot.remember_user_pattern(mem, text, "сегодня")
    bot.save_mem(uid, mem)
    return "Запомнил, как ты сегодня отвечал."


def import_tg_chat(mem: dict, uid: str, text: str, owner_name: str = "") -> str:
    mem, stats = bot.import_telegram_chat_to_memory(mem, text, uid=uid, owner_name=owner_name)
    bot.save_mem(uid, mem)

    if stats["total"] == 0:
        return "Не нашел сообщений для импорта."

    if stats["mine"] == 0:
        return (
            f"Импортировал {stats['total']} сообщений, но не понял, какие из них твои. "
            "Укажи свое имя/ник как в экспорте и импортируй еще раз."
        )

    return (
        f"Импортировал {stats['total']} сообщений.\n"
        f"Твои: {stats['mine']}.\n"
        f"Собеседника: {stats['other']}.\n"
        f"Не определил: {stats['unknown']}.\n"
        "В стиль сохранил только твои сообщения."
    )


def feedback_good(mem: dict, uid: str) -> str:
    last = mem.get("last_exchange", {})

    if not last:
        return "Пока нет ответа для оценки."

    item = {
        "date": bot.now_str(),
        "user": last.get("user", ""),
        "assistant": last.get("assistant", ""),
    }
    mem["feedback"].append({**item, "rating": "good"})
    mem["good_examples"].append(item)
    mem = bot.remember_user_pattern(mem, last.get("assistant", ""), "удачный ответ")
    mem["feedback"] = mem["feedback"][-120:]
    mem["good_examples"] = mem["good_examples"][-40:]
    bot.save_mem(uid, mem)
    return "Сохранил как удачный пример."


def feedback_correction(mem: dict, uid: str, text: str) -> str:
    last = mem.get("last_exchange", {})

    if not last:
        return "Ответ для исправления потерялся."

    if bot.normalize_for_compare(text) == bot.normalize_for_compare(last.get("assistant", "")):
        return "Ты не изменил ответ. Напиши правильный вариант, иначе я снова запомню плохой."

    item = {
        "date": bot.now_str(),
        "user": last.get("user", ""),
        "bad_answer": last.get("assistant", ""),
        "correct_answer": text,
    }
    mem["feedback"].append({**item, "rating": "corrected"})
    mem["correction_examples"].append(item)
    mem["qa"].append({
        "date": bot.now_str(),
        "question": last.get("user", ""),
        "answer": text,
    })
    mem = bot.remember_user_pattern(mem, text, "исправленный ответ")
    mem["feedback"] = mem["feedback"][-120:]
    mem["correction_examples"] = mem["correction_examples"][-50:]
    mem["qa"] = mem["qa"][-120:]
    bot.save_mem(uid, mem)
    return "Исправление сохранил."


def delete_memory(mem: dict, uid: str, category: str, number: int) -> str:
    resolved = bot.resolve_category(category)

    if not resolved:
        return "Не нашел такой раздел памяти."

    removed = bot.delete_memory_item(mem, resolved, number)

    if removed is None:
        return "Такого номера нет."

    bot.save_mem(uid, mem)
    return f"Удалил: {removed}"


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/action")
def action(payload: ActionRequest) -> dict:
    uid, mem = load_user_mem(payload.initData)
    action_name = payload.action
    text = payload.text.strip()

    if action_name == "chat":
        if not text:
            return {"ok": False, "result": "Напиши сообщение для двойника."}

        return {"ok": True, "result": chat(mem, uid, text)}

    if action_name == "generate_question":
        return {"ok": True, "result": generate_question(mem)}

    if action_name == "save_qa":
        question = payload.question.strip()
        answer = payload.answer.strip()

        if not question or not answer:
            return {"ok": False, "result": "Нужны вопрос и ответ."}

        return {"ok": True, "result": save_qa(mem, uid, question, answer)}

    if action_name == "today_pattern":
        if not text:
            return {"ok": False, "result": "Нужен пример ответа."}

        return {"ok": True, "result": today_pattern(mem, uid, text)}

    if action_name == "import_tg_chat":
        if not text:
            return {"ok": False, "result": "Вставь текст экспорта или фрагмент Telegram-чата."}

        return {"ok": True, "result": import_tg_chat(mem, uid, text, payload.question.strip())}

    if action_name == "feedback_good":
        return {"ok": True, "result": feedback_good(mem, uid)}

    if action_name == "feedback_correction":
        if not text:
            return {"ok": False, "result": "Напиши правильный вариант ответа."}

        return {"ok": True, "result": feedback_correction(mem, uid, text)}

    if action_name == "memory":
        return {"ok": True, "result": bot.build_memory_overview(mem)}

    if action_name == "delete_memory":
        return {
            "ok": True,
            "result": delete_memory(mem, uid, payload.category, payload.number),
        }

    if action_name == "help":
        return {"ok": True, "result": bot.functions_window_text()}

    return {"ok": False, "result": "Неизвестное действие."}
