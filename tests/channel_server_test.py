"""HTTP-приём channel_server (stdlib http.server) — приём push от оркестратора.

Канал переписан с aiohttp на stdlib, чтобы работать и на хосте (bwrap), и ВНУТРИ
гостя agent-vm (там aiohttp нет). Проверяем реальный threaded-сервер + мост в
event loop: auth-гейт, /ping, /notify→push в Claude, /permission→вердикт, битый JSON.

Запуск: .venv/bin/python tests/channel_server_test.py
"""
import asyncio
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import orchestrator.channel_server as cs  # noqa: E402

# Без прокси: на этой машине в env бывает HTTP_PROXY — 127.0.0.1 должен идти прямо.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _req(method, port, path, token=None, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


async def _main():
    port = _free_port()
    cs.CHANNEL_PORT = port
    cs.ORCH_TOKEN = "sekret"

    server = cs.ChannelServer()
    server._loop = asyncio.get_running_loop()
    written: list = []

    async def fake_write(msg):
        written.append(msg)
    server._write_message = fake_write
    server._initialized.set()  # хендшейк «завершён» — /notify не будет ждать

    httpd = server._start_http()
    try:
        # auth-гейт: без/с неверным токеном — 401
        assert _req("GET", port, "/ping")[0] == 401
        assert _req("GET", port, "/ping", token="wrong")[0] == 401
        # /ping с токеном — 200 OK
        assert _req("GET", port, "/ping", token="sekret") == (200, "OK")
        print("OK auth-гейт: 401 без/с неверным токеном, 200 /ping с верным")

        # /notify → push notifications/claude/channel в Claude
        code, _ = await asyncio.to_thread(
            _req, "POST", port, "/notify", "sekret",
            {"content": "привет", "context_id": "tg:demo:1"},
        )
        assert code == 200
        push = written[-1]
        assert push["method"] == "notifications/claude/channel"
        assert push["params"]["content"] == "привет"
        assert push["params"]["meta"]["context_id"] == "tg:demo:1"
        print("OK /notify → push в Claude с content+context_id")

        # /permission → вердикт (нормализация behavior/request_id)
        code, _ = await asyncio.to_thread(
            _req, "POST", port, "/permission", "sekret",
            {"request_id": "ReQ-1", "behavior": "allow"},
        )
        assert code == 200
        perm = written[-1]
        assert perm["method"] == "notifications/claude/channel/permission"
        assert perm["params"] == {"request_id": "req-1", "behavior": "allow"}
        # любой не-allow → deny
        await asyncio.to_thread(
            _req, "POST", port, "/permission", "sekret",
            {"request_id": "x", "behavior": "wat"},
        )
        assert written[-1]["params"]["behavior"] == "deny"
        print("OK /permission → вердикт (request_id lower, allow/deny)")

        # битый JSON → 400
        url = f"http://127.0.0.1:{port}/notify"
        req = urllib.request.Request(
            url, data=b"{not-json", method="POST",
            headers={"Authorization": "Bearer sekret", "Content-Type": "application/json"},
        )
        try:
            _OPENER.open(req, timeout=5)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 400
        print("OK битый JSON → 400")

        # keep-alive: 401-POST С ТЕЛОМ, затем валидный запрос на ТОМ ЖЕ соединении.
        # Без вычитывания тела остаток рассинхронил бы сокет и следующий запрос
        # вернул бы мусор/400 (баг, пойманный ревью).
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/notify",
                     body=json.dumps({"content": "x", "context_id": "1"}),
                     headers={"Content-Type": "application/json"})  # без токена
        r1 = conn.getresponse()
        assert r1.status == 401
        r1.read()
        conn.request("GET", "/ping", headers={"Authorization": "Bearer sekret"})
        r2 = conn.getresponse()
        assert r2.status == 200 and r2.read() == b"OK", (r2.status,)
        conn.close()
        print("OK keep-alive: 401-POST с телом не ломает следующий запрос на сокете")
    finally:
        httpd.shutdown()
        httpd.server_close()
    print("ALL CHANNEL-SERVER OK")


def test_channel_server():
    asyncio.run(_main())


if __name__ == "__main__":
    asyncio.run(_main())
