import json
import pytest

from fastapi.testclient import TestClient

import bot
import services
import web_backend
from questions import build_question_request, clean_question, grounded_question, normalized
from storage import MemoryStore
from test_api import signed_data


def test_question_generation_receives_actual_owner_life(app_state):
    store, gateway = app_state
    mem = bot.load_mem("123")
    mem["facts"] = ["Я открыл пекарню и хочу развивать своё дело"]
    bot.save_mem("123", mem)
    def complete(request, *args):
        gateway.calls.append(request)
        return "Что оказалось сложнее всего после открытия пекарни?"
    gateway.complete = complete
    response = TestClient(web_backend.app).post("/api/action", json={"action": "generate_question", "initData": signed_data()})
    assert response.status_code == 200
    request = gateway.calls[-1]
    prompt = request.render() if hasattr(request, "render") else request
    assert "пекарню" in prompt


def queued_answers(gateway, *answers):
    queue = iter(answers)
    def complete(request, *args):
        gateway.calls.append(request)
        return next(queue)
    gateway.complete = complete


def request_data(gateway):
    return json.loads(gateway.calls[-1].messages[-1]["content"])


def store_life():
    mem = bot.load_mem("123")
    mem["facts"] = ["Я открыл пекарню"]
    mem["preferences"] = ["В свободное время люблю играть на гитаре"]
    bot.save_mem("123", mem)


def answer_generated(result, text):
    return services.save_qa(bot, bot.load_mem("123"), "123", result["result"], text, result["question_id"])


def test_questions_deepen_answers_then_switch_life_topic_after_restart(app_state, monkeypatch):
    store, gateway = app_state
    store_life()
    # A recent event makes the bakery the first grounded topic.
    mem = bot.load_mem("123")
    mem["daily_events"] = [{"date": "2026-11-02", "text": "Сегодня открыл пекарню"}]
    bot.save_mem("123", mem)
    queued_answers(gateway, "Что оказалось самым сложным после открытия пекарни?",
        "Как ты выбираешь сотрудников для пекарни?", "Как реагируешь на ошибку сотрудника во время смены?",
        "Что тебе больше всего нравится в игре на гитаре?")
    first = services.generate_question(bot, "123")
    assert request_data(gateway)["mode"] == "known_detail"
    answer_generated(first, "Сложнее всего оказалось найти сотрудников")
    monkeypatch.setattr(bot, "_store", MemoryStore(store.path))
    second = services.generate_question(bot, "123")
    payload = request_data(gateway)
    assert payload["mode"] == "follow_up" and payload["depth"] == 1
    assert "найти сотрудников" in payload["anchor"]["text"]
    answer_generated(second, "Сначала провожу пробную смену и смотрю, как человек работает")
    third = services.generate_question(bot, "123")
    payload = request_data(gateway)
    assert payload["depth"] == 2 and "пробную смену" in payload["anchor"]["text"]
    answer_generated(third, "Спокойно объясняю ошибку и прошу попробовать ещё раз")
    fourth = services.generate_question(bot, "123")
    payload = request_data(gateway)
    assert payload["mode"] == "known_detail"
    assert "гитаре" in payload["anchor"]["text"]
    assert len({r["question_id"] for r in (first, second, third, fourth)}) == 4


def test_start_without_life_data_does_not_invent_a_biography(app_state):
    store, gateway = app_state
    queued_answers(gateway, "Чем ты сейчас занимаешься больше всего?")
    result = services.generate_question(bot, "123")
    payload = request_data(gateway)
    assert payload["mode"] == "discovery" and payload["anchor"] is None
    assert payload["known_life"] == []
    assert "Не выдумывай" in gateway.calls[-1].instructions
    mem = bot.load_mem("123")
    assert not mem["facts"] and not mem["qa"]
    assert store.find_question("123", question_id=result["question_id"])["status"] == "asked"


