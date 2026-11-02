"""Small, local Russian-aware lexical index; no extra model calls."""
import re
from datetime import datetime

STOP = set("это как что где когда меня тебе тебя мне для или про без уже так вот все да нет при чем чем бы был была они она оно мы вы он я ты на из по за от до во со не ну и а в с к у о".split())
SUFFIXES = sorted("иями ями ами ого ему ому ими ыми иях ах ях ов ев ей ий ый ая яя ое ее ые ие ою ею ить ать ять еть ть ешь ете ем ут ют ет ит ат ят ы и а я у ю е о".split(), key=len, reverse=True)
INTENTS = {
    "greeting": {"привет", "здравствуй", "здравствуйте", "дарова", "здарова", "хай"},
    "status": {"дела", "делишки", "самочувствие", "настроение"},
    "invitation": {"пойдем", "пойдём", "погуляем", "гулять", "прогулка", "встретимся", "увидимся"},
    "refusal": {"отказ", "отказать", "откажешь", "отказываешь", "отказаться"},
    "anger": {"злишься", "злюсь", "раздражает", "бесит", "разозлился"},
}


def terms(text):
    words = re.findall(r"[a-zа-яё0-9_]{2,}", str(text).lower().replace("ё", "е"))
    result = set()
    for word in words:
        if word in STOP:
            continue
        stem = word
        for suffix in SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                stem = word[:-len(suffix)]
                break
        result.add(stem)
        for intent, aliases in INTENTS.items():
            if word in {w.replace("ё", "е") for w in aliases}:
                result.add(intent)
    return result


def rank_examples(items, query, limit=6):
    query_terms = terms(query)
    unique = {}
    for index, item in enumerate(items):
        context = str(item.get("context", item.get("question", item.get("user", ""))))
        answer = str(item.get("answer", item.get("correct_answer", item.get("assistant", ""))))
        if not context or not answer:
            continue
        key = (context.strip().lower(), answer.strip().lower())
        overlap = len(terms(context) & query_terms)
        source_bonus = 2 if item.get("source") == "correction" else 1 if item.get("source") in {"qa", "approved", "rehearsal"} else 0
        score = (bool(overlap), overlap, source_bonus, index)
        if key not in unique or score > unique[key][0]:
            unique[key] = (score, {**item, "context": context, "answer": answer})
    ranked = sorted(unique.values(), key=lambda row: row[0], reverse=True)
    selected, per_chat = [], {}
    for score, item in ranked:
        # Without a lexical match, use only explicitly approved examples.
        if not score[0] and item.get("source") not in {"correction", "qa", "approved", "rehearsal"}:
            continue
        chat = item.get("chat_id", "")
        if chat and per_chat.get(chat, 0) >= 3:
            continue
        selected.append(item)
        per_chat[chat] = per_chat.get(chat, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def dialogue_examples(records):
    """Group adjacent owner replies, preserving explicit Telegram reply links."""
    by_id = {str(r.get("message_id")): r for r in records if r.get("message_id")}
    examples, pending, last_example, previous_role = [], [], None, ""
    for index, record in enumerate(records):
        role = record.get("role", "unknown")
        if role == "other":
            pending.append(record)
            last_example = None
        elif role == "me" and not record.get("forwarded"):
            if record.get("reply_to"):
                target = by_id.get(str(record["reply_to"]))
                context = target["text"] if target and target.get("role") == "other" else ""
            elif previous_role == "me" and last_example:
                last_example["answer"] += "\n" + record["text"]
                continue
            else:
                recent = pending[-3:]
                if recent and recent[-1].get("date") and record.get("date"):
                    try:
                        gap = (datetime.fromisoformat(record["date"]) - datetime.fromisoformat(recent[-1]["date"])).total_seconds()
                        if gap < 0 or gap > 86400:
                            recent = []
                    except (ValueError, TypeError):
                        pass
                context = "\n".join(r["text"] for r in recent)
            if context:
                last_example = {"id": record.get("id", f"legacy-import-{index}"),
                    "chat_id": record.get("chat_id", "legacy-import"), "date": record.get("date", ""),
                    "context": context, "answer": record["text"], "source": "telegram"}
                examples.append(last_example)
            pending = []
        else:
            pending, last_example = [], None
        previous_role = role
    return examples
