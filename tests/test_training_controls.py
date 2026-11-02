import json

import pytest
from fastapi.testclient import TestClient

import bot
import services
import web_backend as web
from owner_memory import catalog
from questions import build_question_request, choose_plan
from storage import MemoryStore
from test_api import signed_data


def test_seven_card_session_is_free_resumable_and_not_biography(app_state, monkeypatch):
    store, gateway = app_state
    client = TestClient(web.app)
    auth = signed_data()
    data = client.post("/api/action", json={"action": "start_training", "initData": auth}).json()
    first_card = data["card"]
    assert first_card["total"] == 7
    monkeypatch.setattr(bot, "_store", MemoryStore(store.path))
    assert services.start_training(bot, "123")["card"] == first_card
    for index in range(7):
        card = data["card"]
        assert card["position"] == index + 1
        response = client.post("/api/action", json={"action": "answer_training", "initData": auth,
            "session_id": card["session_id"], "card_id": card["id"], "answer": "Я живу на Луне, шучу"})
        assert response.status_code == 200
        data = response.json()
    assert data["card"] is None
    mem = bot.load_mem("123")
    assert len(mem["reaction_examples"]) == 7
    assert mem["facts"] == mem["qa"] == []
    assert not gateway.calls
    request = bot.build_prompt(mem, [], "У меня сегодня ничего не получилось")
    assert "Я живу на Луне" in request.render()
    assert "условная ситуация, не биография" in request.render()


def test_training_rejects_stale_and_other_owner_cards(app_state):
    first = services.start_training(bot, "123")["card"]
    second = services.start_training(bot, "456")["card"]
    with pytest.raises(ValueError, match="уже пройдена"):
        services.answer_training(bot, "456", first["session_id"], first["id"], "чужой ответ")
    services.answer_training(bot, "123", first["session_id"], first["id"], "", skip=True)
    with pytest.raises(ValueError, match="уже пройдена"):
        services.answer_training(bot, "123", first["session_id"], first["id"], "повтор")
    assert not bot.load_mem("123")["reaction_examples"]
    assert services.current_card(bot.load_mem("456")["training_session"])["id"] == second["id"]


def test_memory_correction_filters_derived_quotes_and_retrieval(app_state):
    store, gateway = app_state
    raw = json.dumps({"id": 900, "messages": [
        {"id": 1, "from_id": "user456", "text": "Где ты живёшь?"},
        {"id": 2, "from_id": "user123", "text": "Я живу в Старомгороде"}]}, ensure_ascii=False)
    services.import_chat(bot, bot.load_mem("123"), "123", raw)
    mem = bot.load_mem("123")
    mem["facts"].append("Я живу в Старомгороде")
    mem["survey"]["answers"] = [{"question": "Где ты живёшь?", "answer": "Я живу в Старомгороде"}]
    mem["qa"].append({"id": "source-qa", "question": "Где ты живёшь?", "answer": "Я живу в Старомгороде"})
    mem["history"] = [{"id": "old-history", "role": "assistant", "content": "Я живу в Старомгороде"}]
    bot.save_mem("123", mem)
    entry = next(e for e in catalog(bot.load_mem("123")) if "Старомгороде" in e["text"])
    services.update_memory(bot, "123", entry["id"], "Я живу в Новомгороде")
    fresh = bot.load_mem("123")
    prompt = bot.build_prompt(fresh, fresh["history"], "где живёшь")
    assert "Старомгороде" not in prompt.render()
    assert "Новомгороде" in prompt.render()
    assert "Старомгороде" not in build_question_request(fresh).render()
    training = services.start_training(bot, "123")
    assert "Старомгороде" not in str(training)
    services.update_memory(bot, "123", entry["id"], enabled=False)
    assert "Новомгороде" not in bot.build_prompt(bot.load_mem("123"), [], "где живёшь").render()
    assert not gateway.calls