def test_survey_preferences_and_recent_events_are_available(app_state):
    _, gateway = app_state
    mem = bot.load_mem("123")
    mem["survey"]["answers"] = [
        {"question": "Как тебя зовут?", "answer": "Илья"},
        {"question": "Сколько тебе лет?", "answer": "25"},
        {"question": "Где живёшь?", "answer": "Омск"},
        {"question": "Чем занимаешься?", "answer": "Разрабатываю игры"},
    ]
    mem["preferences"] = ["Люблю играть на гитаре"]
    mem["daily_events"] = [{"date": "2026-11-02", "text": "Выступил на концерте с гитарой"}]
    bot.save_mem("123", mem)
    request = bot.build_generate_question_prompt(bot.load_mem("123"))
    text = request.render()
    assert all(value in text for value in ("Разрабатываю игры", "Омск", "Илья", "концерте", "гитаре"))
    assert request.count() <= bot.CONTEXT_TOKEN_BUDGET


def test_imported_owner_life_is_used_without_other_people_or_forwarded_text(app_state):
    store, gateway = app_state
    raw = json.dumps({"id": "life-chat", "messages": [
        {"id": 1, "from_id": "user456", "text": "STRANGER_ONLY: Я открыл ферму"},
        {"id": 2, "from_id": "user123", "text": "Я недавно запустил свой книжный клуб"},
        {"id": 3, "from_id": "user123", "forwarded_from": "Other", "text": "FORWARDED_ONLY: Я переехал в Париж"},
    ]}, ensure_ascii=False)
    services.import_chat(bot, bot.load_mem("123"), "123", raw)
    request = bot.build_generate_question_prompt(bot.load_mem("123"))
    assert "книжный клуб" in request.render()
    assert "STRANGER_ONLY" not in request.render() and "FORWARDED_ONLY" not in request.render()
    other = bot.build_generate_question_prompt(bot.load_mem("456"))
    assert "книжный клуб" not in other.render()


def test_declining_a_topic_stops_followups(app_state):
    _, gateway = app_state
    store_life()
    mem = bot.load_mem("123")
    mem["daily_events"] = [{"text": "Открыл пекарню"}]
    bot.save_mem("123", mem)
    queued_answers(gateway, "Что тебе нравится в работе пекарни?", "Что тебе даёт игра на гитаре?")
    first = services.generate_question(bot, "123")
    answer_generated(first, "Не хочу обсуждать работу")
    services.generate_question(bot, "123")
    payload = request_data(gateway)
    assert payload["mode"] != "follow_up"
    assert "гитаре" in payload["anchor"]["text"]
    assert "work" in payload["deferred_topics"]


def test_repeat_is_detected_even_after_it_leaves_recent_window(app_state):
    store, gateway = app_state
    mem = bot.load_mem("123")
    mem["facts"] = ["Я открыл пекарню"]
    old = "Что оказалось сложнее всего после открытия пекарни?"
    mem["generated_questions"] = [{"id": f"past-{i}", "question": old if i == 0 else f"Как проходит твоя встреча номер {i}?",
        "normalized": normalized(old if i == 0 else f"Как проходит твоя встреча номер {i}?"), "status": "asked"} for i in range(85)]
    bot.save_mem("123", mem)
    assert all(row["id"] != "past-0" for row in bot.load_mem("123")["generated_questions"])
    queued_answers(gateway, old, "Что помогает тебе развивать пекарню?")
    result = services.generate_question(bot, "123")
    assert result["result"] != old
    assert len(gateway.calls) == 2
    assert store.count("123", "generated_questions") == 86


def test_unrelated_model_question_is_retried_without_becoming_user_memory(app_state):
    store, gateway = app_state
    mem = bot.load_mem("123")
    mem["facts"] = ["Я открыл пекарню"]
    bot.save_mem("123", mem)
    queued_answers(gateway, "Какой фильм ты любишь пересматривать?", "Что тебя радует в работе пекарни?")
    result = services.generate_question(bot, "123")
    assert "пекарни" in result["result"]
    assert len(gateway.calls) == 2
    assert bot.load_mem("123")["facts"] == ["Я открыл пекарню"]
    assert store.count("123", "generated_questions") == 1


def test_failed_generation_does_not_advance_progress(app_state):
    store, gateway = app_state
    mem = bot.load_mem("123")
    mem["facts"] = ["Я открыл пекарню"]
    bot.save_mem("123", mem)
    queued_answers(gateway, "Какой фильм нравится?", "Тебе нравится кино?")
    with pytest.raises(ValueError, match="по твоим данным"):
        services.generate_question(bot, "123")
    assert store.count("123", "generated_questions") == 0
    assert not bot.load_mem("123")["question_state"]
    with store.turn("123"):
        pass


