import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot
from storage import MemoryStore


class FakeGateway:
    def __init__(self):
        self.calls = []

    def events(self, request, *args):
        self.calls.append(request)
        yield {"delta": "ща, "}
        yield {"delta": "пять минут"}
        yield {"complete": "ща, пять минут", "ttft_ms": 12,
               "usage": {"input_tokens": 700, "cached_tokens": 300, "output_tokens": 8}}

    def complete(self, request, *args):
        self.calls.append(request)
        return "Как ты отказываешь другу во встрече?"


@pytest.fixture
def app_state(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.sqlite3", tmp_path)
    gateway = FakeGateway()
    monkeypatch.setattr(bot, "_store", store)
    monkeypatch.setattr(bot, "gateway", gateway)
    monkeypatch.setattr(bot, "TG_TOKEN", "123456:test-token-not-a-secret")
    return store, gateway
