"""Local import previews. Participant labels never replace Telegram account identity."""
from __future__ import annotations

import hashlib
import re

from retrieval import dialogue_examples


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower().lstrip("@"))


def sender_identity(record):
    sender_id = normalize(record.get("from_id", ""))
    author = normalize(record.get("author", ""))
    return ("id:" + sender_id) if sender_id else ("name:" + author) if author else ""


def participant_key(record):
    identity = sender_identity(record)
    return hashlib.sha256(identity.encode()).hexdigest()[:24] if identity else ""


def participants(records, uid):
    grouped = {}
    for record in records:
        key = participant_key(record)
        if not key:
            continue
        sender_id = normalize(record.get("from_id", ""))
        own_id = sender_id in {str(uid), "user" + str(uid)}
        item = grouped.setdefault(key, {"key": key, "names": [], "from_id": record.get("from_id", ""),
            "count": 0, "verified_self": own_id, "selectable": not sender_id or own_id})
        name = record.get("author", "").strip()
        if name and name[:200] not in item["names"] and len(item["names"]) < 5:
            item["names"].append(name[:200])
        item["count"] += 1
    if len(grouped) > 200:
        raise ValueError("В файле больше 200 участников. Экспортируй отдельный личный диалог.")
    return sorted(grouped.values(), key=lambda item: (not item["verified_self"], -item["count"], item["key"]))


def suggested_participant(people, user, hint=""):
    verified = [p for p in people if p["verified_self"]]
    if len(verified) == 1:
        return verified[0]["key"]
    aliases = {normalize(hint), normalize(user.get("username")), normalize(user.get("first_name")),
        normalize(" ".join(str(user.get(k) or "") for k in ("first_name", "last_name")))} - {""}
    matches = [p for p in people if p["selectable"] and any(normalize(name) in aliases for name in p["names"])]
    return matches[0]["key"] if len(matches) == 1 else ""


def classify(records, people, selected, archive_only=False):
    participant = next((p for p in people if p["key"] == selected), None)
    if not archive_only and (not participant or not participant["selectable"]):
        raise ValueError("Выбери себя среди участников. ID другого аккаунта нельзя заменить совпадением имени.")
    return [{**record, "role": "me" if not archive_only and participant_key(record) == selected
        else "other" if sender_identity(record) else "unknown"} for record in records]


def preview_payload(stage, token):
    data = stage["data"]
    selected = data.get("selected", "")
    classified = classify(data["records"], data["participants"], selected, archive_only=not selected)
    pairs = dialogue_examples(classified)
    mine = [r for r in classified if r["role"] == "me"]
    def representative(items):
        return [items[index] for index in sorted({0, len(items) // 2, len(items) - 1})] if items else []
    samples = [{"context": item["context"][:500], "answer": item["answer"][:500]} for item in representative(pairs)]
    own_samples = [r["text"][:500] for r in representative([r for r in mine if not r.get("forwarded")])]
    participant = next((p for p in data["participants"] if p["key"] == selected), None)
    warning = ("В этом файле нет надёжной связи выбранного имени с Telegram ID. Проверь примеры своих сообщений."
        if participant and not participant["verified_self"] else "")
    return {"token": token, "revision": data["revision"], "format": data["format"], "filename": data.get("filename", ""), "participants": data["participants"],
        "selected": selected, "can_confirm": bool(participant and participant["selectable"]), "warning": warning,
        "total": len(classified), "mine": len(mine), "trainable_mine": sum(not r.get("forwarded") for r in mine),
        "other": sum(r["role"] == "other" for r in classified), "unknown": sum(r["role"] == "unknown" for r in classified),
        "pairs": len(pairs), "samples": samples, "own_samples": own_samples}


def describe_preview(preview):
    lines = [f"Предпросмотр {preview['format']}: сообщений {preview['total']}.",
        "Файл: " + (preview["filename"] or "вставленный текст"),
        "Новый файл пока не используется для ответов и обучения.", "", "Участники:"]
    for index, person in enumerate(preview["participants"], 1):
        names = " / ".join(person["names"][:3]) or "Без имени"
        details = f" · {person['from_id']}" if person["from_id"] else " · без ID"
        label = " — выбран ты" if person["key"] == preview["selected"] else ""
        if not person["selectable"]:
            label += " — ID другого аккаунта"
        lines.append(f"{index}. {names}{details}: {person['count']}{label}")
    lines += ["", f"Твои сообщения: {preview['mine']}; для обучения: {preview['trainable_mine']}; пар ответов: {preview['pairs']}."]
    if preview["warning"]:
        lines.append(preview["warning"])
    if not preview["can_confirm"]:
        lines.append("Выбери себя по номеру. Если твоих сообщений нет, можно сохранить только архив.")
    for sample in preview["samples"]:
        lines += ["", "Собеседник: " + sample["context"], "Твой ответ: " + sample["answer"]]
    if not preview["samples"]:
        lines += ["", *("Твоё сообщение: " + text for text in preview["own_samples"])]
    lines += ["", "Проверь автора и примеры, затем подтверди импорт. Можно выбрать другого участника номером или отменить.",
        "Предпросмотр и импорт работают без API."]
    return "\n".join(lines)