def test_closed_topic_blocks_followups_and_background_context(app_state):
    mem = bot.load_mem("123")
    mem["facts"] = ["Работаю пекарем", "Играю на гитаре"]
    mem["qa"] = [{"id": "latest", "question": "Что сложно в пекарне?", "answer": "Сложно найти сотрудников",
        "question_topic": "work", "question_depth": 0}]
    bot.save_mem("123", mem)
    services.set_question_topic(bot, "123", "work", True)
    fresh = bot.load_mem("123")
    plan = choose_plan(fresh)
    assert plan["topic"] != "work"
    assert "пекар" not in build_question_request(fresh, plan).render().lower()
    assert "пекар" not in str(services.start_training(bot, "123")).lower()
    # Sharing a manual QA does not silently lift an explicit topic block.
    services.save_qa(bot, fresh, "123", "Что нового на работе?", "Сегодня работал пекарем")
    assert "work" in bot.load_mem("123")["question_state"]["blocked_topics"]


def test_memory_edit_and_topics_require_owner_and_no_api(app_state):
    store, gateway = app_state
    mem = bot.load_mem("123")
    mem["facts"] = ["Люблю велосипед"]
    bot.save_mem("123", mem)
    entry = catalog(mem)[0]
    client = TestClient(web.app)
    payload = {"action": "update_memory", "memory_id": entry["id"], "text": "Люблю бег"}
    assert client.post("/api/action", json=payload).status_code == 401
    payload["initData"] = signed_data(456)
    assert client.post("/api/action", json=payload).status_code == 400
    payload["initData"] = signed_data()
    assert client.post("/api/action", json=payload).status_code == 200
    assert not gateway.calls


def test_feedback_reason_is_local_unless_general_rule_is_explicit(app_state):
    store, gateway = app_state
    result = services.chat(bot, "123", "Как дела?")
    services.feedback(bot, bot.load_mem("123"), "123", "норм", result["exchange_id"], reason="formal")
    mem = bot.load_mem("123")
    assert not mem["style_rules"]
    assert mem["correction_examples"][0]["reason_note"] == "Слишком официально"
    assert not mem["facts"]
    result = services.chat(bot, "123", "Что нового?")
    services.feedback(bot, bot.load_mem("123"), "123", "да ничего", result["exchange_id"], reason="formal", scope="general")
    assert "Пиши проще" in bot.build_prompt(bot.load_mem("123"), [], "привет").instructions
    assert len(gateway.calls) == 2


def test_potential_conflicts_are_visible_with_unknown_dates(app_state):
    mem = bot.load_mem("123")
    mem["facts"] = ["Я живу в Томске", "Я живу в Омске"]
    bot.save_mem("123", mem)
    entries = services.memory_catalog(bot, "123")["entries"]
    assert all(entry["needs_review"] for entry in entries)
    assert all(entry["date"] == "" for entry in entries)


def test_answer_fact_has_source_and_deleting_qa_removes_derived_fact(app_state):
    store, gateway = app_state
    services.save_qa(bot, bot.load_mem("123"), "123", "Где ты живёшь?", "Я живу в Истокгороде")
    mem = bot.load_mem("123")
    assert mem["owner_facts"] and not mem["facts"]
    entry = next(item for item in catalog(mem) if "Истокгороде" in item["text"])
    assert entry["sources"] == ["Твой ответ"] and entry["date"]
    bot.delete_memory_item(mem, "qa", 1)
    bot.save_mem("123", mem)
    assert "Истокгороде" not in bot.build_prompt(bot.load_mem("123"), [], "где ты живёшь").render()
    assert not gateway.calls


def test_import_deletion_clears_training_sessions_and_examples(app_state):
    raw = json.dumps({"id": 999, "messages": [
        {"id": 1, "from_id": "user456", "text": "Привет, как поживаешь?"},
        {"id": 2, "from_id": "user123", "text": "хорошо"}]})
    services.import_chat(bot, bot.load_mem("123"), "123", raw)
    card = services.start_training(bot, "123", telegram=True)["card"]
    assert card["kind"] == "real"
    services.answer_training(bot, "123", card["session_id"], card["id"], "новый ответ")
    mem = bot.load_mem("123")
    bot.delete_memory_item(mem, "imported_chats", 1)
    bot.save_mem("123", mem)
    fresh = bot.load_mem("123")
    assert not fresh["reaction_examples"] and not fresh["training_session"]
    assert fresh["mode"] == "chat"
