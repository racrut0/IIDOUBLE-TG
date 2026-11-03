"""Owner-managed interlocutors; no identity or relationship is inferred by a model."""
from __future__ import annotations

import uuid
from contextlib import closing

from owner_memory import contains_excluded
from retrieval import example_context, normalized_text


RELATIONSHIPS = {"friend": "Друг", "family": "Семья", "partner": "Близкий человек",
                 "colleague": "Коллега", "acquaintance": "Знакомый", "other": "Другое"}


def active_id(mem):
    return mem.get("active_interlocutor_id", "")


def profile_for(mem, profile_id=None):
    profile_id = active_id(mem) if profile_id is None else profile_id
    return next((item for item in mem.get("interlocutors", []) if item["id"] == profile_id), None)


def allowed_example(mem, item):
    if contains_excluded(mem, item):
        return False
    excluded = getattr(mem, "excluded_chat_ids", set())
    if item.get("chat_id") in excluded or item.get("question_chat_id") in excluded:
        return False
    if normalized_text(example_context(item)) in getattr(mem, "excluded_example_contexts", set()):
        return False
    scoped = item.get("interlocutor_id", "")
    if scoped and scoped != active_id(mem):
        return False
    # Imported examples are tied to the selected person's chats. General owner
    # Q&A and feedback remain usable; other people's scoped feedback does not.
    profile = profile_for(mem)
    return not (profile and item.get("source") == "telegram" and item.get("chat_id") not in profile["chat_ids"])


def history_for(mem, history):
    return [item for item in history if item.get("interlocutor_id", "") == active_id(mem)]


def chat_catalog(api, uid, mem=None):
    mem = api.load_mem(uid) if mem is None else mem
    result = []
    with closing(api.get_store().connect()) as db:
        for index, chat in enumerate(mem.get("imported_chats", []), 1):
            row = db.execute("""SELECT json_extract(data,'$.author') FROM memory_items WHERE uid=?
                AND category='imported_dialogues' AND json_extract(data,'$.chat_id')=?
                AND json_extract(data,'$.role')='other' AND json_extract(data,'$.author') != ''
                ORDER BY seq DESC LIMIT 1""", (str(uid), chat["id"])).fetchone()
            label = str(row[0])[:60] if row else f"Чат {index}"
            result.append({"id": chat["id"], "label": label, "messages": chat.get("messages_count", 0),
                           "archive_only": chat.get("archive_only", False)})
    return result


def catalog(api, uid):
    mem = api.load_mem(uid)
    return {"ok": True, "interlocutors": mem["interlocutors"], "active_interlocutor_id": active_id(mem),
            "relationships": RELATIONSHIPS, "chats": chat_catalog(api, uid, mem), "result": "Настройки собеседников загружены."}


def save(api, uid, label, relationship, chat_ids, notes="", profile_id=""):
    label, notes = label.strip(), notes.strip()
    if not label or len(label) > 60 or len(notes) > 400:
        raise ValueError("Название — от 1 до 60 символов, пожелания — до 400 символов.")
    if relationship not in RELATIONSHIPS:
        raise ValueError("Выбери тип отношений с собеседником.")
    if len(chat_ids) > 20 or any(not isinstance(item, str) or len(item) > 100 for item in chat_ids):
        raise ValueError("Можно привязать до 20 своих чатов.")
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        existing = profile_for(mem, profile_id) if profile_id else None
        if profile_id and not existing:
            raise ValueError("Собеседник не найден в твоём профиле.")
        if not existing and len(mem["interlocutors"]) >= 25:
            raise ValueError("Можно сохранить до 25 собеседников.")
        available = {item["id"] for item in mem["imported_chats"] if not item.get("archive_only")}
        if set(chat_ids) - available:
            raise ValueError("Для обучения можно привязать только свои подтверждённые чаты.")
        item = {"id": profile_id or uuid.uuid4().hex, "label": label, "relationship": relationship,
                "chat_ids": list(dict.fromkeys(chat_ids)), "notes": notes, "updated": api.now_str()}
        mem["interlocutors"] = [item if p["id"] == item["id"] else p for p in mem["interlocutors"]] if existing else [*mem["interlocutors"], item]
        if active_id(mem) == item["id"]:
            mem["training_session"] = {}
        api.save_mem(uid, mem)
    return {**catalog(api, uid), "saved_interlocutor_id": item["id"], "result": "Настройки собеседника сохранены без API."}


def select(api, uid, profile_id):
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        profile = profile_for(mem, profile_id)
        if profile_id and not profile:
            raise ValueError("Собеседник не найден в твоём профиле.")
        mem["active_interlocutor_id"] = profile_id
        mem["training_session"], mem["pending_feedback_id"], mem["mode"] = {}, "", "chat"
        # Feedback buttons in another client may still refer to the previously
        # displayed reply. Require a new reply after switching people.
        mem["last_exchange"] = {}
        api.save_mem(uid, mem)
    label = profile["label"] if profile else "общая манера"
    return {**catalog(api, uid), "result": f"Текущий собеседник: {label}. История разговора отдельная."}


def delete(api, uid, profile_id):
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        if not profile_for(mem, profile_id):
            raise ValueError("Собеседник не найден в твоём профиле.")
        mem["interlocutors"] = [item for item in mem["interlocutors"] if item["id"] != profile_id]
        if active_id(mem) == profile_id:
            mem["active_interlocutor_id"], mem["mode"] = "", "chat"
            mem["last_exchange"], mem["training_session"] = {}, {}
        mem.get("conversation_summaries", {}).pop(profile_id, None)
        api.save_mem(uid, mem)
    return {**catalog(api, uid), "result": "Настройки собеседника удалены. Исходные чаты сохранены."}
