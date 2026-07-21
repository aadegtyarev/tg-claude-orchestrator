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
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.bubble import BubbleManager  # noqa: E402
from orchestrator.core.subagentnaming import SubagentNaming  # noqa: E402
from orchestrator.core.toolactivity import ToolActivity  # noqa: E402
from orchestrator.core.turn import TurnSupervisor  # noqa: E402


class FakeTransport:
    name = "fake"

    async def bubble_post(self, session, html, *, stop_button, unblock_active=False):
        return "1"

    async def bubble_edit(self, session, ref, html, *, stop_button, unblock_active=False):
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

    def transcript_path(self, session):
        # Несуществующий путь → read_last_model вернёт None (модель неизвестна):
        # тест проверяет ИМЕНОВАНИЕ и фолбэки, не чтение файла.
        return Path("/nonexistent/does-not-exist.jsonl")


async def _settle(bm: BubbleManager, name: str) -> None:
    bubble = bm._bubbles.get(name)
    if bubble is not None and bubble.flush_task is not None:
        await asyncio.wait_for(bubble.flush_task, timeout=5)


def make_core(bm: BubbleManager) -> OrchestratorCore:
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.manager = FakeMgr()
    core._texts = {
        "subagent": "🤖 {agent}",
        "subagent_done_named": "✅ Сабагент {agent} завершил · {model}",
        "subagent_done_named_nomodel": "✅ Сабагент {agent} завершил",
        "subagent_done": "✅ Сабагент завершил · {model}",
        "subagent_done_nomodel": "✅ Сабагент завершил",
    }
    core.tools = ToolActivity()
    core.naming = SubagentNaming()
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

    await _named_subagent_stop()
    await _late_tool_after_stop_toplevel()
    await _model_via_newest_transcript_fallback()
    print("ALL AGENT-BUBBLE OK")


async def _model_via_newest_transcript_fallback():
    """Модель сабагента в строке «завершил» через ФОЛБЭК на новейший
    subagents/agent-*.jsonl — когда в payload нет agent_transcript_path, а
    agent_id не совпал с файлом (иначе модель терялась, строка была без · модели)."""
    _TEXTS = {
        "bubble_working": "Работаю", "subagent": "🤖 {agent}",
        "subagent_done_named": "✅ Сабагент {agent} завершил · {model}",
        "subagent_done_named_nomodel": "✅ Сабагент {agent} завершил",
        "subagent_done": "✅ Сабагент завершил · {model}",
        "subagent_done_nomodel": "✅ Сабагент завершил",
    }
    tmp = Path(tempfile.mkdtemp(prefix="agent_bubble_"))
    sess_stem = tmp / "proj" / "11111111-2222-3333-4444-555555555555"
    subdir = sess_stem / "subagents"
    subdir.mkdir(parents=True)
    (subdir / "agent-real123.jsonl").write_text('{"type":"assistant","model":"deepseek-v4-pro"}\n')

    class TmpMgr(FakeMgr):
        def transcript_path(self, session):
            return sess_stem.with_suffix(".jsonl")

    tr = FakeTransport()
    bm = BubbleManager(
        lambda: [tr], lambda n: SESSION if n == "noos" else None,
        lambda k, **kw: _TEXTS[k].format(**kw), delete_after=True,
    )
    core = make_core(bm)
    core.manager = TmpMgr()
    bm.open("noos")

    # SubagentStop с НЕсовпадающим agent_id и без agent_transcript_path →
    # точный путь agent-mismatch.jsonl не существует, спасает фолбэк на новейший.
    await core.handle_tool_event("noos", {
        "hook_event_name": "SubagentStop", "agent_id": "mismatch",
    })
    await _settle(bm, "noos")
    text = bm._render_text(bm._bubbles["noos"])
    assert "deepseek-v4-pro" in text, text
    print("OK модель в «завершил» через фолбэк на новейший субагентский транскрипт")


