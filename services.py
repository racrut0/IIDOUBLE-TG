"""Shared application operations for Telegram and the Mini App."""
from __future__ import annotations

import time
import uuid

from personalization import refresh_style_profile, repair_history, summarize_history


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
            yield {"type": "done", "result": answer, "exchange_id": exchange_id, "metrics": metrics, "ok": True}
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


def save_qa(api, mem, uid, question, answer):
    mem["qa"].append({"id": uuid.uuid4().hex, "date": api.now_str(), "question": question, "answer": answer, "source": "qa"})
    api.extract_local_memory(mem, answer)
    refresh_style_profile(mem, api.get_store().owner_samples(uid))
    api.save_mem(uid, mem)
    return "Сохранил ситуацию и твой ответ как пример для обучения."


def today_pattern(api, mem, uid, text):
    mem["daily_state"].append({"id": uuid.uuid4().hex, "date": api.now_str(), "text": text})
    api.save_mem(uid, mem)
    return "Сохранил недавнюю манеру общения. Она не станет постоянной чертой профиля."


def import_chat(api, mem, uid, text, owner_name=""):
    mem, stats = api.import_telegram_chat_to_memory(mem, text, uid, owner_name)
    api.save_mem(uid, mem)
    if not stats["total"]:
        return "Не нашёл сообщений. Проверь формат экспорта."
    if not stats["mine"]:
        return (f"Сохранил {stats['total']} сообщений в архив, но не определил твои. "
                "Укажи своё имя как в экспорте и повтори импорт. Архив будет обновлён без дубликатов.")
    return (f"Сохранил сообщений: {stats['total']}. Твои: {stats['mine']}; собеседников: {stats['other']}; "
            f"без автора: {stats['unknown']}. Примеров реакций: {stats['pairs']}. Полный текст остался в архиве.")


def last_exchange(mem, expected_id=""):
    last = mem.get("last_exchange", {})
    if not last:
        raise ValueError("Пока нет ответа для оценки")
    if expected_id and expected_id != last.get("id"):
        raise ValueError("Уже появился новый ответ. Оцени последний ответ двойника.")
    return last


def feedback(api, mem, uid, corrected=None, expected_id=""):
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
    mem[key] = [e for e in mem[key] if e.get("id") != exchange_id] + [item]
    mem["feedback"] = [e for e in mem["feedback"] if e.get("id") != exchange_id] + [{**item, "rating": "corrected" if corrected is not None else "good"}]
    api.save_mem(uid, mem)
    return "Исправление сохранил. Буду использовать твой вариант." if corrected is not None else "Сохранил подтверждённый пример ответа."
