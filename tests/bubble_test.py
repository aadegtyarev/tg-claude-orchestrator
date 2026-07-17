"""Регрессия статус-бабла: гейт активности (событие после close не рождает
бабл-сироту, новый ход снова работает). Без сети и Telegram.

Запуск: .venv/bin/python tests/bubble_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from bubble import BubbleManager  # noqa: E402

_TEXTS = {"bubble_working": "Работаю", "bubble_stop": "Стоп"}


class FakeBot:
    async def send_message(self, **k):
        return SimpleNamespace(message_id=1)

    async def edit_message_text(self, **k):
        pass

    async def delete_message(self, **k):
        pass

    async def edit_message_reply_markup(self, **k):
        pass


async def main():
    bm = BubbleManager(FakeBot(), lambda: -100, lambda k, **kw: _TEXTS[k], delete_after=True)

    # append без open — сироты быть не должно
    await bm.append(7, "🔧 late")
    await asyncio.sleep(2)
    assert not bm.has(7)
    print("OK append without open ignored")

    # активный ход
    bm.open(7)
    await bm.append(7, "🔧 Bash: ls")
    await asyncio.sleep(2)
    assert bm.has(7)
    print("OK bubble created during active turn")

    # событие после close — не сирота
    await bm.close(7)
    assert not bm.has(7)
    await bm.append(7, "🔧 late hook")
    await asyncio.sleep(2)
    assert not bm.has(7)
    print("OK event after close does not orphan")

    # новый ход снова работает
    bm.open(7)
    await bm.append(7, "🔧 new turn")
    await asyncio.sleep(2)
    assert bm.has(7)
    print("OK new turn after close works")
    print("ALL BUBBLE OK")


if __name__ == "__main__":
    asyncio.run(main())
