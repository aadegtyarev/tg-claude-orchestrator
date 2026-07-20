"""Регресс: send_to_claude ретраит, пока channel-сервер поднимается.

Гонка двух сообщений подряд (media group / быстрый повтор): второе видит
session.running=True сразу после старта процесса claude, но channel-сервер
(MCP-подпроцесс) на порту ещё не слушает → ConnectionRefused. Короткий ретрай
закрывает стартовое окно; дохлая сессия → сразу пробрасываем.

Запуск: .venv/bin/python tests/send_retry_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp  # noqa: E402

from orchestrator.core import sessions  # noqa: E402
from orchestrator.core.sessions import SessionManager  # noqa: E402


def _conn_err() -> aiohttp.ClientConnectorError:
    # Конструктор ClientConnectorError разный по версиям — берём инстанс без
    # __init__: для except-по-типу и isinstance этого достаточно.
    return aiohttp.ClientConnectorError.__new__(aiohttp.ClientConnectorError)


class _Resp:
    def raise_for_status(self):
        return None


class _CM:
    def __init__(self, fail):
        self.fail = fail

    async def __aenter__(self):
        if self.fail:
            raise _conn_err()
        return _Resp()

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        return _CM(self.calls <= self.fail_times)


def _mgr(http):
    m = SessionManager.__new__(SessionManager)
    m._http = http
    m._http_session = lambda: http
    m._channel_headers = lambda: {}
    return m


async def run():
    sessions.SEND_RETRY_TIMEOUT = 5.0  # тест не должен ждать долго

    # 3 отказа подряд, потом успех — ретрай доводит доставку.
    http = _Http(fail_times=3)
    m = _mgr(http)
    sess = SimpleNamespace(name="s", port=12345, last_activity=0.0, running=True)
    await m.send_to_claude(sess, "hi", "ctx")
    assert http.calls == 4, http.calls
    print("OK send_to_claude: ретраит до подъёма channel-сервера")

    # Сессия умерла во время ретрая → сразу пробрасываем, не крутимся.
    http2 = _Http(fail_times=99)
    m2 = _mgr(http2)
    dead = SimpleNamespace(name="s", port=12345, last_activity=0.0, running=False)
    try:
        await m2.send_to_claude(dead, "hi", "ctx")
        assert False, "должно было пробросить"
    except aiohttp.ClientConnectorError:
        pass
    assert http2.calls == 1, http2.calls
    print("OK send_to_claude: дохлая сессия → не ретраим, пробрасываем")


def main():
    asyncio.run(run())
    print("ALL SEND-RETRY OK")


if __name__ == "__main__":
    main()
