"""Reaction retrieval checks use invented conversations and never call a model."""

import json

import pytest

import bot
import services
from personalization import examples_for
from retrieval import reaction_signals
from storage import MemoryStore


def example(context, answer, **extra):
    return {"context": context, "answer": answer, "source": "telegram", **extra}


def test_failure_does_not_choose_celebration_for_the_same_subject(app_state):
    store, gateway = app_state
    mem = bot.load_mem("123")
    mem["examples"] = [
        example("Завалил зачёт, очень расстроен", "OWNER_SUPPORT", id="support"),
        example("Сдал экзамен, наконец получилось!", "OWNER_CELEBRATION", id="joy"),
    ]
    store.save("123", mem)
    ranked = examples_for(bot.load_mem("123"), "Провалил экзамен, настроение ужасное")
    assert ranked and ranked[0]["answer"] == "OWNER_SUPPORT"
    assert all(item["answer"] != "OWNER_CELEBRATION" for item in ranked)
    assert not gateway.calls


def test_related_correction_has_priority_over_a_literal_import():
    mem = bot.normalize_mem({"examples": [
        example("Сможешь выручить с задачей?", "OWNER_UNWANTED_REACTION"),
    ], "correction_examples": [
        {"user": "Можешь помочь с задачей?", "bad_answer": "OWNER_UNWANTED_REACTION",
         "correct_answer": "OWNER_CORRECTED_REACTION", "source": "correction"},
    ]})
    ranked = examples_for(mem, "Сможешь выручить с задачей?")
    assert ranked[0]["answer"] == "OWNER_CORRECTED_REACTION"
    assert all(item["answer"] != "OWNER_UNWANTED_REACTION" for item in ranked)


def test_short_followup_keeps_the_situation_for_retrieval():
    mem = bot.normalize_mem({"examples": [
        example("Завалил зачёт, очень расстроен", "OWNER_SUPPORT"),
    ]})
    history = [
        {"role": "user", "content": "Завалил зачёт, очень расстроен"},
        {"role": "assistant", "content": "эх"},
    ]
    request = bot.build_prompt(mem, history, "и что дальше?")
    assert "OWNER_SUPPORT" in request.instructions
    assert request.messages[-1] == {"role": "user", "content": "и что дальше?"}
    assert request.count() <= bot.CONTEXT_TOKEN_BUDGET


def test_unrelated_life_answer_is_not_a_reaction_example():
    mem = bot.normalize_mem({"qa": [{"question": "В каком городе ты живёшь?", "answer": "В Самаре"}]})
    assert not examples_for(mem, "Как проверить тормозную жидкость?")


@pytest.mark.parametrize("query", ["Не сдал экзамен", "Ничего не получилось", "Не выиграл конкурс"])
def test_negated_success_is_not_used_as_celebration(query):
    mem = bot.normalize_mem({"examples": [
        example("Сдал экзамен, наконец получилось! Выиграл конкурс", "OWNER_JOY"),
        example("Завалил зачёт, расстроен. Не выиграл конкурс", "OWNER_DRY_REACTION"),
    ]})
    ranked = examples_for(mem, query)
    assert ranked and ranked[0]["answer"] == "OWNER_DRY_REACTION"
    assert all(item["answer"] != "OWNER_JOY" for item in ranked)


def test_new_explicit_situation_does_not_inherit_previous_emotion():
    mem = bot.normalize_mem({"examples": [
        example("Завалил зачёт, очень расстроен", "OWNER_DISTRESS"),
        example("Выиграл соревнование!", "OWNER_JOY"),
    ]})
    request = bot.build_prompt(mem, [{"role": "user", "content": "Завалил зачёт, очень расстроен"},
        {"role": "assistant", "content": "эх"}], "Я выиграл соревнование!")
    assert "OWNER_JOY" in request.instructions
    assert "OWNER_DISTRESS" not in request.instructions
    assert request.sections["retrieval_used_history"] is False


def test_newest_correction_replaces_older_reaction_to_same_situation():
    mem = bot.normalize_mem({"correction_examples": [
        {"user": "Можешь помочь с задачей?", "correct_answer": "OWNER_OLD", "date": "2026-10-01"},
        {"user": "Можешь помочь с задачей?", "correct_answer": "OWNER_CURRENT", "date": "2026-11-01"},
    ]})
    ranked = examples_for(mem, "Можешь помочь с задачей?")
    assert [item["answer"] for item in ranked] == ["OWNER_CURRENT"]


