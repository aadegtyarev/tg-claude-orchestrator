"""Регрессия Stop-хука: фолбэк на «потерянный финал» хода.

Найдено разбором живых транскриптов (noos + эта сессия): 19/19 длинных ходов
за день заканчивались ОБЫЧНЫМ ТЕКСТОМ вместо вызова reply_to_telegram — канал
ретранслирует только явные tool-call'ы, голый ассистент-текст до Telegram не
долетает (остаётся в TUI/транскрипте). Реальный кейс noos: reply_to_telegram
был в СЕРЕДИНЕ хода, а после него ещё шли Bash/Edit, и именно текст ПОСЛЕ них
терялся — поэтому гейт не «reply был в ходе», а «reply было последним
действием перед Stop» (_last_action_was_reply, сбрасывается любым другим
тул-вызовом).

Запуск: .venv/bin/python tests/stop_fallback_test.py
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from bot import TelegramBot  # noqa: E402


def make_bot():
    sent = []

    class FakeMgr:
        def get_by_name(self, name):
            return SimpleNamespace(thread_id=204, name="noos") if name == "noos" else None

    b = TelegramBot.__new__(TelegramBot)
    b.manager = FakeMgr()
    b.chat_id = -100
    b._texts = {"stop_fallback_prefix": "💬 (fallback)"}
    b.t = lambda k, **kw: b._texts[k]
    b._last_action_was_reply = {}
    b.bubbles = SimpleNamespace(append=lambda *a: asyncio.sleep(0))

    async def fake_send(chat_id, thread_id, text, reply_to=None):
        sent.append((chat_id, thread_id, text))
    b._send = fake_send
    return b, sent


async def main():
    b, sent = make_bot()

    # Реальный кейс noos: reply в середине хода, потом ЕЩЁ работа (Bash/Edit),
    # затем Stop без reply — финал должен быть перехвачен.
    await b.handle_tool_event("noos", {"tool_name": "mcp__tg-channel-noos__reply_to_telegram"})
    await b.handle_tool_event("noos", {"tool_name": "Bash", "tool_input": {"command": "git commit"}})
    await b.handle_tool_event("noos", {"tool_name": "Edit", "tool_input": {"file_path": "/x.py"}})
    await b.handle_stop_event("noos", {"last_assistant_message": "PR открыт, жду CI green"})
    assert len(sent) == 1 and "PR открыт" in sent[0][2] and sent[0][1] == 204, sent
    print("OK: reply в середине + работа после → fallback сработал (реальный кейс noos)")

    sent.clear()
    # reply — самое последнее действие перед Stop → фолбэк не нужен (не дублируем).
    await b.handle_tool_event("noos", {"tool_name": "mcp__tg-channel-noos__reply_to_telegram"})
    await b.handle_stop_event("noos", {"last_assistant_message": "тот же текст что уже ушёл"})
    assert sent == [], sent
    print("OK: reply последним перед Stop → fallback не сработал")

    sent.clear()
    # несколько Stop подряд без reply — каждый фолбэчит отдельно (окно сбрасывается).
    await b.handle_stop_event("noos", {"last_assistant_message": "ход 1"})
    await b.handle_stop_event("noos", {"last_assistant_message": "ход 2"})
    assert len(sent) == 2 and sent[0][2].endswith("ход 1") and sent[1][2].endswith("ход 2"), sent
    print("OK: несколько Stop подряд без reply → каждый фолбэчит отдельно")

    sent.clear()
    # пустой last_assistant_message — не спамим тишиной.
    await b.handle_stop_event("noos", {"last_assistant_message": ""})
    assert sent == []
    print("OK: пустой last_assistant_message → тишина")

    sent.clear()
    # неизвестная сессия — тихо игнорируется, не падает.
    await b.handle_stop_event("ghost", {"last_assistant_message": "x"})
    assert sent == []
    print("OK: неизвестная сессия → тихо игнорируется")

    sent.clear()
    # send_file_to_telegram — НЕ reply, тоже сбрасывает флаг.
    await b.handle_tool_event("noos", {"tool_name": "mcp__tg-channel-noos__reply_to_telegram"})
    await b.handle_tool_event("noos", {"tool_name": "mcp__tg-channel-noos__send_file_to_telegram"})
    await b.handle_stop_event("noos", {"last_assistant_message": "текст после отправки файла"})
    assert len(sent) == 1, sent
    print("OK: send_file между reply и Stop → флаг сброшен, fallback сработал")

    print("ALL STOP-FALLBACK OK")


if __name__ == "__main__":
    asyncio.run(main())
