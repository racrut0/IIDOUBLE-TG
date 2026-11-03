import pytest

import bot
import interlocutors as people
import services
from personalization import examples_for
from test_memory import transcript


def prepare(app_state):
    store, gateway = app_state
    for chat_id, answer in ((901, "FRIEND_OWNER_REPLY"), (902, "COLLEAGUE_OWNER_REPLY")):
        services.import_chat(bot, bot.load_mem("123"), "123", transcript(chat_id=chat_id).replace("ща, пять минут OWNER_1", answer))
    friend = people.save(bot, "123", "Друг для теста", "friend", ["901"])["saved_interlocutor_id"]
    colleague = people.save(bot, "123", "Коллега для теста", "colleague", ["902"])["saved_interlocutor_id"]
    return store, gateway, friend, colleague


def test_selected_person_uses_only_their_imports_and_context(app_state):
    store, gateway, friend, colleague = prepare(app_state)
    people.select(bot, "123", friend)
    request = bot.build_prompt(bot.load_mem("123"), [], "Пойдем гулять?")
    assert "FRIEND_OWNER_REPLY" in request.render()
    assert "COLLEAGUE_OWNER_REPLY" not in request.render()
    assert "Друг для теста" in request.instructions
    people.select(bot, "123", colleague)
    request = bot.build_prompt(bot.load_mem("123"), [], "Пойдем гулять?")
    assert "COLLEAGUE_OWNER_REPLY" in request.render()
    assert "FRIEND_OWNER_REPLY" not in request.render()
    assert request.count() <= bot.CONTEXT_TOKEN_BUDGET
    assert not gateway.calls


def test_corrections_training_and_history_do_not_cross_people(app_state):
    store, gateway, friend, colleague = prepare(app_state)
    people.select(bot, "123", friend)
    response = services.chat(bot, "123", "FRIEND_CONTEXT")
    services.feedback(bot, bot.load_mem("123"), "123", "FRIEND_CORRECTION", response["exchange_id"])
    people.select(bot, "123", colleague)
    for index in range(12):
        services.chat(bot, "123", f"COLLEAGUE_CONTEXT_{index}")
    assert "FRIEND_CONTEXT" not in gateway.calls[-1].render()
    assert "FRIEND_CORRECTION" not in gateway.calls[-1].render()
    people.select(bot, "123", friend)
    services.chat(bot, "123", "и что дальше?")
    assert "FRIEND_CONTEXT" in gateway.calls[-1].render()
    assert "FRIEND_CORRECTION" in gateway.calls[-1].render()
    assert "COLLEAGUE_CONTEXT" not in gateway.calls[-1].render()
    assert store.count("123", "history") == 28
    card = services.start_training(bot, "123")["card"]
    services.answer_training(bot, "123", card["session_id"], card["id"], "TRAINED_FRIEND")
    assert bot.load_mem("123")["reaction_examples"][-1]["interlocutor_id"] == friend
    people.select(bot, "123", colleague)
    assert not any(item["answer"] == "TRAINED_FRIEND" for item in examples_for(bot.load_mem("123"), card["context"]))


def test_legacy_general_history_is_preserved_when_switching(app_state):
    store, gateway, friend, colleague = prepare(app_state)
    services.chat(bot, "123", "LEGACY_GENERAL_MESSAGE")
    people.select(bot, "123", friend)
    services.chat(bot, "123", "NEW_FRIEND_MESSAGE")
    assert "LEGACY_GENERAL_MESSAGE" not in gateway.calls[-1].render()
    people.select(bot, "123", "")
    services.chat(bot, "123", "и что дальше?")
    assert "LEGACY_GENERAL_MESSAGE" in gateway.calls[-1].render()
    assert "NEW_FRIEND_MESSAGE" not in gateway.calls[-1].render()
    assert store.count("123", "history") == 6


