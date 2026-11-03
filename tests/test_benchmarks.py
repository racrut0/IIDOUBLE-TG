from types import SimpleNamespace

import pytest

import benchmarks
import bot
import services
from llm import ModelGateway
from storage import MemoryStore, StaleMemoryError
from test_memory import transcript


def prepare(app_state):
    store, gateway = app_state
    for chat_id in (901, 902):
        raw = transcript(count=60, chat_id=chat_id).replace("OWNER_", f"PRIVATE_{chat_id}_ANSWER_")
        services.import_chat(bot, bot.load_mem("123"), "123", raw)
    suite = benchmarks.create_suite(bot, "123", "901")["suite"]
    return store, gateway, suite


def test_twenty_cases_are_fixed_and_source_chat_and_answers_are_held_out(app_state):
    store, gateway, suite = prepare(app_state)
    assert len(suite["cases"]) == 20
    assert benchmarks.create_suite(bot, "123", "902")["suite"] == suite
    mem = bot.load_mem("123")
    mem["import_facts"].append({"id": "heldout-fact", "category": "facts", "text": "PRIVATE_HELDOUT_FACT", "chat_id": "901"})
    mem["qa"].append({"id": "duplicate-question", "question": suite["cases"][0]["context"], "answer": "PRIVATE_DUPLICATE_ANSWER"})
    mem["summary"] = "PRIVATE_OLD_SUMMARY"
    bot.save_mem("123", mem)
    result = benchmarks.start_run(bot, "123")
    saved = store.evaluation_item("123", "run", result["run"]["id"])
    assert len(saved["requests"]) == 20
    for payload in saved["requests"].values():
        rendered = payload["instructions"] + str(payload["messages"])
        assert "PRIVATE_901_ANSWER" not in rendered
        assert "PRIVATE_HELDOUT_FACT" not in rendered
        assert "PRIVATE_DUPLICATE_ANSWER" not in rendered
        assert "PRIVATE_OLD_SUMMARY" not in rendered
        assert payload["estimated_tokens"] <= bot.CONTEXT_TOKEN_BUDGET
    assert not gateway.calls
    assert "requests" not in result["run"]
    assert result["run"]["score"] is None


def test_generation_is_once_per_case_rating_is_local_and_restart_keeps_results(app_state, monkeypatch):
    store, gateway, suite = prepare(app_state)
    run = benchmarks.start_run(bot, "123")["run"]
    case_id = suite["cases"][0]["id"]
    first = benchmarks.generate(bot, "123", run["id"], case_id)
    assert first["run"]["generated"] == 1
    assert len(gateway.calls) == 1
    assert gateway.calls[0].owner_id == "123"
    assert not store.count("123", "history")
    assert not bot.load_mem("123")["feedback"]
    monkeypatch.setattr(bot, "_store", MemoryStore(store.path))
    benchmarks.generate(bot, "123", run["id"], case_id, retry=True)
    assert len(gateway.calls) == 1
    rated = benchmarks.rate(bot, "123", run["id"], case_id, "partial", "Реакция близкая, но суховата")
    assert rated["run"]["score"] == 50
    assert rated["run"]["rated"] == 1
    rated = benchmarks.rate(bot, "123", run["id"], case_id, "like")
    assert rated["run"]["score"] == 100
    assert rated["run"]["rated"] == 1
    assert len(gateway.calls) == 1
    assert not bot.load_mem("123")["correction_examples"]


def test_runs_are_owner_scoped_and_source_deletion_removes_derived_tests(app_state):
    store, gateway, suite = prepare(app_state)
    run = benchmarks.start_run(bot, "123")["run"]
    old = store.evaluation_item("123", "run", run["id"])
    assert benchmarks.get(bot, "456")["suite"] is None
    for operation in (lambda: benchmarks.get(bot, "456", run["id"]),
                      lambda: benchmarks.generate(bot, "456", run["id"], "case-01")):
        with pytest.raises(ValueError, match="не найден"):
            operation()
    store.delete_chat("123", "901")
    assert benchmarks.get(bot, "123")["suite"] is None
    assert not store.evaluation_runs("123")
    with pytest.raises(StaleMemoryError, match="удалена"):
        store.save_evaluation("123", "run", old, bot.load_mem("123").generation, require_existing=True)
    assert not gateway.calls


