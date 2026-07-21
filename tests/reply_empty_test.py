"""reply_to_user с пустым text НЕ пересылается пользователю, а отклоняется.

Наблюдение (agent-vm, 2026-07-21): изредка `reply_to_user` приходит с
`text=""`, хотя модель нарратит ответ — оркестратор логировал `reply len=0`,
и пользователь получал пустое сообщение, т.е. ответ терялся молча.

Пустой текст пользователю бесполезен при любой первопричине, поэтому канал
отвечает Claude isError с просьбой прислать текст (модель перепошлёт) и
логирует сырые arguments — это и есть капчур для диагностики, доезжающий
до хоста через лог оркестратора, а не только в stderr гостя.

Запуск: .venv/bin/python tests/reply_empty_test.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import orchestrator.channel_server as cs  # noqa: E402


def _server():
    server = cs.ChannelServer()
    written: list = []
    posted: list = []

    async def fake_write(msg):
        written.append(msg)

    async def fake_post(url, payload, timeout=30):
        posted.append((url, payload))
        return 200, "ok"

    server._write_message = fake_write
    server._post = fake_post
    return server, written, posted


async def _call(server, args, name="reply_to_user"):
    await server._handle_request(7, "tools/call", {"name": name, "arguments": args})
    # _forward_to_orchestrator уходит в фон через _spawn — дать ему прокрутиться.
    for _ in range(5):
        await asyncio.sleep(0)


async def _main():
    # 1. Пустой text — не пересылаем, отвечаем isError.
    server, written, posted = _server()
    await _call(server, {"context_id": "c1", "text": "", "complete": True})
    assert not posted, f"пустой reply не должен уходить оркестратору: {posted}"
    assert written, "Claude должен получить ответ на tools/call"
    result = written[-1]["result"]
    assert result.get("isError") is True, result
    text = result["content"][0]["text"].lower()
    assert "text" in text, result
    print("OK пустой text отклонён с isError (ответ не потерян молча)")

    # 2. Пробелы/переводы строк — тоже пусто.
    server, written, posted = _server()
    await _call(server, {"context_id": "c1", "text": "   \n\t ", "complete": False})
    assert not posted, f"пробельный reply не должен уходить: {posted}"
    assert written[-1]["result"].get("isError") is True
    print("OK пробельный text считается пустым")

    # 3. Нормальный текст проходит как раньше.
    server, written, posted = _server()
    await _call(server, {"context_id": "c1", "text": "готово", "complete": True})
    assert len(posted) == 1, posted
    assert posted[0][1]["text"] == "готово", posted
    assert written[-1]["result"].get("isError") is not True, written[-1]
    print("OK непустой text пересылается оркестратору")

    # 4. send_file_to_user с пустой caption не задет (caption необязателен).
    server, written, posted = _server()
    await _call(
        server,
        {"context_id": "c1", "file_path": "/tmp/a.png", "caption": ""},
        name="send_file_to_user",
    )
    assert len(posted) == 1, posted
    assert posted[0][1]["file_path"] == "/tmp/a.png"
    print("OK send_file_to_user с пустой caption проходит")

    print("ALL REPLY-EMPTY OK")


if __name__ == "__main__":
    asyncio.run(_main())
