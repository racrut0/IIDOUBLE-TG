"""Small, local Russian-aware lexical index; no extra model calls."""
import re
import math
from collections import Counter
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

# These describe the incoming situation, never the answer the owner ought to give.
# The owner's real reply can be dry, warm, sarcastic, or a refusal in any category.
REACTION_PATTERNS = {
    "distress": (
        r"\b(?:провал\w*|завалил\w*|расстро\w*|печаль\w*|груст\w*|тяжело|одинок\w*|"
        r"погиб\w*|умер\w*|уволил\w*|разошл\w*|расстал\w*|заболел\w*|больно|"
        r"беда|неудач\w*|ужасн\w*)\b|\bмне плохо\b|\bне (?:справил\w*|получил\w*|сдал\w*)\b"
    ),
    "celebration": r"\b(?:выиграл\w*|победил\w*|ура|радуюсь|счастлив\w*|получилось|успех\w*)\b|\bсдал(?:а|и)? (?:экзам\w*|зачет\w*|сесси\w*|на права)\b",
    "help": r"\b(?:помог\w*|помож\w*|помоч\w*|помощ\w*|выруч\w*|подсоб\w*)\b|\bнужен совет\b",
    "pressure": r"\b(?:игнориру\w*|ответь немедленно)\b|\bпочему ты\b.{0,60}\bне ответ\w*|\bя (?:же |уже )?(?:час |давно )?жду\b",
    "criticism": r"\b(?:зря|ерунд\w*|бессмыслен\w*|глупость)\b|\bне соглас\w*|\bты не прав\b|\bтратишь\b.{0,30}\bврем\w*",
    "clarification": r"\bне (?:совсем )?понял\w*|\b(?:объясни\w*|уточни\w*|поясни\w*|непонятно)\b|\bчто ты имеешь в виду\b",
    "humor": r"\b(?:шутк\w*|пошути\w*|анекдот\w*)\b",
}
REACTION_REGEX = {key: re.compile(pattern) for key, pattern in REACTION_PATTERNS.items()}
RETRIEVAL_STOP = set("сегодня сейчас еще очень просто теперь потом свой твой какой какая какие сколько можно будет быть дальше".split())
FOLLOWUP = re.compile(
    r"(?:(?:а|и|ну|но) )?(?:почему|зачем|как так|что (?:теперь|тогда|дальше)|что делать|"
    r"дальше|тогда|и дальше|продолжи|это как|а ты|да|нет|угу|и|ну)"
)


def normalized_text(text):
    return " ".join(re.findall(r"[a-zа-яё0-9_]+", str(text).lower().replace("ё", "е")))


def reaction_signals(text):
    text = normalized_text(text)
    signals = terms(text) & INTENTS.keys()
    for name, pattern in REACTION_REGEX.items():
        for match in pattern.finditer(text):
            # A negated success is not a celebration ("не сдал", "не получилось").
            if name in {"celebration", "humor", "distress"} and re.search(r"\bне\s+$", text[max(0, match.start() - 16):match.start()]):
                if name == "celebration":
                    signals.add("distress")
                continue
            signals.add(name)
            break
    return signals


def example_context(item):
    return str(item.get("context", item.get("question", item.get("user", ""))))


def search_text(category, item):
    if category == "examples" and isinstance(item, dict):
        context = example_context(item)
        # A word in the answer must not make its incoming situation relevant.
        return context.lower() + " " + " ".join(sorted(terms(context) | {"rx" + s for s in reaction_signals(context)}))
    return None


def dialogue_query(text, history=()):
    """Resolve only generic follow-ups; a new explicit situation starts a new query."""
    if not FOLLOWUP.fullmatch(normalized_text(text)):
        return text
    for message in reversed(history):
        if message.get("role") != "user":
            continue
        previous = str(message.get("content", ""))
        if previous and not FOLLOWUP.fullmatch(normalized_text(previous)):
            return previous[-1000:] + "\n" + text
    return text


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
    latest_corrections = {}
    for index, item in enumerate(items):
        if item.get("source") == "correction":
            key = normalized_text(example_context(item))
            stamp = (str(item.get("date", "")), index)
            if key not in latest_corrections or stamp > latest_corrections[key][0]:
                latest_corrections[key] = (stamp, item)
    items = [item for item in items if item.get("source") != "correction"
             or item is latest_corrections[normalized_text(example_context(item))][1]]
    query_terms = terms(query) - set(INTENTS) - RETRIEVAL_STOP
    query_signals = reaction_signals(query)
    query_normalized = normalized_text(query)
    documents = [terms(example_context(item)) - set(INTENTS) - RETRIEVAL_STOP for item in items]
    frequencies = Counter(term for document in documents for term in document)
    weights = {term: 1 + math.log((len(documents) + 1) / (count + 1)) for term, count in frequencies.items()}
    unique = {}
    corrections = []
    for index, (item, context_terms) in enumerate(zip(items, documents)):
        context = example_context(item)
        answer = str(item.get("answer", item.get("correct_answer", item.get("assistant", ""))))
        if not context or not answer:
            continue
        signals = reaction_signals(context)
        if ((query_signals & {"distress", "celebration"}) == {"distress"} and
                (signals & {"distress", "celebration"}) == {"celebration"}) or (
                (query_signals & {"distress", "celebration"}) == {"celebration"} and
                (signals & {"distress", "celebration"}) == {"distress"}):
            continue
        overlap = context_terms & query_terms
        shared = signals & query_signals
        exact = normalized_text(context) == query_normalized
        if not (overlap or shared or exact):
            continue
        lexical = sum(weights.get(word, 1) for word in overlap) / max(1, sum(weights.get(word, 1) for word in query_terms | context_terms))
        relevant_correction = item.get("source") == "correction" and (exact or lexical >= 0.4 or bool(shared and overlap))
        if relevant_correction and item.get("bad_answer"):
            corrections.append(normalized_text(item["bad_answer"]))
        source_bonus = {"correction": 3, "rehearsal": 2, "approved": 1}.get(item.get("source"), 0)
        score = (relevant_correction, exact, bool(shared), len(shared), lexical, source_bonus, str(item.get("date", "")), index)
        key = (normalized_text(context), normalized_text(answer))
        if key not in unique or score > unique[key][0]:
            unique[key] = (score, {**item, "context": context, "answer": answer})
    ranked = sorted(unique.values(), key=lambda row: row[0], reverse=True)
    selected, per_chat = [], {}
    for score, item in ranked:
        if item.get("source") != "correction" and normalized_text(item["answer"]) in corrections:
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
