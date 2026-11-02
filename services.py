"""Shared application operations for Telegram and the Mini App."""
from __future__ import annotations

import time
import uuid
import copy
import re

from personalization import refresh_style_profile, repair_history, summarize_history
from questions import (build_question_request, choose_plan, clean_question, declined,
                       duplicate_question, grounded_question, normalized, topic_for)
from storage import StaleMemoryError
from owner_memory import catalog, effective_memory, memory_id, contains_excluded
from questions import TOPIC_NAMES
from training import cards_for, current_card, describe_card
from imports import participants, suggested_participant, classify, preview_payload, describe_preview


REASONS = {
    "long": ("Слишком длинно", "По умолчанию отвечай короче; разворачивай объяснение только по ситуации."),
    "formal": ("Слишком официально", "Пиши проще и разговорнее, избегай официальных оборотов."),
    "emojis": ("Лишние эмодзи", "Не используй эмодзи, если я отдельно их не попрошу."),
    "tone": ("Неподходящий тон", ""), "invented": ("Выдуманы факты", ""), "custom": ("Другое замечание", ""),
}


def sync_snapshot(target, source):
    target.clear()
    target.update(source)
    if hasattr(target, "baseline"):
        target.baseline = copy.deepcopy(source.baseline)


def budget_data(api):
    return api.spending.status() if hasattr(api, "spending") else None


def chat_events(api, uid, text, user=None):
    store = api.get_store()
    with store.turn(uid):
        started = time.perf_counter()
        mem = api.load_mem(uid)
        if user:
            mem["user"] = user
        # Conversation is not owner training. Only explicit QA, imports and feedback train.
        request = api.build_prompt(mem, mem["history"], text)
        prep_ms = round(1000 * (time.perf_counter() - started), 1)
        exchange_id = uuid.uuid4().hex
        metrics = {"model": api.MODEL, "estimated_input_tokens": request.estimated_tokens,
                   "prepare_ms": prep_ms, "status": "error"}
        complete, committed = None, False
        try:
            for event in api.gateway.events(request, api.MAX_OUTPUT_TOKENS, 0.6):
                if "delta" in event:
                    yield {"type": "delta", "text": event["delta"]}
                elif "complete" in event:
                    complete = event
            if not complete:
                raise RuntimeError("Не получен завершённый ответ")
            answer = complete["complete"]
            mem["history"].extend([
                {"id": exchange_id + "-user", "exchange_id": exchange_id, "role": "user", "content": text},
                {"id": exchange_id + "-assistant", "exchange_id": exchange_id, "role": "assistant", "content": answer},
            ])
            mem["last_exchange"] = {"id": exchange_id, "date": api.now_str(), "user": text, "assistant": answer}
            mem["message_count"] += 1
            summarize_history(mem)
            api.save_mem(uid, mem)
            metrics.update(complete.get("usage", {}))
            metrics.update(ttft_ms=complete.get("ttft_ms"), status="ok")
            metrics["total_ms"] = round(1000 * (time.perf_counter() - started), 1)
            store.metric(uid, metrics)
            committed = True
            yield {"type": "done", "result": answer, "exchange_id": exchange_id, "metrics": metrics,
                   "budget": budget_data(api), "ok": True}
        except BaseException as exc:
            if committed:
                raise
            metrics.update(status="cancelled" if isinstance(exc, GeneratorExit) else "error",
                           error_type=type(exc).__name__, total_ms=round(1000 * (time.perf_counter() - started), 1))
            store.metric(uid, metrics)
            raise


def chat(api, uid, text, user=None):
    result = None
    for event in chat_events(api, uid, text, user):
        if event["type"] == "done":
            result = event
    return result


