import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from telegram import InlineKeyboardMarkup

import bot


def test_reply_keyboard_does_not_launch_a_webapp_without_identity(monkeypatch):
    monkeypatch.setattr(bot, "WEBAPP_URL", "https://miniapp.example/")
    monkeypatch.setattr(bot, "WEBAPP_API_URL", "https://miniapp.example")
    button = bot.main_menu().keyboard[0][0]
    assert button.text == bot.BTN_WEBAPP
    # Telegram supplies no initData to apps launched directly from reply keyboards.
    assert button.web_app is None


def test_miniapp_menu_action_sends_an_authenticated_launch_button(app_state, monkeypatch):
    _, gateway = app_state
    monkeypatch.setattr(bot, "WEBAPP_URL", "https://miniapp.example/")
    monkeypatch.setattr(bot, "WEBAPP_API_URL", "https://miniapp.example")
    replies = []

    async def reply(update, text, **options):
        replies.append((text, options))

    monkeypatch.setattr(bot, "safe_reply", reply)
    handled = asyncio.run(bot.handle_button(SimpleNamespace(), bot.load_mem("123"), "123", bot.BTN_WEBAPP))
    assert handled is True
    assert len(replies) == 1
    markup = replies[0][1]["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    button = markup.inline_keyboard[0][0]
    assert button.web_app is not None
    parts = urlsplit(button.web_app.url)
    assert (parts.scheme, parts.netloc) == ("https", "miniapp.example")
    assert parse_qs(parts.query)["api"] == ["https://miniapp.example"]
    assert not gateway.calls


def test_unconfigured_menu_keeps_setup_help_and_does_not_call_model(app_state, monkeypatch):
    _, gateway = app_state
    monkeypatch.setattr(bot, "WEBAPP_URL", "")
    replies = []

    async def reply(update, text, **options):
        replies.append(text)

    monkeypatch.setattr(bot, "safe_reply", reply)
    asyncio.run(bot.handle_button(SimpleNamespace(), bot.load_mem("123"), "123", bot.BTN_WEBAPP))
    assert "Start Mini App.cmd" in replies[0]
    assert not gateway.calls