def test_budget_preflight_and_changed_training_memory_stop_before_api(app_state, monkeypatch):
    store, gateway, suite = prepare(app_state)
    quote = bot.spending.quote
    monkeypatch.setattr(bot.spending, "quote", lambda *args: (benchmarks.MAX_RUN_RESERVE, {}))
    with pytest.raises(ValueError, match="резерв"):
        benchmarks.start_run(bot, "123")
    assert not gateway.calls and not store.evaluation_runs("123")
    monkeypatch.setattr(bot.spending, "quote", quote)
    run = benchmarks.start_run(bot, "123")["run"]
    mem = bot.load_mem("123")
    mem["facts"].append("Содержательная новая деталь")
    bot.save_mem("123", mem)
    with pytest.raises(ValueError, match="изменились"):
        benchmarks.generate(bot, "123", run["id"], "case-01")
    assert not gateway.calls


def test_failed_attempt_needs_explicit_retry_and_does_not_publish_partial_text(app_state, monkeypatch):
    store, gateway, suite = prepare(app_state)
    run = benchmarks.start_run(bot, "123")["run"]
    events = gateway.events

    def broken(*args):
        gateway.calls.append(args[0])
        yield {"delta": "PRIVATE_UNFINISHED_ANSWER"}
        raise RuntimeError("fake network failure")

    monkeypatch.setattr(gateway, "events", broken)
    with pytest.raises(RuntimeError, match="fake network"):
        benchmarks.generate(bot, "123", run["id"], "case-01")
    state = benchmarks.get(bot, "123", run["id"])["run"]["states"]["case-01"]
    assert state["status"] == "error" and not state["answer"]
    with pytest.raises(ValueError, match="отдельно"):
        benchmarks.generate(bot, "123", run["id"], "case-01")
    assert len(gateway.calls) == 1
    monkeypatch.setattr(gateway, "events", events)
    result = benchmarks.generate(bot, "123", run["id"], "case-01", retry=True)
    assert result["run"]["states"]["case-01"]["attempts"] == 2
    assert len(gateway.calls) == 2


def test_new_run_uses_same_cases_and_keeps_old_scores(app_state):
    store, gateway, suite = prepare(app_state)
    first = benchmarks.start_run(bot, "123")["run"]
    benchmarks.generate(bot, "123", first["id"], "case-01")
    benchmarks.rate(bot, "123", first["id"], "case-01", "unlike")
    second = benchmarks.start_run(bot, "123")
    assert second["run"]["id"] != first["id"]
    assert second["suite"] == suite
    assert second["run"]["score"] is None
    assert second["run_history"][1]["score"] == 0
    store.reset("123")
    assert not store.evaluation_runs("123")
    assert benchmarks.get(bot, "123")["suite"] is None


def test_benchmark_paid_path_uses_the_shared_spending_guard(app_state, monkeypatch):
    store, gateway, suite = prepare(app_state)
    run = benchmarks.start_run(bot, "123")["run"]

    class Stream:
        def __iter__(self):
            yield SimpleNamespace(type="response.output_text.delta", delta="Проверочный ответ")
            yield SimpleNamespace(type="response.completed", response=SimpleNamespace(status="completed", model=bot.MODEL,
                service_tier="default", output_text="Проверочный ответ", output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="Проверочный ответ")])],
                usage=SimpleNamespace(input_tokens=100, output_tokens=5, input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0))))
        def close(self):
            pass

    guarded = ModelGateway("fake-local-only", bot.MODEL, bot.CONTEXT_TOKEN_BUDGET, bot.spending, bot.MAX_OUTPUT_TOKENS)
    guarded._client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: Stream()))
    monkeypatch.setattr(bot, "gateway", guarded)
    result = benchmarks.generate(bot, "123", run["id"], "case-01")
    assert result["run"]["generated"] == 1
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM api_spending WHERE uid='123'").fetchone()[0] == 1
    spent = bot.spending.status()["accounted_usd"]
    assert spent > 0
    store.reset("123")
    assert bot.spending.status()["accounted_usd"] == spent
