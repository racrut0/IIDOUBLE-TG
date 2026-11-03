"""Telegram screens for interlocutors and a fixed, owner-rated comparison."""
from __future__ import annotations

import asyncio
import re

from telegram import ReplyKeyboardMarkup

import benchmarks
import interlocutors


PEOPLE = "👥 Собеседники"
BENCHMARK = "🧪 Проверка сходства"
ADD = "➕ Добавить собеседника"
EDIT = "✏️ Изменить текущего"
DELETE = "🗑 Удалить текущего"
KEEP = "Оставить как есть"
NO_NOTES = "⏭ Без пожеланий"
CREATE = "Подобрать 20 ситуаций"
PREPARE = "🧪 Подготовить прогон · бесплатно"
GENERATE = "▶️ Получить ответ · API"
RETRY = "🔁 Повторить ответ · API"
NEXT = "➡️ Следующая ситуация"
RATING_BUTTONS = {"✅ Реакция похожа": "like", "➖ Частично": "partial", "❌ Реакция не похожа": "unlike"}


def keyboard(api, rows):
    return ReplyKeyboardMarkup([*rows, [api.BTN_BACK]], resize_keyboard=True)


def state(api, uid, mode, key, data):
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        mem["mode"], mem[key] = mode, data
        api.save_mem(uid, mem)


async def open_people(api, update, uid):
    data = interlocutors.catalog(api, uid)
    current = next((p for p in data["interlocutors"] if p["id"] == data["active_interlocutor_id"]), None)
    lines = [f"Сейчас: {current['label'] if current else 'общая манера'}.", "0. Общая манера"]
    lines.extend(f"{index}. {p['label']} — {interlocutors.RELATIONSHIPS[p['relationship']]}" for index, p in enumerate(data["interlocutors"], 1))
    lines.append("Выбери номер. Настройки и переключение не расходуют API.")
    state(api, uid, "people_select", "ui_people", {"ids": [p["id"] for p in data["interlocutors"]], "current": data["active_interlocutor_id"]})
    rows = [["0. Общая манера"]] + [[f"{index}. {p['label']}"] for index, p in enumerate(data["interlocutors"], 1)] + [[ADD]]
    if current:
        rows.append([EDIT, DELETE])
    await api.safe_reply(update, "\n".join(lines), reply_markup=keyboard(api, rows))


async def ask_chats(api, update, uid, draft):
    chats = [item for item in interlocutors.chat_catalog(api, uid) if not item["archive_only"]]
    draft["available_chats"] = [item["id"] for item in chats]
    lines = ["Какие переписки относятся к этому человеку? Напиши номера через запятую или 0 без привязки."]
    lines += [f"{index}. {chat['label']} — {chat['messages']} сообщений" for index, chat in enumerate(chats, 1)]
    state(api, uid, "people_chats", "ui_people", draft)
    await api.send_long_message(update, "\n".join(lines), reply_markup=keyboard(api, [["0"], [KEEP]] if draft.get("id") else [["0"]]))


async def people_flow(api, update, mem, uid, text):
    mode, draft = mem["mode"], dict(mem.get("ui_people", {}))
    if mode == "people_select":
        if text in {ADD, EDIT}:
            old = interlocutors.profile_for(mem, draft.get("current", "")) if text == EDIT else None
            if text == EDIT and not old:
                raise ValueError("Сначала выбери собеседника.")
            state(api, uid, "people_name", "ui_people", dict(old or {}))
            await api.safe_reply(update, "Как назвать собеседника? До 60 символов." + (f" Сейчас: {old['label']}" if old else ""),
                reply_markup=keyboard(api, [[KEEP]] if old else []))
            return
        if text == DELETE:
            result = interlocutors.delete(api, uid, draft.get("current", ""))
            await api.safe_reply(update, result["result"])
            await open_people(api, update, uid)
            return
        match = re.fullmatch(r"(\d+)(?:\..*)?", text)
        if not match or int(match[1]) > len(draft.get("ids", [])):
            raise ValueError("Выбери номер собеседника или добавь нового.")
        number = int(match[1])
        result = interlocutors.select(api, uid, draft["ids"][number - 1] if number else "")
        await api.safe_reply(update, result["result"], reply_markup=api.main_menu())
    elif mode == "people_name":
        label = draft.get("label", "") if text == KEEP else text.strip()
        if not label or len(label) > 60:
            raise ValueError("Напиши название от 1 до 60 символов.")
        draft["label"] = label
        state(api, uid, "people_relationship", "ui_people", draft)
        labels = list(interlocutors.RELATIONSHIPS.values())
        await api.safe_reply(update, "Кем тебе приходится этот человек?", reply_markup=keyboard(api, [labels[i:i+2] for i in range(0, len(labels), 2)]))
    elif mode == "people_relationship":
        relationship = next((key for key, label in interlocutors.RELATIONSHIPS.items() if label == text), "")
        if not relationship:
            raise ValueError("Выбери тип отношений кнопкой.")
        draft["relationship"] = relationship
        await ask_chats(api, update, uid, draft)
    elif mode == "people_chats":
        if text != KEEP or not draft.get("id"):
            if not re.fullmatch(r"\d+(?:\s*[, ]\s*\d+)*", text):
                raise ValueError("Напиши номера через запятую, например 1, 3, или 0.")
            numbers = [int(number) for number in re.findall(r"\d+", text)]
            if any(number > len(draft["available_chats"]) for number in numbers) or (0 in numbers and len(numbers) > 1):
                raise ValueError("Таких номеров нет. 0 означает без привязки чата.")
            draft["chat_ids"] = [draft["available_chats"][number - 1] for number in numbers if number]
        state(api, uid, "people_notes", "ui_people", draft)
        await api.safe_reply(update, "Как ты общаешься именно с ним? До 400 символов. Например: «давно дружим, часто подшучиваем друг над другом».",
            reply_markup=keyboard(api, [[NO_NOTES], [KEEP]] if draft.get("id") else [[NO_NOTES]]))
    elif mode == "people_notes":
        notes = draft.get("notes", "") if text == KEEP else "" if text == NO_NOTES else text
        result = interlocutors.save(api, uid, draft["label"], draft["relationship"], draft.get("chat_ids", []), notes, draft.get("id", ""))
        selected = interlocutors.select(api, uid, result["saved_interlocutor_id"])
        await api.safe_reply(update, result["result"] + "\n" + selected["result"], reply_markup=api.main_menu())


