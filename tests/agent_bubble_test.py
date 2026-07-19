"""End-to-end регресс: core.handle_tool_event → tool_line → bubbles.append с
верной атрибуцией (agent_id, tool для схлопывания).

Проверяет всю цепочку, не только отдельные звенья (bubble_test.py гоняет
BubbleManager напрямую, tool_line_test.py — только tool_line): реальный
PreToolUse-payload от Claude Code с agent_id/agent_type должен долететь до
BubbleManager.append с правильными kwargs, а спавн сабагента (tool="Agent")
и TodoWrite — НЕ схлопываться.

Запуск: .venv/bin/python tests/agent_bubble_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.bubble import BubbleManager  # noqa: E402
from orchestrator.core.turn import TurnSupervisor  # noqa: E402


class FakeTransport:
    name = "fake"

    async def bubble_post(self, session, html, *, stop_button):
        return "1"

    async def bubble_edit(self, session, ref, html, *, stop_button):
        pass

    async def bubble_finish(self, session, ref, *, delete):
        pass

    async def bubble_freeze(self, session, ref):
        pass


SESSION = SimpleNamespace(name="noos")


class FakeMgr:
    def get(self, name):
        return SESSION if name == "noos" else None

    get_by_name = get


async def _settle(bm: BubbleManager, name: str) -> None:
    bubble = bm._bubbles.get(name)
    if bubble is not None and bubble.flush_task is not None:
        await asyncio.wait_for(bubble.flush_task, timeout=5)


def make_core(bm: BubbleManager) -> OrchestratorCore:
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.manager = FakeMgr()
    core._texts = {"subagent": "🤖 {agent}"}
    core.bubbles = bm
    core.turns = TurnSupervisor(
        core.manager, core.t,
        lambda session, text: asyncio.sleep(0),
        lambda session: asyncio.sleep(0),
    )
    return core


async def main():
    _TEXTS = {"bubble_working": "Работаю", "bubble_stop": "Стоп", "subagent": "🤖 {agent}"}
    tr = FakeTransport()
    bm = BubbleManager(
        lambda: [tr], lambda n: SESSION if n == "noos" else None,
        lambda k, **kw: _TEXTS[k].format(**kw), delete_after=True,
    )
    core = make_core(bm)
    bm.open("noos")

    # Реальный сценарий noos: спавн Agent, затем куча Bash/Read ВНУТРИ него
    # (agent_id проставлен Claude Code на каждом дочернем тул-вызове).
    await core.handle_tool_event("noos", {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "dev-reviewer", "description": "Fresh review slice 3"},
    })
    for i in range(5):
        await core.handle_tool_event("noos", {
            "tool_name": "Bash", "agent_id": "a02cba49",
            "tool_input": {"command": f"grep pattern{i} file.py"},
        })
    await _settle(bm, "noos")

    entries = bm._bubbles["noos"].entries
    assert len(entries) == 2, [e.render() for e in entries]  # спавн + схлопнутая серия Bash
    assert entries[0].agent_id is None and "dev-reviewer" in entries[0].html
    assert entries[1].agent_id == "a02cba49" and entries[1].count == 5
    text = bm._render_text(bm._bubbles["noos"])
    assert "  ↳ " in text and "5× " in text, text
    print("OK handle_tool_event: спавн агента (без отступа) + 5×Bash сабагента (отступ, схлопнуто)")

    # Главный поток продолжает работу параллельно — свой agent_id=None, своё схлопывание
    await core.handle_tool_event("noos", {"tool_name": "Bash", "tool_input": {"command": "git status"}})
    await core.handle_tool_event("noos", {"tool_name": "Bash", "tool_input": {"command": "git log"}})
    await _settle(bm, "noos")
    main_entries = [e for e in bm._bubbles["noos"].entries if e.agent_id is None and e.tool == "Bash"]
    assert len(main_entries) == 1 and main_entries[0].count == 2
    print("OK главный поток схлопывается отдельно от сабагента, без отступа")

    # TodoWrite не схлопывается даже подряд (это состояние, не действие)
    n_before = len(bm._bubbles["noos"].entries)
    await core.handle_tool_event("noos", {"tool_name": "TodoWrite", "tool_input": {"todos": [1, 2]}})
    await core.handle_tool_event("noos", {"tool_name": "TodoWrite", "tool_input": {"todos": [1, 2, 3]}})
    await _settle(bm, "noos")
    assert len(bm._bubbles["noos"].entries) == n_before + 2
    print("OK TodoWrite не схлопывается")

    # reply_to_user / send_file_to_user не попадают в бабл вообще
    n_before = len(bm._bubbles["noos"].entries)
    await core.handle_tool_event("noos", {"tool_name": "mcp__channel-noos__reply_to_user"})
    await core.handle_tool_event("noos", {"tool_name": "mcp__channel-noos__send_file_to_user"})
    await _settle(bm, "noos")
    assert len(bm._bubbles["noos"].entries) == n_before
    print("OK reply/send_file не попадают в бабл (без изменений)")

    print("ALL AGENT-BUBBLE OK")


if __name__ == "__main__":
    asyncio.run(main())
