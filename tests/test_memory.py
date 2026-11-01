import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import bot
import services
from personalization import ContextBudgetError, build_request
from storage import BusyUserError, MemoryStore, StaleMemoryError


def transcript(count=2, chat_id=900):
    return json.dumps({"id": chat_id, "messages": [
        {"id": i + 1, "type": "message", "from": "Friend" if i % 2 == 0 else "Owner",
         "from_id": "user456" if i % 2 == 0 else "user123",
         "date": "2026-10-31T12:00:00", "text": f"Пойдем гулять? CONTEXT_{i}" if i % 2 == 0 else f"ща, пять минут OWNER_{i}"}
        for i in range(count)
    ]}, ensure_ascii=False)


def test_full_import_reimport_and_retrieval(app_state):
    store, gateway = app_state
    mem = bot.load_mem("123")
    services.import_chat(bot, mem, "123", transcript(1000))
    assert store.count("123", "imported_dialogues") == 1000
    assert store.count("123", "examples") == 500
    services.import_chat(bot, bot.load_mem("123"), "123", transcript(1000))
    assert store.count("123", "imported_dialogues") == 1000
    assert store.count("123", "examples") == 500
    prompt = bot.build_prompt(bot.load_mem("123"), [], "Пойдем гулять? CONTEXT_0")
    assert "CONTEXT_0" in prompt.render()
    assert "OWNER_1" in prompt.render()
    assert not gateway.calls


def test_owner_id_has_priority_over_same_name():
    item = bot.make_chat_record("чужое сообщение", "Owner", "user999", aliases={"owner", "user123"})
    assert item["role"] == "other"


def test_import_one_letter_replies_and_grouped_messages():
    raw = json.dumps({"messages": [
        {"id": 1, "from_id": "user456", "from": "Friend", "text": "ты придешь?"},
        {"id": 2, "from_id": "user123", "from": "Owner", "text": "я"},
        {"id": 3, "from_id": "user123", "from": "Owner", "text": "позже"},
    ]})
    mem, stats = bot.import_telegram_chat_to_memory(bot.normalize_mem({}), raw, "123")
    assert stats["total"] == 3
    assert mem["examples"][0]["answer"] == "я\nпозже"


def test_chat_does_not_train_on_test_questions_and_keeps_archives(app_state):
    store, gateway = app_state
    services.import_chat(bot, bot.load_mem("123"), "123", transcript())
    for i in range(121):
        services.chat(bot, "123", f"проверочный вопрос {i}")
    mem = bot.load_mem("123")
    assert mem["style"] == mem["patterns"] == mem["training_samples"] == []
    assert store.count("123", "history") == 242
    assert len(mem["history"]) == 20
    assert "OWNER_1" in bot.build_prompt(mem, [], "пойдем гулять").render()
    assert len(gateway.calls) == 121
    assert store.metrics("123")[0]["input_tokens"] == 700


def test_permanent_rules_never_lexically_filtered():
    mem = bot.normalize_mem({"style_rules": ["не используй эмодзи", "пиши коротко"]})
    request = bot.build_prompt(mem, [], "пиши про отпуск")
    assert "не используй эмодзи" in request.instructions
    assert "пиши коротко" in request.instructions
    assert request.messages[-1] == {"role": "user", "content": "пиши про отпуск"}


def test_legacy_style_instruction_survives_its_duplicate_pattern():
    mem = bot.normalize_mem({"style": ["не используй эмодзи"], "patterns": ["сообщение пользователя: не используй эмодзи"]})
    assert "не используй эмодзи" in bot.build_prompt(mem, [], "пиши про отпуск").instructions


def test_context_budget_with_large_corpus():
    mem = bot.normalize_mem({})
    mem["qa"] = [{"question": f"Как дела {i}", "answer": "Обычно я так и отвечаю. " * 20} for i in range(100)]
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "длинная история " * 100} for i in range(20)]
    request = build_request(mem, history, "как дела", 3500)
    assert request.count() <= 3500
    assert request.render().count("Пример реакции") <= 6
    assert "Ответь 1-4 словами" not in request.instructions
    with pytest.raises(ContextBudgetError):
        build_request(mem, [], "огромный ввод " * 5000, 3500)


def test_correction_used_once_and_wrong_history_removed(app_state):
    store, gateway = app_state
    result = services.chat(bot, "123", "Пойдем гулять?")
    mem = bot.load_mem("123")
    services.feedback(bot, mem, "123", "CORRECT_OWNER_794", result["exchange_id"])
    mem = bot.load_mem("123")
    prompt = bot.build_prompt(mem, mem["history"], "Пойдем гулять?")
    assert prompt.render().count("CORRECT_OWNER_794") == 1
    assert "ща, пять минут" not in prompt.render()
    assert not mem["qa"] and not mem["style"] and not mem["patterns"]
    with pytest.raises(ValueError, match="новый ответ"):
        services.feedback(bot, mem, "123", expected_id="old-answer")


def test_parallel_updates_and_counter_deltas(app_state):
    store, _ = app_state
    first, second = bot.load_mem("123"), bot.load_mem("123")
    first["facts"].append("first")
    second["facts"].append("second")
    first["message_count"] += 1
    second["message_count"] += 1
    store.save("123", first)
    store.save("123", second)
    loaded = bot.load_mem("123")
    assert loaded["facts"] == ["first", "second"]
    assert loaded["message_count"] == 2
    def add(index):
        mem = bot.load_mem("123")
        mem["facts"].append(str(index))
        store.save("123", mem)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(add, range(20)))
    assert len(bot.load_mem("123")["facts"]) == 22


