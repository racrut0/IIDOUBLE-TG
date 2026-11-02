import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import bot
import services
import web_backend as web
from storage import MemoryStore
from test_api import signed_data
from test_memory import transcript


def confirm(client, preview, user_id=123, **extra):
    return client.post("/api/action", json={"action": "confirm_import", "initData": signed_data(user_id),
        "import_token": preview["token"], "participant_key": preview["selected"], "preview_revision": preview["revision"], **extra})


def test_upload_only_previews_then_explicit_confirmation_trains_once(app_state):
    store, gateway = app_state
    client = TestClient(web.app)
    response = client.post("/api/action", json={"action": "import_tg_chat", "text": transcript(), "initData": signed_data()})
    assert response.status_code == 200
    preview = response.json()["preview"]
    assert preview["mine"] == 1 and preview["pairs"] == 1
    assert "OWNER_1" in preview["samples"][0]["answer"]
    for category in ("imported_dialogues", "examples", "import_facts", "imported_chats"):
        assert store.count("123", category) == 0
    assert "OWNER_1" not in bot.build_prompt(bot.load_mem("123"), [], "пойдём гулять").render()
    assert confirm(client, preview).status_code == 200
    assert store.count("123", "imported_dialogues") == 2
    assert store.count("123", "examples") == 1
    assert confirm(client, preview).status_code == 400
    assert store.count("123", "examples") == 1
    assert not gateway.calls
    assert bot.spending.status()["requests"] == 0


def test_same_names_and_changed_nicknames_do_not_override_id(app_state):
    store, _ = app_state
    raw = json.dumps({"id": 900, "messages": [
        {"id": 1, "from_id": "user999", "from": "VisibleName", "text": "Чужая фраза"},
        {"id": 2, "from_id": "user123", "from": "Старое имя", "text": "Свой ответ"},
        {"id": 3, "from_id": "user123", "from": "Новое имя", "text": "Своё продолжение"}]})
    preview = services.preview_import(bot, "123", raw, "VisibleName")["preview"]
    assert preview["mine"] == 2
    mine = next(p for p in preview["participants"] if p["verified_self"])
    assert mine["names"] == ["Старое имя", "Новое имя"]
    other = next(p for p in preview["participants"] if not p["verified_self"])
    assert not other["selectable"]
    with pytest.raises(ValueError, match="ID другого"):
        services.select_import_owner(bot, "123", preview["token"], other["key"], preview["revision"])
    assert store.count("123", "examples") == 0


def test_txt_requires_review_and_stale_author_confirmation_is_rejected(app_state):
    store, gateway = app_state
    client = TestClient(web.app)
    preview = services.preview_import(bot, "123", "Друг: Пойдём гулять?\nЯ в файле: позже приду")["preview"]
    assert not preview["can_confirm"]
    person = next(p for p in preview["participants"] if "Я в файле" in p["names"])
    response = client.post("/api/action", json={"action": "select_import_owner", "initData": signed_data(),
        "import_token": preview["token"], "participant_key": person["key"], "preview_revision": preview["revision"]})
    selected = response.json()["preview"]
    assert selected["can_confirm"] and selected["warning"]
    assert selected["samples"][0]["answer"] == "позже приду"
    assert confirm(client, preview).status_code == 400
    assert confirm(client, selected).status_code == 200
    assert store.count("123", "examples") == 1
    assert not gateway.calls


def test_preview_survives_restart_but_not_profile_reset_or_expiry(app_state, monkeypatch):
    store, _ = app_state
    preview = services.preview_import(bot, "123", transcript())["preview"]
    monkeypatch.setattr(bot, "_store", MemoryStore(store.path))
    assert services.get_import_preview(bot, "123")["preview"] == preview
    with store.transaction() as db:
        db.execute("UPDATE pending_imports SET created=0 WHERE uid='123'")
    with pytest.raises(ValueError, match="устарел"):
        services.confirm_import(bot, "123", preview["token"], preview["selected"], preview["revision"])
    preview = services.preview_import(bot, "123", transcript())["preview"]
    store.reset("123")
    with pytest.raises(ValueError, match="устарел"):
        services.confirm_import(bot, "123", preview["token"], preview["selected"], preview["revision"])
    assert store.count("123", "imported_dialogues") == 0


