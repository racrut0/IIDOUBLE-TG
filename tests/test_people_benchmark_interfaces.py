import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

import bot
import services
import telegram_workflows as screens
import web_backend as web
from test_api import signed_data
from test_memory import transcript


def imports():
    for chat_id in (901, 902):
        services.import_chat(bot, bot.load_mem("123"), "123", transcript(count=60, chat_id=chat_id))


def test_signed_api_profiles_and_benchmark_use_the_same_owner_services(app_state):
    store, gateway = app_state
    imports()
    client = TestClient(web.app)

    def send(action, **extra):
        return client.post("/api/action", json={"action": action, "initData": signed_data(), **extra})

    assert client.post("/api/action", json={"action": "benchmark"}).status_code == 401
    assert send("save_interlocutor", label="Друг", relationship="unknown").status_code == 400
    person = send("save_interlocutor", label="Друг", relationship="friend", chat_ids=["901"]).json()["saved_interlocutor_id"]
    assert send("select_interlocutor", interlocutor_id=person).json()["active_interlocutor_id"] == person
    suite = send("create_benchmark", chat_id="901").json()["suite"]
    assert len(suite["cases"]) == 20
    run = send("start_benchmark").json()["run"]
    assert not gateway.calls
    assert send("generate_benchmark", case_id="case-01").status_code == 400
    assert send("rate_benchmark", case_id="case-01", rating="like").status_code == 400
    assert client.post("/api/action", json={"action": "generate_benchmark", "initData": signed_data(456), "run_id": run["id"], "case_id": "case-01"}).status_code == 400
    assert send("generate_benchmark", run_id=run["id"], case_id="case-01").json()["run"]["generated"] == 1
    assert len(gateway.calls) == 1
    response = send("rate_benchmark", run_id=run["id"], case_id="case-01", rating="partial")
    assert response.json()["run"]["score"] == 50
    assert send("rate_benchmark", run_id=run["id"], case_id="case-01", rating="invented").status_code == 422
    assert not store.count("123", "history")


def test_telegram_wizard_and_per_case_review_never_fall_into_paid_chat(app_state):
    store, gateway = app_state
    imports()
    replies = []

    async def reply(text, **kwargs):
        replies.append(text)

    async def typing(**kwargs):
        pass

    update = SimpleNamespace(message=SimpleNamespace(text="", reply_text=reply), effective_chat=SimpleNamespace(id=123),
        effective_user=SimpleNamespace(id=123, username="synthetic_owner", first_name="Owner", last_name=None, language_code="ru"))
    ctx = SimpleNamespace(bot=SimpleNamespace(send_chat_action=typing))

    async def press(text):
        update.message.text = text
        await bot.handle(update, ctx)
        assert not any(message.startswith("ошибка:") for message in replies)

    async def scenario():
        await press(screens.PEOPLE)
        await press(screens.ADD)
        await press("Друг для проверки")
        await press("Друг")
        await press("1")
        await press(screens.NO_NOTES)
        mem = bot.load_mem("123")
        assert mem["interlocutors"][0]["chat_ids"] == ["901"]
        assert mem["active_interlocutor_id"] == mem["interlocutors"][0]["id"]
        assert not gateway.calls
        await press(screens.BENCHMARK)
        await press("1")
        await press(screens.PREPARE)
        assert not gateway.calls
        await press(screens.GENERATE)
        assert len(gateway.calls) == 1
        await press("➖ Частично")
        assert len(gateway.calls) == 1
        assert bot.load_mem("123")["ui_benchmark"]["case_id"] != "case-01"
        await press(bot.BTN_BACK)
        assert bot.load_mem("123")["mode"] == "chat"

    asyncio.run(scenario())
    assert not store.count("123", "history")
    assert any("50%" in message for message in replies)
