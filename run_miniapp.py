"""Run the real Telegram bot and Mini App through a temporary HTTPS tunnel.

No credentials or URLs are written to config.json. Child processes share the
existing configuration and database; only their web addresses are overridden.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "work" / "miniapp-runtime"
TUNNEL_VERSION = "2026.10.7"
TUNNEL_SHA256 = "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343"
TUNNEL_DOWNLOAD = (
    "https://github.com/cloudflare/cloudflared/releases/download/"
    f"{TUNNEL_VERSION}/cloudflared-windows-amd64.exe"
)
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\b")


def file_hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def tunnel_binary():
    target = RUNTIME / f"cloudflared-{TUNNEL_VERSION}.exe"
    if target.is_file() and file_hash(target) == TUNNEL_SHA256:
        return target
    print("Скачиваю официальный Cloudflare Tunnel для первого запуска...", flush=True)
    request = urllib.request.Request(TUNNEL_DOWNLOAD, headers={"User-Agent": "IIDouble-launcher"})
    temporary = target.with_suffix(".download")
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    if file_hash(temporary) != TUNNEL_SHA256:
        raise RuntimeError("Проверка скачанного Cloudflare Tunnel не пройдена. Программа не запущена.")
    temporary.replace(target)
    return target


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def existing_bot():
    query = (
        "$ErrorActionPreference='Stop'; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        r"""Where-Object { $_.CommandLine -match '(?i)(^|[\s\\/"])bot\.py([\s"]|$)' } | """
        "Select-Object -ExpandProperty ProcessId"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", query],
                            capture_output=True, text=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Не удалось проверить, запущен ли бот. Повтори запуск.")
    return bool(result.stdout.strip())


def wait_for_tunnel(process, log_path, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Cloudflare Tunnel завершился. Подробности: work/miniapp-runtime/tunnel.log")
        contents = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        match = URL_PATTERN.search(contents)
        if match:
            return match.group(0)
        time.sleep(0.25)
    raise RuntimeError("Не удалось получить HTTPS-адрес за минуту. Проверь интернет и tunnel.log.")


def wait_for_health(base, process, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Сервер Mini App остановился. Подробности: work/miniapp-runtime/backend.log")
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as response:
                if response.status == 200 and json.load(response).get("ok") is True:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Сервер пока недоступен по HTTPS. Проверь интернет и повтори запуск.")


def check_auth(base):
    request = urllib.request.Request(base + "/api/action", data=b'{"action":"memory"}',
                                     headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return
    raise RuntimeError("Проверка защиты Telegram не пройдена. Запуск остановлен.")


def run(check_only=False):
    if sys.platform != "win32":
        raise RuntimeError("Этот способ быстрого запуска предназначен для Windows.")
    import msvcrt

    RUNTIME.mkdir(parents=True, exist_ok=True)
    # One launcher per checkout; the OS releases this lock even after a crash.
    with (RUNTIME / "launch.lock").open("a+b") as launch_lock:
        launch_lock.seek(0)
        if not launch_lock.read(1):
            launch_lock.write(b"0")
            launch_lock.flush()
        launch_lock.seek(0)
        try:
            msvcrt.locking(launch_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("Mini App уже запущен. Используй существующее окно запуска.") from error
        if not check_only and existing_bot():
            raise RuntimeError("Бот уже работает. Закрой окно с bot.py и запусти «Запустить Mini App» снова.")
        from bot import config

        if not config.get("TG_TOKEN"):
            raise RuntimeError("В config.json или .env не указан TG_TOKEN.")
        binary = tunnel_binary()
        port = unused_port()
        local = f"http://127.0.0.1:{port}"
        children, logs = [], []

        def start(arguments, name, environment):
            log = (RUNTIME / f"{name}.log").open("w", encoding="utf-8")
            logs.append(log)
            child = subprocess.Popen(arguments, cwd=ROOT, env=environment, stdout=log,
                                     stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            children.append(child)
            return child

        try:
            print("Подключаю временный HTTPS-адрес...", flush=True)
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            tunnel = start([str(binary), "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", local],
                           "tunnel", environment)
            public = wait_for_tunnel(tunnel, RUNTIME / "tunnel.log")
            environment.update(WEBAPP_URL=public + "/", WEBAPP_API_URL=public,
                               WEBAPP_ORIGINS=public, WEBAPP_VERSION="13")
            backend = start([sys.executable, "-u", "-m", "uvicorn", "web_backend:app", "--host",
                             "127.0.0.1", "--port", str(port), "--no-access-log"], "backend", environment)
            wait_for_health(local, backend)
            wait_for_health(public, backend)
            check_auth(public)
            print(f"Mini App: {public}", flush=True)
            print("Проверено: HTTPS доступен, память защищена подписью Telegram.", flush=True)
            if check_only:
                print("Проверка завершена без запуска Telegram-бота и без API модели.", flush=True)
                return
            start([sys.executable, "-u", str(ROOT / "bot.py")], "bot", environment)
            print("Бот запущен. В Telegram отправь /start, нажми «Mini App», затем «Открыть Mini App» под сообщением бота.", flush=True)
            print("Оставь это окно открытым. Для остановки нажми Ctrl+C.", flush=True)
            print("Адрес временный: после нового запуска снова отправь боту /start.", flush=True)
            while True:
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("Один из компонентов остановился. Логи: work/miniapp-runtime/.")
                time.sleep(1)
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    # Windows venv executables can start a second Python process.
                    # Stop the tree created by this launcher, not unrelated bots.
                    subprocess.run(["taskkill.exe", "/PID", str(child.pid), "/T", "/F"],
                                   capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
            for log in logs:
                log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="Check HTTPS and auth without starting the bot")
    options = parser.parse_args()
    try:
        run(options.check_only)
        return 0
    except KeyboardInterrupt:
        print("Mini App и бот остановлены.")
        return 0
    except ImportError:
        print("Не установлены зависимости проекта. Выполни установку из README.md.", file=sys.stderr)
        return 1
    except (RuntimeError, OSError) as error:
        print(f"Не удалось запустить Mini App: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
