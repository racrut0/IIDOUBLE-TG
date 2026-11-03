"""Seven local rehearsal cards: real chat contexts and explicitly hypothetical reactions."""
from __future__ import annotations

import uuid

from owner_memory import effective_memory, usable_records
from questions import life_anchors
from interlocutors import allowed_example, profile_for


SCENARIOS = [
    ("support", "Поддержка", "У меня сегодня ничего не получилось. Можешь немного поддержать?"),
    ("refusal", "Отказ", "Поможешь мне сегодня? Если неудобно, скажи прямо."),
    ("boundary", "Границы", "Почему ты до сих пор не ответил? Я же жду."),
    ("clarify", "Уточнение", "Я не совсем понял, о чём ты. Объясни своими словами?"),
    ("joy", "Радость", "У меня наконец получилось! Хочу с тобой поделиться."),
    ("disagree", "Несогласие", "Мне кажется, ты зря тратишь на это время. Ты согласен?"),
    ("humor", "Юмор", "Сегодня всё идёт наперекосяк. Есть шутка на такой случай?"),
]


def cards_for(mem):
    mem = effective_memory(mem)
    blocked = set(mem.get("question_state", {}).get("blocked_topics", [])) | set(mem.get("question_state", {}).get("deferred_topics", []))
    anchors = [a for a in life_anchors(mem) if a["topic"] not in blocked and a["topic"] in {"work", "learning", "interests", "goals", "daily_life"}]
    # Imported contexts are real messages, not invented events. Never show the stored answer.
    profile = profile_for(mem)
    real = usable_records(mem, mem.store.recent_examples(mem.uid, 20, chat_ids=profile["chat_ids"] if profile else None)) if hasattr(mem, "store") else mem.get("examples", [])
    real = [item for item in real if allowed_example(mem, item)]
    cards = []
    if not blocked:  # An imported context has no reliable topic label; respect private-topic controls conservatively.
        for item in real[:2]:
            if len(item.get("context", "")) <= 1200:
                cards.append({"id": uuid.uuid4().hex, "context": item["context"], "kind": "real",
                    "intent": "Реальная переписка", "life_context": "", "chat_id": item.get("chat_id", "")})
    offset = mem.get("training_round", 0) % len(SCENARIOS)
    scenarios = SCENARIOS[offset:] + SCENARIOS[:offset]
    for index, (key, title, context) in enumerate(scenarios):
        if len(cards) >= 7:
            break
        # Only cite known text; the situation itself remains labelled hypothetical.
        anchor = anchors[index % len(anchors)] if anchors and key in {"clarify", "disagree"} else None
        if anchor:
            context = ("Расскажешь, что для тебя сейчас важно в этом занятии?" if key == "clarify"
                else "Мне трудно понять, зачем тебе это нужно. Как ты к этому относишься?")
        cards.append({"id": uuid.uuid4().hex, "context": context, "kind": "hypothetical", "intent": title,
            "life_context": anchor["text"][:300] if anchor else "", "chat_id": anchor["chat_id"] if anchor else ""})
    return cards


def current_card(session):
    index, cards = session.get("index", 0), session.get("cards", [])
    if index >= len(cards):
        return None
    return {**cards[index], "session_id": session["id"], "position": index + 1, "total": len(cards)}


def describe_card(card):
    if not card:
        return "Сессия завершена. Твои ответы сохранены как примеры манеры общения. API-токены не расходовались."
    label = "Реальное сообщение из твоей переписки" if card["kind"] == "real" else "Условная ситуация для тренировки"
    life = f"\nИз твоей жизни: {card['life_context']}" if card.get("life_context") else ""
    return (f"{card['position']}/{card['total']}. {card['intent']}\n{label}.{life}\n\n"
        f"Собеседник: «{card['context']}»\n\nКак ты ответишь? Напиши сам текст ответа.")