async def _late_tool_after_stop_toplevel():
    """Гонка доставки async-хуков: тул-хук сабагента прилетает ПОСЛЕ его
    SubagentStop. Такую запоздалую строку рендерим ВЕРХНИМ уровнем (без отступа
    под «завершил»), иначе на бабле «сабагент завершил, но работа под ним идёт»."""
    _TEXTS = {
        "bubble_working": "Работаю", "subagent": "🤖 {agent}",
        "subagent_done_named": "✅ Сабагент {agent} завершил · {model}",
        "subagent_done_named_nomodel": "✅ Сабагент {agent} завершил",
        "subagent_done": "✅ Сабагент завершил · {model}",
        "subagent_done_nomodel": "✅ Сабагент завершил",
    }
    tr = FakeTransport()
    bm = BubbleManager(
        lambda: [tr], lambda n: SESSION if n == "noos" else None,
        lambda k, **kw: _TEXTS[k].format(**kw), delete_after=True,
    )
    core = make_core(bm)
    bm.open("noos")

    # Сабагент a1 поработал (Bash под отступом) и завершился.
    await core.handle_tool_event("noos", {
        "tool_name": "Bash", "agent_id": "a1", "agent_type": "dev-planner",
        "tool_input": {"command": "grep x"},
    })
    await core.handle_tool_event("noos", {
        "hook_event_name": "SubagentStop", "agent_id": "a1",
    })
    await _settle(bm, "noos")

    # Запоздалый тул ТОГО ЖЕ сабагента (обогнал доставку SubagentStop).
    await core.handle_tool_event("noos", {
        "tool_name": "Bash", "agent_id": "a1",
        "tool_input": {"command": "echo late"},
    })
    await _settle(bm, "noos")

    entries = bm._bubbles["noos"].entries
    # Запоздалый Bash — отдельной строкой ВЕРХНЕГО уровня (agent_id=None).
    late = [e for e in entries if e.agent_id is None and e.tool == "Bash"]
    assert late and "echo late" in late[-1].html, [e.render() for e in entries]
    # Он НЕ схлопнулся в строку сабагента a1 (та осталась count=1, grep x).
    sub = [e for e in entries if e.agent_id == "a1" and e.tool == "Bash"]
    assert len(sub) == 1 and sub[0].count == 1, [e.render() for e in entries]
    print("OK запоздалый тул завершённого сабагента → верхний уровень, не под «завершил»")


async def _named_subagent_stop():
    """SubagentStop называет ИМЕННО завершившегося сабагента (dev-planner/…),
    даже когда их несколько подряд — иначе безымянное «завершил» читалось как
    «завершил, но идёт дальше». Плюс мягкая деградация: тип из дочернего
    события → из порядка спавнов → без имени."""
    _TEXTS = {
        "bubble_working": "Работаю", "subagent": "🤖 {agent}",
        "subagent_done_named": "✅ Сабагент {agent} завершил · {model}",
        "subagent_done_named_nomodel": "✅ Сабагент {agent} завершил",
        "subagent_done": "✅ Сабагент завершил · {model}",
        "subagent_done_nomodel": "✅ Сабагент завершил",
    }
    tr = FakeTransport()
    bm = BubbleManager(
        lambda: [tr], lambda n: SESSION if n == "noos" else None,
        lambda k, **kw: _TEXTS[k].format(**kw), delete_after=True,
    )
    core = make_core(bm)
    bm.open("noos")

    # Тип из ДОЧЕРНЕГО тул-события (agent_id + agent_type) — самый надёжный путь.
    await core.handle_tool_event("noos", {
        "tool_name": "Bash", "agent_id": "a1", "agent_type": "dev-planner",
        "tool_input": {"command": "grep x"},
    })
    await core.handle_tool_event("noos", {
        "hook_event_name": "SubagentStop", "agent_id": "a1",
    })
    await _settle(bm, "noos")
    text = bm._render_text(bm._bubbles["noos"])
    assert "Сабагент dev-planner завершил" in text, text
    print("OK завершение названо по agent_type из дочернего события")

    # Фолбэк: только спавн-строка (agent_id ещё нет) → тип по порядку спавнов.
    await core.handle_tool_event("noos", {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "dev-builder", "description": "build"},
    })
    await core.handle_tool_event("noos", {
        "hook_event_name": "SubagentStop", "agent_id": "a2",
    })
    await _settle(bm, "noos")
    text = bm._render_text(bm._bubbles["noos"])
    assert "Сабагент dev-builder завершил" in text, text
    print("OK завершение названо по порядку спавнов (фолбэк без agent_id)")

    # Мягкая деградация: ничего не известно о типе → безымянная строка (не падаем).
    await core.handle_tool_event("noos", {
        "hook_event_name": "SubagentStop", "agent_id": "unknown",
    })
    await _settle(bm, "noos")
    text = bm._render_text(bm._bubbles["noos"])
    assert "✅ Сабагент завершил" in text, text
    print("OK неизвестный сабагент — мягкая деградация до безымянной строки")


async def test_agent_bubble():
    await main()

if __name__ == "__main__":
    asyncio.run(main())
