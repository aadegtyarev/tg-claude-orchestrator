"""Регрессия логики inline-кнопок Telegram-адаптера: парсинг callback_data,
гварды доступа, роутинг в нужный метод ядра. Без сети и Telegram.

Ловит класс багов вроде «message_thread_id передан в message.answer()» —
хендлеры прогоняются на фейках, проверяется, что нужный метод ядра вызван и
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

from orchestrator.adapters.telegram import adapter as tgmod  # noqa: E402
from orchestrator.adapters.telegram.adapter import TelegramAdapter  # noqa: E402
from orchestrator.core.texts import get_texts  # noqa: E402

calls: list = []

SESSION = SimpleNamespace(
    name="t", title="T", model=None, running=True, bindings={"telegram": "7"}
)


class FakeMsg:
    def __init__(self):
        self.text = "🔐\nBash: cmd\nx"

    async def answer(self, text, **kw):
        return SimpleNamespace(edit_text=self._edit)

    async def edit_text(self, text, **kw):
        pass

    async def edit_reply_markup(self, **kw):
        pass

    async def _edit(self, text, **kw):
        pass


class FakeMgr:
    def get_by_binding(self, adapter, address):
        return SESSION if (adapter, address) == ("telegram", "7") else None


class FakeCore:
    def t(self, k, **kw):
        return get_texts("ru")[k].format(**kw)

    async def switch_model(self, s, m):
        calls.append(("set_model", m))
        return True

    async def close_session(self, s):
        calls.append(("close",))

    async def permission_verdict(self, s, rid, beh, via):
        calls.append(("perm", rid, beh))
        return True

    async def soft_stop(self, s, origin):
        calls.append(("stop_sent",))

    async def hard_stop(self, s):
        calls.append(("esc_sent",))

    def stats_text(self, s):
        return "stats"


def make_adapter():
    tgmod.Message = FakeMsg  # isinstance-гвард: FakeMsg считаем Message
    a = TelegramAdapter.__new__(TelegramAdapter)
    a.core = FakeCore()
    a.t = a.core.t
    a.manager = FakeMgr()
    a.chat_id = -100
    a.config = SimpleNamespace(allowed_user_ids={1})
    a._perm_msgs = {}
    return a


def cb(data, uid=1, msg=None):
    async def answer(*a, **k):
        pass
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), data=data,
                           message=msg or FakeMsg(), answer=answer)


async def main():
    a = make_adapter()

    calls.clear()
    await a.on_model_button(cb("model:7:opus"))
    assert ("set_model", "opus") in calls
    print("OK on_model_button allow")

    calls.clear()
    await a.on_model_button(cb("model:7:opus", uid=999))  # чужой
    assert not calls
    print("OK on_model_button denies stranger")

    calls.clear()
    await a.on_model_button(cb("model:xx:opus"))  # битый thread_id
    assert not any(c[0] == "set_model" for c in calls)
    print("OK on_model_button bad thread_id")

    calls.clear()
    await a.on_session_button(cb("sess:close:7"))
    assert ("close",) in calls
    print("OK on_session_button close")

    calls.clear()
    await a.on_perm_button(cb("perm:7:abcde:allow"))
    assert ("perm", "abcde", "allow") in calls
    print("OK on_perm_button allow")

    calls.clear()
    await a.on_perm_button(cb("perm:7:ab:cd:deny"))  # request_id с ':'
    assert ("perm", "ab:cd", "deny") in calls
    print("OK on_perm_button request_id with colon (rsplit)")

    calls.clear()
    await a.on_stop_button(cb("stop:7"))
    assert ("stop_sent",) in calls
    print("OK on_stop_button")

    calls.clear()
    await a.on_esc_button(cb("esc:7"))
    assert ("esc_sent",) in calls
    print("OK on_esc_button (жёсткое прерывание)")

    calls.clear()
    await a.on_esc_button(cb("esc:7", uid=999))  # чужой
    assert not calls
    print("OK on_esc_button denies stranger")

    # несуществующая сессия — все хендлеры не должны падать
    for data, h in [("model:5:o", a.on_model_button), ("sess:close:5", a.on_session_button),
                    ("perm:5:abcde:allow", a.on_perm_button), ("stop:5", a.on_stop_button),
                    ("esc:5", a.on_esc_button)]:
        await h(cb(data))
    print("OK missing session handled")

    print("ALL CALLBACKS OK")


if __name__ == "__main__":
    asyncio.run(main())