def test_previews_are_private_and_new_file_invalidates_previous_token(app_state):
    store, _ = app_state
    client = TestClient(web.app)
    old = services.preview_import(bot, "123", transcript())["preview"]
    assert confirm(client, old, user_id=456).status_code == 400
    assert client.post("/api/action", json={"action": "get_import_preview", "import_token": old["token"], "initData": signed_data(456)}).status_code == 400
    new = services.preview_import(bot, "123", transcript(chat_id=901))["preview"]
    assert confirm(client, old).status_code == 400
    assert confirm(client, new).status_code == 200
    assert store.count("456", "imported_dialogues") == 0


def test_cancel_does_not_touch_existing_archive_and_reimport_is_deduplicated(app_state):
    store, _ = app_state
    client = TestClient(web.app)
    first = services.preview_import(bot, "123", transcript())["preview"]
    assert confirm(client, first).status_code == 200
    second = services.preview_import(bot, "123", transcript())["preview"]
    services.cancel_import(bot, "123", second["token"])
    assert store.count("123", "imported_dialogues") == 2
    assert confirm(client, second).status_code == 400
    third = services.preview_import(bot, "123", transcript())["preview"]
    assert confirm(client, third).status_code == 200
    assert store.count("123", "imported_dialogues") == 2
    assert store.count("123", "examples") == 1


def test_archive_only_never_trains_foreign_messages(app_state):
    store, gateway = app_state
    raw = json.dumps({"id": 901, "messages": [{"id": 1, "from": "Owner", "from_id": "user999", "text": "Я живу в Чужомгороде"}]})
    preview = services.preview_import(bot, "123", raw, "Owner")["preview"]
    assert not preview["can_confirm"]
    result = confirm(TestClient(web.app), preview, archive_only=True)
    assert result.status_code == 200
    assert store.count("123", "imported_dialogues") == 1
    assert not store.count("123", "examples") and not store.count("123", "import_facts")
    assert "Чужомгороде" not in bot.build_prompt(bot.load_mem("123"), [], "где ты живёшь").render()
    assert not gateway.calls


def test_html_forwarded_text_is_excluded_and_outer_author_is_preserved(app_state):
    raw = '''<html><div class="message default"><div class="from_name">Моё имя</div>
        <div class="forwarded body"><div class="from_name">Чужой автор</div><div class="text">Я живу в Пересланномгороде</div></div></div>
        <div class="message default joined"><div class="text">Мой настоящий ответ</div></div></html>'''
    preview = services.preview_import(bot, "123", raw, "Моё имя")["preview"]
    assert preview["mine"] == 2 and preview["trainable_mine"] == 1
    assert preview["own_samples"] == ["Мой настоящий ответ"]
    assert "Чужой автор" not in str(preview["participants"])
    assert confirm(TestClient(web.app), preview).status_code == 200
    assert not bot.load_mem("123")["import_facts"]


def test_atomic_confirmation_keeps_preview_if_save_rolls_back(app_state, monkeypatch):
    store, _ = app_state
    preview = services.preview_import(bot, "123", transcript())["preview"]
    original = store._put
    def broken(db, uid, category, value):
        original(db, uid, category, value)
        if category == "imported_dialogues":
            raise RuntimeError("simulated disk failure")
    monkeypatch.setattr(store, "_put", broken)
    with pytest.raises(RuntimeError):
        services.confirm_import(bot, "123", preview["token"], preview["selected"], preview["revision"])
    assert store.pending_import("123", preview["token"])
    assert store.count("123", "imported_dialogues") == 0
    assert store.count("123", "examples") == 0


def test_telegram_upload_is_pending_until_button_confirmation(app_state):
    store, gateway = app_state
    messages = []
    async def reply(text, **kwargs):
        messages.append(text)
    update = SimpleNamespace(message=SimpleNamespace(text="/import_chat " + transcript(), reply_text=reply),
        effective_user=SimpleNamespace(id=123, username="owner", first_name="Owner", last_name=None, language_code="ru"))
    async def scenario():
        await bot.import_chat_command(update, None)
        assert store.count("123", "imported_dialogues") == 0
        mem = bot.load_mem("123")
        assert mem["mode"] == "import_review"
        await bot.handle_import_review(update, mem, "123", bot.BTN_IMPORT_CONFIRM)
    asyncio.run(scenario())
    assert store.count("123", "imported_dialogues") == 2
    assert any("Импорт подтверждён" in text for text in messages)
    assert not gateway.calls


def test_old_telegram_preview_cannot_restore_pending_mode_after_reset(app_state):
    store, _ = app_state
    result = services.preview_import(bot, "123", transcript(), telegram=True)
    store.reset("123")
    with pytest.raises(ValueError, match="сброшен"):
        asyncio.run(bot.show_import_preview(None, "123", result))
    assert not bot.load_mem("123")["pending_import_token"]
