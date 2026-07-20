"""Офлайн-тест reply-сервера: единственная сетевая HTTP-поверхность оркестратора.

Токен (_auth) страхует от того, что любой локальный процесс / вкладка через
DNS-rebinding POST'нет /reply с file_path и выгрузит файл в чат. Проверяем:
401 без/с неверным токеном на каждом роуте; 400 на битом JSON; асимметрию
политики ошибок — /reply и /permission отдают 500 при падении хендлера, а
/event и /stop проглатывают (200), чтобы не блокировать Claude.

Запуск: .venv/bin/python tests/reply_server_test.py
"""
import asyncio
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp  # noqa: E402

from orchestrator.core.reply_server import start_reply_server  # noqa: E402

TOKEN = "secret-token-123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _boom(*a):
    raise RuntimeError("handler blew up")


async def _ok(*a):
    return None


async def test_reply_server():
    port = free_port()
    config = SimpleNamespace(
        orch_token=TOKEN, orch_host="127.0.0.1", orch_port=port
    )
    # reply/permission — падают (проверяем 500); event/stop — падают (проверяем
    # проглатывание, 200).
    runner = await start_reply_server(
        config,
        reply_handler=_boom,
        tool_event_handler=_boom,
        permission_handler=_boom,
        stop_handler=_boom,
    )
    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {TOKEN}"}
    routes = ["/reply", "/event/x", "/permission/x", "/stop/x"]
    try:
        async with aiohttp.ClientSession() as http:
            # 1. Без токена → 401 на каждом роуте.
            for r in routes:
                async with http.post(base + r, json={}) as resp:
                    assert resp.status == 401, (r, resp.status)
            print("OK 401 без Authorization на всех роутах")

            # 2. Неверный токен → 401.
            for r in routes:
                async with http.post(
                    base + r, json={}, headers={"Authorization": "Bearer nope"}
                ) as resp:
                    assert resp.status == 401, (r, resp.status)
            print("OK 401 при неверном токене")

            # 2б. Не-ASCII токен не роняет сравнение (compare_digest на байтах).
            async with http.post(
                base + "/reply", json={}, headers={"Authorization": "Bearer ключ"}
            ) as resp:
                assert resp.status == 401, resp.status
            print("OK не-ASCII токен → 401, без падения сервера")

            # 3. Верный токен + битый JSON → 400 на каждом роуте.
            for r in routes:
                async with http.post(
                    base + r, data=b"{not json", headers=auth
                ) as resp:
                    assert resp.status == 400, (r, resp.status)
            print("OK 400 на невалидном JSON")

            # 4. Хендлер падает: /reply и /permission → 500 (ошибка видна).
            for r in ("/reply", "/permission/x"):
                async with http.post(base + r, json={}, headers=auth) as resp:
                    assert resp.status == 500, (r, resp.status)
            print("OK /reply и /permission → 500 при падении хендлера")

            # 5. /event и /stop проглатывают падение хендлера → 200 (хук не
            #    должен блокировать Claude).
            for r in ("/event/x", "/stop/x"):
                async with http.post(base + r, json={}, headers=auth) as resp:
                    assert resp.status == 200, (r, resp.status)
            print("OK /event и /stop → 200 даже при падении (проглочено)")
    finally:
        await runner.cleanup()

    # 6. Happy-path на отдельном сервере с нефейлящими хендлерами.
    port2 = free_port()
    config2 = SimpleNamespace(orch_token=TOKEN, orch_host="127.0.0.1", orch_port=port2)
    seen = []
    runner2 = await start_reply_server(
        config2,
        reply_handler=lambda d: seen.append(("reply", d)) or _ok(),
        tool_event_handler=_ok,
        permission_handler=_ok,
        stop_handler=_ok,
    )
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"http://127.0.0.1:{port2}/reply", json={"text": "hi"}, headers=auth
            ) as resp:
                assert resp.status == 200, resp.status
                assert (await resp.text()) == "OK"
        assert seen == [("reply", {"text": "hi"})], seen
        print("OK happy-path: 200 OK, хендлер получил payload")
    finally:
        await runner2.cleanup()


def main():
    asyncio.run(test_reply_server())
    print("ALL REPLY-SERVER OK")


if __name__ == "__main__":
    main()