def generate_question(api, uid, user=None, telegram=False):
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        if user:
            mem["user"] = user
        plan, rejected = choose_plan(mem), []
        for _ in range(2):
            request = build_question_request(mem, plan, api.CONTEXT_TOKEN_BUDGET, rejected)
            raw = api.ask_openai(request, 180, 0.6)
            try:
                question = clean_question(raw)
            except ValueError:
                rejected.append(str(raw)[:300])
                continue
            if duplicate_question(mem, question) or not grounded_question(plan, question):
                rejected.append(question)
                continue
            break
        else:
            raise ValueError("Не удалось получить новый вопрос по твоим данным. Попробуй ещё раз или добавь новую деталь о себе.")
        record = {"id": uuid.uuid4().hex, "date": api.now_str(), "question": question,
            "normalized": normalized(question), "status": "asked", "topic": plan["topic"],
            "root_id": plan["root_id"], "depth": plan["depth"], "parent_answer_id": plan["parent_answer_id"],
            "chat_id": plan["chat_id"]}
        mem["generated_questions"].append(record)
        state = mem["question_state"]
        state["last_topic"] = plan["topic"]
        counts = state.setdefault("topic_counts", {})
        counts[plan["topic"]] = counts.get(plan["topic"], 0) + 1
        if plan["kind"] == "known_detail":
            visits = state.setdefault("anchor_visits", {})
            visits[plan["root_id"]] = visits.get(plan["root_id"], 0) + 1
        if plan["parent_answer_id"]:
            state["last_followed_answer_id"] = plan["parent_answer_id"]
        if telegram:
            mem["pending_question"], mem["pending_question_id"], mem["mode"] = question, record["id"], "qa_answer"
        api.save_mem(uid, mem)
        return {"ok": True, "result": question, "question_id": record["id"], "budget": budget_data(api)}


def save_qa(api, mem, uid, question, answer, question_id=""):
    store = api.get_store()
    with store.turn(uid):
        current = api.load_mem(uid)
        if hasattr(mem, "generation") and mem.generation != current.generation:
            raise StaleMemoryError("Память была сброшена. Создай новый вопрос.")
        if mem.get("user"):
            current["user"] = mem["user"]
        generated = store.find_question(uid, question_id=question_id) if question_id else store.find_question(uid, normalized=normalized(question))
        if question_id and (not generated or generated["normalized"] != normalized(question)):
            raise ValueError("Этот вопрос изменён или удалён. Создай новый вопрос либо сохрани ручной Q&A.")
        if generated and generated["status"] != "asked":
            raise ValueError("Ответ на этот вопрос уже сохранён")
        item = {"id": uuid.uuid4().hex, "date": api.now_str(), "question": question, "answer": answer, "source": "qa"}
        state = current["question_state"]
        state["last_answer_id"] = item["id"]
        if generated:
            item.update(question_id=generated["id"], question_depth=generated["depth"],
                question_root_id=generated["root_id"], question_topic=generated["topic"], question_chat_id=generated.get("chat_id", ""))
            generated.update(status="answered", answer_id=item["id"])
            current["generated_questions"] = [q for q in current["generated_questions"] if q["id"] != generated["id"]] + [generated]
            if declined(answer):
                state["deferred_topics"] = list(dict.fromkeys([*state.get("deferred_topics", []), generated["topic"]]))
        elif not declined(answer):
            # Voluntarily sharing a new answer reopens its topic.
            state["deferred_topics"] = [t for t in state.get("deferred_topics", []) if t != topic_for(question + " " + answer)]
        current["qa"].append(item)
        extracted = api.extract_local_memory(api.normalize_mem({}), answer)
        for category in ("facts", "preferences"):
            for text in extracted[category]:
                current["owner_facts"].append({"id": item["id"] + ":" + category + ":" + memory_id(text),
                    "text": text, "category": category, "source_id": item["id"], "question": question,
                    "date": item["date"], "chat_id": item.get("question_chat_id", "")})
        refresh_style_profile(current, store.owner_samples(uid))
        api.save_mem(uid, current)
        sync_snapshot(mem, current)
    return "Сохранил твой ответ. Следующие вопросы будут учитывать его."


def today_pattern(api, mem, uid, text):
    mem["daily_state"].append({"id": uuid.uuid4().hex, "date": api.now_str(), "text": text})
    api.save_mem(uid, mem)
    return "Сохранил недавнюю манеру общения. Она не станет постоянной чертой профиля."


def import_chat(api, mem, uid, text, owner_name=""):
    """Internal compatibility helper; user-facing uploads use preview_import first."""
    mem, stats = api.import_telegram_chat_to_memory(mem, text, uid, owner_name)
    api.save_mem(uid, mem)
    if not stats["total"]:
        return "Не нашёл сообщений. Проверь формат экспорта."
    if not stats["mine"]:
        return (f"Сохранил {stats['total']} сообщений в архив, но не определил твои. "
                "Укажи своё имя как в экспорте и повтори импорт. Архив будет обновлён без дубликатов.")
    return (f"Сохранил сообщений: {stats['total']}. Твои: {stats['mine']}; собеседников: {stats['other']}; "
            f"без автора: {stats['unknown']}. Примеров реакций: {stats['pairs']}. Полный текст остался в архиве.")


