"""Регрессия логики inline-кнопок: парсинг callback_data, гварды доступа,
роутинг в нужный метод менеджера. Без сети и Telegram.

Ловит класс багов вроде «message_thread_id передан в message.answer()» —
хендлеры прогоняются на фейках, проверяется, что нужный метод вызван и
чужой пользователь отклонён.

Запуск: .venv/bin/python tests/callbacks_test.py
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

import bot as botmod  # noqa: E402
from bot import TelegramBot  # noqa: E402
from texts import get_texts  # noqa: E402

calls: list = []


class FakeMsg:
    def __init__(self):
        self.text = "🔐\nBash: cmd\nx"

    async def answer(self, text, **kw):
        return SimpleNamespace(edit_text=self._edit)

    async def edit_text(self, text, **kw):
        pass

    async def _edit(self, text, **kw):
        pass


class FakeMgr:
    def get(self, tid):
        if tid == 7:
            return SimpleNamespace(thread_id=7, title="T", name="t", model=None, running=True)
        return None

    async def set_model(self, s, m):
        calls.append(("set_model", m))
        return True

    async def close(self, s):
        calls.append(("close",))

    async def send_permission(self, s, rid, beh):
        calls.append(("perm", rid, beh))

    async def send_to_claude(self, s, t, c):
        calls.append(("stop_sent",))

    def read_stats(self, s):
        return None


class FakeBubbles:
    async def close(self, tid):
        pass

    async def append(self, tid, line):
        pass


def make_bot():
    botmod.Message = FakeMsg  # isinstance-гвард: FakeMsg считаем Message
    b = TelegramBot.__new__(TelegramBot)
    b._texts = get_texts("ru")
    b.manager = FakeMgr()
    b.chat_id = -100
    b.config = SimpleNamespace(allowed_user_ids={1})
    b._typing = {}
    b.bubbles = FakeBubbles()
    b._stop_typing = lambda tid: None
    b._switch_model = TelegramBot._switch_model.__get__(b)
    b._stats_text = lambda s: "stats"
    return b


def cb(data, uid=1, msg=None):
    async def answer(*a, **k):
        pass
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), data=data,
                           message=msg or FakeMsg(), answer=answer)


async def main():
    b = make_bot()

    calls.clear()
    await b.on_model_button(cb("model:7:opus"))
    assert ("set_model", "opus") in calls
    print("OK on_model_button allow")

    calls.clear()
    await b.on_model_button(cb("model:7:opus", uid=999))  # чужой
    assert not calls
    print("OK on_model_button denies stranger")

    calls.clear()
    await b.on_model_button(cb("model:xx:opus"))  # битый thread_id
    assert not any(c[0] == "set_model" for c in calls)
    print("OK on_model_button bad thread_id")

    calls.clear()
    await b.on_session_button(cb("sess:close:7"))
    assert ("close",) in calls
    print("OK on_session_button close")

    calls.clear()
    await b.on_perm_button(cb("perm:7:abcde:allow"))
    assert ("perm", "abcde", "allow") in calls
    print("OK on_perm_button allow")

    calls.clear()
    await b.on_perm_button(cb("perm:7:ab:cd:deny"))  # request_id с ':'
    assert ("perm", "ab:cd", "deny") in calls
    print("OK on_perm_button request_id with colon (rsplit)")

    calls.clear()
    await b.on_stop_button(cb("stop:7"))
    assert ("stop_sent",) in calls
    print("OK on_stop_button")

    # несуществующая сессия — все хендлеры не должны падать
    for data, h in [("model:5:o", b.on_model_button), ("sess:close:5", b.on_session_button),
                    ("perm:5:abcde:allow", b.on_perm_button), ("stop:5", b.on_stop_button)]:
        await h(cb(data))
    print("OK missing session handled")

    print("ALL CALLBACKS OK")


if __name__ == "__main__":
    asyncio.run(main())