def test_reset_does_not_resurrect_legacy_or_inflight_state(tmp_path):
    legacy = tmp_path / "123.json"
    legacy.write_text(json.dumps({"facts": ["old fact"]}), encoding="utf-8")
    store = MemoryStore(tmp_path / "db.sqlite3", tmp_path)
    stale = store.load("123")
    assert stale["facts"] == ["old fact"]
    store.reset("123")
    with pytest.raises(StaleMemoryError):
        stale["facts"].append("inflight")
        store.save("123", stale)
    assert not store.load("123").get("facts")
    assert legacy.exists()


def test_corrupt_legacy_is_visible_and_never_overwritten(tmp_path):
    legacy = tmp_path / "123.json"
    legacy.write_text("{broken", encoding="utf-8")
    store = MemoryStore(tmp_path / "db.sqlite3", tmp_path)
    with pytest.raises(json.JSONDecodeError):
        store.load("123")
    assert legacy.read_text() == "{broken"


def test_cross_process_turn_lock_and_owner_isolation(app_state):
    store, _ = app_state
    services.import_chat(bot, bot.load_mem("123"), "123", transcript())
    second_store = MemoryStore(store.path)
    assert not second_store.search_examples("456", "пойдем гулять")
    with store.turn("123"):
        with pytest.raises(BusyUserError):
            with second_store.turn("123"):
                pass
        with second_store.turn("456"):
            pass
    with second_store.turn("123"):
        pass


def test_import_fact_provenance_and_delete_chat(app_state):
    store, _ = app_state
    raw = json.dumps({"id": 901, "messages": [{"id": 1, "from": "Owner", "from_id": "user123", "text": "я живу в Томске"}]})
    services.import_chat(bot, bot.load_mem("123"), "123", raw)
    mem = bot.load_mem("123")
    assert "Томске" in bot.build_prompt(mem, [], "где ты живешь").render()
    assert bot.delete_memory_item(mem, "imported_chats", 1)
    bot.save_mem("123", mem)
    mem = bot.load_mem("123")
    assert not store.count("123", "imported_dialogues")
    assert "Томске" not in bot.build_prompt(mem, [], "где ты живешь").render()


def test_legacy_import_gets_contextual_examples(tmp_path):
    data = {"imported_dialogues": [
        {"role": "other", "text": "пойдем гулять"},
        {"role": "me", "text": "LEGACY_OWNER_REPLY"},
    ]}
    (tmp_path / "123.json").write_text(json.dumps(data), encoding="utf-8")
    store = MemoryStore(tmp_path / "db.sqlite3", tmp_path)
    mem = bot.normalize_mem(store.load("123"))
    assert "LEGACY_OWNER_REPLY" in bot.build_prompt(mem, [], "пойдем гулять").render()


def test_updated_import_removes_superseded_facts(app_state):
    store, _ = app_state
    data = {"id": 900, "messages": [{"id": 1, "from_id": "user123", "text": "я живу в Томске"}]}
    services.import_chat(bot, bot.load_mem("123"), "123", json.dumps(data))
    data["messages"][0]["text"] = "просто проверка"
    services.import_chat(bot, bot.load_mem("123"), "123", json.dumps(data))
    assert "Томске" not in bot.build_prompt(bot.load_mem("123"), [], "где ты живешь").render()


def test_last_exchange_is_atomic_during_parallel_correction(app_state):
    store, _ = app_state
    initial = bot.load_mem("123")
    initial["last_exchange"] = {"id": "first", "assistant": "answer"}
    store.save("123", initial)
    chat, correction = bot.load_mem("123"), bot.load_mem("123")
    correction["last_exchange"]["assistant"] = "corrected"
    store.save("123", correction)
    chat["last_exchange"] = {"id": "second", "assistant": "answer"}
    store.save("123", chat)
    assert bot.load_mem("123")["last_exchange"] == {"id": "second", "assistant": "answer"}


def test_large_gap_is_not_an_implicit_reply():
    raw = json.dumps({"messages": [
        {"id": 1, "from_id": "user456", "text": "пойдем гулять?", "date": "2026-10-01T12:00:00"},
        {"id": 2, "from_id": "user123", "text": "я занят", "date": "2026-10-05T12:00:00"},
    ]})
    mem, stats = bot.import_telegram_chat_to_memory(bot.normalize_mem({}), raw, "123")
    assert not mem["examples"]


def test_telegram_serializes_one_owner_but_not_all_owners():
    import asyncio
    from types import SimpleNamespace
    async def scenario():
        processor = bot.PerUserUpdateProcessor()
        entered, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []
        async def first():
            entered.set()
            await release.wait()
            order.append("first")
        async def second():
            order.append("second")
        async def other():
            other_done.set()
        a = asyncio.create_task(processor.process_update(SimpleNamespace(effective_user=SimpleNamespace(id=123)), first()))
        await entered.wait()
        b = asyncio.create_task(processor.process_update(SimpleNamespace(effective_user=SimpleNamespace(id=123)), second()))
        c = asyncio.create_task(processor.process_update(SimpleNamespace(effective_user=SimpleNamespace(id=456)), other()))
        await asyncio.wait_for(other_done.wait(), 2)
        assert order == []
        release.set()
        await asyncio.gather(a, b, c)
        assert order == ["first", "second"]
    asyncio.run(scenario())
