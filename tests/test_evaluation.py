import pytest

import bot
import services
from evaluate import compare, ensure_held_out, load_cases
from test_memory import transcript


def test_evaluation_rejects_imported_chat(app_state):
    store, _ = app_state
    services.import_chat(bot, bot.load_mem("123"), "123", transcript(chat_id=900))
    with pytest.raises(ValueError, match="обучающей памяти"):
        ensure_held_out(store, "123", [{"chat_id": "900"}])
    ensure_held_out(store, "456", [{"chat_id": "900"}])


def test_offline_evaluation_template():
    cases = load_cases("evaluation/dataset.example.json")
    result = compare(cases[0], "позже напишу")
    assert result["owner_rating"] is None
    assert result["length_ratio"] > 0
