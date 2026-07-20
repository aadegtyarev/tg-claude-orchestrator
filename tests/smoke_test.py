"""Оффлайн-смоук без Telegram и без Claude:
- контракт channel_server (handshake, tools, push, permission-вердикт);
- split_text; чтение конфига; паритет ключей texts.py.

Запуск: .venv/bin/python tests/smoke_test.py
"""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_split_text():
    from orchestrator.core.mdrender import split_text
    assert split_text("") == []
    assert split_text("a" * 5000) == ["a" * 4096, "a" * 904]
    chunks = split_text(("слово " * 100 + "\n") * 20)
    assert all(len(c) <= 4096 for c in chunks)
    print("OK split_text")


def test_md_table():
    from orchestrator.core.mdrender import md_to_html
    md = "| a | b |\n|---|---|\n| 1 | longcell |\n| 22 | x |"
    out = md_to_html(md)
    assert "<pre>" in out and "</pre>" in out, out
    # колонки выровнены (моношрифт): "1 " добито под ширину "22".
    assert "a  | b       " in out, out          # шапка добита под ширину колонок
    assert "1  | longcell" in out and "22 | x" in out, out
    # обычная строка с | (без разделителя) — не таблица, не оборачивается
    assert "<pre>" not in md_to_html("cd /a | grep b")
    # таблица внутри код-блока не пере-форматируется (одно <pre>, как есть)
    fenced = md_to_html("```\n| a | b |\n|---|---|\n```")
    assert fenced.count("<pre>") == 1 and "|---|---|" in fenced
    print("OK md-таблица → выровненный <pre>; обычный | и код-блок не тронуты")


def test_texts():
    from orchestrator.core.texts import MESSAGES
    assert set(MESSAGES["ru"]) == set(MESSAGES["en"]), \
        set(MESSAGES["ru"]) ^ set(MESSAGES["en"])
    print("OK texts (ru/en паритет)")


def test_config():
    os.environ.update({"TELEGRAM_BOT_TOKEN": "x", "ALLOWED_USER_IDS": "1, 2,мусор,"})
    from orchestrator.config import Config
    c = Config.from_env()
    assert c.allowed_user_ids == frozenset({1, 2})
    assert c.permission_mode == "auto" and c.bot_lang == "ru"
    print("OK config")


async def test_channel_server():
    await _run_channel_server()


async def _run_channel_server():
    port = free_port()
    # ORCH_TOKEN явно убираем: под pytest (один процесс) load_dotenv из
    # соседнего теста мог протащить реальный .env-токен в os.environ, и тогда
    # channel_server встал бы в auth-режим, а тест контракта шлёт без токена.
    env = {**os.environ, "CHANNEL_PORT": str(port), "SESSION_NAME": "smoke",
           "ORCH_HOST": "127.0.0.1", "ORCH_PORT": str(free_port())}
    env.pop("ORCH_TOKEN", None)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "orchestrator" / "channel_server.py"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env)

    async def send(m):
        proc.stdin.write((json.dumps(m) + "\n").encode())
        await proc.stdin.drain()

    async def recv():
        return json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))

    try:
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        r = await recv()
        caps = r["result"]["capabilities"]
        assert caps["experimental"]["claude/channel"] == {}
        assert caps["experimental"]["claude/channel/permission"] == {}
        assert caps["tools"] == {} and r["result"]["instructions"]
        # Регресс: инструкция явно предупреждает, что голый текст невидим —
        # источник «потерянных финалов» (см. tests/stop_fallback_test.py).
        assert "INVISIBLE" in r["result"]["instructions"]
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # битая строка не роняет цикл
        proc.stdin.write(b"{broken json\n")
        await proc.stdin.drain()

        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = [t["name"] for t in (await recv())["result"]["tools"]]
        assert tools == ["reply_to_user", "send_file_to_user"], tools

        import aiohttp
        await asyncio.sleep(0.3)
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/ping") as resp:
                assert resp.status == 200
            async with http.post(f"http://127.0.0.1:{port}/notify",
                                 json={"content": "тест", "context_id": "telegram:smoke:1:2:3"}) as resp:
                assert resp.status == 200
        push = await recv()
        assert push["params"] == {"content": "тест", "meta": {"context_id": "telegram:smoke:1:2:3"}}

        # вердикт разрешения
        async with aiohttp.ClientSession() as http:
            async with http.post(f"http://127.0.0.1:{port}/permission",
                                 json={"request_id": "ABCDE", "behavior": "allow"}) as resp:
                assert resp.status == 200
        verdict = await recv()
        assert verdict["method"] == "notifications/claude/channel/permission"
        assert verdict["params"] == {"request_id": "abcde", "behavior": "allow"}

        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 10)
        assert proc.returncode == 0
        print("OK channel_server (handshake, tools, push, permission)")
    finally:
        # Гарантированно прибираем subprocess: иначе под pytest (один процесс)
        # он висит дочерним и ломает watchdog_test (has_kids=True).
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


if __name__ == "__main__":
    test_split_text()
    test_md_table()
    test_texts()
    test_config()
    asyncio.run(test_channel_server())
    print("ALL OK")