def preview_import(api, uid, text, owner_name="", user=None, telegram=False, filename=""):
    text = text.lstrip("\ufeff").strip()
    if len(text.encode("utf-8")) > 15 * 1024 * 1024:
        raise ValueError("Экспорт должен быть не больше 15 МБ.")
    records = api.extract_chat_records(text, set())
    if not records:
        raise ValueError("Не нашёл сообщений. Экспортируй один чат в JSON, HTML или TXT.")
    if len(records) > 100000:
        raise ValueError("В файле больше 100000 сообщений. Раздели экспорт на части.")
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        if user:
            mem["user"] = user
        people = participants(records, uid)
        source_format = "HTML" if text.startswith("<") else "JSON" if text.startswith(("{", "[")) and not re.match(r"^\[\d{1,2}\.", text) else "TXT"
        data = {"records": records, "participants": people, "selected": suggested_participant(people, mem["user"], owner_name),
            "revision": 0, "format": source_format, "filename": re.split(r"[\\/]", filename)[-1][:255]}
        token = uuid.uuid4().hex
        api.get_store().stage_import(uid, mem.generation, token, data)
        if telegram:
            mem["pending_import_token"], mem["mode"] = token, "import_review"
            mem["pending_import_revision"], mem["pending_import_participant"] = data["revision"], data["selected"]
        api.save_mem(uid, mem)
        preview = preview_payload({"data": data}, token)
        return {"ok": True, "result": describe_preview(preview), "preview": preview}


def get_import_preview(api, uid, token=""):
    stage = api.get_store().pending_import(uid, token)
    if not stage:
        if token:
            raise ValueError("Предпросмотр не найден или устарел. Загрузи файл заново.")
        return {"ok": True, "result": "Загрузи файл или вставь переписку для предпросмотра.", "preview": None}
    mem = api.load_mem(uid)
    if stage["generation"] != mem.generation:
        raise StaleMemoryError("Память была сброшена. Загрузи файл заново.")
    preview = preview_payload(stage, stage["token"])
    return {"ok": True, "result": describe_preview(preview), "preview": preview}


def select_import_owner(api, uid, token, participant, revision):
    with api.get_store().turn(uid):
        stage = api.get_store().pending_import(uid, token)
        if not stage or stage["data"]["revision"] != revision:
            raise ValueError("Предпросмотр изменился. Открой его заново.")
        data = stage["data"]
        classify(data["records"], data["participants"], participant)
        data["selected"], data["revision"] = participant, revision + 1
        api.get_store().update_import(uid, token, stage["generation"], data)
        preview = preview_payload({"data": data}, token)
        return {"ok": True, "result": describe_preview(preview), "preview": preview}


def confirm_import(api, uid, token, participant, revision, archive_only=False):
    with api.get_store().turn(uid):
        stage = api.get_store().pending_import(uid, token)
        if not stage:
            raise ValueError("Предпросмотр уже обработан или устарел. Загрузи файл заново.")
        data = stage["data"]
        if data["revision"] != revision or data["selected"] != participant:
            raise ValueError("Автор изменился. Проверь новый предпросмотр перед подтверждением.")
        mem = api.load_mem(uid)
        if mem.generation != stage["generation"]:
            raise StaleMemoryError("Память была сброшена. Загрузи файл заново.")
        records = classify(data["records"], data["participants"], participant, archive_only)
        mem, stats = api.merge_import_records(mem, records)
        selected = next((p for p in data["participants"] if p["key"] == participant), None)
        imported = next(c for c in mem["imported_chats"] if c["id"] == records[0]["chat_id"])
        imported.update(filename=data.get("filename", ""), archive_only=bool(archive_only),
            owner_names=selected["names"] if selected and not archive_only else [],
            owner_from_id=selected["from_id"] if selected and not archive_only else "",
            owner_verified_by_id=bool(selected and selected["verified_self"] and not archive_only))
        if mem.get("pending_import_token") == token:
            mem["pending_import_token"] = ""
            if mem["mode"] == "import_review":
                mem["mode"] = "chat"
        # Publishing the archive and consuming its preview are one transaction.
        api.get_store().save(uid, mem, import_token=token)
        result = (f"Сохранил только архив: {stats['total']} сообщений. Он не добавляет примеров для обучения."
            if archive_only else f"Импорт подтверждён. Сообщений: {stats['total']}, твоих: {stats['mine']}, пар ответов: {stats['pairs']}.")
        return {"ok": True, "result": result + " API-токены не расходовались.", "preview": None}


