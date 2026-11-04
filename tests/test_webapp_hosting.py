from fastapi.testclient import TestClient
from urllib.parse import parse_qs, urlsplit

import bot
import web_backend as web


def test_same_origin_app_serves_only_public_assets(app_state):
    client = TestClient(web.app)
    page = client.get("/")
    assert page.status_code == 200
    assert '<html lang="ru">' in page.text
    assert 'assets/app.css' in page.text
    assert client.get("/index.html").content == page.content
    assert "no-cache" in page.headers["cache-control"]
    for path in ("/assets/app.css", "/assets/fonts/Onest-Variable.ttf", "/assets/fonts/OFL.txt"):
        response = client.get(path)
        assert response.status_code == 200
        assert len(response.content) > 100
    for path in ("/config.json", "/.env", "/bot.py", "/memory/twin.sqlite3", "/work/miniapp-runtime/bot.log",
                 "/assets/config.json", "/assets/../config.json", "/assets/%2e%2e/config.json",
                 "/assets/fonts/%2e%2e/%2e%2e/config.json"):
        assert client.get(path).status_code == 404, path


def test_hosting_does_not_bypass_telegram_auth(app_state):
    _, gateway = app_state
    client = TestClient(web.app)
    assert client.get("/").status_code == 200
    assert client.post("/api/action", json={"action": "memory"}).status_code == 401
    assert client.post("/api/chat/stream", json={"action": "chat", "text": "привет"}).status_code == 401
    assert not gateway.calls


def test_configured_same_origin_creates_a_telegram_webapp_button(monkeypatch):
    monkeypatch.setattr(bot, "WEBAPP_URL", "https://miniapp.example/")
    monkeypatch.setattr(bot, "WEBAPP_API_URL", "https://miniapp.example")
    button = bot.webapp_menu().inline_keyboard[0][0]
    assert button.web_app is not None
    parts = urlsplit(button.web_app.url)
    assert (parts.scheme, parts.netloc) == ("https", "miniapp.example")
    assert parse_qs(parts.query)["api"] == ["https://miniapp.example"]
