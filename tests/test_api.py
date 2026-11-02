import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import pytest

import bot
import services
import web_backend as web
from llm import ModelGateway
from personalization import ModelRequest
from spending import SpendingController
from storage import MemoryStore


def signed_data(user_id=123, auth_date=None):
    fields = {"auth_date": str(int(time.time()) if auth_date is None else auth_date), "user": json.dumps({"id": user_id, "first_name": "Owner"})}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot.TG_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_signed_auth_and_no_dev_bypass(app_state, monkeypatch):
    monkeypatch.setenv("WEBAPP_DEV_USER_ID", "123")
    client = TestClient(web.app)
    assert client.post("/api/action", json={"action": "memory"}).status_code == 401
    for data in (signed_data(auth_date=0), signed_data(auth_date=int(time.time()) + 600), signed_data(auth_date=1), signed_data() + "&auth_date=0", "hash=русский"):
        assert client.post("/api/action", json={"action": "memory", "initData": data}).status_code == 401
    assert client.post("/api/action", json={"action": "memory", "initData": signed_data()}).status_code == 200


def test_chat_stream_usage_and_feedback_binding(app_state):
    store, gateway = app_state
    client = TestClient(web.app)
    response = client.post("/api/chat/stream", json={"action": "chat", "text": "Пойдем гулять?", "initData": signed_data()})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["delta", "delta", "done"]
    done = events[-1]
    assert done["metrics"]["cached_tokens"] == 300
    assert done["exchange_id"]
    feedback = {"action": "feedback_correction", "text": "позже, я занят", "initData": signed_data(), "exchange_id": done["exchange_id"]}
    assert client.post("/api/action", json=feedback).status_code == 200
    feedback["exchange_id"] = "old"
    assert client.post("/api/action", json=feedback).status_code == 400
    assert len(gateway.calls) == 1
    assert store.count("123", "history") == 2


def test_incomplete_stream_is_not_committed(app_state, monkeypatch):
    store, gateway = app_state
    def broken(*args):
        yield {"delta": "обрывок"}
        raise RuntimeError("fake disconnect")
    monkeypatch.setattr(gateway, "events", broken)
    client = TestClient(web.app)
    response = client.post("/api/chat/stream", json={"action": "chat", "text": "привет", "initData": signed_data()})
    assert json.loads(response.text.splitlines()[-1])["type"] == "error"
    assert store.count("123", "history") == 0
    assert store.metrics("123")[0]["status"] == "error"
    with store.turn("123"):
        pass


@pytest.mark.parametrize("model,reasoning", [
    ("gpt-6.1-terra", {"effort": "none"}),
    ("gpt-6-luna", {"effort": "none"}),
])
def test_transport_uses_instructions_and_roles_not_one_string(model, reasoning, tmp_path):
    captured = {}
    class FakeStream:
        def __iter__(self):
            yield SimpleNamespace(type="response.output_text.delta", delta="ответ")
            yield SimpleNamespace(type="response.completed", response=SimpleNamespace(model=model, status="completed", service_tier="default",
                usage=SimpleNamespace(input_tokens=100, output_tokens=2, input_tokens_details=SimpleNamespace(cached_tokens=64, cache_write_tokens=0))))
        def close(self):
            captured["closed"] = True
    def create(**kwargs):
        captured.update(kwargs)
        return FakeStream()
    store = MemoryStore(tmp_path / "api.sqlite3")
    gateway = ModelGateway("fake", model, spending=SpendingController(lambda: store))
    gateway._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    request = ModelRequest("правила владельца", [{"role": "assistant", "content": "прошлый ответ"}, {"role": "user", "content": "новая реплика"}], "123")
    assert gateway.complete(request) == "ответ"
    assert captured["instructions"] == "правила владельца"
    assert captured["input"] == request.messages
    assert captured["stream"] is True and captured["closed"] is True
    assert captured["store"] is False
    assert captured["service_tier"] == "default"
    assert captured.get("reasoning") == reasoning
    if reasoning:
        assert "temperature" not in captured


def test_training_never_calls_model_and_style_rules_are_retrievable(app_state):
    store, gateway = app_state
    client = TestClient(web.app)
    auth = signed_data()
    assert client.post("/api/action", json={"action": "save_qa", "question": "Как ответишь другу?", "answer": "ща", "initData": auth}).status_code == 200
    assert client.post("/api/action", json={"action": "save_style_rule", "text": "не используй эмодзи", "initData": auth}).status_code == 200
    assert not gateway.calls
    assert "не используй эмодзи" in bot.build_prompt(bot.load_mem("123"), [], "пиши про отпуск").instructions