def cancel_import(api, uid, token):
    if not token:
        raise ValueError("Выбери предпросмотр для отмены")
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        api.get_store().discard_import(uid, token)
        if mem.get("pending_import_token") == token:
            mem["pending_import_token"] = ""
            if mem["mode"] == "import_review":
                mem["mode"] = "chat"
            api.save_mem(uid, mem)
    return {"ok": True, "result": "Импорт отменён. Активная память не изменилась.", "preview": None}


def last_exchange(mem, expected_id=""):
    last = mem.get("last_exchange", {})
    if not last:
        raise ValueError("Пока нет ответа для оценки")
    if expected_id and expected_id != last.get("id"):
        raise ValueError("Уже появился новый ответ. Оцени последний ответ двойника.")
    return last


def _feedback(api, mem, uid, corrected=None, expected_id="", reason="", note="", scope="situation"):
    last = last_exchange(mem, expected_id)
    exchange_id = last.get("id") or "legacy-last"
    if corrected is not None and api.normalize_for_compare(corrected) == api.normalize_for_compare(last.get("assistant", "")):
        raise ValueError("Ответ не изменён. Напиши свой вариант.")
    answer = corrected if corrected is not None else last["assistant"]
    key = "correction_examples" if corrected is not None else "good_examples"
    item = {"id": exchange_id, "date": api.now_str(), "user": last["user"],
            "source": "correction" if corrected is not None else "approved"}
    if corrected is not None:
        item.update(bad_answer=last["assistant"], correct_answer=answer)
        mem["good_examples"] = [e for e in mem["good_examples"] if e.get("id") != exchange_id]
        repair_history(mem, exchange_id, answer)
        last["assistant"] = answer
    else:
        item["assistant"] = answer
    if reason:
        apply_reason(mem, item, reason, note, scope)
        if scope == "general":
            api.build_prompt(mem, [], "Привет")
    mem[key] = [e for e in mem[key] if e.get("id") != exchange_id] + [item]
    mem["feedback"] = [e for e in mem["feedback"] if e.get("id") != exchange_id] + [{**item, "rating": "corrected" if corrected is not None else "good"}]
    api.save_mem(uid, mem)
    return "Исправление сохранил. Буду использовать твой вариант." if corrected is not None else "Сохранил подтверждённый пример ответа."


def feedback(api, mem, uid, corrected=None, expected_id="", reason="", note="", scope="situation"):
    with api.get_store().turn(uid):
        current = api.load_mem(uid)
        if hasattr(mem, "generation") and mem.generation != current.generation:
            raise StaleMemoryError("Память была сброшена. Оцени новый ответ.")
        # Bind Telegram's old screen as well as a web card to its particular exchange.
        expected_id = expected_id or mem.get("last_exchange", {}).get("id", "")
        result = _feedback(api, current, uid, corrected, expected_id, reason, note, scope)
        sync_snapshot(mem, current)
        return result


def apply_reason(mem, item, reason, note="", scope="situation"):
    if reason not in REASONS or scope not in {"situation", "general"}:
        raise ValueError("Выбери причину исправления и область её действия")
    title, rule = REASONS[reason]
    if reason == "custom" and not note.strip():
        raise ValueError("Напиши своё замечание")
    item.update(reason=reason, reason_note=(title + (": " + note.strip() if note.strip() else ""))[:1200], scope=scope)
    if scope == "general":
        rule = note.strip() if reason == "custom" else rule
        if rule and rule not in mem["style_rules"]:
            mem["style_rules"].append(rule)


def feedback_reason(api, uid, exchange_id, reason):
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        item = next((e for e in mem["correction_examples"] if e["id"] == exchange_id), None)
        if not item:
            raise ValueError("Исправление не найдено. Оцени новый ответ.")
        apply_reason(mem, item, reason)
        for rated in mem["feedback"]:
            if rated.get("id") == exchange_id:
                rated.update(reason=item["reason"], reason_note=item["reason_note"], scope="situation")
        api.save_mem(uid, mem)
    return "Сохранил причину для этой ситуации. Общее правило можно добавить в «Правила общения»."