def test_existing_archive_is_reindexed_once_without_changing_its_contents(app_state):
    store, _ = app_state
    mem = bot.load_mem("123")
    owner_example = example("Завалил зачёт, очень расстроен", "OWNER_REPLY", id="old-archive")
    mem["examples"] = [owner_example]
    store.save("123", mem)
    with store.transaction() as db:
        db.execute("UPDATE memory_items SET search_text='завалил зачёт очень расстроен' WHERE category='examples'")
        db.execute("DELETE FROM schema_metadata WHERE key='reaction_index'")
        before = [tuple(row) for row in db.execute("SELECT uid,category,item_key,data FROM memory_items ORDER BY seq")]
        db.execute("INSERT INTO api_spending(id,uid,created,model,reserved,charged,status) VALUES('preserve','123',1,'test',20,10,'done')")
    migrated = MemoryStore(store.path)
    assert any(item["answer"] == "OWNER_REPLY" for item in migrated.search_examples("123", "Провалил экзамен"))
    assert not migrated.search_examples("456", "Провалил экзамен")
    with migrated.transaction() as db:
        assert [tuple(row) for row in db.execute("SELECT uid,category,item_key,data FROM memory_items ORDER BY seq")] == before
        assert db.execute("SELECT charged FROM api_spending WHERE id='preserve'").fetchone()[0] == 10
        db.execute("CREATE TRIGGER no_reindex BEFORE UPDATE ON memory_items BEGIN SELECT RAISE(ABORT,'unnecessary rebuild'); END")
    MemoryStore(store.path)  # A restart must not rebuild an already current index.


def test_index_upgrade_rolls_back_if_interrupted(app_state, monkeypatch):
    import storage

    store, _ = app_state
    mem = bot.load_mem("123")
    mem["examples"] = [example("Завалил зачёт", "OWNER_ONE", id="one"), example("Сдал экзамен", "OWNER_TWO", id="two")]
    store.save("123", mem)
    with store.transaction() as db:
        db.execute("UPDATE memory_items SET search_text='legacy' WHERE category='examples'")
        db.execute("DELETE FROM schema_metadata WHERE key='reaction_index'")
    original = storage.search_text

    def interrupted(category, item):
        if item["id"] == "two":
            raise RuntimeError("simulated interrupted index upgrade")
        return original(category, item)

    monkeypatch.setattr(storage, "search_text", interrupted)
    with pytest.raises(RuntimeError, match="simulated interrupted"):
        MemoryStore(store.path)
    with store.transaction() as db:
        assert all(row[0] == "legacy" for row in db.execute("SELECT search_text FROM memory_items WHERE category='examples'"))
        assert not db.execute("SELECT 1 FROM schema_metadata WHERE key='reaction_index'").fetchone()
    monkeypatch.setattr(storage, "search_text", original)
    assert MemoryStore(store.path).search_examples("123", "Провалил экзамен")


def test_chat_uses_one_model_call_and_records_only_retrieval_counts(app_state):
    store, gateway = app_state
    mem = bot.load_mem("123")
    mem["correction_examples"] = [{"id": "fix", "user": "Можешь помочь с задачей?",
        "correct_answer": "OWNER_PRIVATE_REPLY", "bad_answer": "WRONG_PRIVATE_REPLY"}]
    store.save("123", mem)
    result = services.chat(bot, "123", "Сможешь выручить с задачей?")
    assert len(gateway.calls) == 1
    request = gateway.calls[0]
    assert request.count() <= bot.CONTEXT_TOKEN_BUDGET
    assert "исправление владельца" in request.instructions
    assert "OWNER_PRIVATE_REPLY" in request.instructions
    assert "WRONG_PRIVATE_REPLY" not in request.render()
    assert result["metrics"]["reaction_examples"] == result["metrics"]["correction_examples"] == 1
    assert "OWNER_PRIVATE_REPLY" not in json.dumps(store.metrics("123"))


def test_renting_out_an_apartment_and_not_being_sad_are_not_success_or_distress():
    assert "celebration" not in reaction_signals("Я сдал квартиру")
    assert "distress" not in reaction_signals("Я не расстроен")