def test_api_binds_answer_to_the_displayed_question(app_state):
    store, gateway = app_state
    queued_answers(gateway, "Чем ты сейчас занимаешься больше всего?")
    client = TestClient(web_backend.app)
    auth = signed_data()
    result = client.post("/api/action", json={"action": "generate_question", "initData": auth}).json()
    body = {"action": "save_qa", "question_id": result["question_id"], "question": "Другой вопрос?", "answer": "Пишу игру", "initData": auth}
    assert client.post("/api/action", json=body).status_code == 400
    assert not bot.load_mem("123")["qa"]
    body["question"] = result["result"]
    assert client.post("/api/action", json=body).status_code == 200
    saved = bot.load_mem("123")["qa"][-1]
    assert saved["question_id"] == result["question_id"]
    assert client.post("/api/action", json=body).status_code == 400
    assert len(bot.load_mem("123")["qa"]) == 1


def test_web_question_does_not_replace_telegram_pending_question(app_state):
    _, gateway = app_state
    queued_answers(gateway, "Чем ты сейчас занимаешься больше всего?", "Что помогает тебе отдыхать после насыщенного дня?")
    telegram = services.generate_question(bot, "123", telegram=True)
    web = services.generate_question(bot, "123")
    mem = bot.load_mem("123")
    assert mem["pending_question_id"] == telegram["question_id"]
    assert mem["pending_question"] == telegram["result"]
    answer_generated(telegram, "Я занимаюсь разработкой игр")
    assert bot.get_store().find_question("123", question_id=web["question_id"])["status"] == "asked"


def test_question_prompt_stays_within_budget_with_large_personal_archive():
    mem = bot.normalize_mem({"facts": [f"Я разрабатываю проект {i}: " + "игра с открытым миром " * 60 for i in range(100)],
        "qa": [{"id": f"qa-{i}", "question": f"Как дела с проектом {i}?", "answer": "Работаю над игровыми персонажами " * 40} for i in range(100)]})
    request = build_question_request(mem, budget=1000)
    assert request.count() <= 1000
    assert "персонажами" in request.render()


def test_deleting_import_also_removes_derived_pending_questions(app_state):
    store, gateway = app_state
    raw = json.dumps({"id": "life-chat", "messages": [{"id": 1, "from_id": "user123", "text": "Я открыл пекарню"}]})
    services.import_chat(bot, bot.load_mem("123"), "123", raw)
    queued_answers(gateway, "Что оказалось сложным после открытия пекарни?")
    result = services.generate_question(bot, "123", telegram=True)
    mem = bot.load_mem("123")
    bot.delete_memory_item(mem, "imported_chats", 1)
    bot.save_mem("123", mem)
    assert not store.find_question("123", question_id=result["question_id"])
    assert not bot.load_mem("123")["pending_question"]
    assert "пекарню" not in bot.build_generate_question_prompt(bot.load_mem("123")).render()


def test_api_question_id_cannot_be_used_by_another_owner(app_state):
    _, gateway = app_state
    queued_answers(gateway, "Чем ты сейчас занимаешься больше всего?")
    result = services.generate_question(bot, "123")
    response = TestClient(web_backend.app).post("/api/action", json={"action": "save_qa",
        "question_id": result["question_id"], "question": result["result"], "answer": "Работаю над игрой",
        "initData": signed_data(user_id=456)})
    assert response.status_code == 400
    assert not bot.load_mem("456")["qa"]


def test_question_normalization_keeps_a_complete_personal_question():
    value = "Какая часть работы над твоей пекарней оказалась сложнее, чем ты ожидал при её открытии"
    assert clean_question(value) == value + "?"
    with pytest.raises(ValueError):
        clean_question("Где ты работаешь? Как отдыхаешь?")


def test_grounding_accepts_russian_cases_of_a_known_project():
    plan = {"kind": "known_detail", "anchor": {"text": "Я делаю свой проект", "context": ""}}
    assert grounded_question(plan, "Что оказалось сложным в работе над проектом?")
    plan["anchor"]["text"] = "Я делаю Telegram-бота"
    assert grounded_question(plan, "Что оказалось самым интересным в работе над ботом?")
    assert not grounded_question(plan, "Какой фильм ты любишь пересматривать?")
