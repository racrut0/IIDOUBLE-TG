from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from llm import ModelGateway
from personalization import ModelRequest
from spending import SCALE, SpendingController, SpendingLimitError
from storage import MemoryStore


def request(uid="123"):
    return ModelRequest("Пиши кратко", [{"role": "user", "content": "привет"}], uid)


def gateway_with_stream(store, events, **limits):
    spending = SpendingController(lambda: store, **limits)
    gateway = ModelGateway("fake", "gpt-6-luna", spending=spending)
    state = {"calls": 0, "closed": False}

    class Stream:
        def __iter__(self):
            yield from events
        def close(self):
            state["closed"] = True

    def create(**kwargs):
        state["calls"] += 1
        state["kwargs"] = kwargs
        return Stream()
    gateway._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return gateway, spending, state


def terminal(status="completed", model="gpt-6-luna", incoming=100, outgoing=3):
    return SimpleNamespace(type="response." + status, response=SimpleNamespace(status=status, model=model,
        service_tier="default", usage=SimpleNamespace(input_tokens=incoming, output_tokens=outgoing,
            input_tokens_details=SimpleNamespace(cached_tokens=20, cache_write_tokens=30))))


def test_global_budget_is_atomic_across_connections_and_owners(tmp_path):
    path = tmp_path / "shared.sqlite3"
    store = MemoryStore(path)
    quote = SpendingController(lambda: store).quote(request(), "gpt-6-luna", 240)[0]
    controllers = [SpendingController(lambda: MemoryStore(path), total_usd=quote * 1.5 / SCALE) for _ in range(6)]
    def reserve(index):
        try:
            controllers[index].reserve(request(str(index + 1)), "gpt-6-luna", 240)
            return True
        except SpendingLimitError:
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(6))) == 1


def test_budget_survives_profile_reset_and_restart(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    store = MemoryStore(path)
    spending = SpendingController(lambda: store)
    ticket = spending.reserve(request(), "gpt-6-luna", 240)
    store.reset("123")
    after = SpendingController(lambda: MemoryStore(path)).status()
    assert after["accounted_usd"] == ticket.maximum / SCALE
    assert after["requests"] == 1


def test_preflight_stops_before_any_paid_request(tmp_path):
    gateway, spending, state = gateway_with_stream(MemoryStore(tmp_path / "small.sqlite3"), [], total_usd=0.000001)
    with pytest.raises(SpendingLimitError, match="общий бюджет"):
        gateway.complete(request())
    assert state["calls"] == 0
    assert spending.status()["requests"] == 0


def test_usage_includes_cache_writes_and_counts_each_token_once(tmp_path):
    events = [SimpleNamespace(type="response.output_text.delta", delta="да"), terminal()]
    gateway, spending, state = gateway_with_stream(MemoryStore(tmp_path / "cost.sqlite3"), events)
    assert gateway.complete(request()) == "да"
    # (50 ordinary*100 + 20 cached*10 + 30 writes*125 + 3 output*500) * 1.10
    assert spending.status()["accounted_usd"] == 11495 / SCALE
    assert state["kwargs"]["max_output_tokens"] == 240
    assert state["kwargs"]["service_tier"] == "default"
    assert state["closed"]


def test_incomplete_response_is_charged_without_a_retry(tmp_path):
    gateway, spending, state = gateway_with_stream(MemoryStore(tmp_path / "incomplete.sqlite3"), [terminal("incomplete")])
    with pytest.raises(RuntimeError, match="не завершён"):
        gateway.complete(request())
    assert spending.status()["accounted_usd"] > 0
    assert state["calls"] == 1


def test_disconnect_and_cancellation_keep_the_reservation(tmp_path):
    gateway, spending, state = gateway_with_stream(MemoryStore(tmp_path / "cancel.sqlite3"),
        [SimpleNamespace(type="response.output_text.delta", delta="обрывок")])
    events = gateway.events(request())
    assert next(events)["delta"] == "обрывок"
    events.close()
    data = spending.status()
    assert data["accounted_usd"] == data["conservative_usd"] > 0
    assert state["closed"]


def test_unknown_model_and_unexpected_billing_tier_stop_traffic(tmp_path):
    store = MemoryStore(tmp_path / "unknown.sqlite3")
    gateway, spending, state = gateway_with_stream(store, [terminal(model="other-model")])
    with pytest.raises(SpendingLimitError, match="другую модель"):
        gateway.complete(request())
    assert spending.status()["blocked"]
    with pytest.raises(SpendingLimitError, match="требует проверки"):
        gateway.complete(request("456"))
    assert state["calls"] == 1
    with pytest.raises(SpendingLimitError, match="тариф"):
        spending.reserve(request(), "expensive-unknown-model", 240)


def test_request_limits_are_global_and_per_user(tmp_path):
    store = MemoryStore(tmp_path / "limits.sqlite3")
    spending = SpendingController(lambda: store, daily_requests=2, user_daily_requests=1)
    spending.reserve(request(), "gpt-6-luna", 240)
    with pytest.raises(SpendingLimitError, match="лимит запросов"):
        spending.reserve(request(), "gpt-6-luna", 240)
    spending.reserve(request("456"), "gpt-6-luna", 240)
    with pytest.raises(SpendingLimitError, match="лимит запросов"):
        spending.reserve(request("789"), "gpt-6-luna", 240)


def test_daily_money_limit_and_missing_usage_cannot_escape_accounting(tmp_path):
    store = MemoryStore(tmp_path / "daily.sqlite3")
    spending = SpendingController(lambda: store, daily_usd=0.000001)
    with pytest.raises(SpendingLimitError, match="денежный лимит"):
        spending.reserve(request(), "gpt-6-luna", 240)
    gateway, spending, state = gateway_with_stream(store, [])
    with pytest.raises(RuntimeError, match="расхода API"):
        gateway.complete(request())
    assert spending.status()["accounted_usd"] > 0
    assert spending.status()["accounted_usd"] == spending.status()["conservative_usd"]
    assert state["calls"] == 1


def test_unexpected_tier_and_oversized_usage_block_subsequent_requests(tmp_path):
    for name, response in (("tier", terminal()), ("oversized", terminal(outgoing=1000))):
        if name == "tier":
            response.response.service_tier = "priority"
        gateway, spending, _ = gateway_with_stream(MemoryStore(tmp_path / (name + ".sqlite3")), [response])
        with pytest.raises(SpendingLimitError):
            gateway.complete(request())
        assert spending.status()["blocked"]


def test_sdk_automatic_retries_are_disabled(tmp_path, monkeypatch):
    import llm
    captured = {}
    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(llm, "OpenAI", fake_client)
    gateway = ModelGateway("fake", "gpt-6-luna", spending=SpendingController(lambda: MemoryStore(tmp_path / "sdk.sqlite3")))
    assert gateway.client
    assert captured["max_retries"] == 0
    assert captured["base_url"] == "https://api.openai.com/v1"
