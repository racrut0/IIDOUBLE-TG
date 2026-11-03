"""Local, reviewable life-memory controls with source information."""
from __future__ import annotations

import hashlib
import re


SOURCE_NAMES = {"facts": "Факт", "preferences": "Предпочтение", "import_facts": "Твоя переписка", "survey": "Анкета", "owner_facts": "Твой ответ"}


def normalized(value):
    return " ".join(re.findall(r"\w+", str(value).lower().replace("ё", "е")))


def memory_id(text):
    return hashlib.sha256(normalized(text).encode()).hexdigest()[:24]


def catalog(mem):
    controls = {item["id"]: item for item in mem.get("memory_controls", [])}
    entries = {}

    def add(text, category, date="", context="", slot=""):
        text = str(text).strip()
        if not text:
            return
        key = memory_id(text)
        item = entries.setdefault(key, {"id": key, "original": text, "text": text, "enabled": True,
            "sources": [], "date": date, "context": context, "category": category, "slot": slot})
        label = SOURCE_NAMES[category]
        if label not in item["sources"]:
            item["sources"].append(label)
        if context:
            item["context"] = context
        if date:
            item["date"] = date
        if slot:
            item["slot"] = slot

    for category in ("facts", "preferences"):
        for text in mem.get(category, []):
            add(text, category)
    for item in mem.get("import_facts", []):
        add(item.get("text", ""), "import_facts", item.get("date", ""))
    for item in mem.get("owner_facts", []):
        add(item.get("text", ""), "owner_facts", item.get("date", ""), item.get("question", ""))
    # Survey style answers are managed in the existing style/rules screen.
    slots = {0: "name", 1: "age", 2: "place", 3: "occupation", 4: "goal", 5: "project"}
    for index, item in enumerate(mem.get("survey", {}).get("answers", [])):
        if index in {*range(9), 16, 17, 18}:
            add(item.get("answer", ""), "survey", item.get("date", ""), item.get("question", ""), slots.get(index, ""))
    for item in entries.values():
        control = controls.get(item["id"], {})
        item["text"] = control.get("replacement") or item["text"]
        item["enabled"] = control.get("enabled", True)
        item["updated"] = control.get("updated", "")
        value = normalized(item["text"])
        if not item["slot"]:
            for slot, pattern in (("place", r"\b(?:я )?живу\b"), ("occupation", r"\b(?:я )?работаю\b"), ("name", r"\bменя зовут\b")):
                if re.search(pattern, value):
                    item["slot"] = slot
                    break
    slots = {}
    for item in entries.values():
        if item["enabled"] and item["slot"]:
            slots.setdefault(item["slot"], set()).add(normalized(item["text"]))
    conflicts = {slot for slot, values in slots.items() if len(values) > 1}
    for item in entries.values():
        item["needs_review"] = item["slot"] in conflicts
    return list(entries.values())


def contains_excluded(mem, value):
    if not getattr(mem, "excluded_texts", None):
        return False
    if isinstance(value, dict):
        return any(contains_excluded(mem, part) for key, part in value.items()
            if key not in {"id", "chat_id", "message_id", "date", "source"})
    if isinstance(value, list):
        return any(contains_excluded(mem, part) for part in value)
    if not isinstance(value, str):
        return False
    haystack = " " + normalized(value) + " "
    return any(" " + text + " " in haystack for text in mem.excluded_texts)


def usable_records(mem, values):
    return [item for item in values if not contains_excluded(mem, item)]


class MemoryView(dict):
    pass


def effective_memory(mem):
    if isinstance(mem, MemoryView):
        return mem
    view = MemoryView(mem)
    if hasattr(mem, "store"):
        view.store, view.uid = mem.store, mem.uid
    controls = [item for item in mem.get("memory_controls", []) if not item.get("enabled", True) or item.get("replacement")]
    view.excluded_texts = {normalized(item["original"]) for item in controls if normalized(item.get("original", ""))}
    if not controls:
        return view
    for category in ("facts", "preferences", "import_facts", "owner_facts", "style", "style_rules", "qa", "examples", "reaction_examples", "good_examples",
                     "correction_examples", "imported_dialogues", "daily_events", "daily_state", "generated_questions", "history"):
        view[category] = usable_records(view, mem.get(category, []))
    survey = mem.get("survey", {})
    view["survey"] = {**survey, "answers": usable_records(view, [{**item, "_index": index} for index, item in enumerate(survey.get("answers", []))])}
    # Statistical/summary text could preserve a superseded detail; rebuild only from active sources.
    view["summary"], view["style_profile"], view["conversation_summaries"] = "", "", {}
    view["current_memory_updates"] = []
    for item in controls:
        if item.get("enabled", True) and item.get("replacement"):
            view["facts"].append(item["replacement"])
            view["current_memory_updates"].append({"text": item["replacement"], "date": item.get("updated", "")})
    return view