async def open_benchmark(api, update, uid, run_id="", case_id=""):
    data = benchmarks.get(api, uid, run_id)
    suite, run = data["suite"], data["run"]
    if not suite:
        chats = [item for item in data["chats"] if not item["archive_only"]]
        state(api, uid, "benchmark_source", "ui_benchmark", {"chat_ids": [item["id"] for item in chats]})
        lines = ["Создадим постоянный набор из 20 ситуаций с твоими реальными ответами. Выбери номер чата или доверь подбор боту.",
            "Этот чат будет исключён из примеров во время проверки. Подготовка бесплатна."]
        lines += [f"{index}. {chat['label']} — {chat['messages']} сообщений" for index, chat in enumerate(chats, 1)]
        await api.send_long_message(update, "\n".join(lines), reply_markup=keyboard(api, [[CREATE]]))
        return
    case = next((item for item in suite["cases"] if item["id"] == case_id), None)
    if not case:
        case = next((item for item in suite["cases"] if not run or not run["states"][item["id"]].get("rating")), suite["cases"][0])
    state(api, uid, "benchmark_case", "ui_benchmark", {"run_id": run["id"] if run else "", "case_id": case["id"]})
    index = suite["cases"].index(case) + 1
    lines = [f"Ситуация {index}/20", case["context"]]
    rows = [[PREPARE]]
    if run:
        current = run["states"][case["id"]]
        score = f"{run['score']:g}%" if run["score"] is not None else "пока без оценок"
        lines += [f"\nСобеседник: {current['interlocutor']}", f"Оценено {run['rated']}/20. Сходство по твоим оценкам: {score}."]
        if current.get("answer"):
            lines += ["\nОтвет двойника:", current["answer"], "\nТвой ответ в переписке:", case["expected_answer"]]
            rows = [list(RATING_BUTTONS)[:2], [list(RATING_BUTTONS)[2]], [NEXT], [PREPARE]]
        else:
            lines.append("Получение ответа использует API. Автоматического запуска 20 запросов нет.")
            rows = [[GENERATE if current["status"] == "pending" else RETRY], [NEXT], [PREPARE]]
        lines.append(f"Резерв на 20 ответов: до ${run['maximum_usd']:.4f}; предел прогона $0.05.")
    else:
        lines.append("Набор сохранён. Подготовь прогон бесплатно, затем получай ответы по одному.")
    previous = [item for item in data["run_history"] if item["rated"] and item["id"] != (run["id"] if run else "") and item["suite_id"] == suite["id"]]
    if previous:
        lines.append("\nПредыдущие проверки: " + "; ".join(f"{item['created']}: {item['score']:g}% ({item['rated']}/20)" for item in previous[:3]))
    await api.send_long_message(update, "\n".join(lines), reply_markup=keyboard(api, rows))


async def benchmark_flow(api, update, mem, uid, text):
    draft = mem.get("ui_benchmark", {})
    if mem["mode"] == "benchmark_source":
        chat_ids = draft.get("chat_ids", [])
        if text != CREATE and (not text.isdecimal() or not 1 <= int(text) <= len(chat_ids)):
            raise ValueError("Выбери номер чата или нажми «Подобрать 20 ситуаций».")
        result = await asyncio.to_thread(benchmarks.create_suite, api, uid, "" if text == CREATE else chat_ids[int(text) - 1])
        await api.safe_reply(update, result["result"])
        await open_benchmark(api, update, uid)
        return
    run_id, case_id = draft.get("run_id", ""), draft.get("case_id", "")
    if text == PREPARE:
        result = await asyncio.to_thread(benchmarks.start_run, api, uid)
        await api.safe_reply(update, result["result"])
        await open_benchmark(api, update, uid, result["run"]["id"])
    elif text in {GENERATE, RETRY}:
        await asyncio.to_thread(benchmarks.generate, api, uid, run_id, case_id, text == RETRY)
        await open_benchmark(api, update, uid, run_id, case_id)
    elif text in RATING_BUTTONS:
        result = benchmarks.rate(api, uid, run_id, case_id, RATING_BUTTONS[text])
        await open_benchmark(api, update, uid, run_id)
    elif text == NEXT:
        data = benchmarks.get(api, uid, run_id)
        cases = data["suite"]["cases"]
        index = next((index for index, item in enumerate(cases) if item["id"] == case_id), -1)
        await open_benchmark(api, update, uid, run_id, cases[(index + 1) % len(cases)]["id"])
    else:
        raise ValueError("Выбери действие кнопкой. Платный ответ запускается только отдельной кнопкой API.")


async def handle_flow(api, update, mem, uid, text):
    if text in {api.BTN_BACK, PEOPLE, BENCHMARK}:
        return False
    if mem.get("mode", "").startswith("people_"):
        await people_flow(api, update, mem, uid, text)
        return True
    if mem.get("mode", "").startswith("benchmark_"):
        await benchmark_flow(api, update, mem, uid, text)
        return True
    return False