def test_profiles_and_bound_chats_are_owner_scoped(app_state):
    store, gateway, friend, colleague = prepare(app_state)
    assert people.catalog(bot, "456")["interlocutors"] == []
    with pytest.raises(ValueError, match="не найден"):
        people.select(bot, "456", friend)
    with pytest.raises(ValueError, match="свои подтверждённые"):
        people.save(bot, "456", "Чужой чат", "friend", ["901"])
    people.select(bot, "123", friend)
    changed = people.save(bot, "123", "Новое название", "family", ["901"], "без фамильярности", friend)
    assert changed["active_interlocutor_id"] == friend
    request = bot.build_prompt(bot.load_mem("123"), [], "Привет")
    assert "без фамильярности" in request.instructions
    people.delete(bot, "123", friend)
    assert not bot.load_mem("123")["active_interlocutor_id"]
    assert store.count("123", "examples") == 2


def test_scoped_search_cannot_be_crowded_out_by_another_person(app_state):
    store, _ = app_state
    services.import_chat(bot, bot.load_mem("123"), "123", transcript(count=400, chat_id=902))
    services.import_chat(bot, bot.load_mem("123"), "123", transcript(chat_id=901).replace("OWNER_1", "UNIQUE_FRIEND_REPLY"))
    friend = people.save(bot, "123", "Друг", "friend", ["901"])["saved_interlocutor_id"]
    people.select(bot, "123", friend)
    assert "UNIQUE_FRIEND_REPLY" in bot.build_prompt(bot.load_mem("123"), [], "Пойдем гулять?").render()


def test_person_with_no_bound_chat_does_not_take_other_peoples_imports(app_state):
    store, gateway, friend, colleague = prepare(app_state)
    empty = people.save(bot, "123", "Друг без импорта", "friend", [])["saved_interlocutor_id"]
    people.select(bot, "123", empty)
    assert not examples_for(bot.load_mem("123"), "Пойдем гулять?")
    assert not gateway.calls


def test_scoped_correction_has_priority_over_newer_general_correction(app_state):
    store, gateway, friend, colleague = prepare(app_state)
    mem = bot.load_mem("123")
    mem["correction_examples"] = [
        {"id": "specific", "user": "Можешь помочь с задачей?", "correct_answer": "FRIEND_SPECIFIC", "interlocutor_id": friend, "date": "2026-10-01"},
        {"id": "general", "user": "Можешь помочь с задачей?", "correct_answer": "GENERAL_LATER", "date": "2026-11-01"},
    ]
    store.save("123", mem)
    people.select(bot, "123", friend)
    request = bot.build_prompt(bot.load_mem("123"), [], "Сможешь выручить с задачей?")
    assert "FRIEND_SPECIFIC" in request.render()
    assert "GENERAL_LATER" not in request.render()
    people.select(bot, "123", colleague)
    request = bot.build_prompt(bot.load_mem("123"), [], "Можешь помочь с задачей?")
    assert "GENERAL_LATER" in request.render()
    assert "FRIEND_SPECIFIC" not in request.render()


def test_person_notes_are_not_silently_dropped_from_the_context(app_state):
    store, gateway, friend, colleague = prepare(app_state)
    notes = "🙂" * 390 + "КОНЕЦ"
    people.save(bot, "123", "Друг", "friend", ["901"], notes, friend)
    people.select(bot, "123", friend)
    request = bot.build_prompt(bot.load_mem("123"), [], "Привет")
    assert notes in request.instructions
    assert request.count() <= bot.CONTEXT_TOKEN_BUDGET
    assert not gateway.calls


def test_switching_people_cannot_rate_an_old_reply_shown_in_another_client(app_state):
    store, gateway, friend, colleague = prepare(app_state)
    people.select(bot, "123", friend)
    response = services.chat(bot, "123", "Привет")
    people.select(bot, "123", colleague)
    people.select(bot, "123", friend)
    with pytest.raises(ValueError, match="нет ответа"):
        services.feedback(bot, bot.load_mem("123"), "123", expected_id=response["exchange_id"])
    assert not bot.load_mem("123")["feedback"]
    assert store.conversation_history("123", friend)
