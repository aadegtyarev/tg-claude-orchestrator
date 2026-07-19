"""Регрессия Stop-хука: фолбэк на «потерянный финал» хода.

Найдено разбором живых транскриптов (noos + эта сессия): 19/19 длинных ходов
за день заканчивались ОБЫЧНЫМ ТЕКСТОМ вместо вызова reply_to_user — канал
ретранслирует только явные tool-call'ы, голый ассистент-текст до пользователя
не долетает (остаётся в TUI/транскрипте). Реальный кейс noos: reply_to_user
был в СЕРЕДИНЕ хода, а после него ещё шли Bash/Edit, и именно текст ПОСЛЕ них
терялся — поэтому гейт не «reply был в ходе», а «reply было последним
действием перед Stop» (turn.TurnSupervisor, сбрасывается любым другим
тул-вызовом).

Запуск: .venv/bin/python tests/stop_fallback_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.turn import TurnSupervisor  # noqa: E402

SESSION = SimpleNamespace(name="noos")


def make_core():
    sent = []

    class FakeMgr:
        def get(self, name):
            return SESSION if name == "noos" else None

        get_by_name = get

    core = OrchestratorCore.__new__(OrchestratorCore)
    core.manager = FakeMgr()
    core._texts = {}
    core._history = {}
    core.adapters = {}
    # Реальный TurnSupervisor: проверяем настоящий Stop-гейт (note_tool/
    # pop_reply_flag), доставка — заглушки.
    core.turns = TurnSupervisor(
        core.manager, core.t,
        lambda session, text: asyncio.sleep(0),
        lambda session: asyncio.sleep(0),
    )
    core.bubbles = SimpleNamespace(append=lambda *a, **kw: asyncio.sleep(0))

    async def fake_deliver(session, text, origin, intermediate):
        sent.append((session.name, text, intermediate))
    core._deliver_text = fake_deliver
    return core, sent


async def main():
    core, sent = make_core()

    # Реальный кейс noos: reply в середине хода, потом ЕЩЁ работа (Bash/Edit),
    # затем Stop без reply — финал должен быть перехвачен.
    await core.handle_tool_event("noos", {"tool_name": "mcp__channel-noos__reply_to_user"})
    await core.handle_tool_event("noos", {"tool_name": "Bash", "tool_input": {"command": "git commit"}})
    await core.handle_tool_event("noos", {"tool_name": "Edit", "tool_input": {"file_path": "/x.py"}})
    await core.handle_stop_event("noos", {"last_assistant_message": "PR открыт, жду CI green"})
    assert len(sent) == 1 and "PR открыт" in sent[0][1] and sent[0][0] == "noos", sent
    print("OK reply в середине + работа после → fallback сработал (реальный кейс noos)")

    sent.clear()
    # reply — самое последнее действие перед Stop → фолбэк не нужен (не дублируем).
    await core.handle_tool_event("noos", {"tool_name": "mcp__channel-noos__reply_to_user"})
    await core.handle_stop_event("noos", {"last_assistant_message": "тот же текст что уже ушёл"})
    assert sent == [], sent
    print("OK reply последним перед Stop → fallback не сработал")

    sent.clear()
    # несколько Stop подряд без reply — каждый фолбэчит отдельно (окно сбрасывается).
    await core.handle_stop_event("noos", {"last_assistant_message": "ход 1"})
    await core.handle_stop_event("noos", {"last_assistant_message": "ход 2"})
    assert len(sent) == 2 and sent[0][1].endswith("ход 1") and sent[1][1].endswith("ход 2"), sent
    print("OK несколько Stop подряд без reply → каждый фолбэчит отдельно")

    sent.clear()
    # пустой last_assistant_message — не спамим тишиной.
    await core.handle_stop_event("noos", {"last_assistant_message": ""})
    assert sent == []
    print("OK пустой last_assistant_message → тишина")

    sent.clear()
    # неизвестная сессия — тихо игнорируется, не падает.
    await core.handle_stop_event("ghost", {"last_assistant_message": "x"})
    assert sent == []
    print("OK неизвестная сессия → тихо игнорируется")

    sent.clear()
    # send_file_to_user — НЕ reply, тоже сбрасывает флаг.
    await core.handle_tool_event("noos", {"tool_name": "mcp__channel-noos__reply_to_user"})
    await core.handle_tool_event("noos", {"tool_name": "mcp__channel-noos__send_file_to_user"})
    await core.handle_stop_event("noos", {"last_assistant_message": "текст после отправки файла"})
    assert len(sent) == 1, sent
    print("OK send_file между reply и Stop → флаг сброшен, fallback сработал")

    print("ALL STOP-FALLBACK OK")


async def test_stop_fallback():
    await main()

if __name__ == "__main__":
    asyncio.run(main())
