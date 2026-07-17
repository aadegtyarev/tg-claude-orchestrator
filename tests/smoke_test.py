"""Оффлайн-смоук без Telegram и без Claude:
- контракт channel_server (handshake, tools, push, permission-вердикт);
- split_text; чтение конфига; паритет ключей texts.py.

Запуск: .venv/bin/python tests/smoke_test.py
"""
import asyncio, json, os, socket, sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_split_text():
    from bot import split_text
    assert split_text("") == []
    assert split_text("a" * 5000) == ["a" * 4096, "a" * 904]
    chunks = split_text(("слово " * 100 + "\n") * 20)
    assert all(len(c) <= 4096 for c in chunks)
    print("OK split_text")


def test_texts():
    from texts import MESSAGES
    assert set(MESSAGES["ru"]) == set(MESSAGES["en"]), \
        set(MESSAGES["ru"]) ^ set(MESSAGES["en"])
    print("OK texts (ru/en паритет)")


def test_config():
    os.environ.update({"TELEGRAM_BOT_TOKEN": "x", "ALLOWED_USER_IDS": "1, 2,мусор,"})
    from config import Config
    c = Config.from_env()
    assert c.allowed_user_ids == frozenset({1, 2})
    assert c.permission_mode == "auto" and c.bot_lang == "ru"
    print("OK config")


async def test_channel_server():
    port = free_port()
    env = {**os.environ, "CHANNEL_PORT": str(port), "SESSION_NAME": "smoke",
           "ORCH_HOST": "127.0.0.1", "ORCH_PORT": str(free_port())}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "channel_server.py"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env)

    async def send(m):
        proc.stdin.write((json.dumps(m) + "\n").encode())
        await proc.stdin.drain()

    async def recv():
        return json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))

    await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    r = await recv()
    caps = r["result"]["capabilities"]
    assert caps["experimental"]["claude/channel"] == {}
    assert caps["experimental"]["claude/channel/permission"] == {}
    assert caps["tools"] == {} and r["result"]["instructions"]
    await send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # битая строка не роняет цикл
    proc.stdin.write(b"{broken json\n")
    await proc.stdin.drain()

    await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = [t["name"] for t in (await recv())["result"]["tools"]]
    assert tools == ["reply_to_telegram", "send_file_to_telegram"], tools

    import aiohttp
    await asyncio.sleep(0.3)
    async with aiohttp.ClientSession() as http:
        async with http.get(f"http://127.0.0.1:{port}/ping") as resp:
            assert resp.status == 200
        async with http.post(f"http://127.0.0.1:{port}/notify",
                             json={"content": "тест", "context_id": "tg:1:2:3"}) as resp:
            assert resp.status == 200
    push = await recv()
    assert push["params"] == {"content": "тест", "meta": {"context_id": "tg:1:2:3"}}

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


if __name__ == "__main__":
    test_split_text()
    test_texts()
    test_config()
    asyncio.run(test_channel_server())
    print("ALL OK")
