"""A private, fixed 20-case comparison. Preparing and rating never call a model."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import defaultdict
from contextlib import closing

from interlocutors import active_id, allowed_example, chat_catalog, profile_for
from owner_memory import MemoryView, effective_memory, usable_records
from personalization import ModelRequest, refresh_style_profile
from retrieval import normalized_text, reaction_signals
from spending import SCALE, amount


CASE_COUNT = 20
MAX_RUN_RESERVE = amount("0.05")
PIPELINE_VERSION = "interlocutors-1"
RATINGS = {"like": 1, "partial": 0.5, "unlike": 0}


def signature(api, uid, mem):
    digest = hashlib.sha256(json.dumps({"survey": mem.get("survey"), "style_profile": mem.get("style_profile"),
        "model": api.MODEL, "context": api.CONTEXT_TOKEN_BUDGET, "output": api.MAX_OUTPUT_TOKENS,
        "pipeline": PIPELINE_VERSION}, sort_keys=True, ensure_ascii=False).encode())
    with closing(api.get_store().connect()) as db:
        for row in db.execute("SELECT category,data FROM memory_items WHERE uid=? AND category NOT IN ('history','generated_questions') ORDER BY seq", (str(uid),)):
            digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
    return digest.hexdigest()


def summary(run):
    states = run["states"]
    ratings = [RATINGS[item["rating"]] for item in states.values() if item.get("rating") in RATINGS]
    return {"id": run["id"], "suite_id": run["suite_id"], "created": run["created"], "model": run["model"],
        "generated": sum(bool(item.get("answer")) for item in states.values()), "rated": len(ratings),
        "total": len(states), "score": round(100 * sum(ratings) / len(ratings), 1) if ratings else None,
        "maximum_usd": run["maximum_reserve"] / SCALE, "attempts": sum(item.get("attempts", 0) for item in states.values())}


def get(api, uid, run_id=""):
    store = api.get_store()
    suite = store.evaluation_item(uid, "suite")
    run = store.evaluation_item(uid, "run", run_id)
    if run_id and not run:
        raise ValueError("Проверка не найдена в твоём профиле.")
    public_run = None
    if run:
        public_run = {**summary(run), "states": run["states"]}
    history = [summary(item) for item in store.evaluation_runs(uid)]
    return {"ok": True, "suite": suite, "run": public_run, "run_history": history,
        "chats": chat_catalog(api, uid), "result": "Проверка сходства: подготовка и оценки бесплатны; получение каждого ответа использует API."}


def create_suite(api, uid, chat_id=""):
    with api.get_store().turn(uid):
        store, mem = api.get_store(), api.load_mem(uid)
        existing = store.evaluation_item(uid, "suite")
        if existing:
            return get(api, uid)  # A repeat click cannot quietly change the control set.
        groups = defaultdict(list)
        with closing(store.connect()) as db:
            rows = db.execute("SELECT data FROM memory_items WHERE uid=? AND category='examples' ORDER BY seq", (str(uid),))
            active = effective_memory(mem)
            for row in rows:
                item = json.loads(row[0])
                context, answer = item.get("context", "").strip(), item.get("answer", "").strip()
                if not context or not answer or len(context) > 1000 or len(answer) > 1000:
                    continue
                if len(context.split()) < 3 and not reaction_signals(context):
                    continue
                if usable_records(active, [item]):
                    groups[item.get("chat_id", "")].append(item)
        available = {item["id"] for item in mem["imported_chats"] if not item.get("archive_only")}
        groups = {key: values for key, values in groups.items() if key in available and (not chat_id or key == chat_id)}
        selected, source = [], ""
        current = profile_for(mem)
        preferred = set(current["chat_ids"]) if current else set()
        for candidate, values in sorted(groups.items(), key=lambda row: (row[0] not in preferred, len(row[1]))):
            buckets, seen = defaultdict(list), set()
            for item in sorted(values, key=lambda value: hashlib.sha256(str(value.get("id", value["context"])).encode()).hexdigest()):
                key = normalized_text(item["context"])
                if key in seen:
                    continue
                seen.add(key)
                label = next(iter(sorted(reaction_signals(item["context"]))), "other")
                buckets[label].append(item)
            picked = []
            while any(buckets.values()) and len(picked) < CASE_COUNT:
                for bucket in buckets.values():
                    if bucket and len(picked) < CASE_COUNT:
                        picked.append(bucket.pop())
            if len(picked) == CASE_COUNT:
                selected, source = picked, candidate
                break
        if not selected:
            raise ValueError("Нужен подтверждённый чат минимум с 20 различными содержательными ситуациями и твоими ответами.")
        suite = {"id": uuid.uuid4().hex, "generation": mem.generation, "created": api.now_str(), "source_chat_id": source,
            "cases": [{"id": f"case-{index:02}", "context": item["context"], "expected_answer": item["answer"],
                       "source_example_id": item.get("id", ""), "date": item.get("date", "")} for index, item in enumerate(selected, 1)]}
        store.save_evaluation(uid, "suite", suite, mem.generation)
    return {**get(api, uid), "result": "Сохранены 20 постоянных ситуаций из твоей переписки. API не использовался."}


def evaluation_view(api, mem, suite):
    active = effective_memory(mem)
    view = MemoryView(active)
    for name in ("store", "uid", "excluded_texts"):
        if hasattr(active, name):
            setattr(view, name, getattr(active, name))
    view.excluded_chat_ids = {suite["source_chat_id"]}
    view.excluded_example_contexts = {normalized_text(case["context"]) for case in suite["cases"]}
    profiles = [item for item in mem["interlocutors"] if suite["source_chat_id"] in item["chat_ids"]]
    selected = next((item for item in profiles if item["id"] == active_id(mem)), profiles[0] if len(profiles) == 1 else None)
    view["active_interlocutor_id"] = selected["id"] if selected else ""
    for category in ("qa", "examples", "good_examples", "correction_examples", "reaction_examples"):
        view[category] = [item for item in view.get(category, []) if allowed_example(view, item)]
    view["style_profile"], view["summary"], view["conversation_summaries"], view["history"] = "", "", {}, []
    samples = api.get_store().owner_samples(mem.uid, 1000, excluded_chat_ids=view.excluded_chat_ids)
    refresh_style_profile(view, samples)
    return view


def start_run(api, uid):
    with api.get_store().turn(uid):
        store, mem = api.get_store(), api.load_mem(uid)
        suite = store.evaluation_item(uid, "suite")
        if not suite or suite["generation"] != mem.generation:
            raise ValueError("Сначала создай набор из 20 ситуаций.")
        if not any(item["id"] == suite["source_chat_id"] for item in mem["imported_chats"]):
            raise ValueError("Исходный чат удалён. Этот набор больше не используется.")
        view = evaluation_view(api, mem, suite)
        requests = {case["id"]: api.build_prompt(view, [], case["context"]) for case in suite["cases"]}
        maximum = sum(api.spending.quote(request, api.MODEL, api.MAX_OUTPUT_TOKENS)[0] for request in requests.values())
        status = api.spending.status()
        remaining = min(status["remaining_usd"], status["daily_limit_usd"] - status["daily_accounted_usd"])
        if maximum > MAX_RUN_RESERVE or maximum / SCALE > remaining:
            raise ValueError("Набор не помещается в резерв $0.05 или оставшийся бюджет. Платные запросы не выполнялись.")
        run = {"id": uuid.uuid4().hex, "suite_id": suite["id"], "source_chat_id": suite["source_chat_id"], "generation": mem.generation,
            "created": api.now_str(), "model": api.MODEL, "signature": signature(api, uid, mem),
            "maximum_reserve": maximum, "attempt_reserve": 0,
            "requests": {key: {"instructions": req.instructions, "messages": req.messages, "estimated_tokens": req.estimated_tokens,
                               "sections": req.sections} for key, req in requests.items()},
            "states": {case["id"]: {"status": "pending", "answer": "", "rating": "", "note": "", "attempts": 0,
                "interlocutor": profile_for(view)["label"] if profile_for(view) else "Общая манера"} for case in suite["cases"]}}
        store.save_evaluation(uid, "run", run, mem.generation)
    return {**get(api, uid, run["id"]), "result": f"Прогон подготовлен без API. Резерв на 20 ответов: до ${maximum / SCALE:.4f}; предел одного прогона — $0.05."}


def generate(api, uid, run_id, case_id, retry=False):
    if not run_id or not case_id:
        raise ValueError("Открой подготовленный прогон и выбери ситуацию.")
    with api.get_store().turn(uid):
        store, mem = api.get_store(), api.load_mem(uid)
        run = store.evaluation_item(uid, "run", run_id)
        if not run or case_id not in run["states"]:
            raise ValueError("Ситуация не найдена в твоей проверке.")
        state = run["states"][case_id]
        if state.get("answer"):
            return get(api, uid, run_id)  # Repeating a delivered request is free.
        if run["signature"] != signature(api, uid, mem):
            raise ValueError("Обучающая память или настройки изменились. Подготовь новый прогон на тех же 20 ситуациях.")
        if state["status"] != "pending" and not retry:
            raise ValueError("Предыдущая попытка не завершилась. Повтор с API нужно выбрать отдельно.")
        if state["attempts"] >= 2:
            raise ValueError("Для этой ситуации уже выполнены две попытки. Подготовь новый прогон.")
        saved = run["requests"][case_id]
        request = ModelRequest(saved["instructions"], saved["messages"], str(uid), saved["estimated_tokens"], saved["sections"])
        reserve = api.spending.quote(request, api.MODEL, api.MAX_OUTPUT_TOKENS)[0]
        if run["attempt_reserve"] + reserve > MAX_RUN_RESERVE:
            raise ValueError("Достигнут резерв $0.05 на этот прогон.")
        state.update(status="generating", attempts=state["attempts"] + 1)
        run["attempt_reserve"] += reserve
        store.save_evaluation(uid, "run", run, mem.generation, require_existing=True)
        started, complete = time.perf_counter(), None
        try:
            for event in api.gateway.events(request, api.MAX_OUTPUT_TOKENS, 0.6):
                if "complete" in event:
                    complete = event
            if not complete:
                raise RuntimeError("Не получен завершённый ответ")
            state.update(status="ready", answer=complete["complete"])
            store.save_evaluation(uid, "run", run, mem.generation, require_existing=True)
            store.metric(uid, {"purpose": "benchmark", "model": api.MODEL, "status": "ok",
                "estimated_input_tokens": request.estimated_tokens, "total_ms": round(1000 * (time.perf_counter() - started), 1),
                **complete.get("usage", {})})
        except BaseException:
            state["status"] = "error"
            store.save_evaluation(uid, "run", run, mem.generation, require_existing=True)
            raise
    return {**get(api, uid, run_id), "budget": api.spending.status(), "result": "Ответ готов. Сравни реакцию и поставь свою оценку."}


def rate(api, uid, run_id, case_id, rating, note=""):
    if not run_id or not case_id:
        raise ValueError("Открой подготовленный прогон и выбери ситуацию.")
    if rating not in RATINGS or len(note) > 1000:
        raise ValueError("Выбери «Похоже», «Частично» или «Не похоже»; заметка — до 1000 символов.")
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        run = api.get_store().evaluation_item(uid, "run", run_id)
        if not run or case_id not in run["states"] or not run["states"][case_id].get("answer"):
            raise ValueError("Сначала получи ответ на эту ситуацию.")
        run["states"][case_id].update(rating=rating, note=note.strip())
        api.get_store().save_evaluation(uid, "run", run, mem.generation, require_existing=True)
    return {**get(api, uid, run_id), "result": "Оценка сохранена без API. Она не добавляет тестовую ситуацию в обучение."}
