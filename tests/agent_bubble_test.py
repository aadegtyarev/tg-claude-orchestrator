"""End-to-end регресс: handle_tool_event → _tool_line → bubbles.append с
верной атрибуцией (agent_id, tool для схлопывания).

Проверяет всю цепочку, не только отдельные звенья (bubble_test.py гоняет
BubbleManager напрямую, tool_line_test.py — только _tool_line): реальный
PreToolUse-payload от Claude Code с agent_id/agent_type должен долететь до
BubbleManager.append с правильными kwargs, а спавн сабагента (tool="Agent")
и TodoWrite — НЕ схлопываться.

Запуск: .venv/bin/python tests/agent_bubble_test.py
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.bot import TelegramBot  # noqa: E402
from orchestrator.bubble import BubbleManager  # noqa: E402
from orchestrator.turn import TurnSupervisor  # noqa: E402


class FakeBot:
    async def send_message(self, **k):
        return SimpleNamespace(message_id=1)

    async def edit_message_text(self, **k):
        pass

    async def delete_message(self, **k):
        pass

    async def edit_message_reply_markup(self, **k):
        pass


async def _settle(bm: BubbleManager, thread_id: int) -> None:
    bubble = bm._bubbles.get(thread_id)
    if bubble is not None and bubble.flush_task is not None:
        await asyncio.wait_for(bubble.flush_task, timeout=5)


def make_bot(bubbles: BubbleManager) -> TelegramBot:
    class FakeMgr:
        def get_by_name(self, name):
            return SimpleNamespace(thread_id=204, name="noos") if name == "noos" else None

    b = TelegramBot.__new__(TelegramBot)
    b.manager = FakeMgr()
    b.chat_id = -100
    b.bubbles = bubbles
    b._texts = {"subagent": "🤖 {agent}"}
    b.t = lambda k, **kw: b._texts[k].format(**kw)
    b.turns = TurnSupervisor(
        b.manager, b.t,
        lambda tid, text: asyncio.sleep(0),
        lambda tid: asyncio.sleep(0),
    )
    return b


async def main():
    _TEXTS = {"bubble_working": "Работаю", "bubble_stop": "Стоп"}
    bm = BubbleManager(FakeBot(), lambda: -100, lambda k, **kw: _TEXTS[k], delete_after=True)
    b = make_bot(bm)
    bm.open(204)

    # Реальный сценарий noos: спавн Agent, затем куча Bash/Read ВНУТРИ него
    # (agent_id проставлен Claude Code на каждом дочернем тул-вызове).
    await b.handle_tool_event("noos", {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "dev-reviewer", "description": "Fresh review slice 3"},
    })
    for i in range(5):
        await b.handle_tool_event("noos", {
            "tool_name": "Bash", "agent_id": "a02cba49",
            "tool_input": {"command": f"grep pattern{i} file.py"},
        })
    await _settle(bm, 204)

    entries = bm._bubbles[204].entries
    assert len(entries) == 2, [e.render() for e in entries]  # спавн + схлопнутая серия Bash
    assert entries[0].agent_id is None and "dev-reviewer" in entries[0].html
    assert entries[1].agent_id == "a02cba49" and entries[1].count == 5
    text = bm._render_text(bm._bubbles[204])
    assert "  ↳ " in text and "5× " in text, text
    print("OK handle_tool_event: спавн агента (без отступа) + 5×Bash сабагента (отступ, схлопнуто)")

    # Главный поток продолжает работу параллельно — свой agent_id=None, своё схлопывание
    await b.handle_tool_event("noos", {"tool_name": "Bash", "tool_input": {"command": "git status"}})
    await b.handle_tool_event("noos", {"tool_name": "Bash", "tool_input": {"command": "git log"}})
    await _settle(bm, 204)
    main_entries = [e for e in bm._bubbles[204].entries if e.agent_id is None and e.tool == "Bash"]
    assert len(main_entries) == 1 and main_entries[0].count == 2
    print("OK главный поток схлопывается отдельно от сабагента, без отступа")

    # TodoWrite не схлопывается даже подряд (это состояние, не действие)
    n_before = len(bm._bubbles[204].entries)
    await b.handle_tool_event("noos", {"tool_name": "TodoWrite", "tool_input": {"todos": [1, 2]}})
    await b.handle_tool_event("noos", {"tool_name": "TodoWrite", "tool_input": {"todos": [1, 2, 3]}})
    await _settle(bm, 204)
    assert len(bm._bubbles[204].entries) == n_before + 2
    print("OK TodoWrite не схлопывается")

    # reply_to_telegram / send_file_to_telegram не попадают в бабл вообще (как раньше)
    n_before = len(bm._bubbles[204].entries)
    await b.handle_tool_event("noos", {"tool_name": "mcp__tg-channel-noos__reply_to_telegram"})
    await b.handle_tool_event("noos", {"tool_name": "mcp__tg-channel-noos__send_file_to_telegram"})
    await _settle(bm, 204)
    assert len(bm._bubbles[204].entries) == n_before
    print("OK reply/send_file не попадают в бабл (без изменений)")

    print("ALL AGENT-BUBBLE OK")


if __name__ == "__main__":
    asyncio.run(main())