def start_training(api, uid, telegram=False):
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        session = mem["training_session"]
        if not current_card(session):
            session = {"id": uuid.uuid4().hex, "index": 0, "cards": cards_for(mem)}
            mem["training_session"] = session
            mem["training_round"] += 1
        if telegram:
            mem["mode"] = "training_answer"
        api.save_mem(uid, mem)
        card = current_card(session)
        return {"ok": True, "result": describe_card(card), "card": card}


def answer_training(api, uid, session_id, card_id, answer="", skip=False, telegram=False):
    if not skip and not answer.strip():
        raise ValueError("Напиши свой ответ или пропусти ситуацию")
    if len(answer) > 4000:
        raise ValueError("Ответ для тренировки слишком длинный")
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        session = mem["training_session"]
        card = current_card(session)
        if not card or session.get("id") != session_id or card["id"] != card_id:
            raise ValueError("Эта ситуация уже пройдена или изменена. Открой текущую тренировку.")
        if not skip:
            mem["reaction_examples"].append({"id": card["id"], "context": card["context"], "answer": answer.strip(),
                "source": "rehearsal", "kind": card["kind"], "life_context": card["life_context"], "date": api.now_str(),
                "chat_id": card["chat_id"], "intent": card["intent"]})
            # Rehearsals train manner only. Never extract biography from their answers.
            refresh_style_profile(mem, api.get_store().owner_samples(uid))
        session["index"] += 1
        next_card = current_card(session)
        if telegram:
            mem["mode"] = "training_answer" if next_card else "chat"
        api.save_mem(uid, mem)
        return {"ok": True, "result": describe_card(next_card), "card": next_card}


def memory_catalog(api, uid):
    mem = api.load_mem(uid)
    state = mem["question_state"]
    topics = [{"id": key, "name": name, "blocked": key in state.get("blocked_topics", []),
        "deferred": key in state.get("deferred_topics", [])} for key, name in TOPIC_NAMES.items() if key not in {"life", "identity"}]
    return {"ok": True, "result": "Выбери запись: можно уточнить её или отключить. Источник и дата показаны ниже.",
        "entries": catalog(mem), "topics": topics}


def update_memory(api, uid, entry_id, text="", enabled=True):
    text = text.strip()
    if len(text) > 1000:
        raise ValueError("Уточнение должно быть не длиннее 1000 символов")
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        entry = next((item for item in catalog(mem) if item["id"] == entry_id), None)
        if not entry:
            raise ValueError("Запись не найдена. Обнови список памяти.")
        existing = next((item for item in mem["memory_controls"] if item["id"] == entry_id), {})
        control = {**existing, "id": entry_id, "original": entry["original"], "enabled": bool(enabled), "updated": api.now_str()}
        if text:
            control["replacement"] = text if memory_id(text) != entry_id else ""
        mem["memory_controls"] = [item for item in mem["memory_controls"] if item["id"] != entry_id] + [control]
        mem["summary"], mem["style_profile"], mem["training_session"] = "", "", {}
        active = effective_memory(mem)
        if contains_excluded(active, mem.get("pending_question", "")):
            mem["pending_question"], mem["pending_question_id"] = "", ""
            if mem["mode"] == "qa_answer":
                mem["mode"] = "chat"
        if mem["mode"] == "training_answer":
            mem["mode"] = "chat"
        api.save_mem(uid, mem)
    result = memory_catalog(api, uid)
    result["result"] = "Память обновлена. Изменение учитывается в следующих ответах и вопросах."
    return result


def set_question_topic(api, uid, topic, blocked):
    if topic not in TOPIC_NAMES or topic in {"life", "identity"}:
        raise ValueError("Неизвестная тема")
    with api.get_store().turn(uid):
        mem = api.load_mem(uid)
        state = mem["question_state"]
        values = set(state.get("blocked_topics", []))
        values.add(topic) if blocked else values.discard(topic)
        state["blocked_topics"] = sorted(values)
        if not blocked:
            state["deferred_topics"] = [t for t in state.get("deferred_topics", []) if t != topic]
        mem["training_session"] = {}
        if mem["mode"] == "training_answer":
            mem["mode"] = "chat"
        api.save_mem(uid, mem)
    result = memory_catalog(api, uid)
    result["result"] = "Тема закрыта для вопросов и тренировок." if blocked else "Вопросы по этой теме снова разрешены."
    return result
